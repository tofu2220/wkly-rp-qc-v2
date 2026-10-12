# Weekly Report Quality Checker (local prototype)

Compare commitments in one week's `Next Plan` with the next week's reported `Actual`, `Issue/Risk`, and `Next Plan`. This checks reporting quality and traceability; it does not verify actual work completion or project success.

## Run

Create a local `.env` in the repository root (the file is ignored by Git):

```bash
OPENROUTER_API_KEY="..."
OPENROUTER_MODEL="openai/gpt-5-mini"
```

The CLI loads `.env` automatically. Variables already exported in the shell take precedence. The CLI never prints or saves the key. Then run:

```bash
uv run wkly-rp-qc-v2 compare \
  --input download/1-raw/normalized_rows.json \
  --rules download/5-rules/reporting_quality_rules.md \
  --previous "BC Tuần 31.08 - 05.09" \
  --current "BC Tuần 07.09 - 12.09" \
  --project "SMART PARKING" \
  --role "PTSP" \
  --output results/smart-parking
```

Use `--alias "ANAT GSM"` when the selected source rows use a known alternate project label. Aliases are explicit and exact; the CLI does not infer project-name variants. Sheet names are matched exactly first, then by a unique trimmed spelling. Ambiguous or missing sheets stop the run. The selected previous week's role supplies plans; current-week evidence is searched across all roles for the chosen project.

The input must be the normalized JSON envelope with a `rows` array. Each row retains `sheet`, `source_row`, project/role/PIC fields, `actual`, `next_plan`, `issue_risk`, and matching `source_refs`. The checked-in `download/1-raw/normalized_rows.json` is the intended prototype input.

## Results and failure behavior

Assessment evidence follows a week-specific contract: extracted commitment `plan_evidence` comes from the previous week; assessment `update_evidence` cites current-week sources only and may be empty; finding evidence may compare previous-week plan quotations with current-week reporting. Every non-`Missing Closure` finding must cite at least one current-week source. `Missing Closure` cites the previous-week plan, because absence itself has no quotation. Source IDs are resolved using their supplied week metadata, not their names.

Each invocation creates a unique UTC timestamped child under `--output` and prints its path. Successful runs contain:

- `mentor_review.md` — Vietnamese review with source locations.
- `comparison.json` and `comparison.csv` — structured, traceable results. CSV uses UTF-8 with BOM.
- `run_notes.md` — model, input/rules hashes, usage, historical-period uncertainty, and unresolved policy questions.
- `extraction_response.json` and `assessment_response.json` — credential-redacted provider diagnostics.

Failures preserve `failure.json`, `run_notes.md`, and any response received so far. They do not publish a successful review/comparison. Provider failures are recorded at the request stage with provider diagnostics; local validation failures identify the validation stage and retain the model response for diagnosis. Exit status is 2 for configuration/input errors and 1 for provider, validation, or output failures.

Checkpoint interpretation uses the reporting periods in the historical sheets, never today's date. Without supported year context, ambiguous and year-crossing date comparisons remain unknown. Reference examples and draft severities include proposed and unresolved decisions; generated comparisons require human semantic review. Structural validation confirms exact quotations and source references, not extraction completeness or semantic accuracy.

## Offline verification (2026-10-09)

Selection was checked against the actual normalized export:

| Requested project | Previous sheet resolved | Current sheet resolved | Previous PTSP rows | Current project rows / roles | Nonempty evidence sources |
|---|---|---|---:|---|---:|
| SMART PARKING | `BC Tuần 31.08 - 05.09` | `BC Tuần 07.09 - 12.09` | 1 | 9 rows / 8 roles | 20 |
| DEPOT GSM (`ANAT GSM` alias) | `BC Tuần 31.08 - 05.09` | `BC Tuần 07.09 - 12.09` | 1 | 8 rows / 8 roles | 19 |
| ANAT Công trường | `BC Tuần 27.07 - 01.08 ` (source name includes trailing whitespace) | `BC Tuần 03.08 - 07.08` | 1 | 5 rows / 5 roles | 11 |

The normalized export has null narrative values for blank workbook cells; selection preserves them as blank candidate fields and does not create evidence from them. Reference counts (10, 11, and 3) are review aids, not extraction constraints. Live extraction/comparison and semantic completeness review are blocked because `OPENROUTER_API_KEY` was not configured during verification. No generated model findings have been treated as ground truth.

Verification results: `.venv/bin/python -m unittest discover -s tests -v` — 24 tests passed; `ruff check src tests` — passed; `basedpyright src` — 0 errors and 230 warnings (dynamic JSON/dictionary typing and unused-result diagnostics); `uv run wkly-rp-qc-v2 --help` — installed entry point works.

## Evidence-contract verification (2026-10-12)

Offline regression coverage confirms that comparison findings can cite both weeks, while update citations remain current-week-only. The full offline suite passed 29 tests; Ruff passed; Basedpyright reported 0 errors and 232 warnings; and the CLI `--help` entry point ran successfully. The saved 2026-10-12 failure run was unavailable for offline replay, so no historical response was modified or reinterpreted. These checks establish structural and quotation validation, not semantic accuracy.
