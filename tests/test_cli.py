import copy
import csv
import hashlib
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
from wkly_rp_qc_v2.report import write_reports, write_run_notes


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


def quote_repair_responses(input_path, *, update_quote, finding_quotes=(), update_previous=False):
    rows = json.loads(input_path.read_text(encoding="utf-8"))["rows"]
    scope = select_scope(rows, "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [])
    sources = build_sources(scope)
    previous_id = next(key for key, source in sources.items() if source["week"] == "previous")
    current_id = next(key for key, source in sources.items() if source["week"] == "current" and source["field"] == "actual")
    extracted = {"commitments": [{
        "plan_id": "P001", "planned_commitment": "Ship API",
        "plan_evidence": [{"source_id": previous_id, "quote": "Ship API"}], "parent_plan_id": None,
        "planned_stage": None, "task_owner": None, "planned_deadline_text": None,
    }]}
    finding_records = [{
        "rule": "Closure Incomplete", "severity": "Suggestion", "severity_status": "proposed",
        "explanation": "A checkpoint is not specified.",
        "evidence": [{"source_id": current_id, "quote": quote}],
        "question_for_PM": "What is the next checkpoint?",
    } for quote in finding_quotes]
    assessed = {"assessments": [{
        "plan_id": "P001", "source_reported_status": "In progress", "match_confidence": "Confirmed",
        "match_explanation": "The selected evidence names the same deliverable.",
        "reporting_clarity": "Incomplete",
        "update_evidence": [{"source_id": previous_id if update_previous else current_id, "quote": update_quote}],
        "findings": finding_records, "requires_review": bool(finding_records),
        "review_reasons": [], "policy_questions": [],
    }]}
    raw = {"choices": [{"message": {"content": json.dumps(assessed, ensure_ascii=False)}}]}
    return extracted, assessed, raw, sources, current_id


def two_commitment_responses(input_path):
    rows = json.loads(input_path.read_text(encoding="utf-8"))["rows"]
    scope = select_scope(rows, "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [])
    sources = build_sources(scope)
    previous_id = next(key for key, source in sources.items() if source["week"] == "previous")
    current_id = next(key for key, source in sources.items() if source["week"] == "current" and source["field"] == "actual")
    commitments = [{
        "plan_id": plan_id, "planned_commitment": f"Deliverable {plan_id}",
        "plan_evidence": [{"source_id": previous_id, "quote": "Ship API"}], "parent_plan_id": None,
        "planned_stage": None, "task_owner": None, "planned_deadline_text": None,
    } for plan_id in ("P001", "P002")]
    extraction = {"commitments": commitments}
    assessments = [{
        "plan_id": commitment["plan_id"], "source_reported_status": "Reported",
        "match_confidence": "Confirmed", "match_explanation": "Selected evidence is relevant.",
        "reporting_clarity": "Sufficient",
        "update_evidence": [
            {"source_id": current_id, "quote": "alpha"},
            {"source_id": current_id, "quote": "beta"},
        ],
        "findings": [], "requires_review": False, "review_reasons": [], "policy_questions": [],
    } for commitment in commitments]
    assessment = {"assessments": assessments}
    raw_assessment = {"choices": [{"message": {"content": json.dumps(assessment, ensure_ascii=False)}}]}
    return extraction, assessment, raw_assessment, current_id


def replay_args(input_path, responses_dir, output_dir):
    return [
        "replay", "--input", str(input_path),
        "--previous", "BC Tuần 01.09 - 05.09", "--current", "BC Tuần 07.09 - 12.09",
        "--project", "SMART PARKING", "--role", "PTSP",
        "--responses", str(responses_dir), "--output", str(output_dir),
    ]


def replay_response(payload):
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}, "finish_reason": "stop"}]}


