import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .assessment import (
    assessment_messages,
    assessment_schema,
    extraction_messages,
    extraction_schema,
    restore_assessment_quotes,
    validate_assessment,
    validate_extraction,
)
from .evidence import build_sources, load_rows, period_context, select_scope
from .openrouter import OpenRouterError, complete_json
from .report import write_reports, write_run_notes

JsonDict = dict[str, Any]


class InputError(ValueError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wkly-rp-qc-v2", description="Local weekly-report quality comparison")
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="compare previous-week plans with following-week reports")
    compare.add_argument("--input", required=True, type=Path)
    compare.add_argument("--rules", required=True, type=Path)
    compare.add_argument("--previous", required=True)
    compare.add_argument("--current", required=True)
    compare.add_argument("--project", required=True)
    compare.add_argument("--role", required=True)
    compare.add_argument("--alias", action="append", default=[])
    compare.add_argument("--output", required=True, type=Path)
    return parser


def _new_run_dir(base: Path) -> Path:
    if base.exists() and not base.is_dir():
        raise InputError(f"Output base is not a directory: {base}")
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = base / stamp
    suffix = 0
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = base / f"{stamp}-{suffix}"


def _json_file(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _print_scope(scope: JsonDict) -> None:
    print(f"Selected previous: {scope['previous_sheet']} → current: {scope['current_sheet']}")
    print(f"Project: {scope['project']} | previous role: selected role only | current roles: {', '.join(sorted({row['role'] for row in scope['current_rows']}))}")
    print(f"Selected rows: {len(scope['previous_rows'])} previous plans; {len(scope['current_rows'])} current project rows")
    for week, rows in (("Previous", scope["previous_rows"]), ("Current", scope["current_rows"])):
        for row in rows:
            preview = row["next_plan"] if week == "Previous" else " ".join(filter(None, (row["actual"], row["issue_risk"], row["next_plan"])))
            preview = " ".join(preview.split())
            if len(preview) > 160:
                preview = preview[:157] + "..."
            print(f"  {week} row {row['source_row']} | {row['role']} | {row['pic']}: {preview}")
    for warning in scope["selection_warnings"]:
        print(f"  Selection warning: {warning}")


def _response_file(run_dir: Path, stage: str, result: JsonDict) -> None:
    _json_file(run_dir / f"{stage}_response.json", {"raw_response": result.get("raw_response"), "usage": result.get("usage", {})})


def _failed_run(run_dir: Path | None, stage: str, error: Exception, api_key: str, metadata: JsonDict, scope: JsonDict) -> None:
    message = str(error).replace(api_key, "[REDACTED]") if api_key else str(error)
    failure = {"stage": stage, "error_type": type(error).__name__, "error": message}
    if isinstance(error, OpenRouterError) and error.raw_response is not None and run_dir is not None:
        response_stage = "assessment" if stage in ("assessment request", "assessment validation") else "extraction"
        _json_file(run_dir / f"{response_stage}_response.json", {"raw_response": error.raw_response, "usage": error.usage})
    if run_dir is not None:
        for name in ("mentor_review.md", "comparison.json", "comparison.csv"):
            (run_dir / name).unlink(missing_ok=True)
        _json_file(run_dir / "failure.json", failure)
        write_run_notes(run_dir, metadata, scope, [], failure)
        print(f"Run directory: {run_dir}")
    print(f"FAILED ({stage}): {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "compare":
        return 2
    load_dotenv(Path.cwd() / ".env", override=False)
    run_dir = None
    stage = "configuration/input"
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-5-mini")
    metadata: JsonDict = {"created_at_utc": datetime.now(UTC).isoformat(), "model": model, "usage": {}}
    scope: JsonDict = {"selection_warnings": []}
    try:
        run_dir = _new_run_dir(args.output)
        rows = load_rows(args.input)
        rules_text = args.rules.read_text(encoding="utf-8")
        scope = select_scope(rows, args.previous, args.current, args.project, args.role, args.alias)
        sources = build_sources(scope)
        context = period_context(scope)
        metadata.update({
            "input_sha256": _sha256(args.input), "rules_sha256": _sha256(args.rules),
            "project": args.project, "requested_aliases": args.alias,
            "previous_sheet": scope["previous_sheet"], "current_sheet": scope["current_sheet"],
            "period_context": context,
        })
        _print_scope(scope)
        if not api_key:
            raise InputError("OPENROUTER_API_KEY is not set")

        stage = "extraction request"
        extraction = complete_json(extraction_messages(scope, sources, context), extraction_schema(), api_key=api_key, model=model)
        _response_file(run_dir, "extraction", extraction)
        metadata["usage"]["extraction"] = extraction.get("usage", {})
        stage = "extraction validation"
        commitments = validate_extraction(extraction["payload"], sources)

        stage = "assessment request"
        assessment = complete_json(assessment_messages(commitments, scope, sources, context, rules_text), assessment_schema(), api_key=api_key, model=model)
        _response_file(run_dir, "assessment", assessment)
        metadata["usage"]["assessment"] = assessment.get("usage", {})
        stage = "assessment validation"
        assessment_payload, citation_repairs = restore_assessment_quotes(assessment["payload"], sources)
        metadata["citation_repairs"] = citation_repairs
        assessments = validate_assessment(assessment_payload, commitments, sources)
        metadata["policy_questions"] = [question for item in assessments for question in item["policy_questions"]]
        stage = "report writing"
        write_reports(run_dir, scope, sources, commitments, assessments, metadata)
        print(f"Completed: {len(commitments)} commitments, {sum(bool(item['findings']) for item in assessments)} affected, {sum(len(item['findings']) for item in assessments)} findings")
        print(f"Run directory: {run_dir}")
        return 0
    except (InputError, OSError, json.JSONDecodeError) as error:
        _failed_run(run_dir, stage, error, api_key, metadata, scope)
        return 2 if stage == "configuration/input" else 1
    except (OpenRouterError, ValueError, KeyError, TypeError) as error:
        _failed_run(run_dir, stage, error, api_key, metadata, scope)
        return 2 if stage == "configuration/input" else 1
    except Exception as error:  # noqa: BLE001 — retain diagnostics for unexpected run failures
        _failed_run(run_dir, stage, error, api_key, metadata, scope)
        return 1
