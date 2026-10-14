import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from wkly_rp_qc_v2.report import write_reports


def report_fixture():
    scope = {
        "project": "SMART PARKING",
        "project_aliases": {"SMART PARKING"},
        "previous_sheet": "previous",
        "current_sheet": "current",
        "previous_rows": [],
        "current_rows": [],
        "selection_warnings": [],
    }
    sources = {
        "P1": {
            "source_id": "P1", "week": "previous", "field": "next_plan", "sheet": "previous",
            "source_row": 7, "cell": "E7", "text": "Ship API",
        },
        "C1": {
            "source_id": "C1", "week": "current", "field": "actual", "sheet": "current",
            "source_row": 7, "cell": "D7", "text": "Build <module>|\nNext step",
        },
        "C2": {
            "source_id": "C2", "week": "current", "field": "actual", "sheet": "current",
            "source_row": 8, "cell": "D8", "text": "UAT is still pending",
        },
    }
    commitments = [
        {
            "plan_id": "P1", "planned_commitment": "Deliver API",
            "plan_evidence": [{"source_id": "P1", "quote": "Ship API"}],
        },
        {
            "plan_id": "P2", "planned_commitment": "Finish UAT",
            "plan_evidence": [{"source_id": "P1", "quote": "Ship API"}],
        },
    ]
    assessments = [
        {
            "plan_id": plan_id,
            "source_reported_status": "In progress",
            "match_confidence": "Confirmed",
            "match_explanation": "The current report refers to this commitment.",
            "reporting_clarity": "Incomplete",
            "update_evidence": [{"source_id": source_id, "quote": sources[source_id]["text"]}],
            "findings": [{
                "rule": "Closure Incomplete",
                "severity": effective_severity,
                "severity_status": effective_status,
                "explanation": "A next checkpoint is missing.",
                "evidence": [{"source_id": source_id, "quote": sources[source_id]["text"]}],
                "question_for_PM": question,
            }],
            "requires_review": effective_status != "confirmed",
            "review_reasons": [],
            "policy_questions": [],
        }
        for plan_id, source_id, effective_severity, effective_status, question in (
            ("P1", "C1", "Suggestion", "proposed", "What <b>next</b>?|\nWhich checkpoint?"),
            ("P2", "C2", "Warning", "confirmed", "Who confirms UAT?"),
        )
    ]
    resolutions = [
        {
            "plan_id": "P1", "finding_index": 0, "rule_id": "closure_incomplete", "rule_version": "1",
            "policy_version": "demo-policy-v1", "decision_id": None, "source_reference": None,
            "approved_by": None, "approved_at": None, "model_severity": "Warning",
            "model_severity_status": "confirmed", "effective_severity": "Suggestion",
            "effective_severity_status": "proposed",
        },
        {
            "plan_id": "P2", "finding_index": 0, "rule_id": "closure_incomplete", "rule_version": "1",
            "policy_version": "demo-policy-v1", "decision_id": "test-case-1",
            "source_reference": "synthetic-test-approval <b>|\nline two", "approved_by": "Synthetic tester",
            "approved_at": "2026-10-13", "model_severity": "Suggestion",
            "model_severity_status": "proposed", "effective_severity": "Warning",
            "effective_severity_status": "confirmed",
        },
    ]
    metadata = {
        "policy_version": "demo-policy-v1",
        "policy_sha256": "a" * 64,
        "policy_resolution": resolutions,
    }
    return scope, sources, commitments, assessments, metadata, resolutions


class PolicyReportTests(unittest.TestCase):
    def test_reports_render_effective_policy_and_plan_scoped_audit(self):
        scope, sources, commitments, assessments, metadata, resolutions = report_fixture()
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)

            write_reports(run_dir, scope, sources, commitments, assessments, metadata)

            comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(comparison["metadata"]["policy_resolution"], resolutions)
            self.assertEqual(comparison["assessments"][0]["findings"][0]["severity_status"], "proposed")
            self.assertEqual(comparison["assessments"][0]["findings"][0]["evidence"][0]["quote"], "Build <module>|\nNext step")

            csv_bytes = (run_dir / "comparison.csv").read_bytes()
            self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
            reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
            self.assertIn("policy_resolution", reader.fieldnames)
            records = list(reader)
            self.assertEqual(json.loads(records[0]["policy_resolution"]), [resolutions[0]])
            self.assertEqual(json.loads(records[1]["policy_resolution"]), [resolutions[1]])

            markdown = (run_dir / "mentor_review.md").read_text(encoding="utf-8")
            row_p1 = next(line for line in markdown.splitlines() if line.startswith("| P1 |"))
            row_p2 = next(line for line in markdown.splitlines() if line.startswith("| P2 |"))
            self.assertIn("effective policy", row_p1.lower())
            self.assertIn("Warning (confirmed)", row_p1)
            self.assertIn("Suggestion (proposed)", row_p1)
            self.assertIn("no approved decision", row_p1)
            self.assertIn("&lt;b&gt;", row_p2)
            self.assertIn(r"\|", row_p2)
            self.assertIn("<br>line two", row_p2)
            self.assertIn("test-case-1", row_p2)
            self.assertNotIn("test-case-1", row_p1)
            self.assertIn("&lt;b&gt;", row_p1)
            self.assertIn("<br>Which checkpoint?", row_p1)

            notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
            self.assertIn("## Chính sách mức độ cảnh báo", notes)
            self.assertIn("demo-policy-v1", notes)
            self.assertIn("a" * 64, notes)
            self.assertIn(json.dumps(resolutions[1], ensure_ascii=False), notes)
            self.assertIn("no approved decision", notes)

    def test_legacy_reports_without_policy_metadata_do_not_claim_policy_approval(self):
        scope, sources, commitments, assessments, _, _ = report_fixture()
        assessments = [assessments[0]]
        commitments = [commitments[0]]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)

            write_reports(run_dir, scope, sources, commitments, assessments, {})

            markdown = (run_dir / "mentor_review.md").read_text(encoding="utf-8")
            notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
            csv_record = next(csv.DictReader(io.StringIO(
                (run_dir / "comparison.csv").read_text(encoding="utf-8-sig")
            )))
            self.assertIn("Closure Incomplete", markdown)
            self.assertNotIn("effective policy", markdown.lower())
            self.assertNotIn("test-case-1", markdown)
            self.assertNotIn("## Chính sách mức độ cảnh báo", notes)
            self.assertIn("policy_resolution", csv_record)
            self.assertEqual(json.loads(csv_record["policy_resolution"]), [])


if __name__ == "__main__":
    unittest.main()
