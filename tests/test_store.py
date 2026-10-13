import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import Workbook

from wkly_rp_qc_v2.store import import_workbook, initialize_store


def make_workbook(path: Path, *, actual: str | None = "Reported progress", unknown: bool = False) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "BC Tuần 01.09 - 05.09"
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

            self.assertEqual(scalar(database, "SELECT version FROM schema_version"), 1)
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertTrue({"workbook_versions", "report_snapshots", "report_rows"} <= tables)

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


if __name__ == "__main__":
    unittest.main()
