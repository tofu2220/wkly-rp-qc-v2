import copy
import json
import tempfile
import unittest
from pathlib import Path

from wkly_rp_qc_v2.assessment import validate_assessment
from wkly_rp_qc_v2.policy import PolicyResult, load_policy, resolve_policy

RULES = {
    "identity_uncertain": "Identity Uncertain",
    "status_ambiguous": "Status Ambiguous",
    "milestone_conflict": "Milestone Conflict",
    "closure_incomplete": "Closure Incomplete",
    "missing_closure": "Missing Closure",
}


def policy_fixture():
    return {
        "schema_version": 1,
        "version": "test-policy-v1",
        "rules": [
            {"rule_id": rule_id, "model_label": label, "version": "1"}
            for rule_id, label in RULES.items()
        ],
        "decisions": [],
    }


def decision_fixture(**overrides):
    decision = {
        "decision_id": "decision-1",
        "rule_id": "closure_incomplete",
        "severity": "Warning",
        "severity_status": "confirmed",
        "applies_to": {
            "project": "SMART PARKING",
            "role": "PTSP",
            "previous_sheet": "previous",
            "current_sheet": "current",
            "plan_quote": "Deliver API",
        },
        "basis": "explicit_approval",
        "source_reference": "meeting notes, section 4",
        "approved_by": "Example approver",
        "approved_at": "2026-10-13T10:30:00+07:00",
    }
    decision.update(overrides)
    return decision


def resolver_fixture(plan_quotes=("Deliver API",), *, severity="Suggestion", status="proposed", reasons=(), questions=()):
    sources = {
        "P1": {"source_id": "P1", "week": "previous", "field": "next_plan", "text": "Deliver API; Deliver UI"},
        "C1": {"source_id": "C1", "week": "current", "field": "actual", "text": "API remains in testing"},
        "C2": {"source_id": "C2", "week": "current", "field": "actual", "text": "UI is being reviewed"},
    }
    commitments = [
        {
            "plan_id": f"P{index:03}",
            "planned_commitment": quote,
            "plan_evidence": [{"source_id": "P1", "quote": quote}],
            "parent_plan_id": None,
            "planned_stage": None,
            "task_owner": None,
            "planned_deadline_text": None,
        }
        for index, quote in enumerate(plan_quotes, start=1)
    ]
    assessments = []
    for index, commitment in enumerate(commitments, start=1):
        assessment_reasons = list(reasons)
        assessment_questions = list(questions)
        assessment = {
            "plan_id": commitment["plan_id"],
            "source_reported_status": "In progress",
            "match_confidence": "Confirmed",
            "match_explanation": "The current report refers to this deliverable.",
            "reporting_clarity": "Incomplete",
            "update_evidence": [{"source_id": f"C{index}", "quote": sources[f"C{index}"]["text"]}],
            "findings": [{
                "rule": "Closure Incomplete",
                "severity": severity,
                "severity_status": status,
                "explanation": "No next checkpoint is reported.",
                "evidence": [{"source_id": f"C{index}", "quote": sources[f"C{index}"]["text"]}],
                "question_for_PM": "What is the next checkpoint?",
            }],
            "requires_review": bool(assessment_reasons or assessment_questions or status != "confirmed"),
            "review_reasons": assessment_reasons,
            "policy_questions": assessment_questions,
        }
        assessments.append(assessment)
    scope = {"project": "SMART PARKING", "previous_sheet": "previous", "current_sheet": "current"}
    return assessments, commitments, sources, scope


def rule_proposal_fixture():
    return decision_fixture(
        decision_id="rule-proposal",
        severity="Suggestion",
        severity_status="proposed",
        applies_to={"project": None, "role": None, "previous_sheet": None, "current_sheet": None, "plan_quote": None},
        basis="proposal",
        source_reference="draft policy note",
        approved_by=None,
        approved_at=None,
    )


def case_scope(*, project="SMART PARKING", role="PTSP", previous_sheet="previous", current_sheet="current", plan_quote="Deliver API"):
    return {
        "project": project,
        "role": role,
        "previous_sheet": previous_sheet,
        "current_sheet": current_sheet,
        "plan_quote": plan_quote,
    }


