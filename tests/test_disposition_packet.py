"""Offline integrity checks for the phase-1 disposition packet."""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
PACKET_PATH = ROOT / "docs/phase-4-evaluation/review-dispositions-v1.md"
MANIFEST_PATH = ROOT / "docs/phase-4-evaluation/candidate-cases.json"

EXPECTED_HASHES = {
    "docs/Khối SMC_Báo cáo tuần chi tiết.xlsx": "d02ac1843c6b6d33c43f7faf69c37225ecdf7149e9a4a48dc3d2f14b94e9a5c7",
    "download/1-raw/normalized_rows.json": "78ce6775f3b9783d36b4d5bae348febf8c1a737f7228df2e34a2ef4df5937880",
    "download/3/third_comparison.json": "9b6b834d886afe35f874048bf36434281c683a822475cc7d50b700a6f9b8942a",
    "download/4-depot-gsm/comparison.json": "b5f81c51f50fccaed5f8090109833c9f95e158f336de0a731aaa32174b50801c",
    "download/6-missing-follow-up/comparison.json": "e0aec617f86a629849501a09995331598ff84426000d3544ce62e0a4d32f4b23",
    "docs/phase-4-evaluation/candidate-cases.json": "f7b7190e3b7c214ee2e79d297874e29f4eca01c9fec85ded1479f0e538b70f4b",
    "docs/phase-4-evaluation/business-review-form.md": "c165d6880de553f012218201952b94a702c4082cffd003cc2daea61e010ba55b",
    "docs/phase-4-evaluation/gpt-6.1-draft-review.md": "18ac0ec9c5784783f09a279bb63759e568fe12b8744a4c3ac5d1025cda8241fa",
}

P007_ID = "smart-parking-P007-candidate-finding"
CASE_SCOPE = {
    "smart-parking-P007-candidate-finding": ("SMART PARKING", "P007"),
    "smart-parking-P003-candidate-no-finding": ("SMART PARKING", "P003"),
    "depot-gsm-P002-candidate-finding": ("DEPOT GSM", "P002"),
    "depot-gsm-P001-candidate-no-finding": ("DEPOT GSM", "P001"),
    "anat-cong-truong-P002-candidate-finding": ("ANAT Công trường", "P002"),
    "anat-cong-truong-P003-candidate-no-finding": ("ANAT Công trường", "P003"),
}
REQUIRED_CELL_KEYS = {
    "smart-parking-P007-candidate-finding": {
        ("BC Tuần 31.08 - 05.09", cell) for cell in ("F64", "E64", "G64", "C61", "C64", "D64", "H64")
    } | {("BC Tuần 07.09 - 12.09", cell) for cell in ("F52", "G52", "H52", "D49", "D52", "I52")},
}
REQUIRED_CELL_KEYS["smart-parking-P003-candidate-no-finding"] = REQUIRED_CELL_KEYS[P007_ID]
for case_id in ("depot-gsm-P002-candidate-finding", "depot-gsm-P001-candidate-no-finding"):
    REQUIRED_CELL_KEYS[case_id] = {
        ("BC Tuần 31.08 - 05.09", cell) for cell in ("F20", "E20", "G20", "C17", "C20", "D20", "H20")
    } | {("BC Tuần 07.09 - 12.09", cell) for cell in ("F20", "G20", "H20", "D17", "D20", "I20")}
for case_id in ("anat-cong-truong-P002-candidate-finding", "anat-cong-truong-P003-candidate-no-finding"):
    REQUIRED_CELL_KEYS[case_id] = {
        ("BC Tuần 20.07 - 24.07", cell) for cell in ("D24", "C24", "E24", "B22", "B24", "F24")
    } | {
        ("BC Tuần 27.07 - 01.08 ", f"{column}{row}")
        for row in range(23, 27)
        for column in ("B", "C", "D", "E", "F")
    }
LABEL_MARKERS = (
    "candidate-finding",
    "candidate-no-finding",
    "Milestone Conflict",
    "Closure Incomplete",
    "Missing Closure",
    "no_finding",
)
NEUTRAL_SOURCE_EVENT_DIGESTS = {
    ("smart-parking-P007-candidate-finding", "source_offered"): "d5558edf214bf5c79ebe57a82750edc27a05bc93a44226ef2c9f11dd03ff41c2",
    ("smart-parking-P007-candidate-finding", "source_clarified"): "20aaacb2045b1f1a330021061605356be826429f12190c0e925475c7ca7844fc",
    ("smart-parking-P003-candidate-no-finding", "source_offered"): "d14fb8549d9d21319a4c4c6661cd9b1ade75f3609304a45eb5b3ea31ace9b846",
    ("depot-gsm-P002-candidate-finding", "source_offered"): "113663635be4606869bcebe955caf865a8f25a663a3a7b4cfeaef65e74a3143d",
    ("depot-gsm-P001-candidate-no-finding", "source_offered"): "7339dbff679399deb11b69ee7acc875a1479808e8aca0c8fd51beec16fa31db9",
    ("depot-gsm-P001-candidate-no-finding", "source_clarified"): "fb84f9a88e954efa091129671cadd546112c566e0efe225db057ee011787a1d5",
    ("anat-cong-truong-P002-candidate-finding", "source_offered"): "41dbb1e414cf338a0c78718c30c83facbcc0c4eb06ab5585ddcc4e96b0c046ea",
    ("anat-cong-truong-P003-candidate-no-finding", "source_offered"): "1b52a20d2b0729580631c6e91ea4b31f48e36b1a1ab9d24873f95473bad11941",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_packet(path: Path = PACKET_PATH) -> tuple[str, dict[str, Any]]:
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError(f"{path}: exceeds maximum packet size of 5242880 bytes")
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"```disposition-ledger\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one disposition-ledger block, found {len(matches)}")
    try:
        ledger = json.loads(matches[0])
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: invalid ledger JSON: {error}") from error
    if not isinstance(ledger, dict):
        raise ValueError(f"{path}: disposition ledger must be a JSON object")
    return text, ledger


def canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def disposition_payload(ledger: dict[str, Any]) -> dict[str, Any]:
    """Return mutable review state; immutable source inventory stays single-copy."""
    cases = []
    for case in ledger.get("cases", []):
        cases.append(
            {
                key: case.get(key)
                for key in (
                    "case_id",
                    "preliminary_response",
                    "mentor_decision",
                    "finding_review_state",
                    "severity_approval",
                    "ground_truth_status",
                    "pending_record_links",
                )
            }
        )
    return {
        "schema_version": ledger.get("schema_version"),
        "packet_version": ledger.get("packet_version"),
        "stage": ledger.get("stage"),
        "cases": cases,
        "pending_records": ledger.get("pending_records"),
        "events": ledger.get("events"),
    }


def is_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def has_missing_information(value: Any) -> bool:
    if not isinstance(value, list):
        raise ValueError("missing_information must be a list of non-empty strings")
    if any(not is_nonempty_text(item) for item in value):
        raise ValueError("missing_information must contain only non-empty strings")
    return bool(value)


def has_reviewer_policy_follow_up(response: dict[str, Any]) -> bool:
    provenance_fields = ("reviewer", "reviewer_role", "applicable_business_version")
    missing_provenance = any(not is_nonempty_text(response.get(field)) for field in provenance_fields)
    if not missing_provenance:
        return True
    named_gap = any(
        isinstance(item, str)
        and "reviewer identity/role" in item.lower()
        and "policy version" in item.lower()
        for item in response.get("missing_information", [])
    )
    return named_gap and is_nonempty_text(response.get("follow_up_question"))


