"""Negative tests for fail-closed archive and release evidence handling."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_demo_outcome
import test_disposition_packet
import run_release_checks


class ArchivedEvidenceHarnessTests(unittest.TestCase):
    def test_archived_tracking_requires_all_three_frozen_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in test_demo_outcome.ARCHIVED_TRACKING_PATHS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(test_demo_outcome.ROOT / relative, destination)

            test_demo_outcome.validate_archived_tracking(root=root)

            requirements = root / test_demo_outcome.ARCHIVED_TRACKING_PATHS[1]
            original = requirements.read_bytes()
            requirements.write_text("AMEND-02-DEPOT-NORESULT-01\nĐồng ys\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(AssertionError, "DEMO-01"):
                    test_demo_outcome.validate_archived_tracking(root=root)
            finally:
                requirements.write_bytes(original)

            requirements.unlink()
            with self.assertRaisesRegex(FileNotFoundError, "v1.0-REQUIREMENTS.md"):
                test_demo_outcome.validate_archived_tracking(root=root)

    def test_wrong_stage_and_missing_confirmation_are_rejected(self):
        packet_text, ledger = test_disposition_packet.load_packet()
        with self.assertRaisesRegex(ValueError, "ledger stage: expected 'tracer'"):
            test_disposition_packet.validate_packet(ledger, "tracer")

        missing_confirmation = copy.deepcopy(ledger)
        missing_confirmation["events"] = [
            event for event in missing_confirmation["events"]
            if event.get("kind") != "record_confirmation"
        ]
        test_disposition_packet.resign_current_revision(missing_confirmation)
        with patch.object(test_disposition_packet, "load_packet", return_value=(packet_text, missing_confirmation)):
            with self.assertRaisesRegex(ValueError, "record_confirmation"):
                test_disposition_packet.run_artifact_gate("review_recorded", require_confirmation=True)

    def test_required_fixture_preflight_fails_with_actionable_missing_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(FileNotFoundError, "Required acceptance inputs are missing") as caught:
                run_release_checks.required_acceptance_inputs(Path(temp))
            message = str(caught.exception)
            self.assertIn("workbook", message)
            self.assertIn("normalized", message)
            self.assertIn("replay_assessment_response.json", message)
            self.assertIn("candidate-cases.json", message)

    def test_each_required_acceptance_input_fails_closed_when_overridden_missing(self):
        overrides = {
            "WKLY_RP_QC_SAMPLE_WORKBOOK": "workbook",
            "WKLY_RP_QC_SAMPLE_NORMALIZED": "normalized",
            "WKLY_RP_QC_REPLAY_RUN": "replay_run",
            "WKLY_RP_QC_DISPOSITION_PACKET": "packet",
            "WKLY_RP_QC_CANDIDATE_MANIFEST": "candidate_manifest",
            "WKLY_RP_QC_BUSINESS_REFERENCE": "business_review_reference",
            "WKLY_RP_QC_DRAFT_REFERENCE": "draft_reference",
        }
        with tempfile.TemporaryDirectory() as temp:
            for variable, label in overrides.items():
                with self.subTest(label=label):
                    with self.assertRaises(FileNotFoundError) as caught:
                        run_release_checks.required_acceptance_inputs(
                            environ={variable: str(Path(temp) / f"missing-{label}")}
                        )
                    self.assertIn(label, str(caught.exception))

    def test_each_saved_replay_payload_is_required_individually(self):
        source_run = run_release_checks.REPO_ROOT / "results/smart-parking/20261012T031440747089Z"
        with tempfile.TemporaryDirectory() as temp:
            replay_run = Path(temp) / "smart-parking" / run_release_checks.REPLAY_NAME
            replay_run.mkdir(parents=True)
            for missing in run_release_checks.RAW_INPUTS:
                with self.subTest(missing=missing):
                    for filename in run_release_checks.RAW_INPUTS:
                        destination = replay_run / filename
                        if filename == missing:
                            destination.unlink(missing_ok=True)
                        else:
                            shutil.copyfile(source_run / filename, destination)
                    with self.assertRaises(FileNotFoundError) as caught:
                        run_release_checks.required_acceptance_inputs(
                            environ={"WKLY_RP_QC_REPLAY_RUN": str(replay_run)}
                        )
                    self.assertIn(f"replay_{missing}", str(caught.exception))

    def test_release_packet_gate_cannot_be_relaxed_by_environment(self):
        with patch.dict(os.environ, {"DISPOSITION_EXPECT_STAGE": "tracer", "DISPOSITION_REQUIRE_CONFIRMATION": "0"}):
            self.assertEqual(run_release_checks.run_packet_gate(), 0)

    def test_pytest_manifest_rejects_zero_mismatch_skip_failure_and_missing_report(self):
        passed = {"setup": "passed", "call": "passed", "teardown": "passed"}
        self.assertTrue(run_release_checks.pytest_evidence_passes(["a::test_ok"], ["a::test_ok"], {"a::test_ok": passed}, 0))
        self.assertFalse(run_release_checks.pytest_evidence_passes([], [], {}, 0))
        self.assertFalse(run_release_checks.pytest_evidence_passes(["a::test_ok"], [], {}, 0))
        self.assertFalse(run_release_checks.pytest_evidence_passes(["a::test_ok"], ["a::test_ok"], {"a::test_ok": {"setup": "skip"}}, 0))
        self.assertFalse(run_release_checks.pytest_evidence_passes(["a::test_ok"], ["a::test_ok"], {"a::test_ok": {"setup": "passed", "call": "failed", "teardown": "passed"}}, 1))
        self.assertFalse(run_release_checks.pytest_evidence_passes(["a::test_ok"], ["a::test_ok"], {}, 0))

    def _run_isolated_pytest(self, content: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        module = root / f"test_harness_{uuid4().hex}.py"
        module.write_text(content, encoding="utf-8")
        evidence = root / "evidence"
        evidence.mkdir()
        with patch.dict(os.environ, {run_release_checks.INTERNAL_NEGATIVE_RUN: "1"}):
            result = run_release_checks._run_pytest([str(module)], evidence)
        return temporary, evidence, result

    def test_pytest_runner_rejects_empty_selection_skip_and_failure(self):
        cases = (
            ("value = 1\n", 0),
            ("import pytest\ndef test_skip(): pytest.skip('required')\n", 1),
            ("def test_fail(): assert False\n", 1),
        )
        for content, expected_count in cases:
            with self.subTest(expected_count=expected_count, content=content[:20]):
                temporary, evidence, result = self._run_isolated_pytest(content)
                try:
                    self.assertEqual(result["collected_count"], expected_count)
                    self.assertFalse(result.get("passed", False))
                finally:
                    temporary.cleanup()
                self.assertFalse(evidence.exists())

    def test_raw_diagnostics_preserve_unicode_newlines_and_numeric_text(self):
        diagnostic = "Đường dẫn\nline 2: 18 tests, 0 skipped\n"
        temporary, evidence, result = self._run_isolated_pytest(
            "def test_diagnostic():\n    print(" + repr(diagnostic) + ")\n    assert True\n"
        )
        try:
            self.assertTrue(result["passed"], (result, (evidence / "pytest-execution.stdout.log").read_bytes(), (evidence / "pytest-execution.stderr.log").read_bytes()))
            raw = (evidence / "pytest-execution.stdout.log").read_bytes().decode("utf-8")
            self.assertIn(diagnostic, raw)
            structured = json.loads(json.dumps(result, ensure_ascii=False))
            self.assertEqual(structured["collected_ids"], structured["executed_ids"])
            self.assertEqual(structured["executed_count"], 1)
        finally:
            temporary.cleanup()
        self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
