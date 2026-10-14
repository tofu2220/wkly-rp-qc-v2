"""Offline integrity checks for the phase-1 disposition packet."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
LABEL_MARKERS = (
    "candidate-finding",
    "candidate-no-finding",
    "Milestone Conflict",
    "Closure Incomplete",
    "Missing Closure",
    "no_finding",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_packet(path: Path = PACKET_PATH) -> tuple[str, dict[str, Any]]:
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


def validate_revisions(ledger: dict[str, Any]) -> None:
    revisions = ledger.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        raise ValueError("packet revisions must contain at least one payload snapshot")
    parent: str | None = None
    for index, revision in enumerate(revisions):
        snapshot = revision.get("payload")
        if not isinstance(snapshot, dict):
            raise ValueError(f"revision {index}: missing payload object")
        digest = canonical_digest(snapshot)
        if revision.get("digest") != digest:
            raise ValueError(f"revision {index}: payload digest mismatch")
        if revision.get("parent_digest") != parent:
            raise ValueError(f"revision {index}: parent digest mismatch")
        parent = digest
    if ledger.get("payload_digest") != parent:
        raise ValueError("current payload_digest does not match the latest revision")
    if revisions[-1].get("payload") != disposition_payload(ledger):
        raise ValueError("latest revision snapshot differs from current mutable disposition state")


def validate_packet(ledger: dict[str, Any], expected_stage: str) -> dict[str, Any]:
    if ledger.get("schema_version") != 1 or ledger.get("packet_version") != "review-dispositions-v1":
        raise ValueError("unsupported disposition packet version")
    validate_stage(ledger, expected_stage)
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
        if case.get("ground_truth_status") != "needs_business_review":
            raise ValueError(f"{case.get('case_id')}: ground_truth_status was promoted")
        response = case.get("preliminary_response")
        if expected_stage in {"tracer", "six_case", "pending_ready"}:
            if response.get("status") != "unresolved" or response.get("judgment") is not None:
                raise ValueError(f"{case.get('case_id')}: planning stage fabricated a human judgment")
            if not response.get("missing_information") or not response.get("follow_up_question"):
                raise ValueError(f"{case.get('case_id')}: unresolved status needs missing information and follow-up")
        if case.get("mentor_decision", {}).get("status") != "pending":
            raise ValueError(f"{case.get('case_id')}: mentor decision must remain pending")
    return manifest


def build_chat_projection(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("case_id") != P007_ID:
        raise ValueError("tracer projection is restricted to SMART PARKING P007")
    projection = case.get("chat_projection")
    if not isinstance(projection, dict):
        raise ValueError("P007 neutral chat projection is missing")
    encoded = json.dumps(projection, ensure_ascii=False).lower()
    for marker in LABEL_MARKERS:
        if marker.lower() in encoded:
            raise ValueError(f"neutral chat projection reveals a candidate label: {marker}")
    if projection.get("title") != "SMART PARKING · P007":
        raise ValueError("projection title must be the neutral project + plan identifier")
    if projection.get("question") != "Theo bạn có lỗi / không lỗi / chưa rõ? Lý do ngắn là gì?":
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
        recorded_cells = {
            (cell.get("sheet"), cell.get("cell")): cell
            for cell in packet.get("source_cells", [])
            if cell.get("case_id") == P007_ID
        }
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
    finally:
        workbook.close()


def run_artifact_gate(expected_stage: str, *, require_confirmation: bool = False) -> int:
    if not PACKET_PATH.is_file():
        raise FileNotFoundError(f"disposition packet missing: {PACKET_PATH}")
    text, ledger = load_packet()
    manifest = validate_packet(ledger, expected_stage)
    if expected_stage in {"tracer", "six_case"}:
        verify_p007_workbook_source(manifest, ledger)
    if expected_stage == "tracer":
        projection = build_chat_projection(ledger["cases"][0])
        if not projection.get("source_rows"):
            raise ValueError("P007 projection has no source evidence before the question")
        if "review-dispositions-v1" not in text:
            raise ValueError("packet title/version marker is missing")
    if expected_stage == "six_case":
        for case in ledger["cases"]:
            if case.get("chat_projection"):
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

    def test_tracer_evidence_to_unresolved(self):
        _, ledger = load_packet()
        manifest = validate_packet(ledger, "tracer")
        verify_p007_workbook_source(manifest, ledger)
        projection = build_chat_projection(ledger["cases"][0])
        self.assertEqual(projection["source_rows"][0]["week"], "previous")
        self.assertEqual(projection["source_rows"][1]["week"], "current")
        self.assertEqual(ledger["cases"][0]["preliminary_response"]["status"], "unresolved")

    def test_frozen_inventory_and_input_integrity(self):
        _, ledger = load_packet()
        manifest = validate_packet(ledger, "tracer")
        self.assertEqual(len(manifest["cases"]), 6)


if __name__ == "__main__":
    stage = os.environ.get("DISPOSITION_EXPECT_STAGE", "tracer")
    confirmation = os.environ.get("DISPOSITION_REQUIRE_CONFIRMATION") == "1"
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
