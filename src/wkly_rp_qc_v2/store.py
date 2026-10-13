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