def validate_pending_records(ledger: dict[str, Any]) -> None:
    records = ledger.get("pending_records")
    if not isinstance(records, list):
        raise ValueError("pending_records must be a list")
    by_id = {record.get("record_id"): record for record in records}
    required = {"pending-unfinished", "pending-semantic-severity", "pending-depot-alias"}
    if len(records) != 3 or set(by_id) != required:
        raise ValueError(f"pending record set must be exactly {sorted(required)}")
    for record_id, record in by_id.items():
        if record.get("status") != "unresolved":
            raise ValueError(f"{record_id}: unresolved business authority cannot be promoted")
        if not has_missing_information(record.get("missing_information")) or not is_nonempty_text(record.get("follow_up_question")):
            raise ValueError(f"{record_id}: missing information and a string follow_up_question are required")
        if any(record.get(field) is not None for field in ("respondent", "owner", "deadline", "approved_by", "approved_at")):
            raise ValueError(f"{record_id}: unspecified respondent/owner/deadline/approval must remain null")
        if not isinstance(record.get("case_links"), list) or not record["case_links"]:
            raise ValueError(f"{record_id}: at least one case link is required")
    unfinished = by_id["pending-unfinished"]
    alternatives = unfinished.get("alternatives")
    expected_sources = ["REQ-unfinished-state-mentor", "REQ-unfinished-state-research"]
    if not isinstance(alternatives, list) or [item.get("source_id") for item in alternatives] != expected_sources:
        raise ValueError("pending-unfinished must preserve mentor and research alternatives separately")
    if alternatives[0].get("criterion") == alternatives[1].get("criterion"):
        raise ValueError("pending-unfinished alternatives were merged or made identical")
    baseline = unfinished.get("documented_baseline", {})
    recommendations = unfinished.get("eta_recommendations", {})
    if baseline.get("status") != "documented_requirement" or not baseline.get("source_references"):
        raise ValueError("unfinished-task baseline must cite its documented mentor/workbook requirement")
    if recommendations.get("status") != "proposed" or recommendations.get("approved") is not False:
        raise ValueError("state-specific ETA/checkpoint guidance must remain a proposal")
    semantic = by_id["pending-semantic-severity"]
    if semantic.get("business_decision") is not None or semantic.get("severity_approval") is not None:
        raise ValueError("pending semantic/severity authority cannot contain an invented approval")
    if semantic.get("critical_executable") is not False:
        raise ValueError("unapproved Critical proposals must remain non-executable")
    if not semantic.get("reviewer_comment_evidence") or semantic.get("severity_policy_status") != "proposed_policy":
        raise ValueError("reviewer findings evidence must remain separate from unapproved severity policy")
    alias = by_id["pending-depot-alias"]
    claim = alias.get("reported_claim")
    if not isinstance(claim, dict) or claim.get("is_original_authority_evidence") is not False:
        raise ValueError("reported alias text cannot be treated as original approval evidence")
    if alias.get("decision") is not None:
        raise ValueError("DEPOT alias decision must remain unset without original scoped authority")
    proposal = alias.get("user_proposal", {})
    if proposal.get("status") != "provisional_alias" or proposal.get("approval_status") != "approval_pending":
        raise ValueError("DEPOT alias proposal must remain provisional and approval-pending")
    valid_case_ids = {case.get("case_id") for case in ledger.get("cases", [])}
    for record_id, record in by_id.items():
        if not set(record["case_links"]) <= valid_case_ids:
            raise ValueError(f"{record_id}: pending record links to an unknown frozen case")
    for case in ledger.get("cases", []):
        links = set(case.get("pending_record_links", []))
        if not links <= required or "pending-semantic-severity" not in links:
            raise ValueError(f"{case.get('case_id')}: pending record links are missing or unknown")
        if case.get("case_id", "").startswith("depot-"):
            if "pending-depot-alias" not in links:
                raise ValueError(f"{case.get('case_id')}: conditional DEPOT alias link is missing")
            if case.get("conditional_cross_period") is not True or case.get("replay_status") != "not_established":
                raise ValueError(f"{case.get('case_id')}: DEPOT cross-period/replay state was promoted")


def validate_markdown_consistency(text: str, ledger: dict[str, Any]) -> None:
    for record_id in ("pending-unfinished", "pending-semantic-severity", "pending-depot-alias"):
        if f"### `{record_id}`" not in text:
            raise ValueError(f"Markdown packet is missing shared pending record {record_id}")
    for case in ledger.get("cases", []):
        if case.get("neutral_title") not in text:
            raise ValueError(f"Markdown index is missing neutral case title {case.get('neutral_title')}")
    recorded = [
        case for case in ledger.get("cases", [])
        if case.get("preliminary_response", {}).get("status") == "recorded"
    ]
    if recorded:
        if "Đã có phản hồi sơ bộ của người dùng" not in text:
            raise ValueError("Markdown must disclose that preliminary human responses are recorded")
        stale_claims = (
            "Judgment sơ bộ người dùng: **chưa nhận được**",
            "Human transcript, judgment and mentor/severity/alias approvals: none yet.",
        )
        if any(claim in text for claim in stale_claims):
            raise ValueError("Markdown contains a pre-review status claim that contradicts the recorded review stage")
        numeric_test_claims = (
            r"\b\d+\s+tests?\b",
            r"\btests?\s*[:=]\s*\d+\b",
            r"\btests?\s*\(\s*\d+\s+total\s*\)",
            r"\b\d+\s*/\s*\d+\s+passed\b",
            r"\b\d+\s+passed\b",
            r"\btest\s+count\s*[:=]\s*\d+\b",
        )
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in numeric_test_claims):
            raise ValueError("Markdown must not retain a volatile final test count")
    elif "Chưa có phản hồi" not in text:
        raise ValueError("Markdown must disclose that no human response has been recorded")
    records = {record["record_id"]: record for record in ledger.get("pending_records", [])}
    for alternative in records.get("pending-unfinished", {}).get("alternatives", []):
        if alternative.get("criterion") not in text:
            raise ValueError(f"Markdown is missing attributed unfinished criterion {alternative.get('source_id')}")
    alias_claim = records.get("pending-depot-alias", {}).get("reported_claim", {}).get("text")
    if alias_claim and alias_claim not in text:
        raise ValueError("Markdown must preserve the alias claim as a reported claim")


