import sqlite3
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote

from test_web import ASGITestClient, workbook_bytes

from wkly_rp_qc_v2.store import get_snapshot, resolve_project_alias
from wkly_rp_qc_v2.web import WebSettings, create_app


class WorkbookToolsTests(unittest.TestCase):
    def make_client(self, root: Path):
        settings = WebSettings(root / "store.sqlite3", root / "files", ())
        return ASGITestClient(create_app(settings)), settings

    def upload_unknown(self, client, *, project="SMART PARKING"):
        response = client.post(
            "/workbooks",
            files={"workbook": ("unknown.xlsx", workbook_bytes(unknown=True, project=project), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )
        return int(response.headers["location"].rsplit("/", 1)[1])

    def mapping_data(self, *, template_id="manual-flat", version="1", source_reference="fixture mapping", header_row="1"):
        return {
            "template_id": template_id, "version": version, "source_reference": source_reference,
            "header_row": header_row, "first_data_row": "2", "project_col": "1", "role_col": "2",
            "pic_col": "3", "actual_col": "4", "next_plan_col": "5", "issue_risk_col": "6",
        }

    def test_mapping_tool_validates_return_context_and_redirects_to_exact_sheet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                workbook_id = self.upload_unknown(client)
                snapshot_id = get_snapshot(settings.database_path, 1)["snapshot_id"]
                opened = client.get(f"/workbooks/{workbook_id}", params={"return_app": "1", "sheet": snapshot_id, "tool": "mapping"})
                self.assertEqual(opened.status_code, 200)
                self.assertIn(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}", opened.text)
                self.assertIn(f"return_app=1&amp;sheet={snapshot_id}", opened.text)

                response = client.post(
                    f"/workbooks/{workbook_id}/sheets/{quote('Unknown Layout', safe='')}/mapping",
                    params={"return_app": "1", "sheet": snapshot_id, "return_url": "https://evil.example"},
                    data=self.mapping_data(),
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], f"/app/workbooks/{workbook_id}?sheet={snapshot_id}")
                self.assertIn("workbook_tool_notice=", response.headers.get("set-cookie", ""))
                self.assertEqual(get_snapshot(settings.database_path, snapshot_id)["mapping_status"], "confirmed")
                cookie = SimpleCookie()
                cookie.load(response.headers["set-cookie"])
                preview = client.get(
                    f"/api/workbooks/{workbook_id}",
                    headers={"Cookie": f"workbook_tool_notice={cookie['workbook_tool_notice'].value}"},
                )
                self.assertEqual(preview.headers["cache-control"], "no-store")
                self.assertEqual(preview.json()["tool_notice"], {"operation": "mapping", "snapshot_id": snapshot_id})
                self.assertIn("Max-Age=0", preview.headers.get("set-cookie", ""))
                self.assertNotIn("tool_notice", client.get(f"/api/workbooks/{workbook_id}").json())
                with sqlite3.connect(settings.database_path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 0)

    def test_invalid_mapping_rerenders_submitted_values_and_preserves_context_without_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                workbook_id = self.upload_unknown(client)
                snapshot_id = 1
                values = self.mapping_data(source_reference="keep this exact reason", header_row="0")
                response = client.post(
                    f"/workbooks/{workbook_id}/sheets/Unknown%20Layout/mapping",
                    params={"return_app": "1", "sheet": snapshot_id},
                    data=values,
                    follow_redirects=False,
                )
                self.assertIn(response.status_code, (400, 422))
                self.assertIn("keep this exact reason", response.text)
                self.assertIn('value="0"', response.text)
                self.assertIn(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}", response.text)
                self.assertNotIn("workbook_tool_notice=", response.headers.get("set-cookie", ""))
                self.assertEqual(get_snapshot(settings.database_path, snapshot_id)["mapping_status"], "mapping_required")

    def test_foreign_workbook_snapshot_context_is_rejected_before_mapping_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                first_id = self.upload_unknown(client)
                self.upload_unknown(client, project="ANAT GSM")
                response = client.get(
                    f"/workbooks/{first_id}",
                    params={"return_app": "1", "sheet": 2, "tool": "mapping"},
                )
                self.assertIn(response.status_code, (400, 404))
                mapped = client.post(
                    f"/workbooks/{first_id}/sheets/Unknown%20Layout/mapping",
                    params={"return_app": "1", "sheet": 2},
                    data=self.mapping_data(version="2"),
                    follow_redirects=False,
                )
                self.assertIn(mapped.status_code, (400, 404))
                self.assertEqual(get_snapshot(settings.database_path, 1)["mapping_status"], "mapping_required")
                self.assertEqual(get_snapshot(settings.database_path, 2)["mapping_status"], "mapping_required")
                self.assertNotIn("https://evil.example", response.text)

    def test_alias_return_context_preserves_optional_legacy_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                workbook_id = self.upload_unknown(client)
                response = client.post(
                    f"/workbooks/{workbook_id}/aliases",
                    params={"return_app": "1", "sheet": 1, "return_url": "https://evil.example"},
                    data={"raw_label": "TEST LEGACY", "canonical_label": "TEST PROJECT", "source_reference": "technical fixture"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], f"/app/workbooks/{workbook_id}?sheet=1")
                alias = resolve_project_alias(settings.database_path, "TEST LEGACY")
                self.assertTrue(alias["explicit"])
                self.assertEqual(alias["canonical_label"], "TEST PROJECT")
                self.assertIsNotNone(get_snapshot(settings.database_path, 1))

    def test_foreign_origin_cannot_save_mapping_or_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                workbook_id = self.upload_unknown(client)
                mapping = client.post(
                    f"/workbooks/{workbook_id}/sheets/Unknown%20Layout/mapping",
                    data=self.mapping_data(),
                    headers={"Origin": "https://evil.example"},
                    follow_redirects=False,
                )
                alias = client.post(
                    f"/workbooks/{workbook_id}/aliases",
                    data={"raw_label": "TEST", "canonical_label": "TEST PROJECT", "source_reference": "fixture"},
                    headers={"Origin": "https://evil.example"},
                    follow_redirects=False,
                )
                self.assertEqual(mapping.status_code, 403)
                self.assertEqual(alias.status_code, 403)
                self.assertEqual(get_snapshot(settings.database_path, 1)["mapping_status"], "mapping_required")
                self.assertIsNone(resolve_project_alias(settings.database_path, "TEST")["project_id"])

    def test_invalid_alias_rerenders_values_and_context_without_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                workbook_id = self.upload_unknown(client)
                response = client.post(
                    f"/workbooks/{workbook_id}/aliases",
                    params={"return_app": "1", "sheet": 1},
                    data={"raw_label": "TEST RAW", "canonical_label": "TEST CANONICAL", "source_reference": ""},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("TEST RAW", response.text)
                self.assertIn("TEST CANONICAL", response.text)
                self.assertIn("return_app=1&amp;sheet=1", response.text)
                self.assertNotIn("workbook_tool_notice=", response.headers.get("set-cookie", ""))
                self.assertIsNone(resolve_project_alias(settings.database_path, "TEST RAW")["project_id"])


if __name__ == "__main__":
    unittest.main()
