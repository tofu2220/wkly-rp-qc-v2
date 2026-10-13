import copy
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wkly_rp_qc_v2.core import (
    AssessmentValidationError,
    prepare_scope,
    validate_assessment_payload,
)
from wkly_rp_qc_v2.evidence import build_sources, period_context, select_scope


def row(sheet, source_row, project, role, *, actual=None, next_plan=None, issue_risk=None):
    return {
        "sheet": sheet,
        "source_row": source_row,
        "project": project,
        "role": role,
        "pic": "An",
        "actual": actual,
        "next_plan": next_plan,
        "issue_risk": issue_risk,
        "source_refs": {
            "project": f"A{source_row}", "role": f"B{source_row}", "pic": f"C{source_row}",
            "actual": f"D{source_row}", "next_plan": f"E{source_row}", "issue_risk": f"F{source_row}",
        },
        "review_flags": [],
        "reporting_period": "01.09-05.09",
    }


def sample_rows():
    return [
        row("prior ", 7, "ANAT GSM", "PTSP", actual="Actual tuần trước", next_plan="Hoàn thành API\nkiểm thử UAT"),
        row("current", 8, "ANAT GSM", "PTSP", actual="Đã bàn giao API"),
        row("current", 9, "ANAT GSM", "QC", actual="QC đang kiểm thử API"),
    ]


def assessment_fixture(update_evidence=None):
    sources = {
        "P1": {"source_id": "P1", "week": "previous", "field": "next_plan", "text": "Deliver API"},
        "C1": {"source_id": "C1", "week": "current", "field": "actual", "text": "A  B"},
    }
    commitments = [{
        "plan_id": "P001",
        "planned_commitment": "Deliver API",
        "plan_evidence": [{"source_id": "P1", "quote": "Deliver API"}],
        "parent_plan_id": None,
        "planned_stage": None,
        "task_owner": None,
        "planned_deadline_text": None,
    }]
    payload = {"assessments": [{
        "plan_id": "P001",
        "source_reported_status": "In progress",
        "match_confidence": "Confirmed",
        "match_explanation": "The current report refers to the planned deliverable.",
        "reporting_clarity": "Incomplete",
        "update_evidence": update_evidence or [{"source_id": "C1", "quote": "A B"}],
        "findings": [],
        "requires_review": False,
        "review_reasons": [],
        "policy_questions": [],
    }]}
    return payload, commitments, sources


class CorePreparationTests(unittest.TestCase):
    def test_prepare_scope_preserves_selected_rows_aliases_and_all_current_roles(self):
        rows = sample_rows()
        before = copy.deepcopy(rows)
        expected_scope = select_scope(rows, "prior", "current", "DEPOT GSM", "PTSP", ["ANAT GSM"])

        prepared = prepare_scope(
            rows, previous="prior", current="current", project="DEPOT GSM", role="PTSP", aliases=["ANAT GSM"]
        )

        self.assertEqual(rows, before)
        self.assertEqual(prepared.scope, expected_scope)
        self.assertEqual(prepared.scope["previous_sheet"], "prior ")
        self.assertEqual({item["role"] for item in prepared.scope["current_rows"]}, {"PTSP", "QC"})
        self.assertEqual(prepared.scope["project_aliases"], {"DEPOT GSM", "ANAT GSM"})
        self.assertEqual(prepared.sources, build_sources(expected_scope))
        self.assertEqual(prepared.period_context, period_context(expected_scope))
        self.assertIn("Hoàn thành API\nkiểm thử UAT", [source["text"] for source in prepared.sources.values()])

    def test_prepare_scope_preserves_duplicate_and_ambiguous_sheet_errors(self):
        rows = sample_rows()
        duplicate_rows = rows + [copy.deepcopy(rows[0])]
        with self.assertRaisesRegex(ValueError, "Duplicate sheet/row identity"):
            prepare_scope(
                duplicate_rows, previous="prior", current="current", project="ANAT GSM", role="PTSP", aliases=[]
            )

        ambiguous_rows = sample_rows()
        ambiguous_rows[0]["sheet"] = "prior"
        ambiguous_rows[1]["sheet"] = "prior "
        with self.assertRaisesRegex(ValueError, "Ambiguous sheet name"):
            prepare_scope(
                ambiguous_rows, previous="prior  ", current="current", project="ANAT GSM", role="PTSP", aliases=[]
            )

    def test_prepare_scope_is_silent_and_does_not_call_provider_or_read_files(self):
        output = io.StringIO()
        with patch("wkly_rp_qc_v2.openrouter.complete_json", side_effect=AssertionError("provider called")), \
             patch.object(Path, "read_text", side_effect=AssertionError("file read")), \
             redirect_stdout(output):
            prepared = prepare_scope(
                sample_rows(), previous="prior", current="current", project="ANAT GSM", role="PTSP", aliases=[]
            )

        self.assertTrue(prepared.sources)
        self.assertEqual(output.getvalue(), "")


