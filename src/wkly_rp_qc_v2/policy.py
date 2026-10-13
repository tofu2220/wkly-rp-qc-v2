import copy
import json
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from .assessment import RULES, SEVERITIES, validate_assessment

JsonDict = dict[str, Any]

RULE_IDS = {
    "identity_uncertain": "Identity Uncertain",
    "status_ambiguous": "Status Ambiguous",
    "milestone_conflict": "Milestone Conflict",
    "closure_incomplete": "Closure Incomplete",
    "missing_closure": "Missing Closure",
}
_RULE_ID_BY_LABEL = {label: rule_id for rule_id, label in RULE_IDS.items()}
_REGISTRY_FIELDS = {"schema_version", "version", "rules", "decisions"}
_RULE_FIELDS = {"rule_id", "model_label", "version"}
_DECISION_FIELDS = {
    "decision_id", "rule_id", "severity", "severity_status", "applies_to", "basis",
    "source_reference", "approved_by", "approved_at",
}
_SCOPE_FIELDS = {"project", "role", "previous_sheet", "current_sheet", "plan_quote"}


def _record(value: object, fields: set[str], where: str) -> JsonDict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"Invalid {where} fields")
    return value


def _nonempty_string(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {field}")


def _valid_approval_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        try:
            datetime.fromisoformat(value)
            return True
        except ValueError:
            return False


def _validate_rule(rule: object) -> str:
    item = _record(rule, _RULE_FIELDS, "rule")
    rule_id = item["rule_id"]
    if not isinstance(rule_id, str) or rule_id not in RULE_IDS:
        raise ValueError("Invalid rule_id")
    if item["model_label"] != RULE_IDS[rule_id]:
        raise ValueError("Invalid rule_id/model_label")
    if item["model_label"] not in RULES:
        raise ValueError("Invalid model_label")
    _nonempty_string(item["version"], "rule version")
    return rule_id


def _validate_decision(decision: object) -> tuple[str, tuple[object, ...]]:
    item = _record(decision, _DECISION_FIELDS, "decision")
    _nonempty_string(item["decision_id"], "decision_id")
    if not isinstance(item["rule_id"], str) or item["rule_id"] not in RULE_IDS:
        raise ValueError("Invalid decision rule_id")

    severity = item["severity"]
    status = item["severity_status"]
    if severity is not None and severity not in SEVERITIES:
        raise ValueError("Invalid severity")
    if not isinstance(status, str) or status not in {"confirmed", "proposed", "unresolved"}:
        raise ValueError("Invalid severity_status")
    if (severity is None) != (status == "unresolved"):
        raise ValueError("Invalid severity/severity_status combination")

    applies_to = _record(item["applies_to"], _SCOPE_FIELDS, "applies_to")
    scope_values = tuple(applies_to[field] for field in ("project", "role", "previous_sheet", "current_sheet", "plan_quote"))
    if any(value is None for value in scope_values):
        if not all(value is None for value in scope_values):
            raise ValueError("Invalid applies_to: scope must be all null or all strings")
    else:
        for field, value in zip(("project", "role", "previous_sheet", "current_sheet", "plan_quote"), scope_values):
            _nonempty_string(value, f"applies_to.{field}")

    basis = item["basis"]
    expected_basis = {"confirmed": "explicit_approval", "proposed": "proposal", "unresolved": "unresolved_policy"}[status]
    if basis != expected_basis:
        raise ValueError("Invalid basis for severity_status")
    _nonempty_string(item["source_reference"], "source_reference")

    if status == "confirmed":
        _nonempty_string(item["approved_by"], "approved_by")
        if not _valid_approval_date(item["approved_at"]):
            raise ValueError("Invalid approved_at: expected ISO date or datetime")
    elif item["approved_by"] is not None or item["approved_at"] is not None:
        raise ValueError("Invalid approved_by/approved_at: must be null without explicit approval")

    return item["decision_id"], (item["rule_id"], *scope_values)


def _validate_registry(registry: object) -> JsonDict:
    result = _record(registry, _REGISTRY_FIELDS, "registry")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("Invalid schema_version")
    _nonempty_string(result["version"], "version")

    rules = result["rules"]
    if not isinstance(rules, list):
        raise ValueError("Invalid rules: expected a list")  # noqa: TRY004 — invalid registries use the public ValueError contract
    rule_ids = [_validate_rule(rule) for rule in rules]
    if len(rule_ids) != len(RULE_IDS) or set(rule_ids) != set(RULE_IDS):
        raise ValueError("Invalid rules: each known rule must appear exactly once")

    decisions = result["decisions"]
    if not isinstance(decisions, list):
        raise ValueError("Invalid decisions: expected a list")  # noqa: TRY004 — invalid registries use the public ValueError contract
    decision_ids: set[str] = set()
    selectors: set[tuple[object, ...]] = set()
    for decision in decisions:
        decision_id, selector = _validate_decision(decision)
        if decision_id in decision_ids:
            raise ValueError("Duplicate decision_id")
        if selector in selectors:
            raise ValueError("Duplicate decision applies_to selector")
        decision_ids.add(decision_id)
        selectors.add(selector)
    return result


def load_policy(path: Path | None = None) -> JsonDict:
    if path is None:
        text = files("wkly_rp_qc_v2").joinpath("default_policy.json").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return _validate_registry(json.loads(text))


@dataclass(frozen=True)
class PolicyResult:
    assessments: list[JsonDict]
    resolutions: list[JsonDict]


def resolve_policy(
    assessments: list[JsonDict],
    commitments: list[JsonDict],
    sources: JsonDict,
    scope: JsonDict,
    *,
    role: str,
    registry: JsonDict,
) -> PolicyResult:
    validate_assessment({"assessments": assessments}, commitments, sources)
    registry = _validate_registry(registry)

    rules = {rule["rule_id"]: rule for rule in registry["rules"]}
    decisions_by_rule: dict[str, list[JsonDict]] = {rule_id: [] for rule_id in RULE_IDS}
    for decision in registry["decisions"]:
        decisions_by_rule[decision["rule_id"]].append(decision)

    def selector_matches_scope(applies_to: JsonDict) -> bool:
        return (
            applies_to["project"] == scope.get("project")
            and applies_to["role"] == role
            and applies_to["previous_sheet"] == scope.get("previous_sheet")
            and applies_to["current_sheet"] == scope.get("current_sheet")
        )

    case_commitments: dict[str, JsonDict] = {}
    for decisions in decisions_by_rule.values():
        for decision in decisions:
            applies_to = decision["applies_to"]
            if applies_to["project"] is None or not selector_matches_scope(applies_to):
                continue
            quote = applies_to["plan_quote"]
            matching = [
                commitment for commitment in commitments
                if any(evidence["quote"] == quote for evidence in commitment["plan_evidence"])
            ]
            if len(matching) > 1:
                raise ValueError("Ambiguous case decision selector matches multiple commitments")
            if matching:
                case_commitments[decision["decision_id"]] = matching[0]

    effective = copy.deepcopy(assessments)
    resolutions: list[JsonDict] = []
    commitments_by_id = {commitment["plan_id"]: commitment for commitment in commitments}
    for assessment in effective:
        commitment = commitments_by_id[assessment["plan_id"]]
        for finding_index, finding in enumerate(assessment["findings"]):
            rule_id = _RULE_ID_BY_LABEL[finding["rule"]]
            rule = rules[rule_id]
            applicable = decisions_by_rule[rule_id]
            case_decision = next((
                decision for decision in applicable
                if decision["applies_to"]["project"] is not None
                and case_commitments.get(decision["decision_id"]) is commitment
            ), None)
            rule_decision = next((decision for decision in applicable if decision["applies_to"]["project"] is None), None)
            decision = case_decision or rule_decision
            model_severity = finding["severity"]
            model_status = finding["severity_status"]
            if decision is None:
                effective_severity = model_severity
                effective_status = "proposed" if model_severity is not None else "unresolved"
            else:
                effective_severity = decision["severity"]
                effective_status = decision["severity_status"]
            finding["severity"] = effective_severity
            finding["severity_status"] = effective_status
            resolutions.append({
                "plan_id": assessment["plan_id"],
                "finding_index": finding_index,
                "rule_id": rule_id,
                "rule_version": rule["version"],
                "policy_version": registry["version"],
                "decision_id": decision["decision_id"] if decision else None,
                "source_reference": decision["source_reference"] if decision else None,
                "approved_by": decision["approved_by"] if decision else None,
                "approved_at": decision["approved_at"] if decision else None,
                "model_severity": model_severity,
                "model_severity_status": model_status,
                "effective_severity": effective_severity,
                "effective_severity_status": effective_status,
            })
        assessment["requires_review"] = bool(
            assessment["review_reasons"]
            or assessment["policy_questions"]
            or any(finding["severity_status"] != "confirmed" for finding in assessment["findings"])
        )

    validate_assessment({"assessments": effective}, commitments, sources)
    return PolicyResult(effective, resolutions)
