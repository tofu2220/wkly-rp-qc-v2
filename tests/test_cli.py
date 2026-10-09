import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wkly_rp_qc_v2.assessment import extraction_schema
from wkly_rp_qc_v2.cli import main
from wkly_rp_qc_v2.evidence import build_sources, select_scope
from wkly_rp_qc_v2.openrouter import OpenRouterError
from wkly_rp_qc_v2.report import write_reports


def row(sheet, source_row, role="PTSP", plan="Ship API"):
    return {
        "sheet": sheet, "source_row": source_row, "project": "SMART PARKING", "role": role,
        "pic": "An", "actual": "Đã bàn giao API", "next_plan": plan, "issue_risk": "",
        "source_refs": {"project": f"A{source_row}", "role": f"B{source_row}", "pic": f"C{source_row}",
                        "actual": f"D{source_row}", "next_plan": f"E{source_row}", "issue_risk": f"F{source_row}"},
        "review_flags": [], "reporting_period": "01.09-05.09",
    }


def fixture_files(root, rows=None):
    input_path = root / "input.json"
    rules_path = root / "rules.md"
    rows = rows or [row("BC Tuần 01.09 - 05.09", 7), row("BC Tuần 07.09 - 12.09", 7), row("BC Tuần 07.09 - 12.09", 8, "QC", "")]
    input_path.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    rules_path.write_text("Draft rules; proposed severity is not approved.", encoding="utf-8")
    return input_path, rules_path


def run_args(root, input_path, rules_path, output=None):
    return ["compare", "--input", str(input_path), "--rules", str(rules_path),
            "--previous", "BC Tuần 01.09 - 05.09", "--current", "BC Tuần 07.09 - 12.09",
            "--project", "SMART PARKING", "--role", "PTSP", "--output", str(output or root / "results")]


