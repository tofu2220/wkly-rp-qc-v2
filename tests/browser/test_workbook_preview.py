import sqlite3
from pathlib import Path

from openpyxl import Workbook
from playwright.sync_api import Page, expect


def technical_workbook(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "BC Tuần 01.09 - 05.09"
    for address, value in {
        "A4": "STT", "B4": "Sản phẩm", "C4": "Kết quả tuần",
        "D4": "Kế hoạch tuần", "E4": "Vấn đề / rủi ro", "F4": "PIC",
        "A5": "1", "B5": "SMART PARKING", "A6": "1.1", "B6": "PTSP",
        "C6": "Reported status from source", "D6": "Deliver next step", "F6": "Owner",
    }.items():
        sheet[address] = value
    workbook.save(path)
    workbook.close()
    return path


def test_upload_tracer(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    page.goto(f"{app_url}/app/")
    file_input = page.get_by_label("Chọn tệp Excel")
    file_input.set_input_files(technical_workbook(tmp_path / "technical.xlsx"))
    page.get_by_role("button", name="Tải và xem trước").click()

    preview_response = page.wait_for_response(
        lambda response: "/api/workbooks/" in response.url and response.request.method == "GET"
    )
    assert preview_response.status == 200
    preview = preview_response.json()
    assert preview["sheets"][0]["sheet_name"] == "BC Tuần 01.09 - 05.09"
    expect(page.get_by_text("BC Tuần 01.09 - 05.09", exact=True)).to_be_visible()
    expect(page.get_by_text("Reported status from source", exact=True)).to_be_visible()
    assert page.url.endswith(f"/app/workbooks/{preview['workbook_id']}")

    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0
