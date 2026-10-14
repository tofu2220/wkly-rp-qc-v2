import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from wkly_rp_qc_v2.importer import inspect_workbook, normalized_payload
from wkly_rp_qc_v2.replay_catalog import eligible_replays, normalized_input_for_workbook
from wkly_rp_qc_v2.store import import_workbook


def replay_bundle(root: Path, input_path: Path, *, project="SMART PARKING", role="PTSP", aliases=None, complete=True):
    response_dir = root / "smart-parking" / "20261012T031440747089Z"
    response_dir.mkdir(parents=True)
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    (response_dir / "run_notes.md").write_text(
        f"- SHA-256 input: {input_hash}\n"
        f"- SHA-256 rules: {'a' * 64}\n"
        "- Phạm vi: previous → current\n",
        encoding="utf-8",
    )
    (response_dir / "comparison.json").write_text(json.dumps({"metadata": {
        "project": project, "role": role, "requested_aliases": aliases or [],
    }}), encoding="utf-8")
    if complete:
        for name in ("extraction_response.json", "assessment_response.json"):
            (response_dir / name).write_text('{"raw_response": {}}', encoding="utf-8")
    return response_dir


class ReplayCatalogTests(unittest.TestCase):
    def test_catalog_returns_only_exact_scope_and_hash_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "normalized_rows.json"
            input_path.write_text('{"rows": []}', encoding="utf-8")
            response_dir = replay_bundle(root / "replays", input_path)
            scope = {
                "project": "SMART PARKING",
                "previous_sheet": "previous",
                "current_sheet": "current",
            }

            bundles = eligible_replays(input_path, scope, "PTSP", [], (root / "replays",))

            self.assertEqual(len(bundles), 1)
            self.assertEqual(bundles[0]["response_dir"], response_dir)
            self.assertTrue(bundles[0]["bundle_id"])
            self.assertEqual(bundles[0]["provenance"]["scope_provenance"], "verified")
            self.assertEqual(bundles[0]["provenance"]["input_sha256"], hashlib.sha256(input_path.read_bytes()).hexdigest())

    def test_catalog_does_not_offer_hash_role_or_project_mismatches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "normalized_rows.json"
            input_path.write_text('{"rows": []}', encoding="utf-8")
            replay_bundle(root / "replays", input_path, role="QC")
            scope = {"project": "SMART PARKING", "previous_sheet": "previous", "current_sheet": "current"}

            self.assertEqual(eligible_replays(input_path, scope, "PTSP", [], (root / "replays",)), [])
            input_path.write_text('{"rows": [1]}', encoding="utf-8")
            self.assertEqual(eligible_replays(input_path, scope, "QC", [], (root / "replays",)), [])

    def test_catalog_skips_missing_response_artifacts_and_unconfigured_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "normalized_rows.json"
            input_path.write_text('{"rows": []}', encoding="utf-8")
            replay_bundle(root / "replays", input_path, complete=False)
            scope = {"project": "SMART PARKING", "previous_sheet": "previous", "current_sheet": "current"}

            self.assertEqual(eligible_replays(input_path, scope, "PTSP", [], (root / "replays",)), [])
            self.assertEqual(eligible_replays(input_path, scope, "PTSP", [], (root / "missing",)), [])

    def test_materialized_normalized_input_keeps_original_filename_and_replay_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbook_path = root / "original-upload-name.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "BC Tuần 01.09 - 05.09"
            for address, value in {
                "A4": "STT", "B4": "Sản phẩm", "C4": "Kết quả tuần",
                "D4": "Kế hoạch tuần", "E4": "Vấn đề / rủi ro", "F4": "PIC",
                "A5": "1", "B5": "Project", "A6": "1.1", "B6": "PTSP",
                "C6": "Actual", "D6": "Next plan", "F6": "Owner",
            }.items():
                sheet[address] = value
            workbook.save(workbook_path)
            workbook.close()
            database, storage = root / "store.sqlite3", root / "files"
            imported = import_workbook(workbook_path, database, storage)
            preview = inspect_workbook(workbook_path)
            expected = (json.dumps(
                normalized_payload(preview), ensure_ascii=False, indent=2, allow_nan=False
            ) + "\n").encode("utf-8")

            normalized_path = normalized_input_for_workbook(database, storage, imported.workbook_id)

            self.assertEqual(normalized_path.read_bytes(), expected)
            self.assertEqual(normalized_path.name, hashlib.sha256(expected).hexdigest() + ".json")


if __name__ == "__main__":
    unittest.main()