def validate_review_events(ledger: dict[str, Any]) -> None:
    events = ledger.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    stage = ledger.get("stage")
    if stage in {"tracer", "six_case"}:
        if events:
            raise ValueError("chat/review events cannot be fabricated before the review checkpoint")
        return
    partial = stage == "pending_ready"
    expected_order = [case["case_id"] for case in ledger.get("cases", [])]
    by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in expected_order}
    ordinals = []
    confirmations = []
    expected_actors = {
        "source_offered": "assistant",
        "source_clarified": "assistant",
        "preliminary_response": "user",
        "preliminary_response_confirmation": "user",
        "deferred_by_user": "user",
        "post_response_disclosure": "assistant",
        "record_confirmation": "user",
    }
    for event in events:
        kind = event.get("kind")
        if kind not in expected_actors:
            raise ValueError(f"unsupported review event kind {kind!r}")
        if event.get("actor") != expected_actors[kind]:
            raise ValueError(f"{kind} must be {expected_actors[kind]}-authored")
        if kind in {"source_offered", "source_clarified", "post_response_disclosure"}:
            payload = event.get("payload")
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError(f"{kind} requires a non-empty payload")
            if kind in {"source_offered", "source_clarified"}:
                disclosed = payload.lower()
                expected_payload_digest = NEUTRAL_SOURCE_EVENT_DIGESTS.get((event.get("case_id"), kind))
                actual_payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if expected_payload_digest is None or actual_payload_digest != expected_payload_digest:
                    raise ValueError("source-first event payload differs from its frozen neutral message")
                if any(marker.lower() in disclosed for marker in LABEL_MARKERS):
                    raise ValueError("source-first event payload reveals a candidate label")
                verdict_markers = (
                    "this case is a finding",
                    "this is a finding",
                    "there is a finding",
                    "no finding",
                    "no-finding",
                    "no issue exists",
                    "no issue found",
                    "no issues found",
                    "no issues exist",
                    "no defects found",
                    "this is not a finding",
                    "no error found",
                    "no errors found",
                    "có lỗi",
                    "không lỗi",
                    "chưa rõ",
                    "không phát hiện lỗi",
                    "không thấy lỗi",
                    "không có lỗi",
                    "không ghi nhận lỗi",
                    "không phát hiện sai sót",
                    "không có sai sót",
                    "không ghi nhận sai sót",
                    "có sai sót",
                    "classification: finding",
                    "classification: no_finding",
                    "judgment: finding",
                    "judgment: no_finding",
                    "finding identified",
                    "finding confirmed",
                    "lỗi chất lượng báo cáo",
                )
                if any(marker in disclosed for marker in verdict_markers):
                    raise ValueError("source-first event payload reveals a finding verdict")
        if event.get("kind") == "record_confirmation":
            literal = event.get("literal_response")
            payload = event.get("payload")
            if event.get("case_id") is not None:
                raise ValueError("record_confirmation cannot link to a single case")
            if not isinstance(literal, str) or not literal.strip() or not isinstance(payload, str) or not payload.strip():
                raise ValueError("record_confirmation requires a non-empty string literal_response and payload")
            affirmative = {"đúng rồi", "đúng", "yes", "y", "confirmed", "confirm"}
            if (
                literal.strip().casefold() not in affirmative
                or event.get("decision") != "confirmed"
                or event.get("confirmation_scope") != "six_case_review_records"
            ):
                raise ValueError("record_confirmation requires an affirmative confirmation response")
            target_digest = event.get("confirmed_revision_digest")
            target = next((revision for revision in ledger.get("revisions", []) if revision.get("digest") == target_digest), None)
            if not isinstance(target_digest, str) or target is None:
                raise ValueError("record_confirmation does not reference the confirmed revision")
            target_payload = target.get("payload", {})
            target_cases = target_payload.get("cases", [])
            target_ids = [case.get("case_id") for case in target_cases]
            if target_ids != expected_order or len(set(target_ids)) != 6:
                raise ValueError("record_confirmation target must contain the six distinct frozen case IDs in order")
            if target_payload.get("stage") != "pending_ready" or any(case.get("preliminary_response", {}).get("status") != "recorded" for case in target_cases):
                raise ValueError("record_confirmation target must contain the six recorded case reviews")
            confirmation_children = [
                revision for revision in ledger.get("revisions", [])
                if revision.get("parent_digest") == target_digest
                and isinstance(revision.get("payload"), dict)
            ]
            if len(confirmation_children) != 1:
                raise ValueError("record_confirmation must bind to the direct next revision snapshot")
            child_events = confirmation_children[0]["payload"].get("events", [])
            child_confirmations = [item for item in child_events if item.get("kind") == "record_confirmation"]
            if len(child_confirmations) != 1 or child_confirmations[0] != event:
                raise ValueError("record_confirmation does not match its direct confirmation-bearing snapshot")
            confirmations.append(event)
            if type(event.get("ordinal")) is not int:
                raise ValueError("record confirmation requires an integer chronological ordinal")
            ordinals.append(event["ordinal"])
            continue
        case_id = event.get("case_id")
        if case_id not in by_case:
            raise ValueError(f"review event links to unknown case {case_id!r}")
        if type(event.get("ordinal")) is not int:
            raise ValueError("review events require an integer chronological ordinal")
        ordinals.append(event["ordinal"])
        by_case[case_id].append(event)
    if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
        raise ValueError("review event ordinals must be strictly increasing")
    if stage == "review_recorded":
        if len(confirmations) != 1:
            raise ValueError("review_recorded stage requires one actual record_confirmation")
        case_ordinals = [event["ordinal"] for event in events if event.get("kind") != "record_confirmation"]
        if case_ordinals and confirmations[0]["ordinal"] <= max(case_ordinals):
            raise ValueError("record_confirmation must follow all six case reviews")
    elif confirmations:
        raise ValueError("record_confirmation cannot appear before the review_recorded checkpoint")
    for case_id in expected_order:
        entries = by_case[case_id]
        kinds = [event.get("kind") for event in entries]
        if partial and not entries:
            continue
        if not entries:
            raise ValueError(f"{case_id}: final review stage has no actual interaction event")
        if kinds.count("source_offered") != 1:
            raise ValueError(f"{case_id}: requires exactly one source_offered event")
        completion_kind = "preliminary_response" if kinds.count("preliminary_response") == 1 else "deferred_by_user"
        if kinds.count(completion_kind) != 1 or ("preliminary_response" in kinds and "deferred_by_user" in kinds):
            raise ValueError(f"{case_id}: requires one actual response or explicit user deferral")
        offered = kinds.index("source_offered")
        completed = kinds.index(completion_kind)
        case = next(item for item in ledger.get("cases", []) if item.get("case_id") == case_id)
        case_response = case.get("preliminary_response", {})
        stored_response = case_response.get("literal_response")
        if completion_kind == "preliminary_response":
            response_event = entries[completed]
            literal = response_event.get("literal_response")
            if response_event.get("actor") != "user" or not isinstance(literal, str) or not literal.strip():
                raise ValueError(f"{case_id}: preliminary_response must be user-authored with literal_response")
            if literal != stored_response:
                raise ValueError(f"{case_id}: event literal_response must match the case disposition")
            if response_event.get("judgment") != case_response.get("judgment") or response_event.get("severity_opinion") != case_response.get("severity_opinion"):
                raise ValueError(f"{case_id}: response-event judgment/severity must match the case disposition")
        else:
            deferral_event = entries[completed]
            literal = deferral_event.get("literal_response")
            if deferral_event.get("actor") != "user" or not isinstance(literal, str) or not literal.strip():
                raise ValueError(f"{case_id}: deferred_by_user must be user-authored with literal_response")
            if literal != stored_response:
                raise ValueError(f"{case_id}: deferral literal_response must match the unresolved case record")
        if "preliminary_response_confirmation" in kinds:
            confirmation = entries[kinds.index("preliminary_response_confirmation")]
            literal_confirmation = confirmation.get("literal_response")
            if not is_nonempty_text(literal_confirmation):
                raise ValueError(f"{case_id}: preliminary_response_confirmation requires a non-empty string")
            judgment = case_response.get("judgment")
            if confirmation.get("decision") != "confirmed" or confirmation.get("judgment_confirmed") != judgment:
                raise ValueError(f"{case_id}: confirmation judgment must match the case disposition")
            affirmative_responses = {"đúng rồi", "đúng", "yes", "y", "confirmed", "confirm"}
            judgment_specific = {
                "finding": "giữ có lỗi (recommended)",
                "no_finding": "giữ không lỗi (recommended)",
                "uncertain": "giữ chưa rõ (recommended)",
            }
            allowed = affirmative_responses | {judgment_specific.get(judgment, "")}
            if literal_confirmation.strip().casefold() not in allowed:
                raise ValueError(f"{case_id}: literal response is not an affirmative judgment confirmation")
            if completion_kind != "preliminary_response" or kinds.index("preliminary_response_confirmation") <= completed:
                raise ValueError(f"{case_id}: case confirmation must follow its case response")
        if kinds.count("preliminary_response_confirmation") > 1:
            raise ValueError(f"{case_id}: at most one preliminary_response_confirmation is allowed")
        if offered >= completed:
            raise ValueError(f"{case_id}: source must be offered before its response/deferral")
        late_clarifications = [index for index, event in enumerate(entries) if event.get("kind") == "source_clarified" and index > completed]
        if late_clarifications:
            confirmations = [index for index, event in enumerate(entries) if event.get("kind") == "preliminary_response_confirmation"]
            if completion_kind != "preliminary_response" or not confirmations or confirmations[-1] <= max(late_clarifications):
                raise ValueError(f"{case_id}: source clarification after response requires user confirmation")
            if "post_response_disclosure" in kinds and kinds.index("post_response_disclosure") <= confirmations[-1]:
                raise ValueError(f"{case_id}: candidate disclosure must follow confirmation of clarified source")
        if "post_response_disclosure" in kinds:
            if completion_kind != "preliminary_response" or kinds.index("post_response_disclosure") <= completed:
                raise ValueError(f"{case_id}: candidate disclosure must follow its own actual preliminary response")
        if completion_kind == "deferred_by_user" and "post_response_disclosure" in kinds:
            raise ValueError(f"{case_id}: label disclosure must remain withheld after deferral")
    actual_first_seen = list(dict.fromkeys(
        event["case_id"] for event in events
        if event.get("kind") != "record_confirmation"
    ))
    if partial:
        if actual_first_seen != expected_order[: len(actual_first_seen)]:
            raise ValueError("partial review cases must follow the six-case frozen order")
    elif actual_first_seen != expected_order:
        raise ValueError("review cases must follow the six-case frozen order")


def resign_current_revision(ledger: dict[str, Any]) -> None:
    payload = disposition_payload(ledger)
    digest = canonical_digest(payload)
    ledger["revisions"][-1]["payload"] = payload
    ledger["revisions"][-1]["digest"] = digest
    ledger["payload_digest"] = digest


def validate_stage(ledger: dict[str, Any], expected_stage: str) -> None:
    if ledger.get("stage") != expected_stage:
        raise ValueError(f"ledger stage: expected {expected_stage!r}, got {ledger.get('stage')!r}")


