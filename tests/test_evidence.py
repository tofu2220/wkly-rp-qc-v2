import unittest

from wkly_rp_qc_v2.evidence import (
    build_sources,
    checkpoint_relation,
    period_context,
    select_scope,
)


def row(sheet, source_row, project="SMART PARKING", role="PTSP", plan="deliverable"):
    return {
        "sheet": sheet,
        "source_row": source_row,
        "project": project,
        "role": role,
        "pic": "An",
        "actual": "done",
        "next_plan": plan,
        "issue_risk": "",
        "source_refs": {
            "project": f"B{source_row}",
            "role": f"C{source_row}",
            "pic": f"D{source_row}",
            "actual": f"E{source_row}",
            "next_plan": f"F{source_row}",
            "issue_risk": f"G{source_row}",
        },
        "review_flags": [],
        "reporting_period": "27.07-01.08",
    }


class EvidenceTests(unittest.TestCase):
    def test_selects_original_sheet_strings_alias_and_all_current_roles(self):
        previous = row("BC Tuần 27.07-01.08 ", 7, project="ANAT GSM")
        current_ptsp = row("BC Tuần 03.08-08.08", 7, project="ANAT GSM")
        current_qc = row("BC Tuần 03.08-08.08", 8, project="ANAT GSM", role="QC", plan="")
        scope = select_scope(
            [previous, current_ptsp, current_qc],
            "BC Tuần 27.07-01.08",
            "BC Tuần 03.08-08.08",
            "SMART PARKING",
            "PTSP",
            ["ANAT GSM"],
        )
        self.assertEqual(scope["previous_rows"][0]["sheet"], "BC Tuần 27.07-01.08 ")
        self.assertEqual([r["role"] for r in scope["current_rows"]], ["PTSP", "QC"])
        self.assertEqual(scope["project_aliases"], {"SMART PARKING", "ANAT GSM"})
        self.assertEqual(scope["current_rows"][1]["next_plan"], "")
        sources = build_sources(scope)
        self.assertEqual(list(sources), ["S001", "S002", "S003", "S004"])
        self.assertEqual(sources["S001"]["sheet"], "BC Tuần 03.08-08.08")
        self.assertEqual(sources["S001"]["field"], "actual")

    def test_alias_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "No previous-week plans"):
            select_scope(
                [row("previous", 7, project="ANAT GSM"), row("current", 8)],
                "previous", "current", "SMART PARKING", "PTSP", [],
            )

    def test_duplicate_rows_and_ambiguous_trimmed_sheets_are_rejected(self):
        duplicate = row("previous", 7)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            select_scope(
                [duplicate, dict(duplicate)], "previous", "current", "SMART PARKING", "PTSP", []
            )
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            select_scope(
                [row("previous", 7), row("previous ", 8), row("next", 9)],
                " previous ", "next", "SMART PARKING", "PTSP", [],
            )

    def test_missing_narrative_source_reference_is_rejected(self):
        invalid = row("previous", 7)
        del invalid["source_refs"]["next_plan"]
        with self.assertRaisesRegex(ValueError, "source reference"):
            select_scope([invalid], "previous", "current", "SMART PARKING", "PTSP", [])

    def test_checkpoint_uses_historical_period_and_unknown_for_ambiguous_dates(self):
        scope = select_scope(
            [row("BC Tuần 27.07-01.08", 7), row("BC Tuần 03.08-08.08", 8)],
            "BC Tuần 27.07-01.08", "BC Tuần 03.08-08.08", "SMART PARKING", "PTSP", [],
        )
        context = period_context(scope)
        self.assertEqual(checkpoint_relation("31/07", context), "elapsed")
        self.assertEqual(checkpoint_relation("08/01", context), "unknown")
        self.assertEqual(checkpoint_relation("31/12", context), "unknown")


if __name__ == "__main__":
    unittest.main()
