import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook

from wkly_rp_qc_v2.store import (
    apply_template_mapping,
    confirm_project_alias,
    find_source_conflicts,
    get_original_workbook_path,
    get_snapshot,
    get_template_mapping,
    import_workbook,
    initialize_store,
    list_project_aliases,
    list_snapshots,
    resolve_project_alias,
    save_template_mapping,
)


def make_workbook(
    path: Path,
    *,
    actual: str | None = "Reported progress",
    unknown: bool = False,
    period_name: str = "BC Tuần 01.09 - 05.09",
    draft: bool = False,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = period_name
    if unknown:
        sheet["A1"] = "Unknown header"
        sheet["B9"] = "Unmapped value"
    else:
        for address, value in {
            "A4": "STT", "B4": "Sản phẩm", "C4": "Kết quả tuần",
            "D4": "Kế hoạch tuần", "E4": "Vấn đề / rủi ro", "F4": "PIC",
            "A5": "1", "B5": "Project", "A6": "1.1", "B6": "PTSP",
            "C6": actual, "D6": "Deliver feature", "E6": "", "F6": "Owner",
        }.items():
            sheet[address] = value
        if draft:
            for address in ("C6", "D6", "E6"):
                sheet[address] = None
    workbook.save(path)
    workbook.close()


def scalar(database_path: Path, query: str):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(query).fetchone()[0]


class WorkbookStoreTests(unittest.TestCase):
    def test_store_schema_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"

            initialize_store(database)
            initialize_store(database)

            self.assertEqual(scalar(database, "SELECT MAX(version) FROM schema_version"), 3)
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertTrue({
                "workbook_versions", "report_snapshots", "report_rows", "template_mappings",
                "project_identities", "project_aliases",
            } <= tables)

    def test_import_stores_original_and_source_linked_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.xlsx"
            database = root / "db" / "store.sqlite3"
            storage = root / "files"
            make_workbook(source)
            original_bytes = source.read_bytes()
            expected_hash = hashlib.sha256(original_bytes).hexdigest()

            result = import_workbook(source, database, storage)

            self.assertEqual(result.sha256, expected_hash)
            self.assertFalse(result.duplicate)
            self.assertEqual(result.snapshot_count, 1)
            self.assertEqual(result.row_count, 1)
            self.assertEqual(result.mapping_required_sheets, [])
            stored = storage / expected_hash[:2] / f"{expected_hash}.xlsx"
            self.assertEqual(stored.read_bytes(), original_bytes)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM workbook_versions"), 1)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_snapshots"), 1)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_rows"), 1)
            with sqlite3.connect(database) as connection:
                refs, provenance, workbook_id = connection.execute(
                    "SELECT report_rows.source_refs_json, report_rows.field_provenance_json, "
                    "report_snapshots.workbook_id FROM report_rows JOIN report_snapshots "
                    "USING(snapshot_id)"
                ).fetchone()
                sheet_name, state = connection.execute(
                    "SELECT sheet_name, sheet_state FROM report_snapshots"
                ).fetchone()
            self.assertEqual(workbook_id, result.workbook_id)
            self.assertEqual(sheet_name, "BC Tuần 01.09 - 05.09")
            self.assertEqual(state, "not_assessed")
            self.assertEqual(json.loads(refs)["actual"], "C6")
            self.assertEqual(json.loads(provenance)["actual"]["source_cell"], "C6")

    def test_original_workbook_path_is_hash_checked_and_confined_to_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.xlsx"
            database, storage = root / "store.sqlite3", root / "files"
            make_workbook(source)
            result = import_workbook(source, database, storage)

            stored = get_original_workbook_path(database, storage, result.workbook_id)

            self.assertEqual(stored.read_bytes(), source.read_bytes())
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE workbook_versions SET storage_relpath=?", ("../../outside.xlsx",))
            with self.assertRaisesRegex(ValueError, "escapes"):
                get_original_workbook_path(database, storage, result.workbook_id)

    def test_exact_duplicate_hash_returns_existing_version_without_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.xlsx"
            database = root / "store.sqlite3"
            storage = root / "files"
            make_workbook(source)

            first = import_workbook(source, database, storage)
            second = import_workbook(source, database, storage)

            self.assertEqual(first.workbook_id, second.workbook_id)
            self.assertFalse(first.duplicate)
            self.assertTrue(second.duplicate)
            self.assertEqual(first.row_count, second.row_count)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM workbook_versions"), 1)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_rows"), 1)

    def test_changed_bytes_create_new_immutable_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.xlsx"
            database = root / "store.sqlite3"
            storage = root / "files"
            make_workbook(source, actual="First value")
            first_bytes = source.read_bytes()
            first = import_workbook(source, database, storage)
            make_workbook(source, actual="Changed value")
            second_bytes = source.read_bytes()

            second = import_workbook(source, database, storage)

            self.assertNotEqual(first.sha256, second.sha256)
            self.assertNotEqual(first.workbook_id, second.workbook_id)
            self.assertFalse(second.duplicate)
            self.assertEqual((storage / first.sha256[:2] / f"{first.sha256}.xlsx").read_bytes(), first_bytes)
            self.assertEqual((storage / second.sha256[:2] / f"{second.sha256}.xlsx").read_bytes(), second_bytes)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM workbook_versions"), 2)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_rows"), 2)

    def test_failed_parse_does_not_create_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "corrupt.xlsx"
            source.write_bytes(b"not an xlsx archive")
            database = root / "store.sqlite3"
            storage = root / "files"

            with self.assertRaises(BadZipFile):
                import_workbook(source, database, storage)

            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM workbook_versions"), 0)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_snapshots"), 0)
            self.assertFalse(storage.exists())

    def test_mapping_required_workbook_is_stored_without_guessed_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "unknown.xlsx"
            database = root / "store.sqlite3"
            storage = root / "files"
            make_workbook(source, unknown=True)

            result = import_workbook(source, database, storage)

            self.assertEqual(result.row_count, 0)
            self.assertEqual(result.mapping_required_sheets, ["BC Tuần 01.09 - 05.09"])
            with sqlite3.connect(database) as connection:
                status, count = connection.execute(
                    "SELECT mapping_status, row_count FROM report_snapshots"
                ).fetchone()
            self.assertEqual(status, "mapping_required")
            self.assertEqual(count, 0)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM workbook_versions"), 1)

    def test_schema_migration_adds_mapping_and_alias_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            initialize_store(database)
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")

            initialize_store(database)

            self.assertEqual(scalar(database, "SELECT MAX(version) FROM schema_version"), 3)
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("template_mappings", tables)
            self.assertIn("project_aliases", tables)
            self.assertIn("analysis_jobs", tables)
            self.assertIn("job_results", tables)
            self.assertIn("finding_reviews", tables)

    def test_snapshot_queries_keep_raw_source_addresses_and_workbook_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "snapshot.xlsx"
            database = root / "store.sqlite3"
            make_workbook(source)
            imported = import_workbook(source, database, root / "files")

            snapshot = get_snapshot(database, 1)
            listed = list_snapshots(database)

            self.assertEqual(len(listed), 1)
            self.assertEqual(snapshot["workbook_id"], imported.workbook_id)
            self.assertEqual(snapshot["workbook_sha256"], imported.sha256)
            self.assertEqual(snapshot["rows"][0]["source_refs"]["actual"], "C6")
            self.assertEqual(snapshot["rows"][0]["field_provenance"]["actual"]["source_cell"], "C6")
            self.assertEqual(snapshot["rows"][0]["project_label"], "Project")

    def test_snapshots_keep_unknown_year_and_draft_state_and_sort_by_period(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage = root / "store.sqlite3", root / "files"
            for filename, period, draft in (
                ("sep.xlsx", "BC Tuần 07.09 - 12.09", False),
                ("aug.xlsx", "BC Tuần 31.08 - 05.09", False),
                ("draft.xlsx", "BC Tuần 14.09 - 18.09", True),
            ):
                source = root / filename
                make_workbook(source, period_name=period, draft=draft)
                import_workbook(source, database, storage)

            snapshots = list_snapshots(database)

            self.assertEqual([snapshot["sheet_name"] for snapshot in snapshots], [
                "BC Tuần 31.08 - 05.09", "BC Tuần 07.09 - 12.09", "BC Tuần 14.09 - 18.09",
            ])
            self.assertTrue(all(snapshot["chronology_status"] == "year_unknown" for snapshot in snapshots))
            self.assertEqual(snapshots[-1]["sheet_state"], "draft_or_incomplete")
            self.assertIsNone(snapshots[0]["reporting_period"]["year"])

    def test_overlapping_snapshots_are_reported_without_combining_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage = root / "store.sqlite3", root / "files"
            for filename, actual in (("first.xlsx", "First source"), ("second.xlsx", "Second source")):
                source = root / filename
                make_workbook(source, actual=actual)
                import_workbook(source, database, storage)

            conflicts = find_source_conflicts(database)

            self.assertEqual(len(conflicts), 1)
            self.assertEqual((conflicts[0]["project_label"], conflicts[0]["role_label"]), ("Project", "PTSP"))
            self.assertEqual(len(conflicts[0]["snapshots"]), 2)
            self.assertEqual(len({item["workbook_id"] for item in conflicts[0]["snapshots"]}), 2)
            self.assertEqual(scalar(database, "SELECT COUNT(*) FROM report_rows"), 2)

    def test_mapping_decision_is_versioned_and_reusable(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            initialize_store(database)
            mapping = {"project_col": 2, "role_col": 2, "actual_col": 3}

            save_template_mapping(database, "early", "1", mapping, source_reference="verified template mapping")
            save_template_mapping(database, "early", "2", {**mapping, "actual_col": 4}, source_reference="revised mapping")

            self.assertEqual(get_template_mapping(database, "early", "1")["mapping"], mapping)
            self.assertEqual(get_template_mapping(database, "early", "2")["mapping"]["actual_col"], 4)
            self.assertIsNone(get_template_mapping(database, "early", "missing"))

    def test_project_alias_requires_explicit_record(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            initialize_store(database)

            before = resolve_project_alias(database, "ANAT GSM")
            project_id = confirm_project_alias(
                database, "ANAT GSM", "DEPOT GSM", source_reference="explicit local mapping decision",
            )
            after = resolve_project_alias(database, "ANAT GSM")

            self.assertIsNone(before["project_id"])
            self.assertEqual(before["canonical_label"], "ANAT GSM")
            self.assertFalse(before["explicit"])
            self.assertEqual(after["project_id"], project_id)
            self.assertEqual(after["canonical_label"], "DEPOT GSM")
            self.assertTrue(after["explicit"])
            self.assertEqual(list_project_aliases(database, "DEPOT GSM"), ["ANAT GSM"])
            self.assertEqual(list_project_aliases(database, "ANAT VHR"), [])

    def test_explicit_template_mapping_reprocesses_pending_sheet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "unknown.xlsx"
            database, storage = root / "store.sqlite3", root / "files"
            make_workbook(source, unknown=True, period_name="Unknown Layout")
            workbook = load_workbook(source)
            sheet = workbook.active
            sheet["B9"] = None
            for column, value in enumerate(("Project", "Role", "PIC", "Actual", "Next Plan", "Issue/Risk"), start=1):
                sheet.cell(1, column, value)
            for column, value in enumerate(("Project A", "PTSP", "Owner", "Update", "Next step", None), start=1):
                sheet.cell(2, column, value)
            workbook.save(source)
            workbook.close()
            imported = import_workbook(source, database, storage)
            mapping = {
                "mode": "flat", "template_id": "manual-flat", "version": "1",
                "header_row": 1, "first_data_row": 2,
                "columns": {"project": 1, "role": 2, "pic": 3, "actual": 4, "next_plan": 5, "issue_risk": 6},
            }
            save_template_mapping(database, "manual-flat", "1", mapping, source_reference="PM confirmed column map")

            row_count = apply_template_mapping(
                database, storage, imported.workbook_id, "Unknown Layout", "manual-flat", "1",
            )

            self.assertEqual(row_count, 1)
            snapshot_id = scalar(database, "SELECT snapshot_id FROM report_snapshots")
            with sqlite3.connect(database) as connection:
                status, template_id, count = connection.execute(
                    "SELECT mapping_status, template_id, row_count FROM report_snapshots WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                project, role = connection.execute(
                    "SELECT project_label, role_label FROM report_rows WHERE snapshot_id=?", (snapshot_id,)
                ).fetchone()
            self.assertEqual((status, template_id, count), ("confirmed", "manual-flat", 1))
            self.assertEqual((project, role), ("Project A", "PTSP"))


if __name__ == "__main__":
    unittest.main()
