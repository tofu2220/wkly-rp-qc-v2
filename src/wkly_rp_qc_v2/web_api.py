from dataclasses import asdict
from pathlib import Path
from zipfile import BadZipFile

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from openpyxl.utils.exceptions import InvalidFileException

from .web_service import MAX_UPLOAD_BYTES, WebSettings, import_upload, workbook_details


def _require_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site in {"cross-site", "same-site"}:
        raise HTTPException(status_code=403, detail="Cross-origin workbook uploads are not allowed")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-origin workbook uploads are not allowed")


def create_workbook_router(settings: WebSettings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/workbooks")
    async def upload_workbook(request: Request, workbook: UploadFile = File(...)):
        _require_local_origin(request)
        filename = Path((workbook.filename or "upload.xlsx").replace("\\", "/")).name or "upload.xlsx"
        try:
            content = await workbook.read(MAX_UPLOAD_BYTES + 1)
            result = import_upload(settings, filename, content)
        except (ValueError, BadZipFile, InvalidFileException) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(status_code=400, detail="The workbook could not be stored locally") from error
        finally:
            await workbook.close()
        return JSONResponse(
            status_code=200 if result.duplicate else 201,
            content={**asdict(result), "preview_url": f"/app/workbooks/{result.workbook_id}"},
        )

    @router.get("/api/workbooks/{workbook_id}")
    async def get_workbook(workbook_id: int):
        details = workbook_details(settings, workbook_id)
        if not details:
            raise HTTPException(status_code=404, detail="Workbook version not found")
        return details

    return router