def validate_frozen_inputs(ledger: dict[str, Any]) -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        raise FileNotFoundError(f"required frozen manifest missing: {MANIFEST_PATH}")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 6:
        raise ValueError(f"{MANIFEST_PATH}: expected six frozen cases")
    expected_ids = [case.get("case_id") for case in cases]
    if len(set(expected_ids)) != 6 or any(not case_id for case_id in expected_ids):
        raise ValueError(f"{MANIFEST_PATH}: case IDs must be six distinct non-empty values")
    if ledger.get("frozen_inventory") != cases:
        raise ValueError("packet frozen_inventory differs from the original manifest objects")
    recorded_hashes = ledger.get("input_hashes")
    if not isinstance(recorded_hashes, dict):
        raise ValueError("packet input_hashes must be an object")
    for relative, expected in EXPECTED_HASHES.items():
        path = ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(f"required input missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"{relative}: baseline hash mismatch; expected {expected}, observed {actual}")
        if recorded_hashes.get(relative) != actual:
            raise ValueError(f"{relative}: packet does not preserve the verified byte hash")
    if set(recorded_hashes) < set(EXPECTED_HASHES):
        missing = sorted(set(EXPECTED_HASHES) - set(recorded_hashes))
        raise ValueError(f"packet is missing frozen input hashes: {missing}")
    return manifest


def validate_packet_limits(ledger: dict[str, Any]) -> None:
    limits = {"source_cells": 200, "events": 200, "revisions": 50}
    for key, maximum in limits.items():
        value = ledger.get(key)
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        if len(value) > maximum:
            raise ValueError(f"{key} limit exceeded: {len(value)} entries; maximum {maximum}")


def validate_revisions(ledger: dict[str, Any]) -> None:
    revisions = ledger.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        raise ValueError("packet revisions must contain at least one payload snapshot")
    parent: str | None = None
    current_payload: dict[str, Any] | None = None
    for index, revision in enumerate(revisions):
        revision_payload = revision.get("payload")
        if not isinstance(revision_payload, dict):
            raise ValueError(f"revision {index}: missing payload object")
        delta = revision_payload.get("$patch")
        if delta is not None:
            if current_payload is None or not isinstance(delta, dict):
                raise ValueError(f"revision {index}: patch has no prior snapshot or is malformed")
            current_payload = copy.deepcopy(current_payload)
            for key, value in delta.items():
                if key == "case_updates":
                    if not isinstance(value, list):
                        raise ValueError(f"revision {index}: case_updates must be a list")
                    by_id = {case.get("case_id"): case for case in current_payload.get("cases", [])}
                    for update in value:
                        case_id = update.get("case_id")
                        if case_id not in by_id:
                            raise ValueError(f"revision {index}: case update targets unknown case {case_id!r}")
                        by_id[case_id].update({field: field_value for field, field_value in update.items() if field != "case_id"})
                elif key == "append_events":
                    if not isinstance(value, list):
                        raise ValueError(f"revision {index}: append_events must be a list")
                    current_payload["events"].extend(value)
                elif key == "event_updates":
                    if not isinstance(value, list):
                        raise ValueError(f"revision {index}: event_updates must be a list")
                    by_ordinal = {event.get("ordinal"): event for event in current_payload.get("events", [])}
                    for update in value:
                        ordinal = update.get("ordinal")
                        if ordinal not in by_ordinal:
                            raise ValueError(f"revision {index}: event update targets unknown ordinal {ordinal!r}")
                        by_ordinal[ordinal].update({field: field_value for field, field_value in update.items() if field != "ordinal"})
                else:
                    current_payload[key] = value
        else:
            current_payload = revision_payload
        digest = canonical_digest(current_payload)
        if revision.get("digest") != digest:
            raise ValueError(f"revision {index}: payload digest mismatch")
        if revision.get("parent_digest") != parent:
            raise ValueError(f"revision {index}: parent digest mismatch")
        parent = digest
    if ledger.get("payload_digest") != parent:
        raise ValueError("current payload_digest does not match the latest revision")
    if current_payload != disposition_payload(ledger):
        raise ValueError("latest revision snapshot differs from current mutable disposition state")


def validate_packet(ledger: dict[str, Any], expected_stage: str) -> dict[str, Any]:
    if ledger.get("schema_version") != 1 or ledger.get("packet_version") != "review-dispositions-v1":
        raise ValueError("unsupported disposition packet version")
    validate_stage(ledger, expected_stage)
    validate_packet_limits(ledger)
    manifest = validate_frozen_inputs(ledger)
    validate_revisions(ledger)
    packet_cases = ledger.get("cases")
    if not isinstance(packet_cases, list):
        raise ValueError("packet cases must be a list")
    expected_cases = manifest["cases"][:1] if expected_stage == "tracer" else manifest["cases"]
    expected_ids = [item["case_id"] for item in expected_cases]
    actual_ids = [item.get("case_id") for item in packet_cases]
    if actual_ids != expected_ids:
        raise ValueError(f"case dispositions/order: expected {expected_ids}, got {actual_ids}")
    for case in packet_cases:
        keys = set(case.get("source_cell_keys", []))
        expected_keys = {f"{sheet}!{cell}" for sheet, cell in REQUIRED_CELL_KEYS[case["case_id"]]}
        if keys != expected_keys:
            raise ValueError(f"{case.get('case_id')}: source_cell_keys do not match the complete named inventory")
        if case.get("ground_truth_status") != "needs_business_review":
            raise ValueError(f"{case.get('case_id')}: ground_truth_status was promoted")
        mentor_decision = case.get("mentor_decision")
        if not isinstance(mentor_decision, dict) or mentor_decision.get("status") != "pending" or any(
            mentor_decision.get(field) is not None
            for field in ("decision", "source_reference", "approved_by", "approved_at")
        ):
            raise ValueError(f"{case.get('case_id')}: pending mentor decision cannot contain approval metadata")
        finding_state = case.get("finding_review_state")
        if not isinstance(finding_state, dict) or finding_state.get("status") != "not_started" or any(
            finding_state.get(field) is not None for field in ("reviewer", "review_date")
        ):
            raise ValueError(f"{case.get('case_id')}: finding-review state cannot be promoted without provenance")
        severity = case.get("severity_approval")
        if not isinstance(severity, dict) or severity.get("status") != "unresolved" or any(
            severity.get(field) is not None
            for field in ("severity", "source_reference", "approved_by", "approved_at")
        ):
            raise ValueError(f"{case.get('case_id')}: severity approval cannot contain unapproved case-level metadata")
        response = case.get("preliminary_response")
        if expected_stage in {"tracer", "six_case"}:
            if response.get("status") != "unresolved" or response.get("judgment") is not None:
                raise ValueError(f"{case.get('case_id')}: planning stage fabricated a human judgment")
            if not has_missing_information(response.get("missing_information")) or not is_nonempty_text(response.get("follow_up_question")):
                raise ValueError(f"{case.get('case_id')}: unresolved status needs missing information and follow-up")
        elif expected_stage == "pending_ready":
            if response.get("status") == "unresolved":
                if response.get("judgment") is not None or not has_missing_information(response.get("missing_information")) or not is_nonempty_text(response.get("follow_up_question")):
                    raise ValueError(f"{case.get('case_id')}: unresolved/no-response state requires missing information and follow-up")
            elif response.get("status") == "recorded":
                if response.get("judgment") not in {"finding", "no_finding", "uncertain"} or not is_nonempty_text(response.get("rationale")) or not is_nonempty_text(response.get("literal_response")):
                    raise ValueError(f"{case.get('case_id')}: recorded preliminary response requires actual judgment, rationale and literal response")
                if has_missing_information(response.get("missing_information")) and not is_nonempty_text(response.get("follow_up_question")):
                    raise ValueError(f"{case.get('case_id')}: recorded response gaps require a string follow-up")
                if not has_reviewer_policy_follow_up(response):
                    raise ValueError(f"{case.get('case_id')}: missing reviewer/policy provenance requires an explicit provenance gap and follow-up")
            else:
                raise ValueError(f"{case.get('case_id')}: invalid pending_ready response state")
        elif expected_stage == "review_recorded":
            if response.get("status") == "recorded":
                if response.get("judgment") not in {"finding", "no_finding", "uncertain"} or not is_nonempty_text(response.get("rationale")) or not is_nonempty_text(response.get("literal_response")):
                    raise ValueError(f"{case.get('case_id')}: recorded final review needs judgment, literal text and rationale")
                if has_missing_information(response.get("missing_information")) and not is_nonempty_text(response.get("follow_up_question")):
                    raise ValueError(f"{case.get('case_id')}: recorded response gaps require a string follow-up")
                provenance_fields = ("reviewer", "reviewer_role", "applicable_business_version")
                has_missing_provenance = any(
                    not isinstance(response.get(field), str) or not response[field].strip()
                    for field in provenance_fields
                )
                if has_missing_provenance:
                    provenance_is_named = any(
                        isinstance(item, str)
                        and "reviewer identity/role" in item.lower()
                        and "policy version" in item.lower()
                        for item in response.get("missing_information", [])
                    )
                    follow_up = response.get("follow_up_question")
                    if not provenance_is_named or not isinstance(follow_up, str) or not follow_up.strip():
                        raise ValueError(f"{case.get('case_id')}: missing reviewer/policy provenance requires an explicit provenance gap and follow-up")
            elif response.get("status") == "unresolved":
                follow_up = response.get("follow_up_question")
                if response.get("judgment") is not None or not has_missing_information(response.get("missing_information")) or not is_nonempty_text(follow_up) or not is_nonempty_text(response.get("literal_response")):
                    raise ValueError(f"{case.get('case_id')}: unresolved final review needs explicit deferral, missing information and follow-up")
            else:
                raise ValueError(f"{case.get('case_id')}: final review requires a recorded judgment or explicit unresolved deferral")
        if case.get("mentor_decision", {}).get("status") != "pending":
            raise ValueError(f"{case.get('case_id')}: mentor decision must remain pending")
        mentor_decision = case.get("mentor_decision", {})
        if any(mentor_decision.get(field) is not None for field in ("decision", "source_reference", "approved_by", "approved_at")):
            raise ValueError(f"{case.get('case_id')}: pending mentor decision cannot contain a decision or approval provenance")
        finding_state = case.get("finding_review_state", {})
        if finding_state.get("status") != "not_started" or any(finding_state.get(field) is not None for field in ("reviewer", "review_date")):
            raise ValueError(f"{case.get('case_id')}: finding review state was promoted without recorded review provenance")
        severity = case.get("severity_approval", {})
        if severity.get("status") != "unresolved" or any(severity.get(field) is not None for field in ("severity", "source_reference", "approved_by", "approved_at")):
            raise ValueError(f"{case.get('case_id')}: severity approval was promoted without authority")
        if expected_stage in {"six_case", "pending_ready", "review_recorded"}:
            build_chat_projection(case)
    if ledger.get("pending_records"):
        validate_pending_records(ledger)
    validate_review_events(ledger)
    if expected_stage in {"pending_ready", "review_recorded"}:
        validate_pending_records(ledger)
    validate_markdown_consistency(PACKET_PATH.read_text(encoding="utf-8"), ledger)
    return manifest


def build_chat_projection(case: dict[str, Any]) -> dict[str, Any]:
    case_id = case.get("case_id")
    if case_id not in CASE_SCOPE:
        raise ValueError(f"unknown case for neutral projection: {case_id!r}")
    projection = case.get("chat_projection")
    if not isinstance(projection, dict):
        raise ValueError("P007 neutral chat projection is missing")
    encoded = json.dumps(projection, ensure_ascii=False).lower()
    for marker in LABEL_MARKERS:
        if marker.lower() in encoded:
            raise ValueError(f"neutral chat projection reveals a candidate label: {marker}")
    project, plan_id = CASE_SCOPE[case_id]
    if projection.get("title") != f"{project} · {plan_id}":
        raise ValueError("projection title must be the neutral project + plan identifier")
    if not str(projection.get("question", "")).startswith("Theo bạn có lỗi / không lỗi / chưa rõ? Lý do ngắn là gì?"):
        raise ValueError("projection must ask for a preliminary judgment only after source evidence")
    source_rows = projection.get("source_rows")
    if not isinstance(source_rows, list) or len(source_rows) != 2:
        raise ValueError("projection must present the previous plan beside the following week")
    return projection


def verify_p007_workbook_source(manifest: dict[str, Any], packet: dict[str, Any]) -> None:
    workbook_path = ROOT / manifest["workbook"]["path"]
    workbook = load_workbook(workbook_path, read_only=True, data_only=False, keep_links=False)
    try:
        previous = workbook["BC Tuần 31.08 - 05.09"]
        current = workbook["BC Tuần 07.09 - 12.09"]
        expected_cells = {
            ("BC Tuần 31.08 - 05.09", "F64"): previous["F64"].value,
            ("BC Tuần 31.08 - 05.09", "E64"): previous["E64"].value,
            ("BC Tuần 31.08 - 05.09", "G64"): previous["G64"].value,
            ("BC Tuần 07.09 - 12.09", "F52"): current["F52"].value,
            ("BC Tuần 07.09 - 12.09", "G52"): current["G52"].value,
            ("BC Tuần 07.09 - 12.09", "H52"): current["H52"].value,
        }
        recorded_cells = canonical_source_cells(packet, P007_ID)
        for (sheet, coordinate), raw_value in expected_cells.items():
            record = recorded_cells.get((sheet, coordinate))
            if record is None:
                raise ValueError(f"P007 packet source_cells missing {sheet}!{coordinate}")
            if record.get("raw_value") != raw_value:
                raise ValueError(f"{sheet}!{coordinate}: raw source value changed")
            if record.get("presence") != ("blank" if raw_value is None else "present"):
                raise ValueError(f"{sheet}!{coordinate}: blank/present state is wrong")
        if expected_cells[("BC Tuần 31.08 - 05.09", "F64")] is None:
            raise ValueError("P007 prior-week plan source is blank")
        for record in packet["cases"][0].get("source_cells", []):
            key = (record.get("sheet"), record.get("cell"))
            canonical = recorded_cells.get(key)
            if canonical is None or canonical.get("raw_value") != record.get("raw_value"):
                raise ValueError(f"P007 case-local source copy differs from canonical inventory at {key}")
    finally:
        workbook.close()


def verify_named_cell_inventory(packet: dict[str, Any]) -> None:
    per_case = {case_id: canonical_source_cells(packet, case_id) for case_id in CASE_SCOPE}
    workbook = load_workbook(ROOT / "docs/Khối SMC_Báo cáo tuần chi tiết.xlsx", read_only=False, data_only=False, keep_links=False)
    try:
        if "C64:D64" not in {str(item) for item in workbook["BC Tuần 31.08 - 05.09"].merged_cells.ranges}:
            raise ValueError("SMART PARKING PTSP role merge C64:D64 no longer matches the packet source map")
        if "C20:D20" not in {str(item) for item in workbook["BC Tuần 31.08 - 05.09"].merged_cells.ranges}:
            raise ValueError("DEPOT PTSP role merge C20:D20 no longer matches the packet source map")
        for case_id, expected_keys in REQUIRED_CELL_KEYS.items():
            actual_keys = set(per_case[case_id])
            missing = sorted(expected_keys - actual_keys)
            if missing:
                raise ValueError(f"{case_id}: missing named source cells {missing}")
            for sheet, coordinate in expected_keys:
                record = per_case[case_id][(sheet, coordinate)]
                raw_value = workbook[sheet][coordinate].value
                expected_presence = "blank" if raw_value is None else "present"
                expected_type = "null" if raw_value is None else "string" if isinstance(raw_value, str) else type(raw_value).__name__
                if record.get("raw_value") != raw_value:
                    raise ValueError(f"{case_id} {sheet}!{coordinate}: raw cell value mismatch")
                if record.get("presence") != expected_presence or record.get("raw_type") != expected_type:
                    raise ValueError(f"{case_id} {sheet}!{coordinate}: raw type/presence mismatch")
    finally:
        workbook.close()


def canonical_source_cells(packet: dict[str, Any], case_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in packet.get("source_cells", []):
        owners = set(record.get("case_ids", []))
        if record.get("case_id"):
            owners.add(record["case_id"])
        if case_id not in owners:
            continue
        key = (record.get("sheet"), record.get("cell"))
        previous = selected.get(key)
        revision = record.get("revision", 1)
        previous_revision = previous.get("revision", 1) if previous else -1
        shared_record = bool(record.get("case_ids"))
        previous_shared = bool(previous and previous.get("case_ids"))
        if previous is not None and revision == previous_revision and previous.get("raw_value") != record.get("raw_value"):
            if shared_record and not previous_shared:
                selected[key] = record
                continue
            if previous_shared and not shared_record:
                continue
            raise ValueError(f"{case_id} {key}: same-revision source cell records disagree")
        if previous is None or revision >= previous_revision:
            selected[key] = record
    return selected


def run_artifact_gate(expected_stage: str, *, require_confirmation: bool = False) -> int:
    if not PACKET_PATH.is_file():
        raise FileNotFoundError(f"disposition packet missing: {PACKET_PATH}")
    text, ledger = load_packet()
    manifest = validate_packet(ledger, expected_stage)
    if expected_stage in {"tracer", "six_case"}:
        verify_p007_workbook_source(manifest, ledger)
    if expected_stage == "six_case":
        verify_named_cell_inventory(ledger)
    if expected_stage == "tracer":
        projection = build_chat_projection(ledger["cases"][0])
        if not projection.get("source_rows"):
            raise ValueError("P007 projection has no source evidence before the question")
        if "review-dispositions-v1" not in text:
            raise ValueError("packet title/version marker is missing")
    if expected_stage == "six_case":
        for case in ledger["cases"]:
            build_chat_projection(case)
    if expected_stage == "review_recorded":
        events = ledger.get("events", [])
        for case in ledger["cases"]:
            case_events = [event for event in events if event.get("case_id") == case["case_id"]]
            kinds = [event.get("kind") for event in case_events]
            if "source_offered" not in kinds:
                raise ValueError(f"{case['case_id']}: missing actual source_offered event")
            if case.get("preliminary_response", {}).get("status") == "unresolved":
                if "deferred_by_user" not in kinds:
                    raise ValueError(f"{case['case_id']}: unresolved final review needs explicit user deferral")
            elif "preliminary_response" not in kinds:
                raise ValueError(f"{case['case_id']}: missing actual preliminary_response event")
            if "post_response_disclosure" in kinds and kinds.index("post_response_disclosure") < kinds.index("preliminary_response"):
                raise ValueError(f"{case['case_id']}: candidate disclosure occurred before its response")
    if require_confirmation and not any(event.get("kind") == "record_confirmation" for event in ledger.get("events", [])):
        raise ValueError("missing actual record_confirmation event")
    return 0


class DispositionPacketTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PACKET_PATH.is_file(), f"required packet missing: {PACKET_PATH}")
        self.stage = os.environ.get("DISPOSITION_EXPECT_STAGE", "review_recorded")

    def test_tracer_evidence_to_unresolved(self):
        _, ledger = load_packet()
        manifest = validate_packet(ledger, self.stage)
        if self.stage in {"tracer", "six_case"}:
            verify_p007_workbook_source(manifest, ledger)
        if self.stage == "six_case":
            verify_named_cell_inventory(ledger)
            for case in ledger["cases"]:
                projection = build_chat_projection(case)
                self.assertEqual(projection["source_rows"][0]["week"], "previous")
                self.assertEqual(projection["source_rows"][1]["week"], "current")
        else:
            projection = build_chat_projection(ledger["cases"][0])
            self.assertEqual(projection["source_rows"][0]["week"], "previous")
            self.assertEqual(projection["source_rows"][1]["week"], "current")
        if self.stage in {"tracer", "six_case"}:
            self.assertTrue(all(case["preliminary_response"]["status"] == "unresolved" for case in ledger["cases"]))
        else:
            self.assertTrue(all(case["preliminary_response"]["status"] in {"unresolved", "recorded"} for case in ledger["cases"]))

    def test_frozen_inventory_and_input_integrity(self):
        _, ledger = load_packet()
        manifest = validate_packet(ledger, self.stage)
        self.assertEqual(len(manifest["cases"]), 6)
        expected = manifest["cases"][:1] if self.stage == "tracer" else manifest["cases"]
        self.assertEqual([case["case_id"] for case in ledger["cases"]], [case["case_id"] for case in expected])

    def test_full_named_cell_evidence(self):
        _, ledger = load_packet()
        validate_packet(ledger, self.stage)
        if self.stage == "six_case":
            verify_named_cell_inventory(ledger)

    def test_chat_projection_hides_labels_before_response(self):
        _, ledger = load_packet()
        validate_packet(ledger, self.stage)
        for case in ledger["cases"]:
            projection = build_chat_projection(case)
            self.assertEqual(projection["title"], f"{CASE_SCOPE[case['case_id']][0]} · {CASE_SCOPE[case['case_id']][1]}")
            self.assertEqual(len(projection["source_rows"]), 2)

    def test_six_case_dispositions_and_missing_provenance(self):
        _, ledger = load_packet()
        validate_packet(ledger, self.stage)
        if self.stage == "six_case":
            for case in ledger["cases"]:
                response = case["preliminary_response"]
                self.assertIsNone(response["judgment"], case["case_id"])
                self.assertTrue(response["missing_information"], case["case_id"])
                self.assertTrue(response["follow_up_question"], case["case_id"])
                self.assertEqual(case["mentor_decision"]["status"], "pending")
                self.assertEqual(case["ground_truth_status"], "needs_business_review")

    def test_payload_revision_integrity(self):
        _, ledger = load_packet()
        validate_packet(ledger, self.stage)
        self.assertEqual(ledger["payload_digest"], ledger["revisions"][-1]["digest"])

    def test_unfinished_alternatives_remain_separate(self):
        _, ledger = load_packet()
        validate_pending_records(ledger)
        records = {record.get("record_id"): record for record in ledger.get("pending_records", [])}
        self.assertIn("pending-unfinished", records)
        self.assertEqual(records["pending-unfinished"]["status"], "unresolved")
        alternatives = records["pending-unfinished"]["alternatives"]
        self.assertEqual([item["source_id"] for item in alternatives], ["REQ-unfinished-state-mentor", "REQ-unfinished-state-research"])
        self.assertNotEqual(alternatives[0]["criterion"], alternatives[1]["criterion"])

    def test_authority_separation_and_no_critical_promotion(self):
        _, ledger = load_packet()
        validate_pending_records(ledger)
        record = next(item for item in ledger["pending_records"] if item["record_id"] == "pending-semantic-severity")
        self.assertIsNone(record["business_decision"])
        self.assertIsNone(record["severity_approval"])
        self.assertFalse(record["critical_executable"])
        self.assertTrue(all(case["mentor_decision"]["status"] == "pending" for case in ledger["cases"]))

    def test_depot_alias_is_conditional_without_original_authority(self):
        _, ledger = load_packet()
        validate_pending_records(ledger)
        record = next(item for item in ledger["pending_records"] if item["record_id"] == "pending-depot-alias")
        self.assertFalse(record["reported_claim"]["is_original_authority_evidence"])
        self.assertIsNone(record["decision"])
        for case in ledger["cases"]:
            if case["case_id"].startswith("depot-"):
                self.assertTrue(case["conditional_cross_period"])
                self.assertEqual(case["replay_status"], "not_established")

    def test_review_event_sequence_has_no_fabricated_interaction(self):
        _, ledger = load_packet()
        self.assertEqual(ledger["stage"], self.stage)
        if self.stage in {"tracer", "six_case"}:
            self.assertEqual(ledger["events"], [])
        validate_review_events(ledger)

    def test_review_event_mutations_reject_reveal_before_response(self):
        _, ledger = load_packet()
        if self.stage not in {"pending_ready", "review_recorded"}:
            return
        response = next(
            event for event in ledger["events"]
            if event.get("kind") == "preliminary_response"
        )
        disclosure = next(
            event for event in ledger["events"]
            if event.get("kind") == "post_response_disclosure"
        )
        mutation = copy.deepcopy(ledger)
        mutated_response = next(
            event for event in mutation["events"]
            if event.get("ordinal") == response["ordinal"]
        )
        mutated_disclosure = next(
            event for event in mutation["events"]
            if event.get("ordinal") == disclosure["ordinal"]
        )
        mutated_response["kind"], mutated_disclosure["kind"] = (
            mutated_disclosure["kind"], mutated_response["kind"]
        )
        mutated_response["actor"] = "assistant"
        mutated_response["payload"] = "Premature candidate disclosure"
        mutated_disclosure["actor"] = "user"
        mutated_disclosure["literal_response"] = mutation["cases"][0]["preliminary_response"]["literal_response"]
        mutated_disclosure["judgment"] = mutation["cases"][0]["preliminary_response"]["judgment"]
        mutated_disclosure["severity_opinion"] = mutation["cases"][0]["preliminary_response"].get("severity_opinion")

        with self.assertRaisesRegex(ValueError, "candidate disclosure must follow"):
            validate_review_events(mutation)

    def test_review_event_mutations_reject_reordered_case_groups(self):
        _, ledger = load_packet()
        if self.stage != "review_recorded":
            return
        mutation = copy.deepcopy(ledger)
        groups = {case["case_id"]: [] for case in mutation["cases"]}
        confirmations = []
        for event in mutation["events"]:
            if event.get("kind") == "record_confirmation":
                confirmations.append(event)
            else:
                groups[event["case_id"]].append(event)

        order = [case["case_id"] for case in mutation["cases"]]
        order[1], order[2] = order[2], order[1]
        mutation["events"] = [event for case_id in order for event in groups[case_id]] + confirmations
        for ordinal, event in enumerate(mutation["events"], 1):
            event["ordinal"] = ordinal

        with self.assertRaisesRegex(ValueError, "review cases must follow the six-case frozen order"):
            validate_review_events(mutation)

    def test_source_clarification_after_response_requires_reconfirmation(self):
        _, ledger = load_packet()
        if self.stage != "review_recorded":
            return
        mutation = copy.deepcopy(ledger)
        case_id = "smart-parking-P007-candidate-finding"
        group = [event for event in mutation["events"] if event.get("case_id") == case_id]
        by_kind = {event["kind"]: event for event in group}
        reordered = [
            by_kind["source_offered"],
            by_kind["preliminary_response"],
            by_kind["source_clarified"],
            by_kind["post_response_disclosure"],
        ]
        positions = [mutation["events"].index(event) for event in group]
        for position, event in zip(positions, reordered):
            mutation["events"][position] = event
        for ordinal, event in enumerate(mutation["events"], 1):
            event["ordinal"] = ordinal

        with self.assertRaisesRegex(ValueError, "source clarification after response requires user confirmation"):
            validate_review_events(mutation)

    def test_preliminary_response_event_requires_actual_matching_user_text(self):
        _, ledger = load_packet()
        if self.stage != "review_recorded":
            return
        event = next(item for item in ledger["events"] if item.get("kind") == "preliminary_response")
        mutations = []

        assistant_authored = copy.deepcopy(ledger)
        next(item for item in assistant_authored["events"] if item.get("ordinal") == event["ordinal"])["actor"] = "assistant"
        mutations.append(("non-user actor", assistant_authored, "user-authored"))

        missing_literal = copy.deepcopy(ledger)
        next(item for item in missing_literal["events"] if item.get("ordinal") == event["ordinal"]).pop("literal_response")
        mutations.append(("missing literal response", missing_literal, "literal_response"))

        mismatched_literal = copy.deepcopy(ledger)
        next(item for item in mismatched_literal["events"] if item.get("ordinal") == event["ordinal"])["literal_response"] += " altered"
        mutations.append(("event/case response mismatch", mismatched_literal, "match the case disposition"))

        for label, mutation, message in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                validate_review_events(mutation)

    def test_event_kind_actor_and_required_payloads_are_enforced(self):
        _, ledger = load_packet()
        if self.stage != "review_recorded":
            return

        mutations = []
        source_actor = copy.deepcopy(ledger)
        next(event for event in source_actor["events"] if event.get("kind") == "source_offered")["actor"] = "user"
        mutations.append(("source actor", source_actor, "source_offered must be assistant-authored"))

        disclosure_actor = copy.deepcopy(ledger)
        next(event for event in disclosure_actor["events"] if event.get("kind") == "post_response_disclosure")["actor"] = "user"
        mutations.append(("disclosure actor", disclosure_actor, "post_response_disclosure must be assistant-authored"))

        confirmation_actor = copy.deepcopy(ledger)
        next(event for event in confirmation_actor["events"] if event.get("kind") == "preliminary_response_confirmation")["actor"] = "assistant"
        mutations.append(("case confirmation actor", confirmation_actor, "preliminary_response_confirmation must be user-authored"))

        contradictory_case_confirmation = copy.deepcopy(ledger)
        next(event for event in contradictory_case_confirmation["events"] if event.get("kind") == "preliminary_response_confirmation")["literal_response"] = "No, I do not reaffirm this judgment"
        mutations.append(("contradictory case confirmation", contradictory_case_confirmation, "affirmative judgment confirmation"))

        confirmation_literal = copy.deepcopy(ledger)
        next(event for event in confirmation_literal["events"] if event.get("kind") == "record_confirmation")["literal_response"] = []
        mutations.append(("non-string confirmation", confirmation_literal, "record_confirmation requires a non-empty string"))

        source_payload = copy.deepcopy(ledger)
        next(event for event in source_payload["events"] if event.get("kind") == "source_offered")["payload"] = " "
        mutations.append(("empty source payload", source_payload, "source_offered requires a non-empty payload"))

        source_candidate_label = copy.deepcopy(ledger)
        next(event for event in source_candidate_label["events"] if event.get("kind") == "source_offered")["payload"] += " Milestone Conflict"
        mutations.append(("candidate label in source offer", source_candidate_label, "source-first event payload"))

        source_verdict = copy.deepcopy(ledger)
        next(event for event in source_verdict["events"] if event.get("kind") == "source_offered")["payload"] += " This case is a finding."
        mutations.append(("plain finding verdict in source", source_verdict, "source-first event payload"))

        source_no_finding = copy.deepcopy(ledger)
        next(event for event in source_no_finding["events"] if event.get("kind") == "source_clarified")["payload"] += " Không lỗi."
        mutations.append(("plain no-finding verdict in clarification", source_no_finding, "source-first event payload"))

        altered_source_copy = copy.deepcopy(ledger)
        next(event for event in altered_source_copy["events"] if event.get("kind") == "source_offered")["payload"] += " A flawless result."
        mutations.append(("unclassified verdict wording", altered_source_copy, "source-first event payload differs from its frozen neutral message"))

        paraphrased_verdicts = (
            "No defects found.",
            "This is not a finding.",
            "Không thấy lỗi.",
            "Có sai sót.",
        )
        for phrase in paraphrased_verdicts:
            mutation = copy.deepcopy(ledger)
            next(event for event in mutation["events"] if event.get("kind") == "source_offered")["payload"] += f" {phrase}"
            mutations.append((f"verdict paraphrase: {phrase}", mutation, "source-first event payload"))

        source_candidate_id = copy.deepcopy(ledger)
        next(event for event in source_candidate_id["events"] if event.get("kind") == "source_clarified")["payload"] += " candidate-finding"
        mutations.append(("candidate ID in source clarification", source_candidate_id, "source-first event payload"))

        unknown_kind = copy.deepcopy(ledger)
        next(event for event in unknown_kind["events"] if event.get("ordinal") == 1)["kind"] = "unrecognized_event"
        mutations.append(("unknown event kind", unknown_kind, "unsupported review event kind"))

        confirmation_order = copy.deepcopy(ledger)
        p001 = "depot-gsm-P001-candidate-no-finding"
        group = [event for event in confirmation_order["events"] if 11 <= event["ordinal"] <= 15]
        by_kind = {event["kind"]: event for event in group if event.get("case_id") == p001}
        reordered = [
            next(event for event in group if event.get("kind") == "source_offered"),
            by_kind["preliminary_response_confirmation"],
            by_kind["preliminary_response"],
            by_kind["source_clarified"],
            by_kind["post_response_disclosure"],
        ]
        confirmation_order["events"][10:15] = reordered
        for ordinal, event in enumerate(confirmation_order["events"], 1):
            event["ordinal"] = ordinal
        mutations.append(("case confirmation before response", confirmation_order, "confirmation must follow its case response"))

        negative_record_confirmation = copy.deepcopy(ledger)
        next(event for event in negative_record_confirmation["events"] if event.get("kind") == "record_confirmation")["literal_response"] = "No"
        mutations.append(("negative record confirmation", negative_record_confirmation, "affirmative confirmation response"))

        unbound_confirmation = copy.deepcopy(ledger)
        next(event for event in unbound_confirmation["events"] if event.get("kind") == "record_confirmation")["confirmed_revision_digest"] = "0" * 64
        mutations.append(("confirmation references unknown revision", unbound_confirmation, "does not reference the confirmed revision"))

        altered_confirmation_snapshot = copy.deepcopy(ledger)
        confirmed_digest = next(event for event in altered_confirmation_snapshot["events"] if event.get("kind") == "record_confirmation")["confirmed_revision_digest"]
        child = next(revision for revision in altered_confirmation_snapshot["revisions"] if revision.get("parent_digest") == confirmed_digest)
        next(event for event in child["payload"]["events"] if event.get("kind") == "record_confirmation")["literal_response"] = "No"
        mutations.append(("confirmation differs from bound snapshot", altered_confirmation_snapshot, "confirmation-bearing snapshot"))

        stale_revision_confirmation = copy.deepcopy(ledger)
        stale_target = stale_revision_confirmation["revisions"][13]["digest"]
        next(event for event in stale_revision_confirmation["events"] if event.get("kind") == "record_confirmation")["confirmed_revision_digest"] = stale_target
        mutations.append(("confirmation references stale revision", stale_revision_confirmation, "six recorded case reviews"))

        negative_confirmation_scope = copy.deepcopy(ledger)
        next(event for event in negative_confirmation_scope["events"] if event.get("kind") == "record_confirmation")["confirmation_scope"] = "unrelated_text"
        mutations.append(("confirmation scope mismatch", negative_confirmation_scope, "affirmative confirmation response"))

        unbound_confirmation = copy.deepcopy(ledger)
        next(event for event in unbound_confirmation["events"] if event.get("kind") == "record_confirmation")["confirmed_revision_digest"] = "0" * 64
        mutations.append(("confirmation references unknown revision", unbound_confirmation, "does not reference the confirmed revision"))

        for label, mutation, message in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                validate_review_events(mutation)

    def test_record_confirmation_requires_distinct_frozen_target(self):
        _, ledger = load_packet()
        if self.stage != "review_recorded":
            return
        mutation = copy.deepcopy(ledger)
        target_payload = disposition_payload(mutation)
        target_payload["stage"] = "pending_ready"
        target_payload["events"] = [event for event in target_payload["events"] if event.get("kind") != "record_confirmation"]
        target_payload["cases"][-1]["case_id"] = target_payload["cases"][0]["case_id"]
        target_digest = canonical_digest(target_payload)
        child_payload = disposition_payload(mutation)
        child_digest = canonical_digest(child_payload)
        mutation["revisions"].extend([
            {"payload": target_payload, "digest": target_digest, "parent_digest": mutation["revisions"][-1]["digest"]},
            {"payload": child_payload, "digest": child_digest, "parent_digest": target_digest},
        ])
        mutation["payload_digest"] = child_digest
        next(event for event in mutation["events"] if event.get("kind") == "record_confirmation")["confirmed_revision_digest"] = target_digest

        with self.assertRaisesRegex(ValueError, "six distinct frozen case IDs in order"):
            validate_review_events(mutation)

    def test_review_recorded_accepts_explicit_user_deferral_without_disclosure(self):
        _, ledger = load_packet()
        mutation = copy.deepcopy(ledger)
        case = mutation["cases"][0]
        case["preliminary_response"] = {
            "status": "unresolved",
            "judgment": None,
            "rationale": None,
            "reviewer": None,
            "reviewer_role": None,
            "review_date": None,
            "applicable_business_version": None,
            "literal_response": "Tạm để pending, hiện chưa đưa ra nhận xét sơ bộ.",
            "missing_information": ["User deferred the preliminary judgment."],
            "follow_up_question": "Does the user want to revisit this case?",
        }
        offered = next(event for event in mutation["events"] if event["case_id"] == case["case_id"] and event["kind"] == "source_offered")
        response = next(event for event in mutation["events"] if event["case_id"] == case["case_id"] and event["kind"] == "preliminary_response")
        response.clear()
        response.update({
            "ordinal": 3,
            "kind": "deferred_by_user",
            "case_id": case["case_id"],
            "actor": "user",
            "timestamp": None,
            "timestamp_reason": "Harness transcript timestamp unavailable.",
            "message_ref": "Conversation: explicit deferral test fixture.",
            "literal_response": "Tạm để pending, hiện chưa đưa ra nhận xét sơ bộ.",
        })
        mutation["events"] = [
            event for event in mutation["events"]
            if not (event["case_id"] == case["case_id"] and event["kind"] == "post_response_disclosure")
        ]
        self.assertLess(offered["ordinal"], response["ordinal"])
        resign_current_revision(mutation)
        validate_packet(mutation, "review_recorded")
        validate_review_events(mutation)

        invalid_follow_up = copy.deepcopy(mutation)
        invalid_follow_up["cases"][0]["preliminary_response"]["follow_up_question"] = {"not": "a question"}
        resign_current_revision(invalid_follow_up)
        with self.assertRaisesRegex(ValueError, "unresolved final review needs explicit deferral, missing information and follow-up"):
            validate_packet(invalid_follow_up, "review_recorded")

    def test_recorded_response_missing_reviewer_or_policy_has_follow_up(self):
        _, ledger = load_packet()
        case_id = "smart-parking-P003-candidate-no-finding"
        mutations = []
        missing_values = copy.deepcopy(ledger)
        case = next(item for item in missing_values["cases"] if item["case_id"] == case_id)
        case["preliminary_response"]["missing_information"] = []
        case["preliminary_response"]["follow_up_question"] = None
        mutations.append(("null provenance", missing_values))

        empty_values = copy.deepcopy(ledger)
        case = next(item for item in empty_values["cases"] if item["case_id"] == case_id)
        case["preliminary_response"].update({"reviewer": " ", "reviewer_role": "", "applicable_business_version": "  "})
        case["preliminary_response"]["missing_information"] = []
        case["preliminary_response"]["follow_up_question"] = None
        mutations.append(("empty provenance strings", empty_values))

        unrelated_gap = copy.deepcopy(ledger)
        case = next(item for item in unrelated_gap["cases"] if item["case_id"] == case_id)
        case["preliminary_response"]["missing_information"] = ["The source lacks a result for a separate commitment."]
        case["preliminary_response"]["follow_up_question"] = "Where was that separate result recorded?"
        mutations.append(("unrelated gap does not cover reviewer/policy provenance", unrelated_gap))

        non_string_follow_up = copy.deepcopy(ledger)
        case = next(item for item in non_string_follow_up["cases"] if item["case_id"] == case_id)
        response = case["preliminary_response"]
        response.update({"reviewer": "Reviewer", "reviewer_role": "PM", "applicable_business_version": "rule-v1"})
        response["missing_information"] = ["A separate closure detail remains open."]
        response["follow_up_question"] = {"not": "a question"}
        mutations.append(("non-string provenance follow-up", non_string_follow_up))

        malformed_missing_information = copy.deepcopy(ledger)
        case = next(item for item in malformed_missing_information["cases"] if item["case_id"] == case_id)
        response = case["preliminary_response"]
        response.update({"reviewer": "Reviewer", "reviewer_role": "PM", "applicable_business_version": "rule-v1"})
        response["missing_information"] = "not-a-list"
        response["follow_up_question"] = None
        mutations.append(("malformed missing-information container", malformed_missing_information))

        for label, mutation in mutations:
            resign_current_revision(mutation)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "follow-up|missing_information"):
                validate_packet(mutation, "review_recorded")

    def test_complete_recorded_response_needs_no_follow_up(self):
        _, ledger = load_packet()
        mutation = copy.deepcopy(ledger)
        case = next(item for item in mutation["cases"] if item["case_id"] == "smart-parking-P003-candidate-no-finding")
        case["preliminary_response"].update({
            "reviewer": "Named reviewer",
            "reviewer_role": "PM",
            "applicable_business_version": "business-policy-v1",
            "missing_information": [],
            "follow_up_question": None,
        })
        resign_current_revision(mutation)
        validate_packet(mutation, "review_recorded")

    def test_shared_pending_record_requires_string_follow_up(self):
        _, ledger = load_packet()
        mutation = copy.deepcopy(ledger)
        mutation["pending_records"][0]["follow_up_question"] = {"not": "a question"}
        with self.assertRaisesRegex(ValueError, "string follow_up_question"):
            validate_pending_records(mutation)

    def test_record_confirmation_is_not_fabricated(self):
        _, ledger = load_packet()
        confirmations = [event for event in ledger["events"] if event.get("kind") == "record_confirmation"]
        if self.stage == "review_recorded":
            self.assertEqual(len(confirmations), 1)
            self.assertEqual(confirmations[0].get("actor"), "user")
            run_artifact_gate(self.stage, require_confirmation=True)
        else:
            self.assertFalse(confirmations)
            with self.assertRaises(ValueError):
                run_artifact_gate(self.stage, require_confirmation=True)

    def test_release_stage_and_confirmation_are_required_in_test_runners(self):
        self.assertEqual(self.stage, "review_recorded")
        self.assertEqual(run_artifact_gate("review_recorded", require_confirmation=True), 0)

    def test_negative_packet_mutations_fail_closed(self):
        _, ledger = load_packet()
        mutations = []

        merged_alternatives = copy.deepcopy(ledger)
        merged_alternatives["pending_records"][0]["alternatives"][1]["criterion"] = merged_alternatives["pending_records"][0]["alternatives"][0]["criterion"]
        mutations.append(("merged unfinished alternatives", merged_alternatives))

        promoted_severity = copy.deepcopy(ledger)
        promoted_severity["pending_records"][1]["critical_executable"] = True
        promoted_severity["pending_records"][1]["severity_approval"] = {"status": "confirmed"}
        mutations.append(("unapproved severity promotion", promoted_severity))

        promoted_alias = copy.deepcopy(ledger)
        promoted_alias["pending_records"][2]["reported_claim"]["is_original_authority_evidence"] = True
        promoted_alias["pending_records"][2]["decision"] = "confirmed"
        mutations.append(("reported alias promoted to proof", promoted_alias))

        missing_link = copy.deepcopy(ledger)
        missing_link["cases"][2]["pending_record_links"].remove("pending-depot-alias")
        mutations.append(("DEPOT alias link removed", missing_link))

        contradictory_case_mentor = copy.deepcopy(ledger)
        contradictory_case_mentor["cases"][0]["mentor_decision"]["decision"] = "approved"
        mutations.append(("case mentor decision contradicts pending", contradictory_case_mentor))

        contradictory_case_approver = copy.deepcopy(ledger)
        contradictory_case_approver["cases"][0]["mentor_decision"]["approved_by"] = "Mentor"
        mutations.append(("case mentor approver contradicts pending", contradictory_case_approver))

        contradictory_case_mentor_source = copy.deepcopy(ledger)
        contradictory_case_mentor_source["cases"][0]["mentor_decision"]["source_reference"] = "docs/approval.md"
        mutations.append(("case mentor source contradicts pending", contradictory_case_mentor_source))

        contradictory_case_severity = copy.deepcopy(ledger)
        contradictory_case_severity["cases"][0]["severity_approval"]["severity"] = "Warning"
        mutations.append(("case severity contradicts unresolved", contradictory_case_severity))

        contradictory_case_severity_status = copy.deepcopy(ledger)
        contradictory_case_severity_status["cases"][0]["severity_approval"]["status"] = "approved"
        mutations.append(("case severity status promoted", contradictory_case_severity_status))

        contradictory_case_severity_approver = copy.deepcopy(ledger)
        contradictory_case_severity_approver["cases"][0]["severity_approval"]["approved_by"] = "Mentor"
        mutations.append(("case severity approver promoted", contradictory_case_severity_approver))

        for label, mutation in mutations:
            with self.subTest(label=label):
                resign_current_revision(mutation)
                with self.assertRaises(ValueError):
                    validate_packet(mutation, self.stage)

    def test_packet_size_limit_rejects_oversized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "oversized.md"
            oversized.write_text("x" * (5 * 1024 * 1024 + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum packet size"):
                load_packet(oversized)

    def test_source_cell_limit_rejects_overflow(self):
        _, ledger = load_packet()
        mutation = copy.deepcopy(ledger)
        mutation["source_cells"] = mutation["source_cells"] + [
            {"case_id": "smart-parking-P007-candidate-finding", "sheet": "invalid", "cell": f"X{i}", "raw_value": None}
            for i in range(201)
        ]
        resign_current_revision(mutation)
        with self.assertRaisesRegex(ValueError, "source_cells limit"):
            validate_packet(mutation, self.stage)

        mutation = copy.deepcopy(ledger)
        mutation["events"] = [{}] * 201
        resign_current_revision(mutation)
        with self.assertRaisesRegex(ValueError, "events limit"):
            validate_packet(mutation, self.stage)

        mutation = copy.deepcopy(ledger)
        mutation["revisions"] = mutation["revisions"] * 17
        with self.assertRaisesRegex(ValueError, "revisions limit"):
            validate_packet(mutation, self.stage)

    def test_markdown_and_ledger_agree(self):
        text, ledger = load_packet()
        validate_markdown_consistency(text, ledger)

    def test_markdown_consistency_rejects_stale_pre_review_claims(self):
        text, ledger = load_packet()
        stale = text + "\nJudgment sơ bộ người dùng: **chưa nhận được** (`judgment: null`; `status: unresolved`).\n"
        with self.assertRaisesRegex(ValueError, "contradicts the recorded review stage"):
            validate_markdown_consistency(stale, ledger)
        stale_count = text + "\nFinal packet/source verification: gate passed (18 tests, 0 skipped).\n"
        with self.assertRaisesRegex(ValueError, "volatile final test count"):
            validate_markdown_consistency(stale_count, ledger)
        alternate_stale_count = text + "\nFinal artifact gate and exact test count: 18 tests (0 skipped).\n"
        with self.assertRaisesRegex(ValueError, "volatile final test count"):
            validate_markdown_consistency(alternate_stale_count, ledger)
        for claim in (
            "Final tests: 18/18 passed",
            "Final artifact gate: 18/18 tests, 0 skipped",
            "Tests (18 total)",
            "Final test count: 18",
            "22 passed",
            "22 passed, 0 skipped",
        ):
            with self.subTest(claim=claim), self.assertRaisesRegex(ValueError, "volatile final test count"):
                validate_markdown_consistency(text + "\n" + claim + "\n", ledger)
        validate_pending_records(ledger)


if __name__ == "__main__":
    stage = "review_recorded"
    confirmation = True
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DispositionPacketTests)
    count = suite.countTestCases()
    if count == 0:
        raise SystemExit("artifact gate found zero tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0 or result.testsRun != count or result.skipped:
        raise SystemExit("artifact gate requires non-zero, complete, non-skipped test execution")
    if not result.wasSuccessful():
        raise SystemExit(1)
    run_artifact_gate(stage, require_confirmation=confirmation)
    print(f"artifact gate passed: testsRun={result.testsRun}, stage={stage}")
