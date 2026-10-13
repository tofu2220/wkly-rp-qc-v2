import argparse
from pathlib import Path
from typing import Annotated
from zipfile import BadZipFile

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl.utils.exceptions import InvalidFileException

from .store import (
    find_source_conflicts,
    get_snapshot,
    initialize_store,
    list_project_aliases,
    list_snapshots,
    resolve_project_alias,
)
from .web_service import (
    WebSettings,
    confirm_alias,
    confirm_mapping,
    import_upload,
    scope_preview,
    workbook_details,
)


def create_app(settings: WebSettings) -> FastAPI:
    initialize_store(settings.database_path)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    package_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    app = FastAPI(title="Weekly Report Quality Checker", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=package_dir / "static"), name="static")

    @app.get("/")
    async def home(request: Request):
        snapshots = list_snapshots(settings.database_path)
        return templates.TemplateResponse(request, "home.html", {
            "snapshots": snapshots,
            "conflicts": find_source_conflicts(settings.database_path),
            "local_address": "127.0.0.1",
        })

    @app.post("/workbooks")
    async def upload_workbook(workbook: Annotated[UploadFile, File()]):
        filename = workbook.filename or "upload.xlsx"
        if Path(filename.replace("\\", "/")).suffix.casefold() != ".xlsx":
            raise HTTPException(status_code=400, detail="Only .xlsx workbooks are supported")
        content = await workbook.read(32 * 1024 * 1024 + 1)
        try:
            result = import_upload(settings, filename, content)
        except (ValueError, OSError, BadZipFile, InvalidFileException) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/workbooks/{result.workbook_id}", status_code=303)

    @app.get("/workbooks/{workbook_id}")
    async def workbook_page(request: Request, workbook_id: int):
        details = workbook_details(settings, workbook_id)
        if not details:
            raise HTTPException(status_code=404, detail="Workbook version not found")
        return templates.TemplateResponse(request, "workbook.html", {
            "workbook": details,
            "max_upload_bytes": 32 * 1024 * 1024,
        })

    @app.post("/workbooks/{workbook_id}/aliases")
    async def add_alias(
        workbook_id: int,
        raw_label: str = Form(...),
        canonical_label: str = Form(...),
        source_reference: str = Form(...),
    ):
        try:
            confirm_alias(settings, raw_label, canonical_label, source_reference)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/workbooks/{workbook_id}", status_code=303)

    @app.post("/workbooks/{workbook_id}/sheets/{sheet_name}/mapping")
    async def map_sheet(
        workbook_id: int,
        sheet_name: str,
        template_id: str = Form(...),
        version: str = Form(...),
        source_reference: str = Form(...),
        header_row: int = Form(...),
        first_data_row: int = Form(...),
        project_col: int = Form(...),
        role_col: int = Form(...),
        pic_col: str = Form(""),
        actual_col: str = Form(""),
        next_plan_col: str = Form(""),
        issue_risk_col: str = Form(""),
    ):
        def optional_column(value: str) -> int | None:
            return int(value) if value.strip() else None

        mapping = {
            "mode": "flat",
            "template_id": template_id,
            "version": version,
            "header_row": header_row,
            "first_data_row": first_data_row,
            "columns": {
                "project": project_col,
                "role": role_col,
                "pic": optional_column(pic_col),
                "actual": optional_column(actual_col),
                "next_plan": optional_column(next_plan_col),
                "issue_risk": optional_column(issue_risk_col),
            },
        }
        try:
            confirm_mapping(settings, workbook_id, sheet_name, mapping, source_reference)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/workbooks/{workbook_id}", status_code=303)

    @app.get("/scope")
    async def scope_page(
        request: Request,
        previous_snapshot_id: int | None = None,
        current_snapshot_id: int | None = None,
        project: str | None = None,
        role: str | None = None,
        aliases: Annotated[list[str] | None, Query()] = None,
    ):
        snapshots = list_snapshots(settings.database_path)
        projects, roles = set(), set()
        for snapshot in snapshots:
            detail = get_snapshot(settings.database_path, snapshot["snapshot_id"])
            for row in detail["rows"]:
                if row["project_label"]:
                    alias = resolve_project_alias(settings.database_path, row["project_label"])
                    projects.add(alias["canonical_label"])
                if row["role_label"]:
                    roles.add(row["role_label"])
        prepared, error = None, None
        if all((previous_snapshot_id, current_snapshot_id, project, role)):
            try:
                prepared = scope_preview(
                    settings, previous_snapshot_id=previous_snapshot_id, current_snapshot_id=current_snapshot_id,
                    project=project, role=role, aliases=aliases or [],
                )
            except ValueError as caught:
                error = str(caught)
        return templates.TemplateResponse(request, "scope.html", {
            "snapshots": snapshots, "projects": sorted(projects), "roles": sorted(roles),
            "prepared": prepared, "error": error,
            "available_aliases": list_project_aliases(settings.database_path, project) if project else [],
            "selected": {
                "previous": previous_snapshot_id, "current": current_snapshot_id,
                "project": project, "role": role, "aliases": aliases or [],
            },
        })

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wkly-rp-qc-web", description="Local weekly-report review UI")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/wkly-rp-qc-v2")
    parser.add_argument("--replay-root", action="append", type=Path, default=[])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    settings = WebSettings(
        database_path=args.data_dir / "store.sqlite3",
        storage_dir=args.data_dir / "files",
        replay_roots=tuple(args.replay_root or [Path.cwd() / "results"]),
    )
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
    return 0
