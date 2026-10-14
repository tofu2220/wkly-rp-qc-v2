import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wkly_rp_qc_v2.jobs import JobWorker
from wkly_rp_qc_v2.store import (
    create_job,
    get_job,
    initialize_store,
    list_finding_reviews,
    list_jobs,
    save_finding_review,
    save_job_result,
    set_job_status,
)


def seed_store(database: Path) -> tuple[int, int, int]:
    initialize_store(database)
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO workbook_versions(sha256, original_filename, storage_relpath, imported_at_utc) "
            "VALUES (?, ?, ?, ?)",
            ("a" * 64, "report.xlsx", "aa/report.xlsx", "2026-10-13T00:00:00+00:00"),
        )
        workbook_id = cursor.lastrowid
        snapshots = []
        for index, (sheet, period) in enumerate((
            ("previous", {"status": "identified", "year": None, "start_month_day": "08-31"}),
            ("current", {"status": "identified", "year": None, "start_month_day": "09-07"}),
        )):
            cursor = connection.execute(
                "INSERT INTO report_snapshots(workbook_id, sheet_name, sheet_index, template_id, "
                "mapping_status, sheet_state, reporting_period_json, mapping_json, warnings_json, row_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (workbook_id, sheet, index, "early", "recognized", "not_assessed", json.dumps(period), "{}", "[]", 1),
            )
            snapshots.append(cursor.lastrowid)
    return workbook_id, snapshots[0], snapshots[1]


class WebJobStoreTests(unittest.TestCase):
    def test_create_job_persists_scope_and_pending_state(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)

            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=["ANAT GSM"],
                mode="replay", replay_bundle_id="replay-1",
            )
            job = get_job(database, job_id)

            self.assertEqual(job["status"], "pending")
            self.assertEqual(job["progress"], 0)
            self.assertEqual(job["previous_snapshot_id"], previous_id)
            self.assertEqual(job["current_snapshot_id"], current_id)
            self.assertEqual(job["replay_bundle_id"], "replay-1")
            self.assertEqual(job["aliases"], ["ANAT GSM"])
            self.assertEqual(scalar(database, "SELECT MAX(version) FROM schema_version"), 5)

    def test_job_state_transitions_and_completed_result_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )
            set_job_status(database, job_id, "running", progress=40, coverage={"commitments": 1, "findings": 1})
            result_path = root / "result.json"
            result_path.write_text('{"findings": 1}', encoding="utf-8")

            save_job_result(database, job_id, str(result_path), {"policy_version": "demo-policy-v1"})
            set_job_status(database, job_id, "completed", progress=100)
            job = get_job(database, job_id)

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["progress"], 100)
            self.assertEqual(job["coverage"], {"commitments": 1, "findings": 1})
            self.assertEqual(job["result"]["path"], str(result_path))
            self.assertEqual(job["result"]["metadata"], {"policy_version": "demo-policy-v1"})
            self.assertEqual(len(job["result"]["sha256"]), 64)

    def test_completed_job_cannot_be_reopened(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )
            set_job_status(database, job_id, "running")
            set_job_status(database, job_id, "completed")

            with self.assertRaisesRegex(ValueError, "transition"):
                set_job_status(database, job_id, "running")

    def test_running_job_can_finish_with_no_ai_result_state(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )
            set_job_status(database, job_id, "running")

            set_job_status(database, job_id, "no_ai_result", progress=100)

            self.assertEqual(get_job(database, job_id)["status"], "no_ai_result")

    def test_review_state_and_note_are_job_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            jobs = [create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=f"replay-{index}",
            ) for index in (1, 2)]

            save_finding_review(database, jobs[0], "P001", 0, "accepted", "Source confirms the checkpoint.")
            save_finding_review(database, jobs[1], "P001", 0, "clarification_needed", None)
            save_finding_review(database, jobs[0], "P001", 0, "rejected", "Recheck after mentor review.")

            first = list_finding_reviews(database, jobs[0])
            second = list_finding_reviews(database, jobs[1])
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["state"], "rejected")
            self.assertEqual(first[0]["note"], "Recheck after mentor review.")
            self.assertEqual(second[0]["state"], "clarification_needed")
            self.assertIsNone(second[0]["note"])

    def test_invalid_review_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )

            with self.assertRaisesRegex(ValueError, "review state"):
                save_finding_review(database, job_id, "P001", 0, "approved", "not a policy approval")

    def test_worker_can_list_running_jobs_for_restart_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )
            set_job_status(database, job_id, "running")

            self.assertEqual([job["job_id"] for job in list_jobs(database, status="running")], [job_id])
            self.assertEqual(list_jobs(database, status="completed"), [])

    def test_worker_marks_unqueued_pending_jobs_interrupted_on_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "store.sqlite3"
            workbook_id, previous_id, current_id = seed_store(database)
            job_id = create_job(
                database, workbook_id=workbook_id, previous_snapshot_id=previous_id,
                current_snapshot_id=current_id, project_label="SMART PARKING", role="PTSP", aliases=[],
                mode="replay", replay_bundle_id=None,
            )
            worker = JobWorker(database, Path(temp) / "storage", ())

            worker.start()
            try:
                self.assertEqual(get_job(database, job_id)["status"], "interrupted")
                self.assertEqual([job["job_id"] for job in list_jobs(database, status="interrupted")], [job_id])
            finally:
                worker.stop()


def scalar(database: Path, query: str):
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
