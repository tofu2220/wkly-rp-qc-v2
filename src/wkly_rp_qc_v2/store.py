import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .core import JsonDict
from .importer import WorkbookImportPreview, inspect_workbook


@dataclass(frozen=True)
class ImportResult:
    workbook_id: int
    sha256: str
    duplicate: bool
    snapshot_count: int
    row_count: int
    mapping_required_sheets: list[str]


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_store(database_path: Path) -> None:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(database_path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS workbook_versions (
                workbook_id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                original_filename TEXT NOT NULL,
                storage_relpath TEXT NOT NULL,
                imported_at_utc TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS report_snapshots (
                snapshot_id INTEGER PRIMARY KEY,
                workbook_id INTEGER NOT NULL REFERENCES workbook_versions(workbook_id),
                sheet_name TEXT NOT NULL,
                sheet_index INTEGER NOT NULL,
                template_id TEXT,
                mapping_status TEXT NOT NULL,
                sheet_state TEXT NOT NULL,
                reporting_period_json TEXT,
                mapping_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                UNIQUE(workbook_id, sheet_name)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS report_rows (
                report_row_id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(snapshot_id),
                source_row INTEGER NOT NULL,
                reporting_period TEXT,
                reporting_year INTEGER,
                project_label,
                role_label,
                pic,
                actual,
                next_plan,
                issue_risk,
                source_refs_json TEXT NOT NULL,
                field_provenance_json TEXT NOT NULL,
                subgroup_json TEXT NOT NULL,
                review_flags_json TEXT NOT NULL,
                UNIQUE(snapshot_id, source_row)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS template_mappings (
                template_id TEXT NOT NULL,
                version TEXT NOT NULL,
                mapping_json TEXT NOT NULL,
                source_reference TEXT NOT NULL,
                saved_at_utc TEXT NOT NULL,
                PRIMARY KEY(template_id, version)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS project_identities (
                project_id INTEGER PRIMARY KEY,
                canonical_label TEXT NOT NULL UNIQUE
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS project_aliases (
                raw_label TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES project_identities(project_id),
                source_reference TEXT NOT NULL,
                confirmed_at_utc TEXT NOT NULL
            )
        """)
        connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (2)")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _result_for_existing(connection: sqlite3.Connection, workbook_id: int, sha256: str) -> ImportResult:
    snapshots, rows = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(row_count), 0) FROM report_snapshots WHERE workbook_id=?",
        (workbook_id,),
    ).fetchone()
    mapping_required = [row[0] for row in connection.execute(
        "SELECT sheet_name FROM report_snapshots WHERE workbook_id=? AND mapping_status='mapping_required' ORDER BY sheet_index",
        (workbook_id,),
    )]
    return ImportResult(workbook_id, sha256, True, snapshots, rows, mapping_required)


def _copy_original(workbook_path: Path, sha256: str, storage_dir: Path) -> tuple[Path, bool]:
    parent = storage_dir / sha256[:2]
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / f"{sha256}.xlsx"
    if destination.exists():
        existing_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        if existing_hash != sha256:
            raise OSError(f"Stored workbook hash mismatch: {destination}")
        return destination, False

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=f".{sha256}.", suffix=".tmp", delete=False) as temp:
            temporary_path = Path(temp.name)
            with workbook_path.open("rb") as source:
                shutil.copyfileobj(source, temp)
            temp.flush()
            os.fsync(temp.fileno())
        temporary_path.rename(destination)
        return destination, True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _persist_preview(
    preview: WorkbookImportPreview,
    workbook_path: Path,
    database_path: Path,
    storage_dir: Path,
    sha256: str,
) -> ImportResult:
    if preview.validation["status"] == "FAIL":
        raise ValueError("Workbook extraction validation failed: " + "; ".join(preview.validation["errors"]))
    destination, created_file = _copy_original(workbook_path, sha256, storage_dir)
    relative_path = destination.relative_to(storage_dir).as_posix()
    rows_by_sheet: dict[str, list[JsonDict]] = {}
    for row in preview.rows:
        rows_by_sheet.setdefault(row["sheet"], []).append(row)
    try:
        with _connect(database_path) as connection:
            cursor = connection.execute(
                "INSERT INTO workbook_versions(sha256, original_filename, storage_relpath, imported_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (sha256, preview.workbook_filename, relative_path, datetime.now(UTC).isoformat()),
            )
            workbook_id = cursor.lastrowid
            mapping_required_sheets = []
            total_rows = 0
            for sheet in preview.sheets:
                sheet_rows = rows_by_sheet.get(sheet["sheet"], [])
                total_rows += len(sheet_rows)
                if sheet["mapping_status"] == "mapping_required":
                    mapping_required_sheets.append(sheet["sheet"])
                cursor = connection.execute(
                    "INSERT INTO report_snapshots(workbook_id, sheet_name, sheet_index, template_id, "
                    "mapping_status, sheet_state, reporting_period_json, mapping_json, warnings_json, row_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workbook_id, sheet["sheet"], sheet["sheet_index"], sheet["template"],
                        sheet["mapping_status"], sheet["sheet_state"], _json(sheet["reporting_period"]),
                        _json(sheet["fields"]), _json(sheet["ambiguities"]), len(sheet_rows),
                    ),
                )
                snapshot_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO report_rows(snapshot_id, source_row, reporting_period, reporting_year, "
                    "project_label, role_label, pic, actual, next_plan, issue_risk, source_refs_json, "
                    "field_provenance_json, subgroup_json, review_flags_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(
                        snapshot_id, row["source_row"], row.get("reporting_period"), row.get("reporting_year"),
                        row["project"], row["role"], row["pic"], row["actual"], row["next_plan"],
                        row["issue_risk"], _json(row["source_refs"]), _json(row["field_provenance"]),
                        _json(row["subgroup"]), _json(row["review_flags"]),
                    ) for row in sheet_rows],
                )
        return ImportResult(workbook_id, sha256, False, len(preview.sheets), total_rows, mapping_required_sheets)
    except Exception:
        if created_file:
            destination.unlink(missing_ok=True)
        raise


def import_workbook(workbook_path: Path, database_path: Path, storage_dir: Path) -> ImportResult:
    workbook_path, database_path, storage_dir = Path(workbook_path), Path(database_path), Path(storage_dir)
    initialize_store(database_path)
    sha256 = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    with _connect(database_path) as connection:
        existing = connection.execute(
            "SELECT workbook_id FROM workbook_versions WHERE sha256=?", (sha256,)
        ).fetchone()
        if existing:
            return _result_for_existing(connection, existing[0], sha256)

    preview = inspect_workbook(workbook_path)
    return _persist_preview(preview, workbook_path, database_path, storage_dir, sha256)


def _decode_snapshot(row: sqlite3.Row) -> JsonDict:
    period = json.loads(row["reporting_period_json"]) if row["reporting_period_json"] else None
    if period is None or period.get("status") == "unknown":
        chronology = "unknown"
    elif period.get("conflicts") and any("conflicts" in item for item in period["conflicts"]):
        chronology = "conflict"
    elif period.get("year") is None:
        chronology = "year_unknown"
    else:
        chronology = "certain"
    return {
        "snapshot_id": row["snapshot_id"],
        "workbook_id": row["workbook_id"],
        "workbook_sha256": row["sha256"],
        "original_filename": row["original_filename"],
        "sheet_name": row["sheet_name"],
        "sheet_index": row["sheet_index"],
        "template_id": row["template_id"],
        "mapping_status": row["mapping_status"],
        "sheet_state": row["sheet_state"],
        "reporting_period": period,
        "chronology_status": chronology,
        "mapping": json.loads(row["mapping_json"]),
        "warnings": json.loads(row["warnings_json"]),
        "row_count": row["row_count"],
    }


def get_snapshot(database_path: Path, snapshot_id: int) -> JsonDict | None:
    with _connect(Path(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        snapshot = connection.execute(
            "SELECT s.*, w.sha256, w.original_filename FROM report_snapshots s "
            "JOIN workbook_versions w USING(workbook_id) WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is None:
            return None
        result = _decode_snapshot(snapshot)
        rows = connection.execute(
            "SELECT report_row_id, source_row, reporting_period, reporting_year, project_label, role_label, "
            "pic, actual, next_plan, issue_risk, source_refs_json, field_provenance_json, subgroup_json, "
            "review_flags_json FROM report_rows WHERE snapshot_id=? ORDER BY source_row",
            (snapshot_id,),
        ).fetchall()
        result["rows"] = [{
            "report_row_id": row["report_row_id"],
            "source_row": row["source_row"],
            "reporting_period": row["reporting_period"],
            "reporting_year": row["reporting_year"],
            "project_label": row["project_label"],
            "role_label": row["role_label"],
            "pic": row["pic"],
            "actual": row["actual"],
            "next_plan": row["next_plan"],
            "issue_risk": row["issue_risk"],
            "source_refs": json.loads(row["source_refs_json"]),
            "field_provenance": json.loads(row["field_provenance_json"]),
            "subgroup": json.loads(row["subgroup_json"]),
            "review_flags": json.loads(row["review_flags_json"]),
        } for row in rows]
        return result


def list_snapshots(database_path: Path) -> list[JsonDict]:
    with _connect(Path(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute(
            "SELECT s.*, w.sha256, w.original_filename FROM report_snapshots s "
            "JOIN workbook_versions w USING(workbook_id)"
        ).fetchall()
    snapshots = [_decode_snapshot(row) for row in records]

    def chronology(snapshot: JsonDict) -> tuple[int, int, int, int, int]:
        period = snapshot["reporting_period"]
        if period is None:
            return (2, 0, 0, snapshot["workbook_id"], snapshot["sheet_index"])
        start = period.get("start_month_day")
        if not start:
            return (2, 0, 0, snapshot["workbook_id"], snapshot["sheet_index"])
        month, day = (int(value) for value in start.split("-", 1))
        year = period.get("year")
        if year is None:
            return (1, month, day, snapshot["workbook_id"], snapshot["sheet_index"])
        return (0, year, month * 100 + day, snapshot["workbook_id"], snapshot["sheet_index"])

    return sorted(snapshots, key=chronology)


def find_source_conflicts(database_path: Path) -> list[JsonDict]:
    with _connect(Path(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        groups = connection.execute(
            "SELECT r.project_label, r.role_label, r.reporting_period "
            "FROM report_rows r JOIN report_snapshots s USING(snapshot_id) "
            "GROUP BY r.project_label, r.role_label, r.reporting_period "
            "HAVING r.project_label IS NOT NULL AND COUNT(DISTINCT s.workbook_id) > 1 "
            "ORDER BY r.project_label, r.role_label, r.reporting_period"
        ).fetchall()
        conflicts = []
        for group in groups:
            rows = connection.execute(
                "SELECT s.snapshot_id, s.workbook_id, w.sha256, w.original_filename, s.sheet_name, "
                "r.source_row, r.source_refs_json FROM report_rows r "
                "JOIN report_snapshots s USING(snapshot_id) "
                "JOIN workbook_versions w USING(workbook_id) "
                "WHERE r.project_label=? AND r.role_label IS ? AND r.reporting_period IS ? "
                "ORDER BY s.workbook_id, s.sheet_index, r.source_row",
                (group["project_label"], group["role_label"], group["reporting_period"]),
            ).fetchall()
            by_snapshot: dict[int, JsonDict] = {}
            for row in rows:
                item = by_snapshot.setdefault(row["snapshot_id"], {
                    "snapshot_id": row["snapshot_id"],
                    "workbook_id": row["workbook_id"],
                    "workbook_sha256": row["sha256"],
                    "original_filename": row["original_filename"],
                    "sheet_name": row["sheet_name"],
                    "source_rows": [],
                })
                item["source_rows"].append({
                    "row": row["source_row"],
                    "source_refs": json.loads(row["source_refs_json"]),
                })
            conflicts.append({
                "project_label": group["project_label"],
                "role_label": group["role_label"],
                "reporting_period": group["reporting_period"],
                "snapshots": list(by_snapshot.values()),
            })
        return conflicts


def save_template_mapping(
    database_path: Path,
    template_id: str,
    version: str,
    mapping: JsonDict,
    *,
    source_reference: str,
) -> None:
    if not template_id.strip() or not version.strip() or not source_reference.strip():
        raise ValueError("Template ID, version and source reference must be nonempty")
    with _connect(Path(database_path)) as connection:
        connection.execute(
            "INSERT INTO template_mappings(template_id, version, mapping_json, source_reference, saved_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (template_id, version, _json(mapping), source_reference, datetime.now(UTC).isoformat()),
        )


def get_template_mapping(database_path: Path, template_id: str, version: str) -> JsonDict | None:
    with _connect(Path(database_path)) as connection:
        row = connection.execute(
            "SELECT mapping_json, source_reference, saved_at_utc FROM template_mappings "
            "WHERE template_id=? AND version=?",
            (template_id, version),
        ).fetchone()
    if row is None:
        return None
    return {
        "template_id": template_id,
        "version": version,
        "mapping": json.loads(row[0]),
        "source_reference": row[1],
        "saved_at_utc": row[2],
    }


def confirm_project_alias(
    database_path: Path,
    raw_label: str,
    canonical_label: str,
    *,
    source_reference: str,
) -> int:
    if not raw_label.strip() or not canonical_label.strip() or not source_reference.strip():
        raise ValueError("Raw label, canonical label and source reference must be nonempty")
    with _connect(Path(database_path)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO project_identities(canonical_label) VALUES (?)", (canonical_label,)
        )
        project_id = connection.execute(
            "SELECT project_id FROM project_identities WHERE canonical_label=?", (canonical_label,)
        ).fetchone()[0]
        existing = connection.execute(
            "SELECT project_id FROM project_aliases WHERE raw_label=?", (raw_label,)
        ).fetchone()
        if existing:
            if existing[0] != project_id:
                raise ValueError(f"Project alias already maps to a different identity: {raw_label}")
            return project_id
        connection.execute(
            "INSERT INTO project_aliases(raw_label, project_id, source_reference, confirmed_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (raw_label, project_id, source_reference, datetime.now(UTC).isoformat()),
        )
        return project_id


def resolve_project_alias(database_path: Path, raw_label: str) -> JsonDict:
    with _connect(Path(database_path)) as connection:
        row = connection.execute(
            "SELECT p.project_id, p.canonical_label FROM project_aliases a "
            "JOIN project_identities p USING(project_id) WHERE a.raw_label=?",
            (raw_label,),
        ).fetchone()
    return {
        "raw_label": raw_label,
        "project_id": row[0] if row else None,
        "canonical_label": row[1] if row else raw_label,
        "explicit": row is not None,
    }
