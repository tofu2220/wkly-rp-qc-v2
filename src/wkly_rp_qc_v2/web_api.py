import json
from dataclasses import asdict
from pathlib import Path
from zipfile import BadZipFile

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from openpyxl.utils.exceptions import InvalidFileException

from .web_service import (
    MAX_UPLOAD_BYTES,
    WebSettings,
    import_upload,
    workbook_catalog,
    workbook_details,
)


def _require_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site in {"cross-site", "same-site"}:
        raise HTTPException(status_code=403, detail="Không chấp nhận tải workbook từ nguồn khác trang.")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Không chấp nhận tải workbook từ nguồn khác trang.")


def create_workbook_router(settings: WebSettings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/workbooks")
    async def upload_workbook(request: Request, workbook: UploadFile = File(...)):
        _require_local_origin(request)
        filename = Path((workbook.filename or "upload.xlsx").replace("\\", "/")).name or "upload.xlsx"
        try:
            content = await workbook.read(MAX_UPLOAD_BYTES + 1)
            result = import_upload(settings, filename, content)
        except ValueError as error:
            message = str(error)
            if message == "Only .xlsx workbooks are supported":
                detail = "Chỉ hỗ trợ tệp .xlsx. Hãy chọn workbook Excel hợp lệ."
            elif message == "Workbook exceeds the 32 MiB local upload limit":
                detail = "Tệp vượt giới hạn 32 MiB. Hãy chọn workbook nhỏ hơn."
            else:
                detail = "Không thể đọc workbook. Hãy kiểm tra tệp .xlsx rồi thử lại."
            raise HTTPException(status_code=400, detail=detail) from error
        except (BadZipFile, InvalidFileException) as error:
            raise HTTPException(
                status_code=400,
                detail="Không thể đọc workbook. Hãy kiểm tra tệp .xlsx rồi thử lại.",
            ) from error
        except OSError as error:
            raise HTTPException(status_code=400, detail="Không thể lưu workbook trên thiết bị này. Hãy thử lại.") from error
        finally:
            await workbook.close()
        return JSONResponse(
            status_code=200 if result.duplicate else 201,
            content={**asdict(result), "preview_url": f"/app/workbooks/{result.workbook_id}"},
        )

    @router.get("/api/workbooks/{workbook_id}")
    async def get_workbook(request: Request, workbook_id: int):
        details = workbook_details(settings, workbook_id)
        if not details:
            raise HTTPException(status_code=404, detail="Workbook version not found")
        raw_notice = request.cookies.get("workbook_tool_notice")
        notice_payload = None
        if raw_notice:
            try:
                notice = json.loads(raw_notice)
            except (json.JSONDecodeError, TypeError):
                notice = None
            if isinstance(notice, dict):
                notice_workbook = notice.get("workbook_id")
                notice_snapshot = notice.get("snapshot_id")
                operation = notice.get("operation")
                matching_sheet = any(sheet["snapshot_id"] == notice_snapshot for sheet in details["sheets"])
                if (
                    type(notice_workbook) is int
                    and notice_workbook == workbook_id
                    and type(notice_snapshot) is int
                    and matching_sheet
                    and operation in {"mapping", "alias"}
                ):
                    notice_payload = {"operation": operation, "snapshot_id": notice_snapshot}
        response = JSONResponse(
            content={**details, **({"tool_notice": notice_payload} if notice_payload else {})},
            headers={"Cache-Control": "no-store"},
        )
        if raw_notice:
            response.delete_cookie(
                "workbook_tool_notice", path="/", httponly=True, samesite="strict"
            )
        return response

    @router.get("/api/workbooks")
    async def list_workbooks():
        return workbook_catalog(settings)

    return router
