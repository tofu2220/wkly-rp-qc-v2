"""Negative tests for fail-closed archive and release evidence handling."""

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_demo_outcome
import test_disposition_packet


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


if __name__ == "__main__":
    unittest.main()
