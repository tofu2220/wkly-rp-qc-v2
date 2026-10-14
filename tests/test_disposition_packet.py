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
        keys = set(case.get("source_cell_keys", []))
        expected_keys = {f"{sheet}!{cell}" for sheet, cell in REQUIRED_CELL_KEYS[case["case_id"]]}
        if keys != expected_keys:
            raise ValueError(f"{case.get('case_id')}: source_cell_keys do not match the complete named inventory")
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
        self.stage = os.environ.get("DISPOSITION_EXPECT_STAGE", "tracer")

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
        self.assertTrue(all(case["preliminary_response"]["status"] == "unresolved" for case in ledger["cases"]))

    def test_frozen_inventory_and_input_integrity(self):
        _, ledger = load_packet()
        manifest = validate_packet(ledger, self.stage)
        self.assertEqual(len(manifest["cases"]), 6)
        expected = manifest["cases"][:1] if self.stage == "tracer" else manifest["cases"]
        self.assertEqual([case["case_id"] for case in ledger["cases"]], [case["case_id"] for case in expected])


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
