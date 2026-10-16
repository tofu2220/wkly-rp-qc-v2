import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from test_web import ASGITestClient, workbook_bytes

from wkly_rp_qc_v2.store import initialize_store
from wkly_rp_qc_v2.web import WebSettings, create_app


class WorkbookApiTests(unittest.TestCase):
    def make_client(self, root: Path):
        settings = WebSettings(root / "store.sqlite3", root / "files", ())
        return ASGITestClient(create_app(settings)), settings

    def upload(self, client, content: bytes, filename: str):
        return client.post(
            "/api/workbooks",
            files={
                "workbook": (
                    filename,
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    def test_catalog_starts_empty_and_includes_zero_sheet_persisted_workbook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            with client:
                self.assertEqual(client.get("/api/workbooks").json(), [])
                initialize_store(settings.database_path)
                with sqlite3.connect(settings.database_path) as connection:
                    connection.execute(
                        "INSERT INTO workbook_versions(sha256, original_filename, storage_relpath, imported_at_utc) "
                        "VALUES (?, ?, ?, ?)",
                        ("a" * 64, "empty.xlsx", "aa/empty.xlsx", "2026-10-16T10:00:00+00:00"),
                    )

                catalog_response = client.get("/api/workbooks")
                self.assertEqual(catalog_response.status_code, 200)
                self.assertEqual(len(catalog_response.json()), 1)
                item = catalog_response.json()[0]
                self.assertEqual(item["sheet_count"], 0)
                self.assertEqual(item["mapping_required_count"], 0)
                self.assertEqual(item["warning_count"], 0)
                self.assertEqual(item["original_filename"], "empty.xlsx")
                details = client.get(f"/api/workbooks/{item['workbook_id']}")
                self.assertEqual(details.status_code, 200)
                self.assertEqual(details.json()["sheets"], [])
                self.assertEqual(details.json()["imported_at_utc"], item["imported_at_utc"])
                self.assertEqual(client.get(f"/api/workbooks/{item['workbook_id'] + 1}").status_code, 404)

    def test_catalog_preserves_version_identity_timestamp_and_attention_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, settings = self.make_client(root)
            content = workbook_bytes(unknown=True)
            with client:
                first = self.upload(client, content, "report.xlsx")
                self.assertEqual(first.status_code, 201)
                first_data = first.json()
                catalog = client.get("/api/workbooks")
                self.assertEqual(catalog.status_code, 200)
                self.assertEqual(len(catalog.json()), 1)
                item = catalog.json()[0]
                self.assertEqual(item["workbook_id"], first_data["workbook_id"])
                self.assertEqual(item["original_filename"], "report.xlsx")
                self.assertEqual(item["sha256"], first_data["sha256"])
                self.assertEqual(item["sheet_count"], 1)
                self.assertEqual(item["mapping_required_count"], 1)
                self.assertGreater(item["warning_count"], 0)
                self.assertEqual(item["preview_url"], f"/app/workbooks/{item['workbook_id']}")
                stored_time = datetime.fromisoformat(item["imported_at_utc"])
                self.assertIsNotNone(stored_time.tzinfo)

                duplicate = self.upload(client, content, "renamed.xlsx")
                self.assertEqual(duplicate.status_code, 200)
                after = client.get("/api/workbooks").json()
                self.assertEqual(len(after), 1)
                self.assertEqual(after[0]["workbook_id"], item["workbook_id"])
                self.assertEqual(after[0]["original_filename"], "report.xlsx")
                self.assertEqual(after[0]["imported_at_utc"], item["imported_at_utc"])
                with sqlite3.connect(settings.database_path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0], 0)

    def test_catalog_keeps_same_filename_with_different_bytes_as_distinct_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, _ = self.make_client(root)
            with client:
                first = self.upload(client, workbook_bytes(project="SMART PARKING"), "same.xlsx")
                second = self.upload(client, workbook_bytes(project="ANAT GSM"), "same.xlsx")
                self.assertEqual(first.status_code, 201)
                self.assertEqual(second.status_code, 201)
                catalog = client.get("/api/workbooks").json()
                self.assertEqual(len(catalog), 2)
                self.assertEqual({item["original_filename"] for item in catalog}, {"same.xlsx"})
                self.assertEqual(len({item["workbook_id"] for item in catalog}), 2)
                self.assertEqual(len({item["sha256"] for item in catalog}), 2)

    def test_detail_uses_excel_sheet_order_and_real_import_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client, _ = self.make_client(root)
            workbook = Workbook()
            first_sheet = workbook.active
            first_sheet.title = "BC Tuần 07.09 - 12.09"
            second_sheet = workbook.create_sheet("BC Tuần 31.08 - 05.09")
            for sheet in (first_sheet, second_sheet):
                for address, value in {
                    "A4": "STT", "B4": "Sản phẩm", "C4": "Kết quả tuần",
                    "D4": "Kế hoạch tuần", "E4": "Vấn đề / rủi ro", "F4": "PIC",
                    "A5": "1", "B5": "SMART PARKING", "A6": "1.1", "B6": "PTSP",
                    "C6": "Source actual", "D6": "Next plan", "F6": "Owner",
                }.items():
                    sheet[address] = value
            upload = root / "order.xlsx"
            workbook.save(upload)
            workbook.close()

            with client:
                response = self.upload(client, upload.read_bytes(), "order.xlsx")
                details = client.get(f"/api/workbooks/{response.json()['workbook_id']}")
                self.assertEqual(details.status_code, 200)
                self.assertEqual(
                    [sheet["sheet_name"] for sheet in details.json()["sheets"]],
                    ["BC Tuần 07.09 - 12.09", "BC Tuần 31.08 - 05.09"],
                )
                self.assertEqual(details.json()["imported_at_utc"], client.get("/api/workbooks").json()[0]["imported_at_utc"])


if __name__ == "__main__":
    unittest.main()
