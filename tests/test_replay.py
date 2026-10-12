import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wkly_rp_qc_v2.replay import load_saved_payload, read_replay_provenance

PREVIOUS = "BC Tuần 31.08 - 05.09"
CURRENT = "BC Tuần 07.09 - 12.09"
INPUT = b'{"rows": []}\n'
RULES_HASH = "a" * 64
SCOPE = {
    "previous_sheet": PREVIOUS,
    "current_sheet": CURRENT,
    "project": "SMART PARKING",
    "project_aliases": {"SMART PARKING"},
    "selection_warnings": [],
}


def response(payload, *, finish_reason="stop", refusal=None):
    message = {"content": json.dumps(payload, ensure_ascii=False)}
    if refusal is not None:
        message["refusal"] = refusal
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def run_files(root, *, notes=None, input_bytes=INPUT, extraction=None, assessment=None, comparison=None):
    input_path = root / "normalized_rows.json"
    responses = root / "historical-run"
    responses.mkdir()
    input_path.write_bytes(input_bytes)
    notes = notes or (
        f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n"
        f"- SHA-256 rules: {RULES_HASH}\n"
        f"- Phạm vi: {PREVIOUS} → {CURRENT}\n"
    )
    (responses / "run_notes.md").write_text(notes, encoding="utf-8")
    for name, payload in (("extraction_response.json", extraction), ("assessment_response.json", assessment)):
        (responses / name).write_text(json.dumps({"raw_response": response(payload), "usage": {}}), encoding="utf-8")
    if comparison is not None:
        (responses / "comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
    return input_path, responses


class ReplayInputTests(unittest.TestCase):
    def test_load_saved_payload_reads_provider_diagnostic_envelope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "assessment_response.json"
            payload = {"assessments": [{"plan_id": "P001"}]}
            path.write_text(json.dumps({"raw_response": response(payload), "usage": {}}), encoding="utf-8")

            self.assertEqual(load_saved_payload(path), payload)

    def test_load_saved_payload_rejects_missing_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(OSError):
                load_saved_payload(root / "missing.json")

            invalid_outer = root / "outer.json"
            invalid_outer.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "envelope"):
                load_saved_payload(invalid_outer)

            invalid_content = root / "content.json"
            invalid_content.write_text(json.dumps({"raw_response": response({}) | {"choices": [{"message": {"content": "{"}}]}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "payload JSON"):
                load_saved_payload(invalid_content)

            non_object = root / "array.json"
            non_object.write_text(json.dumps({"raw_response": response([1, 2])}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "object"):
                load_saved_payload(non_object)

    def test_load_saved_payload_rejects_unusable_provider_responses(self):
        cases = (
            ({"error": {"code": 402}}, "provider error"),
            ({"choices": []}, "choices"),
            ({"choices": [{"message": {}}]}, "content"),
            ({"choices": [{"message": {"content": "{}", "refusal": "refused"}}]}, "refusal"),
            ({"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}, "truncated"),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "response.json"
            for raw, expected in cases:
                with self.subTest(expected=expected):
                    path.write_text(json.dumps({"raw_response": raw}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected):
                        load_saved_payload(path)

    def test_provenance_verifies_input_hash_reporting_pair_and_response_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, responses = run_files(root, extraction={"commitments": []}, assessment={"assessments": []})

            result = read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

            self.assertEqual(result["input_sha256"], hashlib.sha256(INPUT).hexdigest())
            self.assertEqual(result["response_sha256"]["extraction_response.json"], hashlib.sha256((responses / "extraction_response.json").read_bytes()).hexdigest())
            self.assertEqual(result["response_sha256"]["assessment_response.json"], hashlib.sha256((responses / "assessment_response.json").read_bytes()).hexdigest())
            self.assertEqual(result["historical_rules_sha256"], RULES_HASH)
            self.assertEqual(result["previous_sheet"], PREVIOUS)
            self.assertEqual(result["current_sheet"], CURRENT)
            self.assertEqual(result["replayed_from"], str(responses.resolve()))
            self.assertEqual(result["scope_provenance"], "legacy_partial")
            self.assertTrue(any("project/role/alias" in warning for warning in result["provenance_warnings"]))

    def test_provenance_marks_fully_verified_explicit_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            comparison = {"metadata": {
                "project": "SMART PARKING", "role": "PTSP", "requested_aliases": [],
            }}
            input_path, responses = run_files(
                root, extraction={"commitments": []}, assessment={"assessments": []}, comparison=comparison,
            )

            result = read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

            self.assertEqual(result["scope_provenance"], "verified")
            self.assertEqual(result["provenance_warnings"], [])

    def test_provenance_checks_each_explicit_scope_field(self):
        cases = (
            {"project": "SMART PARKING", "role": "QC", "requested_aliases": []},
            {"project": "SMART PARKING", "role": "PTSP", "requested_aliases": ["ANAT GSM"]},
            {"project": 7, "role": "PTSP", "requested_aliases": []},
            {"project": "SMART PARKING", "role": "PTSP", "requested_aliases": "ANAT GSM"},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as temp:
                input_path, responses = run_files(
                    Path(temp), extraction={"commitments": []}, assessment={"assessments": []},
                    comparison={"metadata": metadata},
                )
                with self.assertRaises(ValueError):
                    read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

    def test_provenance_treats_missing_rules_hash_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            input_path, responses = run_files(
                Path(temp),
                notes=f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
                extraction={"commitments": []}, assessment={"assessments": []},
            )

            result = read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

            self.assertIsNone(result["historical_rules_sha256"])
            self.assertTrue(any("rules SHA-256 is missing" in warning for warning in result["provenance_warnings"]))

    def test_provenance_rejects_hash_and_reporting_pair_mismatches(self):
        cases = (
            (f"- SHA-256 input: {'b' * 64}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n", INPUT, "does not match"),
            (f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: Other → {CURRENT}\n", INPUT, "period"),
            (f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → Other\n", INPUT, "period"),
        )
        for notes, input_bytes, expected in cases:
            with self.subTest(expected=expected, notes=notes), tempfile.TemporaryDirectory() as temp:
                input_path, responses = run_files(
                    Path(temp), notes=notes, input_bytes=input_bytes,
                    extraction={"commitments": []}, assessment={"assessments": []},
                )
                with self.assertRaisesRegex(ValueError, expected):
                    read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

    def test_provenance_rejects_missing_malformed_or_conflicting_note_fields(self):
        invalid_notes = (
            f"- SHA-256 rules: {RULES_HASH}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
            f"- SHA-256 input: invalid\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
            f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n- Phạm vi: {CURRENT} → {PREVIOUS}\n",
        )
        for notes in invalid_notes:
            with self.subTest(notes=notes), tempfile.TemporaryDirectory() as temp:
                input_path, responses = run_files(
                    Path(temp), notes=notes, extraction={"commitments": []}, assessment={"assessments": []},
                )
                with self.assertRaises(ValueError):
                    read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

    def test_provenance_rejects_missing_notes_or_response_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / "input.json"
            input_path.write_bytes(INPUT)
            responses = root / "responses"
            responses.mkdir()
            with self.assertRaises(OSError):
                read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

            (responses / "run_notes.md").write_text(
                f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
                encoding="utf-8",
            )
            (responses / "extraction_response.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(OSError):
                read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

    def test_provenance_checks_explicit_scope_and_optional_rules_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path, responses = run_files(
                root, notes=f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
                extraction={"commitments": []}, assessment={"assessments": []},
                comparison={"metadata": {"project": "OTHER", "role": "PTSP", "requested_aliases": []}},
            )
            with self.assertRaisesRegex(ValueError, "project"):
                read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])

        with tempfile.TemporaryDirectory() as temp:
            input_path, responses = run_files(
                Path(temp), notes=f"- SHA-256 input: {hashlib.sha256(INPUT).hexdigest()}\n- Phạm vi: {PREVIOUS} → {CURRENT}\n",
                extraction={"commitments": []}, assessment={"assessments": []},
            )
            (responses / "comparison.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "comparison"):
                read_replay_provenance(input_path, responses, SCOPE, "PTSP", [])


if __name__ == "__main__":
    unittest.main()
