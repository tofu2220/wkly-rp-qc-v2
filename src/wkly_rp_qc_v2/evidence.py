import json
import re
from pathlib import Path


NARRATIVE_FIELDS = ("actual", "issue_risk", "next_plan")
PERIOD_RE = re.compile(r"(\d{1,2})[./-](\d{1,2})\s*[-–]\s*(\d{1,2})[./-](\d{1,2})")


def load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("Input must be an object containing a rows list")
    rows = data["rows"]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Every normalized row must be an object")
    return rows


def _period(value: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    match = PERIOD_RE.search(value)
    if not match:
        return None
    start_day, start_month, end_day, end_month = map(int, match.groups())
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12 and 1 <= start_day <= 31 and 1 <= end_day <= 31):
        return None
    return (start_month, start_day), (end_month, end_day)


def _resolve_sheet(rows: list[dict], requested: str) -> str:
    names = list(dict.fromkeys(row["sheet"] for row in rows))
    if requested in names:
        return requested
    matches = [name for name in names if name.strip() == requested.strip()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous sheet name: {requested!r}")
    raise ValueError(f"Sheet not found: {requested!r}")


def select_scope(
    rows: list[dict], previous: str, current: str, project: str, role: str, aliases: list[str]
) -> dict:
    seen = set()
    sheets = set()
    for row in rows:
        for key in ("sheet", "source_row", "project", "role", "pic", "actual", "next_plan", "issue_risk", "source_refs"):
            if key not in row:
                raise ValueError(f"Normalized row missing required field: {key}")
        if not isinstance(row["sheet"], str) or not isinstance(row["source_row"], int):
            raise ValueError("Sheet and source_row must be string and integer")
        identity = (row["sheet"], row["source_row"])
        if identity in seen:
            raise ValueError(f"Duplicate sheet/row identity: {identity}")
        seen.add(identity)
        sheets.add(row["sheet"])
        refs = row["source_refs"]
        if not isinstance(refs, dict):
            raise ValueError(f"Invalid source references for {identity}")
        for field in NARRATIVE_FIELDS:
            if not isinstance(row[field], str):
                raise ValueError(f"Narrative field {field} must be text for {identity}")
            if row[field].strip() and (not isinstance(refs.get(field), str) or not refs[field]):
                raise ValueError(f"Missing source reference for {field} in {identity}")

    if not rows:
        raise ValueError("Input has no rows")
    previous_sheet = _resolve_sheet(rows, previous)
    current_sheet = _resolve_sheet(rows, current)
    if previous_sheet == current_sheet:
        raise ValueError("Previous and current sheets must differ")
    projects = {project, *aliases}
    previous_rows = [
        row for row in rows
        if row["sheet"] == previous_sheet and row["project"] in projects
        and row["role"] == role and row["next_plan"].strip()
    ]
    current_rows = [row for row in rows if row["sheet"] == current_sheet and row["project"] in projects]
    if not previous_rows:
        raise ValueError("No previous-week plans match the selected project and role")
    if not current_rows:
        raise ValueError("No current-week rows match the selected project")
    previous_period = _period(previous_sheet)
    current_period = _period(current_sheet)
    if previous_period and current_period:
        p_start, p_end = previous_period
        c_start, _ = current_period
        if p_start > c_start:
            raise ValueError("Year-crossing period comparison is unsupported without explicit years")
        if p_end >= c_start:
            raise ValueError("Reporting periods are same or reversed")
    aliases_used = {row["project"] for row in previous_rows + current_rows if row["project"] != project}
    warnings = [
        f"Source review flag row {r['sheet']}!{r['source_row']}: {flag}"
        for r in previous_rows + current_rows for flag in r.get("review_flags", [])
    ]
    return {
        "previous_rows": previous_rows,
        "current_rows": current_rows,
        "previous_sheet": previous_sheet,
        "current_sheet": current_sheet,
        "project": project,
        "project_aliases": {project, *aliases_used},
        "selection_warnings": warnings,
    }


def build_sources(scope: dict) -> dict[str, dict]:
    records = []
    for row in scope["previous_rows"]:
        records.append((row, "next_plan", "previous"))
    for row in scope["current_rows"]:
        records.extend((row, field, "current") for field in NARRATIVE_FIELDS)
    records.sort(key=lambda item: (item[0]["sheet"], item[0]["source_row"], item[1]))
    sources = {}
    for row, field, week in records:
        text = row[field]
        if not text.strip():
            continue
        index = len(sources) + 1
        source_id = f"S{index:03d}"
        sources[source_id] = {
            "source_id": source_id,
            "sheet": row["sheet"],
            "source_row": row["source_row"],
            "project": row["project"],
            "role": row["role"],
            "pic": row["pic"],
            "field": field,
            "week": week,
            "cell": row["source_refs"][field],
            "text": text,
        }
    return sources


def period_context(scope: dict) -> dict:
    previous = _period(scope["previous_sheet"])
    current = _period(scope["current_sheet"])
    years = {
        row.get("reporting_year") or row.get("year")
        for row in scope["previous_rows"] + scope["current_rows"]
        if row.get("reporting_year") or row.get("year")
    }
    year = next(iter(years)) if len(years) == 1 else None
    warnings = []
    if len(years) > 1:
        warnings.append("Conflicting explicit reporting years; checkpoint dates are unknown")
    return {
        "previous_period": previous,
        "current_period": current,
        "period_end": current[1] if current else None,
        "year": year,
        "warnings": warnings,
    }


def checkpoint_relation(date_text: str, context: dict) -> str:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*", date_text)
    period_end = context.get("period_end")
    if not match or not period_end or context.get("warnings"):
        return "unknown"
    day, month = map(int, match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return "unknown"
    date = (month, day)
    period_start = context.get("previous_period")
    if period_start and date < period_start[0]:
        return "unknown"
    if date <= period_end:
        return "elapsed"
    if date[0] == period_end[0]:
        return "future"
    return "unknown"