def replay_fixture(root, *, assessment_payload=None, actual_text="alpha\nbeta"):
    previous = row("BC Tuần 01.09 - 05.09", 7)
    current = row("BC Tuần 07.09 - 12.09", 7)
    current["actual"] = actual_text
    input_path, _ = fixture_files(root, [previous, current])
    scope = select_scope(
        json.loads(input_path.read_text(encoding="utf-8"))["rows"],
        "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [],
    )
    sources = build_sources(scope)
    previous_id = next(key for key, source in sources.items() if source["week"] == "previous")
    current_id = next(key for key, source in sources.items() if source["week"] == "current" and source["field"] == "actual")
    extraction = {"commitments": [{
        "plan_id": "P001", "planned_commitment": "Ship API",
        "plan_evidence": [{"source_id": previous_id, "quote": "Ship API"}], "parent_plan_id": None,
        "planned_stage": None, "task_owner": None, "planned_deadline_text": None,
    }]}
    assessment = assessment_payload or {"assessments": [{
        "plan_id": "P001", "source_reported_status": "Reported", "match_confidence": "Confirmed",
        "match_explanation": "The selected evidence is relevant.", "reporting_clarity": "Sufficient",
        "update_evidence": [{"source_id": current_id, "quote": "alpha"}], "findings": [],
        "requires_review": False, "review_reasons": [], "policy_questions": [],
    }]}
    responses_dir = root / "historical-run"
    responses_dir.mkdir()
    notes = (
        f"- SHA-256 input: {hashlib.sha256(input_path.read_bytes()).hexdigest()}\n"
        f"- SHA-256 rules: {'a' * 64}\n"
        "- Phạm vi: BC Tuần 01.09 - 05.09 → BC Tuần 07.09 - 12.09\n"
    )
    (responses_dir / "run_notes.md").write_text(notes, encoding="utf-8")
    for name, payload in (("extraction_response.json", extraction), ("assessment_response.json", assessment)):
        envelope = {"raw_response": replay_response(payload), "usage": {"total_tokens": 123}}
        (responses_dir / name).write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return input_path, responses_dir, extraction, assessment


