"""Integrity checks for the dated record, never a substitute for browser UAT."""

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKET_PATH = "docs/phase-4-evaluation/review-dispositions-v1.md"
RECORD = ROOT / "docs/phase-4-evaluation/honest-offline-pm-demo-2026-10-15.md"
PACKET = ROOT / PACKET_PATH
MANIFEST = ROOT / "docs/phase-4-evaluation/candidate-cases.json"
# Pin the disclosed comparisons, especially candidate no-finding vs uncertain review.
DRAFT_COMPARISONS = (
    "proposal: SP-1 cùng hai rule; plan completion và tháng pilot còn mở",
    "proposal: SP-2 không lỗi báo cáo; không xác nhận release hoàn tất",
    "proposal: DG-1 chưa đủ căn cứ cross-period do alias",
    "proposal: DG-2 chưa đủ căn cứ alias; DONE chỉ riêng verify",
    "proposal: AC-1 thiếu follow-up; không suy ra chưa làm",
    "proposal: AC-2 Identity Uncertain; chưa đủ căn cứ closure riêng",
)


def validate_record(text):
    """Validate the authored evidence contract against frozen repository inputs."""
    packet_text = PACKET.read_text(encoding="utf-8")
    ledger = json.loads(packet_text.split("```disposition-ledger\n")[1].split("```")[0])
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "**Record:** `honest-offline-pm-demo-v1`" in text
    assert "**Date:** 2026-10-15" in text
    assert "**Status:** `partial / human_needed`" in text
    assert f"**Packet version:** `{ledger['packet_version']}`" in text
    assert f"**Packet path:** `{PACKET_PATH}`" in text
    digest = hashlib.sha256(PACKET.read_bytes()).hexdigest()
    assert f"**Packet SHA-256:** `{digest}`" in text
    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == ledger["input_hashes"][
        "docs/phase-4-evaluation/candidate-cases.json"]
    assert "[Phase 01 packet](review-dispositions-v1.md)" in text
    for section in ("Technical replay outcome", "Six case-review outcomes",
                    "Pending policy/severity/alias authority", "Remaining acceptance gaps"):
        assert f"## {section}\n" in text
    table = text.split("## Six case-review outcomes\n")[1].split("\n## ")[0]
    rows = [line for line in table.splitlines() if line.startswith("| `")]
    cases = {case["case_id"]: case for case in ledger["cases"]}
    assert len(rows) == len(cases) == len(manifest["cases"]) == 6
    found = []
    for row in rows:
        cells = [cell.strip().strip("`") for cell in row.strip("|").split("|")]
        assert len(cells) == 7
        case_id, judgment, reference, draft, replay, mentor, severity = cells
        found.append(case_id)
        case = cases[case_id]
        frozen = next(item for item in manifest["cases"] if item["case_id"] == case_id)
        assert judgment == case["preliminary_response"]["judgment"]
        assert reference.startswith(frozen["candidate_outcome"] + ":")
        for rule in frozen["candidate_rules"]:
            assert rule in reference
        index = next(i for i, item in enumerate(manifest["cases"]) if item["case_id"] == case_id)
        assert draft == DRAFT_COMPARISONS[index]
        assert replay == frozen["replay_status"]
        assert mentor == case["mentor_decision"]["status"] == "pending"
        assert severity == case["severity_approval"]["status"] == "unresolved"
    assert set(found) == set(cases) and len(set(found)) == 6
    headings = re.findall(r"^### (.+)$", packet_text, re.MULTILINE)
    for pending in ledger["pending_records"]:
        assert pending["status"] == "unresolved"
        record_id = pending["record_id"]
        heading = next(title for title in headings if title.startswith(f"`{record_id}`"))
        anchor = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        assert f"[{record_id}](review-dispositions-v1.md#{anchor})" in text
    for disclosure in (
        "non-blind/proposal evidence, not ground truth",
        "All six retain `needs_business_review`",
        "S020 identifiers are not the frozen P001/P003/P007 IDs",
        "Provider usage/cost: not applicable, not measured as zero",
        "Rejected-state human demo: USER CONFIRMED",
        "Unavailable-scope UI: NOT VERIFIED",
        "S020-7 discrepancy: RESOLVED BY USER CONFIRMATION",
        "Read-only store: `rejected`",
        "Earlier transcript (history): `clarification_needed`",
        "Current user-confirmed state: `rejected`",
        "S020-7 rejected-row export contents: NOT MANUALLY VERIFIED",
        "Chất lượng báo cáo không cụ thể",
        "legacy_partial",
        "20261012T031440747089Z",
        manifest["workbook"]["sha256"],
    ):
        assert disclosure in text, disclosure
    # Positive assertions of approval/success are forbidden, not the caveats that deny them.
    for claim in (
        r"(?:is|are|constitutes?)\s+(?:the\s+)?ground truth",
        r"(?:mentor|policy|severity|alias)\s+(?:is\s+)?(?:approved|confirmed)",
        r"(?:accuracy|precision|recall)\s*[:=]\s*\d",
        r"(?:DEPOT GSM|ANAT Công trường)\s*[:—-]\s*(?:clean pass|passed|no.finding)",
        r"(?:all demo criteria|phase 02)\s+(?:are\s+|is\s+)?(?:passed|complete)",
    ):
        assert not re.search(claim, text, re.IGNORECASE), claim


