import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from wkly_rp_qc_v2.core import prepare_scope
from wkly_rp_qc_v2.importer import inspect_workbook, normalized_payload
from wkly_rp_qc_v2.jobs import JobWorker
from wkly_rp_qc_v2.replay_catalog import eligible_replays
from wkly_rp_qc_v2.store import (
    create_job,
    get_job,
    import_workbook,
    list_snapshots,
    set_job_status,
)

PREVIOUS = "BC Tuần 31.08 - 05.09"
CURRENT = "BC Tuần 07.09 - 12.09"


def _write_sheet(sheet, title: str, *, actual: str, next_plan: str):
    sheet.title = title
    for address, value in {
        "A4": "STT", "B4": "Sản phẩm", "C4": "Kết quả tuần",
        "D4": "Kế hoạch tuần", "E4": "Vấn đề / rủi ro", "F4": "PIC",
        "A5": "1", "B5": "SMART PARKING", "A6": "1.1", "B6": "PTSP",
        "C6": actual, "D6": next_plan, "F6": "Owner",
    }.items():
        sheet[address] = value


def job_fixture(root: Path, *, invalid_assessment: bool = False, replay_bundle: bool = True):
    root.mkdir(parents=True, exist_ok=True)
    workbook_path = root / "two-weeks.xlsx"
    workbook = Workbook()
    _write_sheet(workbook.active, PREVIOUS, actual="Previous status", next_plan="Ship API")
    _write_sheet(workbook.create_sheet(), CURRENT, actual="API is still in testing", next_plan="Continue testing")
    workbook.save(workbook_path)
    workbook.close()

    database = root / "app.sqlite3"
    storage = root / "storage"
    imported = import_workbook(workbook_path, database, storage)
    preview = inspect_workbook(workbook_path)
    normalized = normalized_payload(preview)
    normalized_path = storage / "normalized" / f"{hashlib.sha256((json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + chr(10)).encode()).hexdigest()}.json"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    snapshots = {snapshot["sheet_name"]: snapshot for snapshot in list_snapshots(database)}
    previous_snapshot = snapshots[PREVIOUS]
    current_snapshot = snapshots[CURRENT]
    prepared = prepare_scope(
        preview.rows, previous=PREVIOUS, current=CURRENT, project="SMART PARKING", role="PTSP", aliases=[]
    )
    previous_source = next(source for source in prepared.sources.values() if source["week"] == "previous")
    current_source = next(
        source for source in prepared.sources.values() if source["week"] == "current" and source["field"] == "actual"
    )
    extraction = {"commitments": [{
        "plan_id": "P001", "planned_commitment": "Ship API",
        "plan_evidence": [{"source_id": previous_source["source_id"], "quote": "Ship API"}],
        "parent_plan_id": None, "planned_stage": None, "task_owner": None, "planned_deadline_text": None,
    }]}
    assessment = {"assessments": [{
        "plan_id": "P001", "source_reported_status": "In progress", "match_confidence": "Confirmed",
        "match_explanation": "The current report names the planned deliverable.",
        "reporting_clarity": "Incomplete",
        "update_evidence": [{"source_id": current_source["source_id"], "quote": current_source["text"]}],
        "findings": [{
            "rule": "Closure Incomplete", "severity": "Suggestion", "severity_status": "proposed",
            "explanation": "No next checkpoint is given.",
            "evidence": [{"source_id": current_source["source_id"], "quote": current_source["text"]}],
            "question_for_PM": "What is the next checkpoint?",
        }],
        "requires_review": True, "review_reasons": [], "policy_questions": [],
    }]}
    if invalid_assessment:
        assessment["assessments"].append(dict(assessment["assessments"][0], plan_id="EXTRA"))

    response_dir = root / "replays" / "smart-parking" / "sample-run"
    response_dir.mkdir(parents=True)
    input_sha256 = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
    (response_dir / "run_notes.md").write_text(
        f"- SHA-256 input: {input_sha256}\n"
        f"- SHA-256 rules: {'b' * 64}\n"
        f"- Phạm vi: {PREVIOUS} → {CURRENT}\n",
        encoding="utf-8",
    )
    (response_dir / "comparison.json").write_text(json.dumps({"metadata": {
        "project": "SMART PARKING", "role": "PTSP", "requested_aliases": [],
    }}), encoding="utf-8")
    for name, payload in (("extraction_response.json", extraction), ("assessment_response.json", assessment)):
        raw = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}, "finish_reason": "stop"}]}
        (response_dir / name).write_text(json.dumps({"raw_response": raw, "usage": {"total_tokens": 100}}), encoding="utf-8")
    catalog = eligible_replays(normalized_path, prepared.scope, "PTSP", [], (root / "replays",))
    bundle_id = catalog[0]["bundle_id"] if replay_bundle and catalog else None
    job_id = create_job(
        database, workbook_id=imported.workbook_id, previous_snapshot_id=previous_snapshot["snapshot_id"],
        current_snapshot_id=current_snapshot["snapshot_id"], project_label="SMART PARKING", role="PTSP",
        aliases=[], mode="replay", replay_bundle_id=bundle_id,
    )
    return database, storage, response_dir, job_id


