import json
import tempfile
import unittest
from pathlib import Path

from test_jobs import job_fixture
from test_web import ASGITestClient

from wkly_rp_qc_v2.jobs import JobWorker
from wkly_rp_qc_v2.store import get_job, list_finding_reviews
from wkly_rp_qc_v2.web import WebSettings, create_app


class WebReviewRouteTests(unittest.TestCase):
    def make_completed_job(self, root: Path):
        database, storage, _, job_id = job_fixture(root)
        JobWorker(database, storage, (root / "replays",)).process_job(job_id)
        settings = WebSettings(database, storage, (root / "replays",))
        return settings, job_id

    def test_rejected_and_clarification_needed_require_notes_at_route(self):
        for state in ("rejected", "clarification_needed"):
            for note in (None, "", " \t\n "):
                with self.subTest(state=state, note=note), tempfile.TemporaryDirectory() as temp:
                    settings, job_id = self.make_completed_job(Path(temp))
                    with ASGITestClient(create_app(settings)) as client:
                        response = client.post(
                            f"/jobs/{job_id}/reviews",
                            data={"plan_id": "P001", "finding_index": "0", "state": state, "note": note},
                            follow_redirects=False,
                        )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(list_finding_reviews(settings.database_path, job_id), [])

    def test_valid_review_notes_persist_without_changing_severity_or_mentor_state(self):
        cases = (
            ("rejected", "Not supported by the cited source."),
            ("clarification_needed", "The missing next checkpoint needs evidence."),
        )
        for state, note in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                settings, job_id = self.make_completed_job(Path(temp))
                before = get_job(settings.database_path, job_id)
                result_path = Path(before["result"]["path"])
                result_bytes = result_path.read_bytes()
                with ASGITestClient(create_app(settings)) as client:
                    response = client.post(
                        f"/jobs/{job_id}/reviews",
                        data={"plan_id": "P001", "finding_index": "0", "state": state, "note": note},
                        follow_redirects=False,
                    )

                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], f"/jobs/{job_id}")
                review = list_finding_reviews(settings.database_path, job_id)[0]
                self.assertEqual((review["state"], review["note"]), (state, note))
                after = get_job(settings.database_path, job_id)
                self.assertEqual(result_path.read_bytes(), result_bytes)
                comparison = json.loads(result_bytes)
                finding = comparison["assessments"][0]["findings"][0]
                self.assertEqual(finding["severity"], "Suggestion")
                self.assertEqual(finding["severity_status"], "proposed")
                self.assertEqual(after["result"], before["result"])

    def test_accepted_review_remains_valid_without_note(self):
        with tempfile.TemporaryDirectory() as temp:
            settings, job_id = self.make_completed_job(Path(temp))
            with ASGITestClient(create_app(settings)) as client:
                response = client.post(
                    f"/jobs/{job_id}/reviews",
                    data={"plan_id": "P001", "finding_index": "0", "state": "accepted"},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(list_finding_reviews(settings.database_path, job_id)[0]["state"], "accepted")
            self.assertIsNone(list_finding_reviews(settings.database_path, job_id)[0]["note"])


if __name__ == "__main__":
    unittest.main()