class CliTests(unittest.TestCase):
    def test_offline_end_to_end_writes_four_reports_and_counts_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, rules_path = fixture_files(root)
            initial_scope = select_scope(json.loads(input_path.read_text())["rows"], "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [])
            sources = build_sources(initial_scope)
            prior_id = next(key for key, item in sources.items() if item["week"] == "previous")
            current_id = next(key for key, item in sources.items() if item["week"] == "current" and item["field"] == "actual")
            extracted = {"commitments": [{
                "plan_id": "P001", "planned_commitment": "Ship API",
                "plan_evidence": [{"source_id": prior_id, "quote": "Ship API"}], "parent_plan_id": None,
                "planned_stage": "API", "task_owner": None, "planned_deadline_text": None,
            }]}
            assessed = {"assessments": [{
                "plan_id": "P001", "source_reported_status": "Đã bàn giao", "match_confidence": "Confirmed",
                "match_explanation": "Cùng deliverable", "reporting_clarity": "Incomplete",
                "update_evidence": [{"source_id": current_id, "quote": "Đã bàn giao API"}],
                "findings": [
                    {"rule": "Closure Incomplete", "severity": "Suggestion", "severity_status": "proposed", "explanation": "Thiếu checkpoint", "evidence": [{"source_id": current_id, "quote": "Đã bàn giao API"}], "question_for_PM": "Khi nào tiếp tục?"},
                    {"rule": "Status Ambiguous", "severity": None, "severity_status": "unresolved", "explanation": "Chưa rõ trạng thái", "evidence": [{"source_id": current_id, "quote": "Đã bàn giao API"}], "question_for_PM": None},
                ], "requires_review": True, "review_reasons": [], "policy_questions": ["Severity unresolved"],
            }]}
            results = iter([extracted, assessed])
            def call(messages, schema, *, api_key, model):
                payload = next(results)
                return {"payload": payload, "raw_response": {"ok": True}, "usage": {"total_tokens": 9}}
            output = io.StringIO()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-token"}), patch("wkly_rp_qc_v2.cli.complete_json", side_effect=call) as client, patch("sys.stdout", output):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 0)
            self.assertEqual(client.call_count, 2)
            self.assertIn("Selected previous", output.getvalue())
            self.assertIn("Run directory:", output.getvalue())
            run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
            expected = {"mentor_review.md", "comparison.json", "comparison.csv", "run_notes.md", "extraction_response.json", "assessment_response.json"}
            self.assertTrue(expected.issubset({path.name for path in run_dir.iterdir()}))
            comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(comparison["counts"], {"commitments": 1, "affected_commitments": 1, "findings": 2})
            csv_bytes = (run_dir / "comparison.csv").read_bytes()
            self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
            records = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            self.assertEqual(records[0]["plan_id"], "P001")
            self.assertIn("Thiếu checkpoint", records[0]["findings"])
            self.assertIn("Confirmed", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertIn("BC Tuần 07.09 - 12.09!D7", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertIn("Severity unresolved", (run_dir / "run_notes.md").read_text(encoding="utf-8"))

    def test_missing_key_stops_before_api_and_preserves_failure_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, rules_path = fixture_files(root)
            with patch.dict(os.environ, {}, clear=True), patch("wkly_rp_qc_v2.cli.complete_json") as client:
                self.assertEqual(main(run_args(root, input_path, rules_path)), 2)
            client.assert_not_called()
            run_dir = next((root / "results").iterdir())
            self.assertTrue((run_dir / "failure.json").exists())
            self.assertFalse((run_dir / "comparison.json").exists())

    def test_ambiguous_sheet_stops_and_existing_output_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [row("BC Tuần 01.09 - 05.09", 7), row("BC Tuần 01.09 - 05.09 ", 8), row("BC Tuần 07.09 - 12.09", 7)]
            input_path, rules_path = fixture_files(root, rows)
            args = run_args(root, input_path, rules_path)
            args[args.index("BC Tuần 01.09 - 05.09")] = "BC Tuần 01.09 - 05.09  "
            output = io.StringIO()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-token"}), patch("wkly_rp_qc_v2.cli.complete_json") as client, patch("sys.stdout", output):
                self.assertEqual(main(args), 2)
            client.assert_not_called()
            self.assertTrue(any((root / "results").iterdir()))
            existing = root / "already-file"
            existing.write_text("keep", encoding="utf-8")
            self.assertEqual(main(run_args(root, input_path, rules_path, existing)), 2)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_second_call_and_invented_quote_fail_without_success_reports(self):
        for invented, fail_second in ((False, True), (True, False)):
            with self.subTest(invented=invented, fail_second=fail_second), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_path, rules_path = fixture_files(root)
                scope = select_scope(json.loads(input_path.read_text())["rows"], "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [])
                sources = build_sources(scope)
                prior_id = next(key for key, item in sources.items() if item["week"] == "previous")
                extracted = {"commitments": [{"plan_id": "P001", "planned_commitment": "Ship API", "plan_evidence": [{"source_id": prior_id, "quote": "invented" if invented else "Ship API"}], "parent_plan_id": None, "planned_stage": None, "task_owner": None, "planned_deadline_text": None}]}
                def call(messages, schema, *, extracted=extracted, fail_second=fail_second, **kwargs):
                    if schema == extraction_schema():
                        return {"payload": extracted, "raw_response": {"stage": "extract"}, "usage": {}}
                    if fail_second:
                        raise OpenRouterError("provider error", raw_response={"error": "safe"})
                    raise AssertionError("Assessment call should not happen for invalid quote")
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-token"}), patch("wkly_rp_qc_v2.cli.complete_json", side_effect=call), patch("sys.stdout", io.StringIO()):
                    self.assertEqual(main(run_args(root, input_path, rules_path)), 1)
                run_dir = next((root / "results").iterdir())
                self.assertTrue((run_dir / "failure.json").exists())
                self.assertTrue((run_dir / "extraction_response.json").exists())
                self.assertFalse((run_dir / "comparison.json").exists())
                self.assertFalse((run_dir / "mentor_review.md").exists())

    def test_report_escapes_markdown_and_round_trips_multiline_vietnamese(self):
        source = {"S1": {"source_id": "S1", "week": "previous", "field": "next_plan", "sheet": "S", "source_row": 3, "cell": "E3", "text": "<b>|\nDòng hai =x"}}
        scope = {"project": "SMART | <P>", "project_aliases": {"SMART | <P>"}, "previous_sheet": "S", "current_sheet": "T", "previous_rows": [], "current_rows": [], "selection_warnings": []}
        commitment = {"plan_id": "P1", "planned_commitment": "=SUM(1,1)<script>|\nDòng hai", "plan_evidence": [{"source_id": "S1", "quote": "<b>|\nDòng hai =x"}]}
        assessment = {"plan_id": "P1", "source_reported_status": None, "match_confidence": "Not Found", "match_explanation": "", "reporting_clarity": "Incomplete", "update_evidence": [], "findings": [], "requires_review": False, "review_reasons": [], "policy_questions": []}
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            write_reports(run_dir, scope, source, [commitment], [assessment], {})
            markdown = (run_dir / "mentor_review.md").read_text(encoding="utf-8")
            self.assertIn("&lt;script&gt;\\|<br>Dòng hai", markdown)
            self.assertNotIn("<script>", markdown)
            self.assertIn("SMART \\| &lt;P&gt;", markdown)
            csv_bytes = (run_dir / "comparison.csv").read_bytes()
            record = next(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            self.assertTrue(record["planned_commitment"].startswith("'="))
            self.assertIn("<script>", record["planned_commitment"])
            self.assertIn("<b>", record["plan_evidence"])


if __name__ == "__main__":
    unittest.main()