class HonestOfflineDemoOutcomeTests(unittest.TestCase):
    def test_record_integrity(self):
        validate_record(RECORD.read_text(encoding="utf-8"))

    def test_digest_tampering_is_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        digest = hashlib.sha256(PACKET.read_bytes()).hexdigest()
        with self.assertRaises(AssertionError):
            validate_record(text.replace(digest, "0" * 64))

    def test_case_omission_duplicate_and_changed_judgment_are_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if line.startswith("| `smart-parking-")]
        mutations = [text.replace(rows[0] + "\n", ""),
                     text.replace(rows[1], rows[0]),
                     text.replace("| `uncertain` |", "| `no_finding` |")]
        for mutated in mutations:
            with self.subTest(mutation=mutated[-80:]), self.assertRaises(AssertionError):
                validate_record(mutated)

    def test_status_promotion_and_missing_gap_disclosures_are_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        for old, new in (("| `pending` |", "| `approved` |"),
                         ("| `unresolved` |", "| `confirmed` |"),
                         ("| `not_established` |", "| `available_exact_scope` |"),
                         ("NOT VERIFIED", "PASSED"),
                         ("NOT MANUALLY VERIFIED", "VERIFIED"),
                         ("Rejected-state human demo: USER CONFIRMED", "Rejected-state demo passed"),
                         ("S020-7 discrepancy: RESOLVED BY USER CONFIRMATION", "S020-7 unresolved")):
            with self.subTest(old=old), self.assertRaises(AssertionError):
                validate_record(text.replace(old, new))

    def test_rejected_confirmation_preserves_history_and_export_limits(self):
        text = RECORD.read_text(encoding="utf-8")
        for old, new in (("Earlier transcript (history): `clarification_needed`", "History removed"),
                         ("Current user-confirmed state: `rejected`", "Current state: `clarification_needed`"),
                         ("S020-7 rejected-row export contents: NOT MANUALLY VERIFIED", "All rows verified"),
                         ("Chất lượng báo cáo không cụ thể", "Different note")):
            with self.subTest(old=old), self.assertRaises(AssertionError):
                validate_record(text.replace(old, new))

    def test_broken_packet_and_pending_links_are_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        for old in (PACKET_PATH, "[Phase 01 packet](review-dispositions-v1.md)",
                    "[pending-depot-alias](review-dispositions-v1.md#"):
            with self.subTest(link=old), self.assertRaises(AssertionError):
                validate_record(text.replace(old, "broken-link"))

    def test_reference_rule_and_draft_comparison_drift_are_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        for old, new in (("finding: Milestone Conflict", "finding: Status Ambiguous"),
                         (DRAFT_COMPARISONS[-1], "proposal: AC-2 chắc chắn không lỗi")):
            with self.subTest(old=old), self.assertRaises(AssertionError):
                validate_record(text.replace(old, new))

    def test_unsupported_claims_are_rejected(self):
        text = RECORD.read_text(encoding="utf-8")
        for claim in ("These judgments are ground truth.", "Mentor approved.",
                      "Policy is approved.", "Severity confirmed.", "Alias confirmed.",
                      "Accuracy: 100%", "Precision=100", "Recall: 100%",
                      "DEPOT GSM: clean pass", "ANAT Công trường — no-finding",
                      "All demo criteria passed.", "Phase 02 is complete."):
            with self.subTest(claim=claim), self.assertRaises(AssertionError):
                validate_record(text + "\n" + claim)


if __name__ == "__main__":
    unittest.main()