class CliTests(unittest.TestCase):
    def test_replay_runs_without_credentials_or_provider_and_labels_each_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, responses_dir, _, _ = replay_fixture(root)
            (root / ".env").write_text('OPENROUTER_API_KEY="must-not-load"\n', encoding="utf-8")
            historical_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in responses_dir.iterdir()
            }
            output = io.StringIO()
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.load_dotenv") as load_env, \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")) as provider, \
                 patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=AssertionError("network called")) as network, \
                 patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(replay_args(input_path, responses_dir, root / "offline-review")), 0)
                self.assertEqual(main(replay_args(input_path, responses_dir, root / "offline-review")), 0)

            load_env.assert_not_called()
            provider.assert_not_called()
            network.assert_not_called()
            run_dirs = [
                Path(line.split(": ", 1)[1]) for line in output.getvalue().splitlines()
                if line.startswith("Replay directory:")
            ]
            self.assertEqual(len(run_dirs), 2)
            self.assertNotEqual(run_dirs[0], run_dirs[1])
            for run_dir in run_dirs:
                comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
                metadata = comparison["metadata"]
                self.assertEqual(metadata["verification_mode"], "offline_replay")
                self.assertEqual(metadata["usage"], {})
                self.assertEqual(metadata["scope_provenance"], "legacy_partial")
                self.assertEqual(set(metadata["response_sha256"]), {
                    "extraction_response.json", "assessment_response.json",
                })
                review = (run_dir / "mentor_review.md").read_text(encoding="utf-8")
                notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
                self.assertIn("Replay offline — response lịch sử, không gọi AI mới", review)
                self.assertIn("Replay offline — response lịch sử, không gọi AI mới", notes)
                self.assertIn("legacy_partial", notes)
                self.assertIn("historical", notes.lower())
                self.assertIn("không phát sinh", notes)
            self.assertEqual(historical_hashes, {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in responses_dir.iterdir()
            })

    def test_replay_provenance_and_saved_decode_errors_exit_two(self):
        for case in ("input-hash", "malformed-envelope"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_path, responses_dir, _, _ = replay_fixture(root)
                if case == "input-hash":
                    input_path.write_text(input_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
                else:
                    (responses_dir / "assessment_response.json").write_text("not-json", encoding="utf-8")
                output = io.StringIO()
                with patch("os.getcwd", return_value=str(root)), \
                     patch.dict(os.environ, {}, clear=True), \
                     patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")), \
                     patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(main(replay_args(input_path, responses_dir, root / "offline-review")), 2)
                run_dir = Path(next(
                    line.split(": ", 1)[1] for line in output.getvalue().splitlines()
                    if line.startswith(("Replay directory:", "Run directory:"))
                ))
                failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
                self.assertIn("replay", failure["stage"])
                self.assertTrue((run_dir / "run_notes.md").exists())
                for name in ("mentor_review.md", "comparison.json", "comparison.csv"):
                    self.assertFalse((run_dir / name).exists())

    def test_replay_validation_failures_keep_diagnostics_and_partial_repairs(self):
        cases = ("extra-assessment", "review-flag", "invented-quote", "partial-repair")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                if case == "partial-repair":
                    actual_text = "A  B plus C"
                    assessment_payload = {"assessments": [{
                        "plan_id": "P001", "source_reported_status": "Reported",
                        "match_confidence": "Confirmed", "match_explanation": "Same item",
                        "reporting_clarity": "Incomplete", "update_evidence": [], "findings": [],
                        "requires_review": False, "review_reasons": [], "policy_questions": [],
                    }]}
                else:
                    actual_text = "alpha\nbeta"
                    assessment_payload = None
                input_path, responses_dir, _, assessment = replay_fixture(
                    root, assessment_payload=assessment_payload, actual_text=actual_text,
                )
                item = assessment["assessments"][0]
                if case == "extra-assessment":
                    extra = copy.deepcopy(item)
                    extra["plan_id"] = "meta"
                    assessment["assessments"].append(extra)
                elif case == "review-flag":
                    item["findings"] = [{
                        "rule": "Closure Incomplete", "severity": "Suggestion", "severity_status": "proposed",
                        "explanation": "No checkpoint.",
                        "evidence": [{"source_id": "S002", "quote": "alpha"}],
                        "question_for_PM": "What is the next checkpoint?",
                    }]
                    item["requires_review"] = False
                elif case == "invented-quote":
                    item["update_evidence"] = [{"source_id": "S002", "quote": "alpha\n...\nbeta"}]
                else:
                    item["update_evidence"] = [
                        {"source_id": "S002", "quote": "A B"},
                        {"source_id": "S002", "quote": "C D"},
                    ]
                response_path = responses_dir / "assessment_response.json"
                response_path.write_text(json.dumps({"raw_response": replay_response(assessment), "usage": {}}), encoding="utf-8")
                response_hash = hashlib.sha256(response_path.read_bytes()).hexdigest()
                output = io.StringIO()
                with patch("os.getcwd", return_value=str(root)), \
                     patch.dict(os.environ, {}, clear=True), \
                     patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")), \
                     patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(main(replay_args(input_path, responses_dir, root / "offline-review")), 1)
                run_dir = Path(next(
                    line.split(": ", 1)[1] for line in output.getvalue().splitlines()
                    if line.startswith(("Replay directory:", "Run directory:"))
                ))
                failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
                self.assertIn("assessment validation", failure["stage"])
                notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
                if case == "partial-repair":
                    self.assertIn('"original_quote": "A B"', notes)
                    self.assertIn('"restored_quote": "A  B"', notes)
                self.assertEqual(hashlib.sha256(response_path.read_bytes()).hexdigest(), response_hash)
                for name in ("mentor_review.md", "comparison.json", "comparison.csv"):
                    self.assertFalse((run_dir / name).exists())

    def test_replay_refuses_output_inside_historical_run_via_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, responses_dir, _, _ = replay_fixture(root)
            alias = root / "historical-alias"
            alias.symlink_to(responses_dir, target_is_directory=True)
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in responses_dir.iterdir()}
            output = io.StringIO()
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")), \
                 patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    main(replay_args(input_path, responses_dir, alias / "nested-output")), 2
                )
            self.assertEqual(output.getvalue(), "")
            after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in responses_dir.iterdir()}
            self.assertEqual(before, after)

    def test_cli_builds_assessment_schema_from_the_extracted_commitments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = row("BC Tuần 01.09 - 05.09", 7)
            current = row("BC Tuần 07.09 - 12.09", 7)
            current["actual"] = "alpha\nbeta"
            input_path, rules_path = fixture_files(root, [previous, current])
            extraction, assessment, raw_assessment, _ = two_commitment_responses(input_path)
            requests = []
            responses = iter((
                {"payload": extraction, "raw_response": {"stage": "extraction"}, "usage": {}},
                {"payload": assessment, "raw_response": raw_assessment, "usage": {}},
            ))

            def call(messages, schema, *, api_key, model):
                requests.append((messages, schema))
                return next(responses)

            output = io.StringIO()
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=call), \
                 patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 0)

            self.assertEqual(requests[0][1], extraction_schema())
            assessment_schema = requests[1][1]
            assessment_array = assessment_schema["properties"]["assessments"]
            self.assertEqual(assessment_array["items"]["properties"]["plan_id"]["enum"], ["P001", "P002"])
            self.assertEqual(assessment_array["minItems"], 2)
            self.assertEqual(assessment_array["maxItems"], 2)
            self.assertIn("Return exactly one assessment", requests[1][0][0]["content"])
            run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
            self.assertTrue((run_dir / "comparison.json").exists())

    def test_cli_rejects_extra_assessments_and_discontinuous_quotes(self):
        cases = ("extra-assessment", "ellipsized-quote")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                previous = row("BC Tuần 01.09 - 05.09", 7)
                current = row("BC Tuần 07.09 - 12.09", 7)
                current["actual"] = "alpha\nbeta"
                input_path, rules_path = fixture_files(root, [previous, current])
                extraction, assessment, _, current_id = two_commitment_responses(input_path)
                if case == "extra-assessment":
                    extra = copy.deepcopy(assessment["assessments"][0])
                    extra["plan_id"] = "meta"
                    assessment["assessments"].append(extra)
                else:
                    assessment["assessments"][0]["update_evidence"] = [{
                        "source_id": current_id, "quote": "alpha\n...\nbeta",
                    }]
                raw_assessment = {"choices": [{"message": {"content": json.dumps(assessment, ensure_ascii=False)}}]}
                responses = iter((
                    {"payload": extraction, "raw_response": {"stage": "extraction"}, "usage": {}},
                    {"payload": assessment, "raw_response": raw_assessment, "usage": {}},
                ))
                output = io.StringIO()
                with patch("os.getcwd", return_value=str(root)), \
                     patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                     patch("wkly_rp_qc_v2.cli.complete_json", side_effect=responses), \
                     patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(main(run_args(root, input_path, rules_path)), 1)

                run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
                failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
                self.assertEqual(failure["stage"], "assessment validation")
                self.assertTrue((run_dir / "assessment_response.json").exists())
                for name in ("comparison.json", "comparison.csv", "mentor_review.md"):
                    self.assertFalse((run_dir / name).exists())

    def test_offline_end_to_end_writes_four_reports_and_counts_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text(
                'OPENROUTER_API_KEY="dotenv-test-token"\nOPENROUTER_MODEL="dotenv-model"\n', encoding="utf-8"
            )
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
                    {"rule": "Identity Uncertain", "severity": "Suggestion", "severity_status": "proposed", "explanation": "Chưa rõ đây có phải cam kết API đã lên kế hoạch.", "evidence": [{"source_id": prior_id, "quote": "Ship API"}, {"source_id": current_id, "quote": "Đã bàn giao API"}], "question_for_PM": "Đây có phải cam kết API đã lên kế hoạch?"},
                    {"rule": "Status Ambiguous", "severity": None, "severity_status": "unresolved", "explanation": "Chưa rõ trạng thái", "evidence": [{"source_id": current_id, "quote": "Đã bàn giao API"}], "question_for_PM": "Có thể xác nhận trạng thái bàn giao?"},
                ], "requires_review": True, "review_reasons": [], "policy_questions": ["Severity unresolved"],
            }]}
            results = iter([extracted, assessed])
            requests = []
            def call(messages, schema, *, api_key, model):
                requests.append((api_key, model, schema))
                payload = next(results)
                return {"payload": payload, "raw_response": {"ok": True}, "usage": {"total_tokens": 9}}
            output = io.StringIO()
            with patch("os.getcwd", return_value=str(root)), patch.dict(os.environ, {"OPENROUTER_MODEL": "shell-model"}, clear=True), patch("wkly_rp_qc_v2.cli.complete_json", side_effect=call) as client, patch("sys.stdout", output):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 0)
            self.assertEqual(client.call_count, 2)
            self.assertEqual([(key, model) for key, model, _ in requests], [("dotenv-test-token", "shell-model")] * 2)
            self.assertEqual(requests[0][2], extraction_schema())
            assessments_schema = requests[1][2]["properties"]["assessments"]
            self.assertEqual(assessments_schema["items"]["properties"]["plan_id"]["enum"], ["P001"])
            self.assertEqual(assessments_schema["minItems"], 1)
            self.assertEqual(assessments_schema["maxItems"], 1)
            self.assertIn("Selected previous", output.getvalue())
            self.assertIn("Run directory:", output.getvalue())
            run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
            expected = {"mentor_review.md", "comparison.json", "comparison.csv", "run_notes.md", "extraction_response.json", "assessment_response.json"}
            self.assertTrue(expected.issubset({path.name for path in run_dir.iterdir()}))
            comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(comparison["counts"], {"commitments": 1, "affected_commitments": 1, "findings": 2})
            identity_finding = comparison["assessments"][0]["findings"][0]
            self.assertEqual([item["source_id"] for item in identity_finding["evidence"]], [prior_id, current_id])
            csv_bytes = (run_dir / "comparison.csv").read_bytes()
            self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
            records = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            self.assertEqual(records[0]["plan_id"], "P001")
            self.assertIn("Identity Uncertain", records[0]["findings"])
            self.assertIn("Confirmed", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertIn("BC Tuần 07.09 - 12.09!D7", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertIn("BC Tuần 01.09 - 05.09!E7", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertIn("Severity unresolved", (run_dir / "run_notes.md").read_text(encoding="utf-8"))
            self.assertNotIn("Replay offline", (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            self.assertNotIn("Replay offline", (run_dir / "run_notes.md").read_text(encoding="utf-8"))
            self.assertNotIn("dotenv-test-token", output.getvalue())
            self.assertNotIn("dotenv-test-token", "".join(path.read_text(encoding="utf-8") for path in run_dir.iterdir()))

    def test_missing_key_stops_before_api_and_preserves_failure_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, rules_path = fixture_files(root)
            with patch("os.getcwd", return_value=str(root)), patch.dict(os.environ, {}, clear=True), patch("wkly_rp_qc_v2.cli.complete_json") as client:
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
            with patch("os.getcwd", return_value=str(root)), patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-token"}), patch("wkly_rp_qc_v2.cli.complete_json") as client, patch("sys.stdout", output):
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
                with patch("os.getcwd", return_value=str(root)), patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-token"}), patch("wkly_rp_qc_v2.cli.complete_json", side_effect=call), patch("sys.stdout", io.StringIO()):
                    self.assertEqual(main(run_args(root, input_path, rules_path)), 1)
                run_dir = next((root / "results").iterdir())
                self.assertTrue((run_dir / "failure.json").exists())
                self.assertTrue((run_dir / "extraction_response.json").exists())
                self.assertFalse((run_dir / "comparison.json").exists())
                self.assertFalse((run_dir / "mentor_review.md").exists())

    def test_previous_week_update_evidence_fails_validation_without_retry_or_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, rules_path = fixture_files(root)
            scope = select_scope(json.loads(input_path.read_text())["rows"], "BC Tuần 01.09 - 05.09", "BC Tuần 07.09 - 12.09", "SMART PARKING", "PTSP", [])
            sources = build_sources(scope)
            prior_id = next(key for key, item in sources.items() if item["week"] == "previous")
            extracted = {"commitments": [{
                "plan_id": "P001", "planned_commitment": "Ship API",
                "plan_evidence": [{"source_id": prior_id, "quote": "Ship API"}], "parent_plan_id": None,
                "planned_stage": None, "task_owner": None, "planned_deadline_text": None,
            }]}
            assessment = {"assessments": [{
                "plan_id": "P001", "source_reported_status": "Đã bàn giao", "match_confidence": "Confirmed",
                "match_explanation": "Same work", "reporting_clarity": "Sufficient",
                "update_evidence": [{"source_id": prior_id, "quote": "Ship API"}], "findings": [],
                "requires_review": False, "review_reasons": [], "policy_questions": [],
            }]}
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=[
                     {"payload": extracted, "raw_response": {"stage": "extract"}, "usage": {}},
                     {"payload": assessment, "raw_response": {"stage": "assess"}, "usage": {}},
                 ]) as client, patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 1)
            self.assertEqual(client.call_count, 2)
            run_dir = next((root / "results").iterdir())
            failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["stage"], "assessment validation")
            self.assertIn("current-week", failure["error"])
            self.assertEqual(json.loads((run_dir / "assessment_response.json").read_text(encoding="utf-8"))["raw_response"], {"stage": "assess"})
            for name in ("mentor_review.md", "comparison.json", "comparison.csv"):
                self.assertFalse((run_dir / name).exists())

    def test_quote_restoration_writes_canonical_reports_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = row("BC Tuần 01.09 - 05.09", 7)
            current = row("BC Tuần 07.09 - 12.09", 7)
            current["actual"] = "7.  Kế hoạch pilot Zurich tháng 11"
            input_path, rules_path = fixture_files(root, [previous, current])
            bad_quote = "7. Kế hoạch pilot Zurich tháng 11"
            extracted, assessed, raw_assessment, _, _ = quote_repair_responses(
                input_path, update_quote=bad_quote, finding_quotes=(bad_quote, bad_quote)
            )
            results = iter((
                {"payload": extracted, "raw_response": {"choices": [{"message": {"content": json.dumps(extracted)}}]}, "usage": {}},
                {"payload": assessed, "raw_response": raw_assessment, "usage": {}},
            ))
            output, error_output = io.StringIO(), io.StringIO()
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=lambda *args, **kwargs: next(results)) as client, \
                 patch("sys.stdout", output), patch("sys.stderr", error_output):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 0)

            self.assertEqual(client.call_count, 2)
            run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
            comparison = json.loads((run_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(len(comparison["metadata"]["citation_repairs"]), 3)
            self.assertEqual(
                [item["quote"] for item in comparison["assessments"][0]["update_evidence"]],
                [current["actual"]],
            )
            self.assertIn(current["actual"], (run_dir / "comparison.csv").read_text(encoding="utf-8-sig"))
            self.assertIn(current["actual"], (run_dir / "mentor_review.md").read_text(encoding="utf-8"))
            notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
            self.assertIn("## Khôi phục trích dẫn từ nguồn", notes)
            self.assertIn(json.dumps(bad_quote, ensure_ascii=False), notes)
            self.assertIn(json.dumps(current["actual"], ensure_ascii=False), notes)
            saved_raw = json.loads((run_dir / "assessment_response.json").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(saved_raw["raw_response"]["choices"][0]["message"]["content"]), assessed)
            self.assertEqual(assessed["assessments"][0]["update_evidence"][0]["quote"], bad_quote)
            self.assertEqual(error_output.getvalue(), "")

    def test_quote_restoration_failures_keep_diagnostics(self):
        cases = (
            ("changed-date", "Release 11/09", "Release 10/09", False),
            ("ambiguous-span", "Pilot  Zurich; Pilot   Zurich", "Pilot Zurich", False),
            ("wrong-week", "Current report", "Ship API", True),
        )
        for name, actual_text, quote, previous_source in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                previous = row("BC Tuần 01.09 - 05.09", 7)
                current = row("BC Tuần 07.09 - 12.09", 7)
                current["actual"] = actual_text
                input_path, rules_path = fixture_files(root, [previous, current])
                extracted, assessed, raw_assessment, _, _ = quote_repair_responses(
                    input_path, update_quote=quote, update_previous=previous_source
                )
                results = iter((
                    {"payload": extracted, "raw_response": {"stage": "extract"}, "usage": {}},
                    {"payload": assessed, "raw_response": raw_assessment, "usage": {}},
                ))
                output = io.StringIO()
                with patch("os.getcwd", return_value=str(root)), \
                     patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                     patch("wkly_rp_qc_v2.cli.complete_json", side_effect=results), \
                     patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(main(run_args(root, input_path, rules_path)), 1)
                run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
                failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
                self.assertEqual(failure["stage"], "assessment validation")
                self.assertTrue((run_dir / "assessment_response.json").exists())
                self.assertFalse((run_dir / "comparison.json").exists())
                self.assertFalse((run_dir / "comparison.csv").exists())
                self.assertFalse((run_dir / "mentor_review.md").exists())

    def test_partial_quote_repairs_are_logged_when_later_validation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = row("BC Tuần 01.09 - 05.09", 7)
            current = row("BC Tuần 07.09 - 12.09", 7)
            current["actual"] = "A  B plus C"
            input_path, rules_path = fixture_files(root, [previous, current])
            extracted, assessed, _, _, current_id = quote_repair_responses(input_path, update_quote="A B")
            assessed["assessments"][0]["update_evidence"].append({"source_id": current_id, "quote": "C D"})
            raw_assessment = {"choices": [{"message": {"content": json.dumps(assessed, ensure_ascii=False)}}]}
            results = iter((
                {"payload": extracted, "raw_response": {"stage": "extract"}, "usage": {}},
                {"payload": assessed, "raw_response": raw_assessment, "usage": {}},
            ))
            output = io.StringIO()
            with patch("os.getcwd", return_value=str(root)), \
                 patch.dict(os.environ, {"OPENROUTER_API_KEY": "dummy-token"}, clear=True), \
                 patch("wkly_rp_qc_v2.cli.complete_json", side_effect=lambda *args, **kwargs: next(results)), \
                 patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(run_args(root, input_path, rules_path)), 1)
            run_dir = Path(output.getvalue().strip().splitlines()[-1].split(": ", 1)[1])
            notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
            self.assertIn("Số trích dẫn được khôi phục: 1", notes)
            self.assertIn(json.dumps("A B", ensure_ascii=False), notes)
            self.assertIn(json.dumps("A  B", ensure_ascii=False), notes)
            saved_raw = json.loads((run_dir / "assessment_response.json").read_text(encoding="utf-8"))
            self.assertEqual(json.loads(saved_raw["raw_response"]["choices"][0]["message"]["content"]), assessed)
            self.assertFalse((run_dir / "comparison.json").exists())

    def test_run_notes_labels_offline_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            metadata = {
                "verification_mode": "offline_replay",
                "replayed_from": "results/smart-parking/20261012T031440747089Z",
                "citation_repairs": [],
                "usage": {},
            }

            write_run_notes(run_dir, metadata, {}, [])

            notes = (run_dir / "run_notes.md").read_text(encoding="utf-8")
            self.assertIn("Kiểm chứng: replay offline, không gọi API", notes)
            self.assertIn("results/smart-parking/20261012T031440747089Z", notes)
            self.assertNotIn("API đã được gọi", notes)

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
