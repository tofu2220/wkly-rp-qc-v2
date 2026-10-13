import copy
import csv
import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from wkly_rp_qc_v2.web_export import write_web_exports


def comparison_fixture():
    sources = {
        "P001": {"source_id": "P001", "week": "previous", "sheet": "previous", "source_row": 7,
                 "field": "next_plan", "cell": "E7", "text": "Ship API"},
        "C001": {"source_id": "C001", "week": "current", "sheet": "current", "source_row": 7,
                 "field": "actual", "cell": "D7", "text": "API status <b>ready</b>|\nsecond line"},
        "C002": {"source_id": "C002", "week": "current", "sheet": "current", "source_row": 8,
                 "field": "actual", "cell": "D8", "text": "UAT is waiting"},
    }
    commitments = [
        {"plan_id": "P001", "planned_commitment": "=1+1", "plan_evidence": [{"source_id": "P001", "quote": "Ship API"}]},
        {"plan_id": "P002", "planned_commitment": "Finish UAT", "plan_evidence": [{"source_id": "P001", "quote": "Ship API"}]},
    ]
    assessments = [
        {
            "plan_id": plan_id, "source_reported_status": "In progress", "match_confidence": "Confirmed",
            "reporting_clarity": "Incomplete", "update_evidence": [{"source_id": source_id, "quote": sources[source_id]["text"]}],
            "review_reasons": [], "policy_questions": [], "findings": [{
                "rule": "Closure Incomplete", "severity": severity, "severity_status": status,
                "explanation": "A next checkpoint is not stated.",
                "evidence": [{"source_id": source_id, "quote": sources[source_id]["text"]}],
                "question_for_PM": question,
            }],
        }
        for plan_id, source_id, severity, status, question in (
            ("P001", "C001", "Warning", "proposed", "Can <b>confirm</b>?|\nWhich checkpoint?"),
            ("P002", "C002", "Warning", "confirmed", "Who confirms UAT?"),
        )
    ]
    return {
        "scope": {
            "project": "SMART PARKING", "project_aliases": ["SMART PARKING"],
            "previous_sheet": "previous", "current_sheet": "current",
        },
        "metadata": {
            "role": "PTSP", "policy_version": "demo-policy-v1", "policy_sha256": "a" * 64,
            "policy_resolution": [
                {
                    "plan_id": "P001", "finding_index": 0, "decision_id": None,
                    "source_reference": None, "approved_by": None, "approved_at": None,
                    "model_severity": "Warning", "model_severity_status": "confirmed",
                    "effective_severity": "Warning", "effective_severity_status": "proposed",
                },
                {
                    "plan_id": "P002", "finding_index": 0, "decision_id": "test-case-1",
                    "source_reference": "synthetic-test-approval <script>|\nline two",
                    "approved_by": "Synthetic tester", "approved_at": "2026-10-13",
                    "model_severity": "Suggestion", "model_severity_status": "proposed",
                    "effective_severity": "Warning", "effective_severity_status": "confirmed",
                },
            ],
        },
        "sources": sources,
        "commitments": commitments,
        "assessments": assessments,
        "counts": {"commitments": 2, "affected_commitments": 2, "findings": 2},
    }


class WebExportTests(unittest.TestCase):
    def test_exports_preserve_audit_review_and_source_fields_safely(self):
        comparison = comparison_fixture()
        original = copy.deepcopy(comparison)
        reviews = [{
            "plan_id": "P001", "finding_index": 0, "state": "clarification_needed", "note": "Check <source>|\nagain",
        }]
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)

            write_web_exports(run_dir, comparison, reviews)

            csv_bytes = (run_dir / "pm_review.csv").read_bytes()
            self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["policy_decision_id"], "no approved decision")
            self.assertEqual(rows[0]["review_state"], "clarification_needed")
            self.assertEqual(rows[0]["closure_status"], "")
            self.assertEqual(rows[0]["milestone_change"], "")
            self.assertEqual(rows[0]["missing_information"], "")
            self.assertIn("synthetic-test-approval", rows[1]["source_reference"])
            self.assertEqual(rows[1]["policy_decision_id"], "test-case-1")
            self.assertTrue(rows[0]["planned_commitment"].startswith("'="))
            self.assertEqual(comparison, original)

            markdown = (run_dir / "pm_review.md").read_text(encoding="utf-8")
            self.assertIn("## Chính sách và nguồn quyết định", markdown)
            self.assertIn("Mức hiệu lực: Warning (proposed)", markdown)
            self.assertIn("Model: Warning (confirmed)", markdown)
            self.assertIn("test-case-1", markdown)
            self.assertIn("&lt;script&gt;", markdown)
            self.assertIn(r"\|", markdown)
            self.assertIn("<br>line two", markdown)
            self.assertIn("&lt;b&gt;confirm&lt;/b&gt;", markdown)
            self.assertIn("<br>Which checkpoint?", markdown)
            self.assertIn(r"Check &lt;source&gt;\|<br>again", markdown)

            workbook = load_workbook(run_dir / "pm_review.xlsx", data_only=False)
            sheet = workbook["Findings"]
            self.assertEqual(sheet["A2"].value, "SMART PARKING")
            planned = next(cell for cell in sheet[1] if cell.value == "planned_commitment").column_letter
            self.assertEqual(sheet[f"{planned}2"].value, "=1+1")
            self.assertEqual(sheet[f"{planned}2"].data_type, "s")
            workbook.close()

    def test_legacy_comparison_without_policy_or_reviews_still_exports(self):
        comparison = comparison_fixture()
        comparison["metadata"] = {"role": "PTSP"}
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)

            write_web_exports(run_dir, comparison, [])

            rows = list(csv.DictReader(io.StringIO(
                (run_dir / "pm_review.csv").read_text(encoding="utf-8-sig")
            )))
            markdown = (run_dir / "pm_review.md").read_text(encoding="utf-8")
            self.assertTrue(all(row["review_state"] == "not_reviewed" for row in rows))
            self.assertNotIn("test-case-1", markdown)
            self.assertIn("Policy provenance unavailable", markdown)


if __name__ == "__main__":
    unittest.main()
