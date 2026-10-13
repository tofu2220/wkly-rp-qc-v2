import copy
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from wkly_rp_qc_v2.core import prepare_scope
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


if __name__ == "__main__":
    unittest.main()