class CoreAssessmentTests(unittest.TestCase):
    def test_validate_assessment_payload_restores_quotes_without_mutating_inputs(self):
        payload, commitments, sources = assessment_fixture()
        before_payload = copy.deepcopy(payload)
        before_sources = copy.deepcopy(sources)

        result = validate_assessment_payload(payload, commitments, sources)

        self.assertEqual(result.assessments[0]["update_evidence"][0]["quote"], "A  B")
        self.assertEqual(len(result.citation_repairs), 1)
        self.assertEqual(result.citation_repairs[0]["original_quote"], "A B")
        self.assertEqual(result.citation_repairs[0]["restored_quote"], "A  B")
        self.assertEqual(result.assessments[0]["source_reported_status"], "In progress")
        self.assertEqual(payload, before_payload)
        self.assertEqual(sources, before_sources)

    def test_validate_assessment_payload_retains_repairs_on_later_failure(self):
        payload, commitments, sources = assessment_fixture([
            {"source_id": "C1", "quote": "A B"},
            {"source_id": "C1", "quote": "C D"},
        ])
        before_payload = copy.deepcopy(payload)
        before_sources = copy.deepcopy(sources)

        with self.assertRaises(AssessmentValidationError) as caught:
            validate_assessment_payload(payload, commitments, sources)

        self.assertIn("Quote is not an exact source substring", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(len(caught.exception.citation_repairs), 1)
        self.assertEqual(caught.exception.citation_repairs[0]["original_quote"], "A B")
        self.assertEqual(payload, before_payload)
        self.assertEqual(sources, before_sources)

    def test_validate_assessment_payload_does_not_salvage_invalid_coverage_or_review(self):
        cases = []
        payload, commitments, sources = assessment_fixture()
        second = copy.deepcopy(commitments[0])
        second["plan_id"] = "P002"
        two_commitments = [commitments[0], second]
        extra = copy.deepcopy(payload["assessments"][0])
        extra["plan_id"] = "meta"
        cases.append((
            "coverage", {"assessments": payload["assessments"] + [extra]}, two_commitments, sources,
            "exactly one entry per commitment", 2,
        ))

        payload, commitments, sources = assessment_fixture()
        payload["assessments"][0]["findings"] = [{
            "rule": "Closure Incomplete", "severity": "Suggestion", "severity_status": "proposed",
            "explanation": "No next checkpoint is reported.",
            "evidence": [{"source_id": "C1", "quote": "A  B"}],
            "question_for_PM": "What is the next checkpoint?",
        }]
        cases.append(("review flag", payload, commitments, sources, "requires_review must agree", 1))

        payload, commitments, sources = assessment_fixture([
            {"source_id": "P1", "quote": "Deliver API"},
        ])
        cases.append(("wrong week", payload, commitments, sources, "current-week source", 0))

        for name, invalid_payload, invalid_commitments, invalid_sources, expected, repair_count in cases:
            with self.subTest(name=name):
                payload_copy = copy.deepcopy(invalid_payload)
                with self.assertRaises(AssessmentValidationError) as caught:
                    validate_assessment_payload(invalid_payload, invalid_commitments, invalid_sources)
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(len(caught.exception.citation_repairs), repair_count)
                self.assertEqual(invalid_payload, payload_copy)


if __name__ == "__main__":
    unittest.main()
