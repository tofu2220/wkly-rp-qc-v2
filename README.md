# Weekly Report Quality Checker

A local application for comparing a previous reporting period's plans with the next period's reported results, issues and follow-up plans. Findings retain source quotations and spreadsheet references for human review. The application checks reporting quality; it does not independently verify that work was completed.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Node.js/npm to build the React frontend. The built application needs only the local Python server, not a separate frontend server or CDN.
- Chromium for browser tests; configure a compatible system browser or provision the locked Playwright browser.

## Run the application

```bash
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked wkly-rp-qc-web
```

Open **http://127.0.0.1:8000/app/** for the React interface. `/` retains the older server-rendered interface. Stop the server with `Ctrl+C`.

The server binds to localhost. By default, SQLite and uploaded workbook versions are stored in `~/.local/share/wkly-rp-qc-v2/`. To select other local storage and historical replay directories:

```bash
uv run --locked wkly-rp-qc-web \
  --data-dir /absolute/path/to/local-store \
  --replay-root /absolute/path/to/saved-runs
```

Uploading an `.xlsx` workbook stores and previews its source; it does not start analysis. The workbook overview separates source issues from missing-year context, offers a searchable sheet list, and retains original source metadata. Exact project selection supports viewing multiple reporting sheets without merging differently spelled project labels.

The current web analysis/review flow uses exact-scope saved responses, not fresh provider requests. A missing eligible replay is unavailable, not a successful analysis with no findings. Mapping and explicit-alias editing retain their existing server-side forms and source references.

## CLI comparison and offline replay

The CLI can request a fresh model comparison. Create a local root `.env` with your own credentials; never commit it:

```dotenv
OPENROUTER_API_KEY=your_api_key_here
OPENROUTER_MODEL=provider/model-id
```

`compare` loads `.env` from the current working directory; exported environment variables take precedence. Keep the key on the backend. A live comparison may incur provider charges.

```bash
uv run --locked wkly-rp-qc-v2 compare \
  --input private-data/normalized_rows.json \
  --rules private-data/reporting_quality_rules.md \
  --previous "Previous reporting sheet" \
  --current "Current reporting sheet" \
  --project "EXACT PROJECT LABEL" \
  --role "EXACT ROLE LABEL" \
  --output results/comparison
```

Input is a normalized JSON envelope containing a `rows` array. Rows retain sheet, source-row, exact project/role, PIC, narrative fields and `source_refs`. The selected previous role supplies plans; current evidence is searched across the project's roles. Aliases are explicit, never inferred. Use `--help` for available options.

Offline replay requires the normalized input and a matching historical run containing `run_notes.md`, `extraction_response.json` and `assessment_response.json`:

```bash
uv run --locked wkly-rp-qc-v2 replay \
  --input private-data/normalized_rows.json \
  --previous "Previous reporting sheet" \
  --current "Current reporting sheet" \
  --project "EXACT PROJECT LABEL" \
  --role "EXACT ROLE LABEL" \
  --responses /absolute/path/to/matching-historical-run \
  --output results/offline-review
```

Replay checks exact input/scope provenance and local schema/quotation rules without loading provider credentials or calling a model. Successful runs write a unique output directory with review Markdown, comparison JSON/CSV and run notes. Invalid responses remain failures, with diagnostics rather than successful reports. Structural validation does not establish semantic accuracy.

## Policy and human review

The packaged default policy contains no confirmed severity decisions. Proposed model findings, effective policy and human review remain distinct. Explicit policy registries require decision provenance; accepting a finding does not approve a general severity rule, alias or actual task completion. Quotes retain their original text and source period; missing follow-up means missing reporting, not proof work was not done.

## Verification

After installing locked dependencies, the self-contained core/transport regression is:

```bash
uv run --locked pytest -q \
  tests/test_assessment.py tests/test_cli.py tests/test_core.py \
  tests/test_evidence.py tests/test_openrouter.py tests/test_policy.py \
  tests/test_policy_report.py tests/test_replay.py tests/test_replay_catalog.py \
  tests/test_store.py tests/test_web.py tests/test_web_api.py \
  tests/test_web_jobs.py tests/test_web_review.py tests/test_web_export.py \
  tests/test_workbook_tools.py
```

Workbook-preview and tool-return browser tests generate isolated technical workbooks:

```bash
uv run --locked python tests/run_frontend_checks.py --pytest \
  tests/browser/test_workbook_preview.py tests/browser/test_workbook_tools.py \
  --browser chromium --tracing retain-on-failure
```

These commands are focused checks, **not** the full private-data release gate. Original business workbooks, frozen business-review/planning records, normalized exports and saved model runs are intentionally not distributed in Git. Tests that verify those records or installed delivery with that inventory require the separately provided local fixtures. The strict full gate remains fail-closed when required inputs are missing; it must not be made green by skipping or weakening acceptance assertions.

Maintainers with that complete private inventory can run:

```bash
DISPOSITION_EXPECT_STAGE=review_recorded \
DISPOSITION_REQUIRE_CONFIRMATION=1 \
uv run --locked python tests/run_frontend_checks.py --release-all
```

Fixture paths and supported local overrides are defined in `tests/run_release_checks.py` and the relevant acceptance tests. Do not use persistent user data for tests. Runtime tests block provider/outbound attempts and use isolated temporary storage.

## Repository contents

Application source, frontend source/lock files, tests and build metadata are published. Credentials, machine-local environments, AI/GSD plans and configuration, private documentation/data, caches and generated artifacts remain local under `.gitignore`. Build the production frontend from source; generated bundles are not committed.
