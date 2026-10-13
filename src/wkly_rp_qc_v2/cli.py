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
    validate_extraction,
)
from .core import AssessmentValidationError, prepare_scope, validate_assessment_payload
from .evidence import load_rows
from .openrouter import OpenRouterError, complete_json
from .policy import load_policy, resolve_policy
from .replay import load_saved_payload, read_replay_provenance
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
    compare.add_argument("--policy", type=Path)
    compare.add_argument("--output", required=True, type=Path)

    replay = commands.add_parser("replay", help="replay saved model responses without calling a provider")
    replay.add_argument("--input", required=True, type=Path)
    replay.add_argument("--previous", required=True)
    replay.add_argument("--current", required=True)
    replay.add_argument("--project", required=True)
    replay.add_argument("--role", required=True)
    replay.add_argument("--alias", action="append", default=[])
    replay.add_argument("--policy", type=Path)
    replay.add_argument("--responses", required=True, type=Path)
    replay.add_argument("--output", required=True, type=Path)
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


def _policy_sha256(registry: JsonDict) -> str:
    canonical = json.dumps(registry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    if isinstance(error, AssessmentValidationError):
        metadata["citation_repairs"] = error.citation_repairs
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


def _run_replay(args: argparse.Namespace) -> int:
    run_dir = None
    stage = "replay configuration/input"
    metadata: JsonDict = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "model": "not called (offline replay)",
        "usage": {},
        "verification_mode": "offline_replay",
        "replayed_from": str(args.responses),
        "role": args.role,
    }
    scope: JsonDict = {"selection_warnings": []}
    try:
        responses_dir = args.responses.resolve(strict=True)
        output_dir = args.output.resolve()
        if output_dir == responses_dir or responses_dir in output_dir.parents:
            raise InputError("Replay output must not be inside the historical response directory")

        run_dir = _new_run_dir(args.output)
        registry = load_policy(args.policy)
        metadata.update({"policy_version": registry["version"], "policy_sha256": _policy_sha256(registry)})
        rows = load_rows(args.input)
        prepared = prepare_scope(
            rows, previous=args.previous, current=args.current, project=args.project, role=args.role, aliases=args.alias
        )
        scope, sources, context = prepared.scope, prepared.sources, prepared.period_context
        metadata.update({
            "input_sha256": _sha256(args.input),
            "project": args.project,
            "requested_aliases": args.alias,
            "previous_sheet": scope["previous_sheet"],
            "current_sheet": scope["current_sheet"],
            "period_context": context,
        })
        _print_scope(scope)

        stage = "replay provenance"
        provenance = read_replay_provenance(args.input, responses_dir, scope, args.role, args.alias)
        metadata.update(provenance)
        scope["selection_warnings"].extend(provenance["provenance_warnings"])

        stage = "replay extraction response"
        extraction_payload = load_saved_payload(responses_dir / "extraction_response.json")
        stage = "replay extraction validation"
        commitments = validate_extraction(extraction_payload, sources)

        stage = "replay assessment response"
        assessment_payload = load_saved_payload(responses_dir / "assessment_response.json")
        stage = "replay assessment validation"
        validated = validate_assessment_payload(assessment_payload, commitments, sources)
        metadata["citation_repairs"] = validated.citation_repairs
        stage = "replay policy resolution"
        policy_result = resolve_policy(validated.assessments, commitments, sources, scope, role=args.role, registry=registry)
        assessments = policy_result.assessments
        metadata["policy_resolution"] = policy_result.resolutions
        metadata["policy_questions"] = [question for item in assessments for question in item["policy_questions"]]

        stage = "replay report writing"
        write_reports(run_dir, scope, sources, commitments, assessments, metadata)
        print(
            f"Replayed: {len(commitments)} commitments, "
            f"{sum(bool(item['findings']) for item in assessments)} affected, "
            f"{sum(len(item['findings']) for item in assessments)} findings"
        )
        print(f"Replay directory: {run_dir}")
        return 0
    except (InputError, OSError, json.JSONDecodeError) as error:
        _failed_run(run_dir, stage, error, "", metadata, scope)
        return 2 if stage in {
            "replay configuration/input", "replay provenance",
            "replay extraction response", "replay assessment response",
        } else 1
    except (ValueError, KeyError, TypeError) as error:
        _failed_run(run_dir, stage, error, "", metadata, scope)
        return 2 if stage in {
            "replay configuration/input", "replay provenance",
            "replay extraction response", "replay assessment response",
        } else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "replay":
        return _run_replay(args)
    if args.command != "compare":
        return 2
    load_dotenv(Path.cwd() / ".env", override=False)
    run_dir = None
    stage = "configuration/input"
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-5-mini")
    metadata: JsonDict = {"created_at_utc": datetime.now(UTC).isoformat(), "model": model, "usage": {}, "role": args.role}
    scope: JsonDict = {"selection_warnings": []}
    try:
        run_dir = _new_run_dir(args.output)
        registry = load_policy(args.policy)
        metadata.update({"policy_version": registry["version"], "policy_sha256": _policy_sha256(registry)})
        rows = load_rows(args.input)
        rules_text = args.rules.read_text(encoding="utf-8")
        prepared = prepare_scope(
            rows, previous=args.previous, current=args.current, project=args.project, role=args.role, aliases=args.alias
        )
        scope, sources, context = prepared.scope, prepared.sources, prepared.period_context
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
        assessment = complete_json(assessment_messages(commitments, scope, sources, context, rules_text), assessment_schema(commitments), api_key=api_key, model=model)
        _response_file(run_dir, "assessment", assessment)
        metadata["usage"]["assessment"] = assessment.get("usage", {})
        stage = "assessment validation"
        validated = validate_assessment_payload(assessment["payload"], commitments, sources)
        metadata["citation_repairs"] = validated.citation_repairs
        stage = "policy resolution"
        policy_result = resolve_policy(validated.assessments, commitments, sources, scope, role=args.role, registry=registry)
        assessments = policy_result.assessments
        metadata["policy_resolution"] = policy_result.resolutions
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