class ReplayJobTests(unittest.TestCase):
    def test_replay_job_completes_with_effective_policy_and_no_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage, response_dir, job_id = job_fixture(root)
            original_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in response_dir.iterdir()}
            worker = JobWorker(database, storage, (root / "replays",))

            with patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")) as provider, \
                 patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=AssertionError("network called")) as network:
                worker.process_job(job_id)

            job = get_job(database, job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["coverage"]["result_state"], "completed_with_findings")
            self.assertEqual(job["coverage"]["findings"], 1)
            comparison = json.loads(Path(job["result"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(comparison["counts"], {"commitments": 1, "affected_commitments": 1, "findings": 1})
            self.assertEqual(comparison["metadata"]["usage"], {})
            self.assertEqual(len(comparison["metadata"]["policy_resolution"]), 1)
            self.assertTrue(all(record["decision_id"] is None for record in comparison["metadata"]["policy_resolution"]))
            provider.assert_not_called()
            network.assert_not_called()
            self.assertEqual(original_hashes, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in response_dir.iterdir()})

    def test_unmatched_replay_is_no_ai_result_not_a_clean_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage, _, job_id = job_fixture(root, replay_bundle=False)

            JobWorker(database, storage, (root / "replays",)).process_job(job_id)

            job = get_job(database, job_id)
            self.assertEqual(job["status"], "no_ai_result")
            self.assertEqual(job["coverage"]["result_state"], "no_ai_result_available")
            self.assertIsNone(job["result"])

    def test_invalid_historical_assessment_fails_without_success_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage, _, job_id = job_fixture(root, invalid_assessment=True)

            JobWorker(database, storage, (root / "replays",)).process_job(job_id)

            job = get_job(database, job_id)
            self.assertEqual(job["status"], "failed")
            self.assertIn("coverage", job["error"].lower())
            self.assertIsNone(job["result"])
            failure_files = list((storage / "runs" / str(job_id)).rglob("failure.json"))
            self.assertEqual(len(failure_files), 1)
            run_dir = failure_files[0].parent
            self.assertFalse((run_dir / "comparison.json").exists())
            self.assertFalse((run_dir / "mentor_review.md").exists())

    def test_worker_marks_interrupted_jobs_and_resume_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage, _, job_id = job_fixture(root, replay_bundle=False)
            set_job_status(database, job_id, "running")
            worker = JobWorker(database, storage, (root / "replays",))

            worker.start()
            try:
                self.assertEqual(get_job(database, job_id)["status"], "interrupted")
                worker.resume(job_id)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and get_job(database, job_id)["status"] not in {"no_ai_result", "failed"}:
                    time.sleep(0.01)
                self.assertEqual(get_job(database, job_id)["status"], "no_ai_result")
            finally:
                worker.stop()

    def test_completed_job_cannot_be_enqueued_for_duplicate_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database, storage, _, job_id = job_fixture(root)
            worker = JobWorker(database, storage, (root / "replays",))
            worker.process_job(job_id)

            with self.assertRaisesRegex(ValueError, "pending"):
                worker.enqueue(job_id)
            self.assertEqual(get_job(database, job_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
