import hashlib
import json
import re
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RESPONSE_FILES = ("extraction_response.json", "assessment_response.json")


def _note_value(notes: str, label: str) -> str | None:
    prefix = f"- {label}:"
    values = []
    for line in notes.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix):]
            values.append(value.removeprefix(" "))
    distinct = list(dict.fromkeys(values))
    if len(distinct) > 1:
        raise ValueError(f"Conflicting historical {label} entries in run notes")
    return distinct[0] if distinct else None


def _reporting_pair(notes: str) -> tuple[str, str]:
    value = _note_value(notes, "Phạm vi")
    if value is None or value.count(" → ") != 1:
        raise ValueError("Missing or malformed historical reporting period pair")
    previous, current = value.split(" → ")
    if not previous or not current:
        raise ValueError("Missing or malformed historical reporting period pair")
    return previous, current


def _verify_comparison_scope(responses_dir: Path, scope: JsonDict, role: str, aliases: list[str]) -> tuple[bool, list[str]]:
    path = responses_dir / "comparison.json"
    if not path.exists():
        return False, []
    try:
        comparison = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid historical comparison.json: {error}") from error
    if not isinstance(comparison, dict):
        raise ValueError("Historical comparison.json must contain an object")  # noqa: TRY004 — malformed persisted input is a replay validation error
    metadata = comparison.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Historical comparison metadata must be an object")  # noqa: TRY004 — malformed persisted input is a replay validation error

    warnings = []
    expected = {"project": scope["project"], "role": role, "requested_aliases": aliases}
    verified = True
    for key, value in expected.items():
        if key not in metadata:
            verified = False
            continue
        historical = metadata[key]
        if key == "requested_aliases":
            if not isinstance(historical, list) or any(not isinstance(item, str) for item in historical):
                raise ValueError("Historical requested_aliases must be a list of strings")
            matches = set(historical) == set(value)
        else:
            if not isinstance(historical, str):
                raise ValueError(f"Historical {key} must be a string")
            matches = historical == value
        if not matches:
            raise ValueError(f"Requested {key} conflicts with historical comparison metadata")
    if not verified:
        warnings.append("Historical comparison metadata does not fully verify project, role, and aliases")
    return verified, warnings


def read_replay_provenance(
    input_path: Path, responses_dir: Path, scope: JsonDict, role: str, aliases: list[str]
) -> JsonDict:
    notes_path = responses_dir / "run_notes.md"
    notes = notes_path.read_text(encoding="utf-8")
    historical_input_hash = _note_value(notes, "SHA-256 input")
    if historical_input_hash is None or SHA256_RE.fullmatch(historical_input_hash) is None:
        raise ValueError("Missing or malformed historical input SHA-256 in run_notes.md")
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if input_hash != historical_input_hash:
        raise ValueError("Normalized input SHA-256 does not match the historical run")

    previous, current = _reporting_pair(notes)
    if previous != scope["previous_sheet"] or current != scope["current_sheet"]:
        raise ValueError("Selected reporting periods do not match the historical run")

    historical_rules_hash = _note_value(notes, "SHA-256 rules")
    provenance_warnings = []
    if historical_rules_hash is not None and SHA256_RE.fullmatch(historical_rules_hash) is None:
        historical_rules_hash = None
        provenance_warnings.append("Historical rules SHA-256 is malformed; rules provenance is unavailable")
    elif historical_rules_hash is None:
        provenance_warnings.append("Historical rules SHA-256 is missing; rules provenance is unavailable")

    response_hashes = {}
    for filename in RESPONSE_FILES:
        response_hashes[filename] = hashlib.sha256((responses_dir / filename).read_bytes()).hexdigest()

    scope_verified, scope_warnings = _verify_comparison_scope(responses_dir, scope, role, aliases)
    if not scope_verified:
        provenance_warnings.append(
            "Historical notes do not record project/role/aliases; scope is selected now and checked against evidence, not independently confirmed"
        )
    provenance_warnings.extend(scope_warnings)
    return {
        "verification_mode": "offline_replay",
        "replayed_from": str(responses_dir.resolve()),
        "input_sha256": input_hash,
        "response_sha256": response_hashes,
        "historical_rules_sha256": historical_rules_hash,
        "previous_sheet": previous,
        "current_sheet": current,
        "project": scope["project"],
        "role": role,
        "requested_aliases": aliases,
        "scope_provenance": "verified" if scope_verified else "legacy_partial",
        "provenance_warnings": provenance_warnings,
    }


def load_saved_payload(path: Path) -> JsonDict:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid saved response envelope: {error}") from error
    if not isinstance(envelope, dict):
        raise ValueError("Saved response envelope must contain an object")  # noqa: TRY004 — malformed persisted input is a replay validation error
    raw = envelope.get("raw_response")
    if not isinstance(raw, dict):
        raise ValueError("Saved response envelope is missing raw_response")  # noqa: TRY004 — malformed persisted input is a replay validation error
    if raw.get("error") is not None:
        raise ValueError("Saved provider error is not a model response")
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Saved provider response is missing choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("Saved provider response is missing message")  # noqa: TRY004 — malformed persisted input is a replay validation error
    if message.get("refusal"):
        raise ValueError("Saved provider response is a refusal")
    if choice.get("finish_reason") == "length":
        raise ValueError("Saved provider response is truncated")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Saved provider response is missing content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid saved response payload JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Saved response payload must contain an object")  # noqa: TRY004 — malformed persisted input is a replay validation error
    return payload
