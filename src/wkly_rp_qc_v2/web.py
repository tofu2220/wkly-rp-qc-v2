import argparse
import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode
from zipfile import BadZipFile

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl.utils.exceptions import InvalidFileException

from .core import JsonDict
from .jobs import JobWorker
from .store import (
    find_source_conflicts,
    get_snapshot,
    get_workbook_version,
    initialize_store,
    list_project_aliases,
    list_snapshots,
    resolve_project_alias,
)
from .web_api import create_workbook_router
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


def react_workbook_url(workbook_id: int, snapshot_id: int) -> str:
    return f"/app/workbooks/{workbook_id}?{urlencode({'sheet': snapshot_id})}"


def validate_tool_context(
    settings: WebSettings,
    workbook_id: int,
    return_app: bool,
    snapshot_id: int | None,
    *,
    sheet_name: str | None = None,
) -> JsonDict | None:
    if not return_app:
        return None
    if snapshot_id is None:
        raise HTTPException(status_code=400, detail="Thiếu ngữ cảnh sheet cần quay lại.")
    if get_workbook_version(settings.database_path, workbook_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy workbook đã lưu.")
    snapshot = get_snapshot(settings.database_path, snapshot_id)
    if snapshot is None or snapshot["workbook_id"] != workbook_id:
        raise HTTPException(status_code=404, detail="Sheet không thuộc workbook đã chọn.")
    if sheet_name is not None and snapshot["sheet_name"] != sheet_name:
        raise HTTPException(status_code=404, detail="Tên sheet không khớp với nguồn đã chọn.")
    return {
        "workbook_id": workbook_id,
        "snapshot_id": snapshot_id,
        "sheet_name": snapshot["sheet_name"],
        "return_url": react_workbook_url(workbook_id, snapshot_id),
    }


def _tool_notice(response: RedirectResponse, context: JsonDict, operation: str) -> RedirectResponse:
    response.set_cookie(
        "workbook_tool_notice",
        json.dumps({**context, "operation": operation}, ensure_ascii=False, separators=(",", ":")),
        max_age=120,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


def _require_local_form_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site in {"cross-site", "same-site"}:
        raise HTTPException(status_code=403, detail="Không chấp nhận thay đổi từ nguồn khác trang.")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Không chấp nhận thay đổi từ nguồn khác trang.")


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

    def render_tool_form(
        request: Request,
        workbook_id: int,
        *,
        context: JsonDict | None = None,
        tool: str | None = None,
        error: str | None = None,
        mapping_values: dict[str, str] | None = None,
        alias_values: dict[str, str] | None = None,
        status_code: int = 200,
    ):
        details = workbook_details(settings, workbook_id)
        if not details:
            raise HTTPException(status_code=404, detail="Workbook version not found")
        mapping_actions = {
            item["snapshot_id"]: f"/workbooks/{workbook_id}/sheets/{quote(item['sheet_name'], safe='')}/mapping"
            for item in details["sheets"]
        }
        return templates.TemplateResponse(request, "workbook.html", {
            "workbook": details,
            "max_upload_bytes": 32 * 1024 * 1024,
            "tool_context": context,
            "selected_tool": tool,
            "tool_error": error,
            "mapping_values": mapping_values or {},
            "alias_values": alias_values or {},
            "mapping_actions": mapping_actions,
        }, status_code=status_code)

    @app.get("/workbooks/{workbook_id}")
    async def workbook_page(
        request: Request,
        workbook_id: int,
        return_app: bool = False,
        sheet: int | None = None,
        tool: str | None = None,
    ):
        if tool not in (None, "mapping", "aliases"):
            raise HTTPException(status_code=400, detail="Không nhận diện được công cụ cần mở.")
        context = validate_tool_context(settings, workbook_id, return_app, sheet)
        return render_tool_form(request, workbook_id, context=context, tool=tool)

    @app.post("/workbooks/{workbook_id}/aliases")
    async def add_alias(request: Request, workbook_id: int, return_app: bool = False, sheet: int | None = None):
        _require_local_form_origin(request)
        context = validate_tool_context(settings, workbook_id, return_app, sheet)
        form = await request.form()
        values = {name: str(form.get(name, "")) for name in ("raw_label", "canonical_label", "source_reference")}
        try:
            confirm_alias(settings, values["raw_label"], values["canonical_label"], values["source_reference"])
        except (ValueError, OSError) as error:
            message = "Chưa lưu được thay đổi. Kiểm tra các trường được đánh dấu rồi lưu lại; nội dung bạn nhập vẫn được giữ."
            if isinstance(error, ValueError):
                message += f" {error}"
            return render_tool_form(
                request, workbook_id, context=context, tool="aliases", error=message,
                alias_values=values, status_code=400,
            )
        if context:
            return _tool_notice(RedirectResponse(context["return_url"], status_code=303), context, "alias")
        return RedirectResponse(f"/workbooks/{workbook_id}", status_code=303)

    @app.post("/workbooks/{workbook_id}/sheets/{sheet_name}/mapping")
    async def map_sheet(
        request: Request,
        workbook_id: int,
        sheet_name: str,
        return_app: bool = False,
        sheet: int | None = None,
    ):
        _require_local_form_origin(request)
        context = validate_tool_context(settings, workbook_id, return_app, sheet, sheet_name=sheet_name)
        form = await request.form()
        names = ("template_id", "version", "source_reference", "header_row", "first_data_row", "project_col", "role_col", "pic_col", "actual_col", "next_plan_col", "issue_risk_col")
        values = {name: str(form.get(name, "")) for name in names}

        def required_integer(name: str) -> int:
            try:
                return int(values[name])
            except ValueError as error:
                raise ValueError(f"Nhập số nguyên hợp lệ cho trường {name}.") from error

        def optional_integer(name: str) -> int | None:
            if not values[name].strip():
                return None
            return required_integer(name)

        try:
            mapping: JsonDict = {
                "mode": "flat",
                "template_id": values["template_id"],
                "version": values["version"],
                "header_row": required_integer("header_row"),
                "first_data_row": required_integer("first_data_row"),
                "columns": {
                    "project": required_integer("project_col"),
                    "role": required_integer("role_col"),
                    "pic": optional_integer("pic_col"),
                    "actual": optional_integer("actual_col"),
                    "next_plan": optional_integer("next_plan_col"),
                    "issue_risk": optional_integer("issue_risk_col"),
                },
            }
            confirm_mapping(settings, workbook_id, sheet_name, mapping, values["source_reference"])
        except (ValueError, OSError) as error:
            message = "Chưa lưu được thay đổi. Kiểm tra các trường được đánh dấu rồi lưu lại; nội dung bạn nhập vẫn được giữ."
            if isinstance(error, ValueError):
                message += f" {error}"
            return render_tool_form(
                request, workbook_id, context=context, tool="mapping", error=message,
                mapping_values=values, status_code=400,
            )
        if context:
            return _tool_notice(RedirectResponse(context["return_url"], status_code=303), context, "mapping")
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
