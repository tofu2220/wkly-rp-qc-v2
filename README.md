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

## Offline replay (no API key or credits)

Replay validates saved model responses against the normalized source without making a model request:

```bash
uv run wkly-rp-qc-v2 replay \
  --input download/1-raw/normalized_rows.json \
  --previous "BC Tuần 31.08 - 05.09" \
  --current "BC Tuần 07.09 - 12.09" \
  --project "SMART PARKING" \
  --role "PTSP" \
  --responses results/smart-parking/20261012T031440747089Z \
  --output results/offline-review
```

The response directory must contain `run_notes.md`, `extraction_response.json`, and `assessment_response.json` from the same historical run. Replay verifies the normalized-input hash and reporting-period pair, checks response hashes and quotations, applies the current local validators and quote-restoration rule, then writes a unique child under `--output`. It does not require `OPENROUTER_API_KEY`, load `.env`, or use network access; `--rules` is not needed.

The output contains `mentor_review.md`, `comparison.json`, `comparison.csv`, and `run_notes.md`. They are labeled **Replay offline — response lịch sử, không gọi AI mới**. `run_notes.md` records response hashes, historical rules hash when present, scope provenance warnings, and quote repairs. Older runs do not record project/role/alias completely, so their scope may be marked `legacy_partial`; replay does not infer missing labels from report text. If your alias or role differs from the original run's explicit comparison metadata, replay stops for review.

An invalid saved response remains invalid: replay preserves failure diagnostics and writes no successful comparison files. It never filters extra assessment objects, fills missing data, or regrades model findings. Input/provenance and saved-response errors exit `2`; local extraction/assessment validation or output errors exit `1`. A successful replay confirms structural/source validity for those historical responses only; it does not evaluate new reports or prove semantic correctness.

## Local browser review

Install the locked Python and frontend dependencies, then build the production assets served by FastAPI:

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
```

The app runs as one local FastAPI server; there is no production Node server or CDN dependency. Start it with explicit storage and replay paths:

```bash
uv run --locked wkly-rp-qc-web \
  --data-dir "$HOME/.local/share/wkly-rp-qc-v2" \
  --replay-root "$PWD/results"
```

Open `http://127.0.0.1:8000/app/`. The server binds only to localhost. The SQLite database and uploaded workbook versions live in the selected `--data-dir`; historical replay runs are read from `--replay-root`. Keep those paths local and backed up as appropriate. Stop the server with Ctrl+C.

Uploading a workbook only imports and previews it. Review mapping, draft/period warnings, project aliases, and overlapping sources before selecting a scope. A replay is offered only when its normalized-input hash and scope match; no match is shown as **no AI result available**, never as a clean pass. This release runs offline replay only: it does not load provider credentials or call a model.

To run the strict release gate, keep the required local workbook, normalized export, exact saved replay, and frozen disposition/reference inputs available at their expected paths. Missing inputs fail preflight; required tests may not be skipped. The command rebuilds the frontend, validates the emitted asset graph, then records exact pytest and unittest execution evidence:

```bash
DISPOSITION_EXPECT_STAGE=review_recorded \
DISPOSITION_REQUIRE_CONFIRMATION=1 \
uv run --locked python tests/run_frontend_checks.py --release-all
```

Python browser-test setup uses the locked Playwright dependency. Provision Chromium with `uv run --locked playwright install chromium` where the host does not provide a compatible browser. Runtime browser/server checks remain offline and use isolated temporary data. Acceptance workbooks, frozen records, and replay files are local test fixtures: they are not bundled into wheel or sdist artifacts, and deployments must not depend on fixture files being present.

The strict release gate requires the local acceptance fixture inventory below. The default paths are relative to the repository root; `WKLY_RP_QC_FIXTURE_ROOT` relocates that inventory, while the individual `WKLY_RP_QC_*` variables can override a specific path. Missing files, a wrong disposition stage/confirmation, empty or mismatched test execution, failures, skips, and xfails all make the gate fail. Do not point runtime checks at a PM's persistent application data directory.

| Input | Default repository path |
|---|---|
| Source workbook | `docs/Khối SMC_Báo cáo tuần chi tiết.xlsx` |
| Normalized source | `download/1-raw/normalized_rows.json` |
| Exact saved replay | `results/smart-parking/20261012T031440747089Z/` (including `run_notes.md`, `extraction_response.json`, and `assessment_response.json`) |
| Frozen disposition packet and candidate/reference records | `docs/phase-4-evaluation/review-dispositions-v1.md`, `candidate-cases.json`, `business-review-form.md`, and `gpt-6.1-draft-review.md` |
| Other frozen packet inputs | Exact inventory in `tests/test_disposition_packet.py::EXPECTED_HASHES` |

The runtime/browser and installed-package checks use temporary data roots and separate provider/backend and browser outbound-request guards. Release evidence is written under a temporary `wkly-release-checks-*` directory and reports the exact collected/executed pytest node IDs, outcomes, unittest IDs/count, and raw logs. Do not round counts or treat a partial green sub-run as the release gate.

## Severity policy and provenance

Both `compare` and `replay` accept an optional `--policy PATH`. If omitted, the packaged `demo-policy-v1` registry is used. It contains the five supported rule versions and **no confirmed decisions**. An invalid explicit policy stops the run; the CLI does not silently fall back to the packaged policy.

`assessment_response.json` retains the original model response. `comparison.json` contains effective assessments and a `metadata.policy_resolution` audit; `comparison.csv`, `mentor_review.md`, and `run_notes.md` distinguish effective policy values from model values and show the decision source. With the default policy, a non-null model severity remains at its model value but is labeled `proposed`; a null severity remains `unresolved`. `requires_review` is recalculated on the effective copy, and the original assessment is strictly validated before policy is applied.

To use a documented approval, provide a versioned JSON registry with a decision record for the rule or exact case. A `confirmed` decision must use `basis: "explicit_approval"` and include nonempty `source_reference`, `approved_by`, and an ISO `approved_at` date or datetime. These fields record the operator-supplied provenance; the application does not independently verify the named person's authority. Only enter a real confirmed decision when the corresponding approval is actually documented. A PM accepting an individual finding is not approval of a rule's general severity.

Case decisions match the exact project, role, previous/current sheet names, and an exact accepted plan quote. They do not use run-local `plan_id`, fuzzy text matching, `[M]` labels, or filenames as approval evidence. If an exact quote identifies multiple commitments in the selected run, policy resolution fails as ambiguous rather than applying the decision to both.

Example of supplying a separate local registry:

```bash
uv run wkly-rp-qc-v2 replay \
  --input download/1-raw/normalized_rows.json \
  --previous "BC Tuần 31.08 - 05.09" \
  --current "BC Tuần 07.09 - 12.09" \
  --project "SMART PARKING" \
  --role "PTSP" \
  --responses results/smart-parking/20261012T031440747089Z \
  --policy ./local-policy.json \
  --output results/offline-review
```

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
