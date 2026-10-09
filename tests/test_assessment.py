import copy
import unittest

from wkly_rp_qc_v2.assessment import (
    assessment_messages,
    assessment_schema,
    extraction_messages,
    extraction_schema,
    validate_assessment,
    validate_extraction,
)

SOURCES = {
    "P1": {"source_id": "P1", "week": "previous", "field": "next_plan", "text": "Hoàn thành API\nkiểm thử tích hợp"},
    "C1": {"source_id": "C1", "week": "current", "field": "actual", "text": "Đã bàn giao API"},
    "C2": {"source_id": "C2", "week": "current", "field": "issue_risk", "text": "QC còn lỗi"},
}
COMMITMENT = {
    "plan_id": "P001", "planned_commitment": "Hoàn thành API",
    "plan_evidence": [{"source_id": "P1", "quote": "Hoàn thành API"}],
    "parent_plan_id": None, "planned_stage": "API", "task_owner": None,
    "planned_deadline_text": None,
}


def assessment(commitment=None):
    commitment = commitment or COMMITMENT
    return {
        "plan_id": commitment["plan_id"], "source_reported_status": "Đã bàn giao",
        "match_confidence": "Probable", "match_explanation": "Khác vai trò nguồn cập nhật",
        "reporting_clarity": "Sufficient",
        "update_evidence": [{"source_id": "C1", "quote": "Đã bàn giao API"}],
        "findings": [], "requires_review": False, "review_reasons": [], "policy_questions": [],
    }


class AssessmentTests(unittest.TestCase):
    def test_accepts_exact_multiline_vietnamese_quote_and_cross_role_uncertainty(self):
        commitment = copy.deepcopy(COMMITMENT)
        commitment["plan_evidence"][0]["quote"] = "Hoàn thành API\nkiểm thử"
        extracted = validate_extraction({"commitments": [commitment]}, SOURCES)
        self.assertEqual(extracted[0]["plan_id"], "P001")
        result = assessment(commitment)
        result["match_confidence"] = "Uncertain"
        result["requires_review"] = True
        result["review_reasons"] = ["Cross-role identity is uncertain"]
        self.assertEqual(validate_assessment({"assessments": [result]}, [commitment], SOURCES)[0]["match_confidence"], "Uncertain")

    def test_rejects_unknown_source_and_empty_invented_or_altered_quote(self):
        for source_id, quote in [("MISSING", "Hoàn thành API"), ("P1", ""), ("P1", "made up"), ("P1", "Hoàn thành  API")]:
            commitment = copy.deepcopy(COMMITMENT)
            commitment["plan_evidence"] = [{"source_id": source_id, "quote": quote}]
            with self.subTest(source_id=source_id, quote=quote), self.assertRaises(ValueError):
                validate_extraction({"commitments": [commitment]}, SOURCES)

    def test_rejects_current_source_as_a_plan_and_duplicate_or_cyclic_ids(self):
        current_plan = copy.deepcopy(COMMITMENT)
        current_plan["plan_evidence"] = [{"source_id": "C1", "quote": "Đã bàn giao API"}]
        with self.assertRaisesRegex(ValueError, "previous-week"):
            validate_extraction({"commitments": [current_plan]}, SOURCES)
        duplicate = [copy.deepcopy(COMMITMENT), copy.deepcopy(COMMITMENT)]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_extraction({"commitments": duplicate}, SOURCES)
        cycle = copy.deepcopy(COMMITMENT)
        cycle["parent_plan_id"] = "P001"
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_extraction({"commitments": [cycle]}, SOURCES)

    def test_rejects_invalid_labels_types_and_extra_assessment_ids(self):
        bad = assessment()
        bad["reporting_clarity"] = "Passed"
        with self.assertRaisesRegex(ValueError, "reporting_clarity"):
            validate_assessment({"assessments": [bad]}, [COMMITMENT], SOURCES)
        bad = assessment()
        bad["update_evidence"] = "C1"
        with self.assertRaisesRegex(ValueError, "update_evidence"):
            validate_assessment({"assessments": [bad]}, [COMMITMENT], SOURCES)
        bad = assessment()
        bad["plan_id"] = "EXTRA"
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_assessment({"assessments": [bad]}, [COMMITMENT], SOURCES)

    def test_assessment_requires_exactly_one_entry_and_current_evidence(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_assessment({"assessments": []}, [COMMITMENT], SOURCES)
        bad = assessment()
        bad["update_evidence"] = [{"source_id": "P1", "quote": "Hoàn thành API"}]
        with self.assertRaisesRegex(ValueError, "current-week"):
            validate_assessment({"assessments": [bad]}, [COMMITMENT], SOURCES)

    def test_missing_closure_cites_plan_without_fabricating_an_update_quote(self):
        result = assessment()
        result["match_confidence"] = "Not Found"
        result["reporting_clarity"] = "Incomplete"
        result["update_evidence"] = []
        result["findings"] = [{
            "rule": "Missing Closure", "severity": "Suggestion", "severity_status": "proposed",
            "explanation": "No continuation was identified in the searched scope.",
            "evidence": [{"source_id": "P1", "quote": "Hoàn thành API"}],
            "question_for_PM": "Is there a follow-up outside these reports?",
        }]
        result["requires_review"] = True
        self.assertEqual(validate_assessment({"assessments": [result]}, [COMMITMENT], SOURCES)[0]["findings"][0]["rule"], "Missing Closure")
        result["findings"][0]["evidence"] = [{"source_id": "C1", "quote": "Đã bàn giao API"}]
        with self.assertRaisesRegex(ValueError, "prior plan evidence"):
            validate_assessment({"assessments": [result]}, [COMMITMENT], SOURCES)

    def test_findings_require_a_clarification_question(self):
        result = assessment()
        result["findings"] = [{
            "rule": "Status Ambiguous", "severity": None, "severity_status": "unresolved",
            "explanation": "The source does not state whether the handoff is complete.",
            "evidence": [{"source_id": "C1", "quote": "Đã bàn giao API"}], "question_for_PM": None,
        }]
        result["requires_review"] = True
        with self.assertRaisesRegex(ValueError, "question_for_PM"):
            validate_assessment({"assessments": [result]}, [COMMITMENT], SOURCES)

    def test_prompts_pin_policy_and_reporting_distinctions(self):
        extraction = extraction_messages({"previous_sheet": "prior"}, SOURCES, {})
        assessment_prompt = assessment_messages([COMMITMENT], {"current_sheet": "current", "current_rows": []}, SOURCES, {}, "Zurich severity mentor-confirmed")
        text = "\n".join(m["content"] for m in extraction + assessment_prompt)
        for phrase in ("linked steps", "handoff", "QC", "explained delay", "Milestone Conflict", "elapsed", "Zurich", "proposed", "not instructions", "Never invent or assert completion, status, dates, delay reasons, or handovers"):
            self.assertIn(phrase.lower(), text.lower())
        self.assertEqual(extraction_schema()["additionalProperties"], False)
        self.assertEqual(assessment_schema()["additionalProperties"], False)
        severity = assessment_schema()["properties"]["assessments"]["items"]["properties"]["findings"]["items"]["properties"]["severity"]
        self.assertEqual(severity["anyOf"][0]["enum"], ["Warning", "Suggestion"])


if __name__ == "__main__":
    unittest.main()