class PolicyLoaderTests(unittest.TestCase):
    def load_registry(self, registry):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            return load_policy(path)

    def test_load_default_policy_has_five_rules_and_no_decisions(self):
        registry = load_policy()

        self.assertEqual(set(registry), {"schema_version", "version", "rules", "decisions"})
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["version"], "demo-policy-v1")
        self.assertEqual(
            {rule["rule_id"]: rule["model_label"] for rule in registry["rules"]},
            RULES,
        )
        self.assertTrue(all(rule["version"] == "1" for rule in registry["rules"]))
        self.assertEqual(registry["decisions"], [])

    def test_load_policy_accepts_provenanced_case_and_rule_level_proposals(self):
        registry = policy_fixture()
        approved_case = decision_fixture()
        proposal = decision_fixture(
            decision_id="decision-2",
            severity="Suggestion",
            severity_status="proposed",
            applies_to={
                "project": None,
                "role": None,
                "previous_sheet": None,
                "current_sheet": None,
                "plan_quote": None,
            },
            basis="proposal",
            source_reference="draft policy note",
            approved_by=None,
            approved_at=None,
        )
        unresolved = decision_fixture(
            decision_id="decision-3",
            severity=None,
            severity_status="unresolved",
            rule_id="missing_closure",
            applies_to={
                "project": None,
                "role": None,
                "previous_sheet": None,
                "current_sheet": None,
                "plan_quote": None,
            },
            basis="unresolved_policy",
            source_reference="unresolved policy note",
            approved_by=None,
            approved_at=None,
        )
        registry["decisions"] = [approved_case, proposal, unresolved]

        self.assertEqual(self.load_registry(registry), registry)

    def test_load_policy_rejects_invalid_or_conflicting_decisions(self):
        invalid_registries = []

        for field, value in (
            ("source_reference", ""),
            ("approved_by", None),
            ("approved_at", "not-a-date"),
        ):
            registry = policy_fixture()
            registry["decisions"] = [decision_fixture(**{field: value})]
            invalid_registries.append((f"confirmed {field}", registry, field))

        cases = [
            ("unknown rule", {"rule_id": "unknown_rule"}, "rule_id"),
            ("unknown severity", {"severity": "Critical"}, "severity"),
            ("null proposed", {"severity": None, "severity_status": "proposed"}, "severity"),
            ("unresolved severity", {"severity_status": "unresolved"}, "severity"),
            ("wrong basis", {"basis": "proposal"}, "basis"),
            ("proposal approval", {"severity_status": "proposed", "basis": "proposal", "approved_by": "Name"}, "approved_by"),
            ("unresolved basis", {"severity": None, "severity_status": "unresolved", "basis": "proposal"}, "basis"),
            (
                "mixed scope",
                {"applies_to": {"project": "P", "role": None, "previous_sheet": None, "current_sheet": None, "plan_quote": None}},
                "applies_to",
            ),
            ("missing field", {"approved_at": None}, "approved_at"),
            ("extra field", {"unplanned": "value"}, "decision"),
        ]
        for name, overrides, field in cases:
            registry = policy_fixture()
            registry["decisions"] = [decision_fixture(**overrides)]
            invalid_registries.append((name, registry, field))

        duplicate_id = policy_fixture()
        duplicate_id["decisions"] = [decision_fixture(), decision_fixture(decision_id="decision-1", rule_id="missing_closure")]
        invalid_registries.append(("duplicate id", duplicate_id, "decision_id"))

        duplicate_selector = policy_fixture()
        duplicate_selector["decisions"] = [decision_fixture(), decision_fixture(decision_id="decision-2")]
        invalid_registries.append(("duplicate selector", duplicate_selector, "applies_to"))

        invalid_rules = policy_fixture()
        invalid_rules["rules"] = invalid_rules["rules"][:-1]
        invalid_registries.append(("missing rule", invalid_rules, "rules"))

        duplicate_rule = policy_fixture()
        duplicate_rule["rules"][-1] = dict(duplicate_rule["rules"][0])
        invalid_registries.append(("duplicate rule", duplicate_rule, "rules"))

        invalid_rule_label = policy_fixture()
        invalid_rule_label["rules"][0]["model_label"] = "Wrong label"
        invalid_registries.append(("invalid rule label", invalid_rule_label, "model_label"))

        invalid_schema_version = policy_fixture()
        invalid_schema_version["schema_version"] = True
        invalid_registries.append(("boolean schema version", invalid_schema_version, "schema_version"))

        extra_registry_field = policy_fixture()
        extra_registry_field["extra"] = True
        invalid_registries.append(("extra registry field", extra_registry_field, "registry"))

        for name, registry, expected_field in invalid_registries:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, expected_field):
                self.load_registry(registry)

    def test_load_policy_rejects_malformed_json_without_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad-policy.json"
            path.write_text('{"schema_version":', encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                load_policy(path)

        with self.assertRaises(OSError):
            load_policy(Path("missing-policy.json"))


class PolicyResolverTests(unittest.TestCase):
    def test_policy_does_not_trust_model_confirmed(self):
        assessments, commitments, sources, scope = resolver_fixture(severity="Warning", status="confirmed")
        originals = copy.deepcopy((assessments, commitments, sources, scope))
        registry = policy_fixture()
        original_registry = copy.deepcopy(registry)
        result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)

        self.assertIsInstance(result, PolicyResult)
        self.assertEqual(result.assessments[0]["findings"][0]["severity"], "Warning")
        self.assertEqual(result.assessments[0]["findings"][0]["severity_status"], "proposed")
        self.assertTrue(result.assessments[0]["requires_review"])
        self.assertEqual(result.resolutions[0]["decision_id"], None)
        self.assertEqual(result.resolutions[0]["model_severity_status"], "confirmed")
        self.assertEqual(result.resolutions[0]["effective_severity_status"], "proposed")
        self.assertEqual(assessments, originals[0])
        self.assertEqual(commitments, originals[1])
        self.assertEqual(sources, originals[2])
        self.assertEqual(scope, originals[3])
        self.assertEqual(registry, original_registry)
        self.assertEqual(len(result.assessments), len(assessments))
        self.assertEqual(len(result.resolutions), sum(len(item["findings"]) for item in assessments))
        validate_assessment({"assessments": result.assessments}, commitments, sources)

    def test_exact_approved_case_wins_over_rule_proposal(self):
        assessments, commitments, sources, scope = resolver_fixture(severity="Suggestion", status="proposed")
        original_assessments = copy.deepcopy(assessments)
        registry = policy_fixture()
        case_decision = decision_fixture(applies_to=case_scope())
        registry["decisions"] = [rule_proposal_fixture(), case_decision]
        original_registry = copy.deepcopy(registry)

        result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)

        finding = result.assessments[0]["findings"][0]
        audit = result.resolutions[0]
        self.assertEqual((finding["severity"], finding["severity_status"]), ("Warning", "confirmed"))
        self.assertFalse(result.assessments[0]["requires_review"])
        self.assertEqual((audit["model_severity"], audit["model_severity_status"]), ("Suggestion", "proposed"))
        self.assertEqual(audit["decision_id"], "decision-1")
        self.assertEqual(audit["source_reference"], "meeting notes, section 4")
        self.assertEqual(audit["approved_by"], "Example approver")
        self.assertEqual(audit["approved_at"], "2026-10-13T10:30:00+07:00")

        expected_audit_fields = {
            "plan_id", "finding_index", "rule_id", "rule_version", "policy_version", "decision_id",
            "source_reference", "approved_by", "approved_at", "model_severity", "model_severity_status",
            "effective_severity", "effective_severity_status",
        }
        self.assertEqual(set(audit), expected_audit_fields)
        self.assertEqual(len(result.resolutions), 1)
        self.assertEqual(result.resolutions[0]["plan_id"], "P001")
        expected_assessments = copy.deepcopy(original_assessments)
        expected_assessments[0]["findings"][0]["severity"] = "Warning"
        expected_assessments[0]["findings"][0]["severity_status"] = "confirmed"
        expected_assessments[0]["requires_review"] = False
        self.assertEqual(result.assessments, expected_assessments)
        self.assertEqual(assessments, original_assessments)
        self.assertEqual(registry, original_registry)
        validate_assessment({"assessments": result.assessments}, commitments, sources)

    def test_case_quote_from_another_commitment_in_same_source_does_not_leak(self):
        assessments, commitments, sources, scope = resolver_fixture(("Deliver API", "Deliver UI"))
        registry = policy_fixture()
        registry["decisions"] = [
            rule_proposal_fixture(),
            decision_fixture(applies_to=case_scope(plan_quote="Deliver UI")),
        ]

        result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)

        self.assertEqual([record["plan_id"] for record in result.resolutions], ["P001", "P002"])
        self.assertEqual([record["decision_id"] for record in result.resolutions], ["rule-proposal", "decision-1"])
        self.assertEqual(
            [(item["findings"][0]["severity"], item["findings"][0]["severity_status"]) for item in result.assessments],
            [("Suggestion", "proposed"), ("Warning", "confirmed")],
        )
        validate_assessment({"assessments": result.assessments}, commitments, sources)

    def test_exact_case_requires_every_scope_field_and_exact_quote(self):
        assessments, commitments, sources, scope = resolver_fixture()
        wrong_scopes = [
            case_scope(project="OTHER"),
            case_scope(role="QC"),
            case_scope(previous_sheet="other previous"),
            case_scope(current_sheet="other current"),
            case_scope(plan_quote="Deliver UI"),
        ]

        for selector in wrong_scopes:
            with self.subTest(selector=selector):
                registry = policy_fixture()
                registry["decisions"] = [rule_proposal_fixture(), decision_fixture(applies_to=selector)]
                result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)
                finding = result.assessments[0]["findings"][0]
                self.assertEqual((finding["severity"], finding["severity_status"]), ("Suggestion", "proposed"))
                self.assertEqual(result.resolutions[0]["decision_id"], "rule-proposal")

    def test_case_quote_shared_by_two_commitments_is_rejected(self):
        assessments, commitments, sources, scope = resolver_fixture(("Deliver API", "Deliver API"))
        originals = copy.deepcopy((assessments, commitments, sources, scope))
        registry = policy_fixture()
        registry["decisions"] = [decision_fixture(applies_to=case_scope())]

        with self.assertRaisesRegex(ValueError, "(?i)ambiguous.*commitment"):
            resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)
        self.assertEqual((assessments, commitments, sources, scope), originals)

        registry["decisions"] = [rule_proposal_fixture()]
        result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)
        self.assertEqual(len(result.resolutions), 2)
        self.assertTrue(all(record["decision_id"] == "rule-proposal" for record in result.resolutions))

    def test_repeated_quote_citations_within_one_commitment_are_not_ambiguous(self):
        assessments, commitments, sources, scope = resolver_fixture()
        commitments[0]["plan_evidence"].append(copy.deepcopy(commitments[0]["plan_evidence"][0]))
        registry = policy_fixture()
        registry["decisions"] = [decision_fixture(applies_to=case_scope())]

        result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)

        self.assertEqual(result.resolutions[0]["decision_id"], "decision-1")
        self.assertEqual(result.assessments[0]["findings"][0]["severity_status"], "confirmed")

    def test_policy_preserves_unknown_and_explicit_review_reasons(self):
        assessments, commitments, sources, scope = resolver_fixture(severity=None, status="unresolved")
        unknown = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=policy_fixture())
        self.assertEqual(unknown.assessments[0]["findings"][0]["severity"], None)
        self.assertEqual(unknown.assessments[0]["findings"][0]["severity_status"], "unresolved")
        self.assertTrue(unknown.assessments[0]["requires_review"])

        for review_field in ("review_reasons", "policy_questions"):
            with self.subTest(review_field=review_field):
                kwargs = {
                    "reasons" if review_field == "review_reasons" else "questions": ("Keep this review reason",)
                }
                assessments, commitments, sources, scope = resolver_fixture(**kwargs)
                registry = policy_fixture()
                registry["decisions"] = [decision_fixture(applies_to=case_scope())]
                result = resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)
                self.assertEqual(result.assessments[0]["findings"][0]["severity_status"], "confirmed")
                self.assertTrue(result.assessments[0]["requires_review"])
                self.assertEqual(result.assessments[0][review_field], ["Keep this review reason"])

    def test_policy_cannot_salvage_invalid_model_assessment(self):
        for invalid in ("review flag", "fabricated quote"):
            with self.subTest(invalid=invalid):
                assessments, commitments, sources, scope = resolver_fixture()
                if invalid == "review flag":
                    assessments[0]["requires_review"] = False
                else:
                    assessments[0]["findings"][0]["evidence"][0]["quote"] = "Invented evidence"
                original = copy.deepcopy(assessments)
                registry = policy_fixture()
                registry["decisions"] = [decision_fixture(applies_to=case_scope())]

                with self.assertRaises(ValueError):
                    resolve_policy(assessments, commitments, sources, scope, role="PTSP", registry=registry)
                self.assertEqual(assessments, original)


if __name__ == "__main__":
    unittest.main()
