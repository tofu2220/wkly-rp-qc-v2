import hashlib
import json
import os
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_web import ASGITestClient

from wkly_rp_qc_v2.store import get_job, list_snapshots
from wkly_rp_qc_v2.web import WebSettings, create_app

PROJECT_ROOT = Path("/home/tofu/Projects/wkly-rp-qc-v2")
SAMPLE_WORKBOOK = Path(os.environ.get(
    "WKLY_RP_QC_SAMPLE_WORKBOOK", str(PROJECT_ROOT / "docs/Khối SMC_Báo cáo tuần chi tiết.xlsx")
))
SAMPLE_NORMALIZED = PROJECT_ROOT / "download/1-raw/normalized_rows.json"
REPLAY_RUN = PROJECT_ROOT / "results/smart-parking/20261012T031440747089Z"
REPLAY_ROOT = REPLAY_RUN.parent


@unittest.skipUnless(SAMPLE_WORKBOOK.is_file() and REPLAY_RUN.is_dir(), "authorized sample data is unavailable")
class BrowserAcceptanceTests(unittest.TestCase):
    def test_sample_upload_offline_replay_pm_review_export_and_restart(self):
        source_hash = hashlib.sha256(SAMPLE_WORKBOOK.read_bytes()).hexdigest()
        normalized_hash = hashlib.sha256(SAMPLE_NORMALIZED.read_bytes()).hexdigest()
        response_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in REPLAY_RUN.iterdir() if path.is_file()
        }

        with TemporaryDirectory() as temp:
            root = Path(temp)
            settings = WebSettings(root / "store.sqlite3", root / "storage", (REPLAY_ROOT,))
            with patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")) as provider, \
                 patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=AssertionError("network called")) as network:
                app = create_app(settings)
                with ASGITestClient(app) as client:
                    upload = client.post("/workbooks", files={
                        "workbook": (SAMPLE_WORKBOOK.name, SAMPLE_WORKBOOK.read_bytes(),
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                    }, follow_redirects=False)
                    self.assertEqual(upload.status_code, 303)
                    workbook_id = int(upload.headers["location"].rsplit("/", 1)[1])
                    preview = client.get(f"/workbooks/{workbook_id}")
                    self.assertEqual(preview.status_code, 200)
                    self.assertIn("Template", preview.text)
                    self.assertIn("Kỳ", preview.text)
                    source_rows = re.search(
                        r"(?s)<details([^>]*)>\s*<summary>Source rows and evidence locations</summary>",
                        preview.text,
                    )
                    self.assertIsNotNone(source_rows)
                    self.assertNotIn("open", source_rows.group(1).split())
                    self.assertIn('class="source-value"', preview.text)
                    self.assertIn('class="source-ref"', preview.text)
                    snapshots = [item for item in list_snapshots(settings.database_path) if item["workbook_id"] == workbook_id]
                    previous = next(item for item in snapshots if item["sheet_name"] == "BC Tuần 31.08 - 05.09")
                    current = next(item for item in snapshots if item["sheet_name"] == "BC Tuần 07.09 - 12.09")
                    scope_args = {
                        "previous_snapshot_id": previous["snapshot_id"],
                        "current_snapshot_id": current["snapshot_id"],
                        "project": "SMART PARKING", "role": "PTSP",
                    }
                    scope = client.get("/scope", params=scope_args)
                    self.assertIn(REPLAY_RUN.name, scope.text)
                    catalog = client.get("/replays", params=scope_args).json()
                    matching = next(item for item in catalog if item["run_name"] == REPLAY_RUN.name)
                    started = client.post("/jobs", data={
                        **scope_args, "replay_bundle_id": matching["bundle_id"],
                    }, follow_redirects=False)
                    self.assertEqual(started.status_code, 303)
                    job_id = int(started.headers["location"].rsplit("/", 1)[1])
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and get_job(settings.database_path, job_id)["status"] in {"pending", "running"}:
                        time.sleep(0.02)
                    job = get_job(settings.database_path, job_id)
                    self.assertEqual(job["status"], "completed")
                    comparison = json.loads(Path(job["result"]["path"]).read_text(encoding="utf-8"))
                    self.assertEqual(comparison["counts"], {"commitments": 10, "affected_commitments": 6, "findings": 7})
                    self.assertEqual(len(comparison["metadata"]["citation_repairs"]), 3)
                    self.assertEqual(len(comparison["metadata"]["policy_resolution"]), 7)
                    sources = comparison["sources"]
                    all_quotes = [
                        evidence for assessment in comparison["assessments"]
                        for evidence in assessment["update_evidence"]
                    ] + [
                        evidence for assessment in comparison["assessments"]
                        for finding in assessment["findings"] for evidence in finding["evidence"]
                    ]
                    self.assertTrue(all(item["quote"] in sources[item["source_id"]]["text"] for item in all_quotes))
                    assessment = next(item for item in comparison["assessments"] if item["findings"])
                    review = client.post(f"/jobs/{job_id}/reviews", data={
                        "plan_id": assessment["plan_id"], "finding_index": 0,
                        "state": "clarification_needed", "note": "PM follow-up needed",
                    }, follow_redirects=False)
                    self.assertEqual(review.status_code, 303)
                    self.assertEqual(client.get(f"/jobs/{job_id}/export.csv").status_code, 200)
                    self.assertEqual(client.get(f"/jobs/{job_id}/export.xlsx").status_code, 200)
                    self.assertEqual(client.get(f"/jobs/{job_id}/review.md").status_code, 200)

                restarted = create_app(settings)
                with ASGITestClient(restarted) as client:
                    page = client.get(f"/jobs/{job_id}")
                    self.assertIn("clarification_needed", page.text)
                    self.assertEqual(get_job(settings.database_path, job_id)["status"], "completed")

            provider.assert_not_called()
            network.assert_not_called()

        self.assertEqual(hashlib.sha256(SAMPLE_WORKBOOK.read_bytes()).hexdigest(), source_hash)
        self.assertEqual(hashlib.sha256(SAMPLE_NORMALIZED.read_bytes()).hexdigest(), normalized_hash)
        self.assertEqual(response_hashes, {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in REPLAY_RUN.iterdir() if path.is_file()
        })


if __name__ == "__main__":
    unittest.main()
