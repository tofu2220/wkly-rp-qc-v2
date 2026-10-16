import argparse
from pathlib import Path
from typing import Annotated
from zipfile import BadZipFile

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl.utils.exceptions import InvalidFileException

from .jobs import JobWorker
from .store import (
    find_source_conflicts,
    get_snapshot,
    initialize_store,
    list_project_aliases,
    list_snapshots,
    resolve_project_alias,
)
from .web_export import write_web_exports
from .web_service import (
    WebSettings,
    confirm_alias,
    confirm_mapping,
    create_replay_job,
    import_upload,
    job_details,
    replay_options,
    review_finding,
    workbook_details,
)
from .web_api import create_workbook_router


def create_app(settings: WebSettings) -> FastAPI:
    initialize_store(settings.database_path)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    package_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    app = FastAPI(title="Weekly Report Quality Checker", docs_url=None, redoc_url=None)
    app.state.settings = settings
    worker = JobWorker(settings.database_path, settings.storage_dir, settings.replay_roots)
    app.state.job_worker = worker
    app.router.on_startup.append(worker.start)
    app.router.on_shutdown.append(worker.stop)
    app.mount("/static", StaticFiles(directory=package_dir / "static"), name="static")
    app.include_router(create_workbook_router(settings))

    frontend_dist = package_dir / "frontend_dist"
    if (frontend_dist / "assets").is_dir():
        app.mount("/app/assets", StaticFiles(directory=frontend_dist / "assets"), name="frontend-assets")

    def frontend_page():
        index = frontend_dist / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="React frontend build is unavailable; build frontend assets first")
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.add_api_route("/app/", frontend_page, methods=["GET"], include_in_schema=False)
    app.add_api_route("/app/workbooks/{workbook_id}", frontend_page, methods=["GET"], include_in_schema=False)

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
        prepared, error, replay_bundles = None, None, []
        if all((previous_snapshot_id, current_snapshot_id, project, role)):
            try:
                prepared, replay_bundles, _ = replay_options(
                    settings, previous_snapshot_id=previous_snapshot_id, current_snapshot_id=current_snapshot_id,
                    project=project, role=role, aliases=aliases or [],
                )
            except ValueError as caught:
                error = str(caught)
        return templates.TemplateResponse(request, "scope.html", {
            "snapshots": snapshots, "projects": sorted(projects), "roles": sorted(roles),
            "prepared": prepared, "error": error,
            "replay_bundles": [{
                "bundle_id": bundle["bundle_id"], "run_name": bundle["run_name"],
                "scope_provenance": bundle["provenance"]["scope_provenance"],
            } for bundle in replay_bundles],
            "available_aliases": list_project_aliases(settings.database_path, project) if project else [],
            "selected": {
                "previous": previous_snapshot_id, "current": current_snapshot_id,
                "project": project, "role": role, "aliases": aliases or [],
            },
        })

    @app.get("/replays")
    async def replay_list(
        previous_snapshot_id: int,
        current_snapshot_id: int,
        project: str,
        role: str,
        aliases: Annotated[list[str] | None, Query()] = None,
    ):
        _, bundles, _ = replay_options(
            settings, previous_snapshot_id=previous_snapshot_id, current_snapshot_id=current_snapshot_id,
            project=project, role=role, aliases=aliases or [],
        )
        return [{
            "bundle_id": bundle["bundle_id"], "run_name": bundle["run_name"],
            "scope_provenance": bundle["provenance"]["scope_provenance"],
        } for bundle in bundles]

    @app.post("/jobs")
    async def start_job(
        previous_snapshot_id: int = Form(...),
        current_snapshot_id: int = Form(...),
        project: str = Form(...),
        role: str = Form(...),
        aliases: Annotated[list[str] | None, Form()] = None,
        replay_bundle_id: str | None = Form(None),
    ):
        try:
            job_id = create_replay_job(
                settings, previous_snapshot_id=previous_snapshot_id, current_snapshot_id=current_snapshot_id,
                project=project, role=role, aliases=aliases or [], replay_bundle_id=replay_bundle_id,
            )
            worker.enqueue(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    async def show_job(request: Request, job_id: int):
        job = job_details(settings, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Analysis job not found")
        return templates.TemplateResponse(request, "job.html", {"job": job})

    @app.post("/jobs/{job_id}/reviews")
    async def save_review(
        job_id: int,
        plan_id: str = Form(...),
        finding_index: int = Form(...),
        state: str = Form(...),
        note: str | None = Form(None),
    ):
        try:
            review_finding(settings, job_id, plan_id, finding_index, state, note)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    def make_export(job_id: int) -> tuple[dict, Path]:
        job = job_details(settings, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Analysis job not found")
        if job["comparison"] is None:
            raise HTTPException(status_code=409, detail="Only completed jobs can be exported")
        run_dir = Path(job["result"]["path"]).parent
        write_web_exports(run_dir, job["comparison"], job["reviews"])
        return job, run_dir

    @app.get("/jobs/{job_id}/export.csv")
    async def export_job_csv(job_id: int):
        _, run_dir = make_export(job_id)
        return FileResponse(run_dir / "pm_review.csv", media_type="text/csv", filename=f"job-{job_id}-review.csv")

    @app.get("/jobs/{job_id}/export.xlsx")
    async def export_job_xlsx(job_id: int):
        _, run_dir = make_export(job_id)
        return FileResponse(
            run_dir / "pm_review.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"job-{job_id}-review.xlsx",
        )

    @app.get("/jobs/{job_id}/review.md")
    async def export_job_markdown(job_id: int):
        _, run_dir = make_export(job_id)
        return FileResponse(run_dir / "pm_review.md", media_type="text/markdown", filename=f"job-{job_id}-review.md")

    @app.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: int):
        try:
            worker.resume(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

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
