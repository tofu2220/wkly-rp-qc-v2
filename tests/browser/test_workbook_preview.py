import sqlite3
from pathlib import Path

import httpx
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
    with page.expect_response(
        lambda response: "/api/workbooks/" in response.url and response.request.method == "GET"
    ) as response_info:
        page.get_by_role("button", name="Tải và xem trước").click()
    preview_response = response_info.value
    assert preview_response.status == 200
    preview = preview_response.json()
    assert preview["sheets"][0]["sheet_name"] == "BC Tuần 01.09 - 05.09"
    expect(page.get_by_text("BC Tuần 01.09 - 05.09", exact=True)).to_be_visible()
    expect(page.get_by_text("Reported status from source", exact=True)).to_be_visible()
    expect(page.get_by_role("status")).to_contain_text("Đã lưu. Workbook chưa được phân tích.")
    await_empty = page.locator("#workbook-file").input_value()
    assert await_empty == ""
    assert page.url.endswith(f"/app/workbooks/{preview['workbook_id']}")

    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0


def test_upload_rejection_api(app_server, tmp_path: Path):
    base_url = app_server["base_url"]
    with httpx.Client(base_url=base_url) as client:
        missing = client.post("/api/workbooks")
        assert missing.status_code == 422

        rejected = [
            ("empty.xlsx", b""),
            ("corrupt.xlsx", b"not an xlsx archive"),
            ("report.csv", technical_workbook(tmp_path / "wrong-extension.xlsx").read_bytes()),
            ("large.xlsx", b"x" * (32 * 1024 * 1024 + 1)),
        ]
        for filename, content in rejected:
            response = client.post(
                "/api/workbooks",
                files={"workbook": (filename, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
            assert response.status_code == 400, filename
            assert str(tmp_path) not in response.text

        foreign_origin = client.post(
            "/api/workbooks",
            files={"workbook": ("foreign.xlsx", technical_workbook(tmp_path / "foreign.xlsx").read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            headers={"Origin": "https://evil.example"},
        )
        assert foreign_origin.status_code == 403

    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0


def test_upload_rejection_preserves_file_and_does_not_retry(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    page.goto(f"{app_url}/app/")
    selected_file = technical_workbook(tmp_path / "bad.xlsx")
    selected_file.write_bytes(b"invalid workbook bytes")
    input_control = page.get_by_label("Chọn tệp Excel")
    input_control.set_input_files(selected_file)
    post_attempts = []
    page.on("request", lambda request: post_attempts.append(request) if request.method == "POST" and request.url.endswith("/api/workbooks") else None)

    def non_json_response(route):
        route.fulfill(status=200, content_type="text/html", body="<html>not json</html>")

    page.route("**/api/workbooks", non_json_response)
    page.get_by_role("button", name="Tải và xem trước").click()
    expect(page.get_by_role("status")).to_contain_text("phản hồi không phải JSON")
    assert page.url.endswith("/app/")
    assert page.locator("#workbook-file").evaluate("element => element.files[0].name") == "bad.xlsx"
    assert page.locator("section").count() == 0

    page.unroute("**/api/workbooks", non_json_response)
    page.get_by_role("button", name="Tải và xem trước").click()
    expect(page.get_by_role("status")).to_contain_text("Không thể đọc workbook")
    assert page.url.endswith("/app/")
    assert page.locator("#workbook-file").evaluate("element => element.files[0].name") == "bad.xlsx"
    assert page.locator("section").count() == 0

    assert len(post_attempts) == 2
    page.reload()
    assert len(post_attempts) == 2
    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0

    input_control.set_input_files(technical_workbook(tmp_path / "corrected.xlsx"))
    page.get_by_role("button", name="Tải và xem trước").click()
    expect(page.get_by_text("BC Tuần 01.09 - 05.09", exact=True)).to_be_visible()
    assert page.url.endswith("/app/workbooks/1")
