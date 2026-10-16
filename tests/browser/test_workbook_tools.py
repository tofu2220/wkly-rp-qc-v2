import hashlib
import sqlite3
from pathlib import Path

from openpyxl import Workbook
from playwright.sync_api import Page, expect


def mapping_workbook(path: Path, *, project: str = "PROJECT-SOURCE") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet & #1 "
    for address, value in {
        "A1": "Nguồn dự án", "B1": "Nguồn vai trò", "C1": "Nguồn PIC",
        "D1": "Nguồn kết quả", "E1": "Nguồn kế hoạch", "F1": "Nguồn rủi ro",
        "A2": project, "B2": "ROLE-SOURCE", "C2": "PIC-SOURCE",
        "D2": "ACTUAL-SOURCE", "E2": "PLAN-SOURCE", "F2": "RISK-SOURCE",
    }.items():
        sheet[address] = value
    workbook.save(path)
    workbook.close()
    return path


def test_tools_entry(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    page.goto(f"{app_url}/app/")
    workbook = mapping_workbook(tmp_path / "mapping.xlsx")
    with page.expect_response(lambda response: response.url.endswith("/api/workbooks") and response.request.method == "POST") as upload_info:
        page.get_by_label("Chọn tệp Excel").set_input_files(workbook)
        page.get_by_role("button", name="Tải và xem trước").click()
    workbook_id = upload_info.value.json()["workbook_id"]
    preview = page.request.get(f"{app_url}/api/workbooks/{workbook_id}").json()
    snapshot_id = preview["sheets"][0]["snapshot_id"]
    page.get_by_role("button", name="Xem sheet Sheet & #1").click()
    page.get_by_role("link", name="Thiết lập cột cho sheet Sheet & #1").click()
    expect(page.get_by_label("Dòng tiêu đề trong Excel")).to_be_visible()
    assert "return_app=1" in page.url and f"sheet={snapshot_id}" in page.url and "tool=mapping" in page.url

    page.get_by_label("Mã thiết lập").fill("manual-flat")
    page.get_by_label("Phiên bản").fill("1")
    page.get_by_label("Dòng tiêu đề trong Excel").fill("3")
    page.get_by_label("Dòng dữ liệu đầu tiên").fill("2")
    page.get_by_label("Cột dự án (số cột)").fill("1")
    page.get_by_label("Cột vai trò (số cột)").fill("2")
    page.get_by_label("Cột kết quả tuần").fill("4")
    page.get_by_label("Nguồn xác định cách ghép cột").fill("keep-mapping-input")
    page.get_by_role("button", name="Lưu thiết lập cột").click()
    expect(page.get_by_role("alert")).to_contain_text("Chưa lưu được thay đổi")
    assert page.evaluate("document.activeElement.id") == "tool-error"
    expect(page.get_by_label("Dòng tiêu đề trong Excel")).to_have_value("3")
    expect(page.get_by_label("Nguồn xác định cách ghép cột")).to_have_value("keep-mapping-input")
    assert page.url.startswith(f"{app_url}/workbooks/{workbook_id}/sheets/")
    assert "return_app=1" in page.url and f"sheet={snapshot_id}" in page.url

    page.get_by_label("Dòng tiêu đề trong Excel").fill("1")
    page.get_by_role("button", name="Lưu thiết lập cột").click()
    expect(page.get_by_role("heading", name="mapping.xlsx")).to_be_visible()
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    expect(page.locator("p[role='status']").first).to_contain_text("Đã cập nhật thiết lập cột.")
    assert page.url.endswith(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}")
    with sqlite3.connect(app_server["database"]) as connection:
        self_mapping = connection.execute("SELECT mapping_status FROM report_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]
        assert self_mapping == "confirmed"
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0] == 0


def test_tools_mapping_return_retries_failed_refresh_without_reposting(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    page.goto(f"{app_url}/app/")
    workbook = mapping_workbook(tmp_path / "mapping.xlsx")
    source_digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    with page.expect_response(lambda response: response.url.endswith("/api/workbooks") and response.request.method == "POST") as upload_info:
        page.get_by_label("Chọn tệp Excel").set_input_files(workbook)
        page.get_by_role("button", name="Tải và xem trước").click()
    workbook_id = upload_info.value.json()["workbook_id"]
    snapshot_id = page.request.get(f"{app_url}/api/workbooks/{workbook_id}").json()["sheets"][0]["snapshot_id"]
    posts = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" and "/mapping" in request.url else None)
    page.get_by_role("button", name="Xem sheet Sheet & #1").click()
    page.get_by_role("link", name="Thiết lập cột cho sheet Sheet & #1").click()
    page.get_by_label("Mã thiết lập").fill("manual-flat")
    page.get_by_label("Phiên bản").fill("1")
    page.get_by_label("Dòng tiêu đề trong Excel").fill("1")
    page.get_by_label("Dòng dữ liệu đầu tiên").fill("2")
    page.get_by_label("Cột dự án (số cột)").fill("1")
    page.get_by_label("Cột vai trò (số cột)").fill("2")
    page.get_by_label("Cột kết quả tuần").fill("4")
    page.get_by_label("Nguồn xác định cách ghép cột").fill("retry fixture")

    preview_url = f"**/api/workbooks/{workbook_id}"
    page.route(preview_url, lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":"temporary read failure"}'))
    page.get_by_role("button", name="Lưu thiết lập cột").click()
    expect(page.get_by_role("status")).to_contain_text("Không thể tải nguồn đã lưu")
    expect(page.get_by_text("Đã cập nhật thiết lập cột.", exact=True)).to_have_count(0)
    assert len(posts) == 1
    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT mapping_status FROM report_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0] == "confirmed"

    page.unroute(preview_url)
    page.get_by_role("button", name="Thử tải lại").click()
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    expect(page.get_by_role("status")).to_contain_text("Đã cập nhật thiết lập cột.")
    assert page.url.endswith(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}")
    page.reload()
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    expect(page.get_by_text("Đã cập nhật thiết lập cột.", exact=True)).to_have_count(0)
    assert len(posts) == 1

    page.get_by_role("button", name="Xem sheet Sheet & #1 ").click()
    page.get_by_text("Công cụ nâng cao", exact=False).click()
    page.get_by_role("link", name="Mở công cụ alias").click()
    expect(page.get_by_label("Nhãn dự án trong nguồn")).to_be_visible()
    assert "return_app=1" in page.url and f"sheet={snapshot_id}" in page.url and "tool=aliases" in page.url
    page.get_by_label("Nhãn dự án trong nguồn").fill("TECHNICAL-SOURCE")
    page.get_by_label("Nhãn dự án chuẩn").fill("TECHNICAL-CANONICAL")
    page.get_by_label("Nguồn xác nhận alias").fill("technical UI fixture only")
    page.get_by_role("button", name="Lưu alias đã xác nhận").click()
    expect(page.locator("p[role='status']").first).to_contain_text("Đã cập nhật alias.")
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    assert page.url.endswith(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}")
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == source_digest
    page.reload()
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    expect(page.get_by_text("Đã cập nhật alias.", exact=True)).to_have_count(0)
    with sqlite3.connect(app_server["database"]) as connection:
        alias = connection.execute(
            "SELECT raw_label FROM project_aliases WHERE raw_label='TECHNICAL-SOURCE'"
        ).fetchone()
        assert alias == ("TECHNICAL-SOURCE",)
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0] == 0


def test_tools_stale_return_does_not_switch_workbook(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    first_file = mapping_workbook(tmp_path / "first.xlsx")
    second_file = mapping_workbook(tmp_path / "second.xlsx", project="PROJECT-B")
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    first = page.request.post(f"{app_url}/api/workbooks", multipart={"workbook": {"name": "first.xlsx", "mimeType": mime, "buffer": first_file.read_bytes()}}).json()
    second = page.request.post(f"{app_url}/api/workbooks", multipart={"workbook": {"name": "second.xlsx", "mimeType": mime, "buffer": second_file.read_bytes()}}).json()
    first_snapshot = page.request.get(f"{app_url}/api/workbooks/{first['workbook_id']}").json()["sheets"][0]
    mapping_response = page.request.post(
        f"{app_url}/workbooks/{first['workbook_id']}/sheets/Sheet%20%26%20%231%20/mapping",
        params={"return_app": "1", "sheet": first_snapshot["snapshot_id"]},
        multipart={
            "template_id": "manual-flat", "version": "1", "source_reference": "stale-context fixture",
            "header_row": "1", "first_data_row": "2", "project_col": "1", "role_col": "2",
            "pic_col": "3", "actual_col": "4", "next_plan_col": "5", "issue_risk_col": "6",
        },
    )
    assert mapping_response.url.endswith(f"/app/workbooks/{first['workbook_id']}?sheet={first_snapshot['snapshot_id']}")

    page.add_init_script(f"""
      const originalFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {{
        const url = typeof input === "string" ? input : input.url;
        if (url.endsWith("/api/workbooks/{first['workbook_id']}") && !window.__heldFirst) {{
          window.__heldFirst = true;
          return new Promise((resolve, reject) => {{
            window.__releaseFirst = () => originalFetch(input, init).then(resolve, reject);
          }});
        }}
        return originalFetch(input, init);
      }};
    """)
    page.goto(f"{app_url}/app/workbooks/{first['workbook_id']}?sheet={first_snapshot['snapshot_id']}")
    page.get_by_role("link", name=f"Mở workbook second.xlsx, phiên bản {second['sha256'][:12]}").click()
    expect(page.get_by_role("heading", name="second.xlsx")).to_be_visible()
    expect(page.get_by_role("status")).to_contain_text("Đang xem nguồn đã lưu")
    expect(page.get_by_text("Đã cập nhật thiết lập cột.", exact=True)).to_have_count(0)
    page.evaluate("window.__releaseFirst()")
    expect(page.get_by_role("heading", name="second.xlsx")).to_be_visible()
    assert page.url.startswith(f"{app_url}/app/workbooks/{second['workbook_id']}")


def test_tools_alias_return(page: Page, app_server, app_url: str, external_attempts, tmp_path: Path):
    page.goto(f"{app_url}/app/")
    workbook = mapping_workbook(tmp_path / "alias.xlsx")
    source_digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    with page.expect_response(lambda response: response.url.endswith("/api/workbooks") and response.request.method == "POST") as upload_info:
        page.get_by_label("Chọn tệp Excel").set_input_files(workbook)
        page.get_by_role("button", name="Tải và xem trước").click()
    workbook_id = upload_info.value.json()["workbook_id"]
    preview = page.request.get(f"{app_url}/api/workbooks/{workbook_id}").json()
    snapshot_id = preview["sheets"][0]["snapshot_id"]
    page.get_by_role("button", name="Xem sheet Sheet & #1").click()
    page.get_by_text("Công cụ nâng cao", exact=False).click()
    page.get_by_role("link", name="Mở công cụ alias").click()
    page.get_by_label("Nhãn dự án trong nguồn").fill("ALIAS-SOURCE-TECHNICAL")
    page.get_by_label("Nhãn dự án chuẩn").fill("ALIAS-CANONICAL-TECHNICAL")
    page.get_by_label("Nguồn xác nhận alias").fill("technical UI fixture only")
    page.get_by_role("button", name="Lưu alias đã xác nhận").click()
    expect(page.get_by_role("status")).to_contain_text("Đã cập nhật alias.")
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    assert page.url.endswith(f"/app/workbooks/{workbook_id}?sheet={snapshot_id}")
    page.reload()
    expect(page.get_by_role("heading", name="Chi tiết sheet: Sheet & #1 ")).to_be_visible()
    expect(page.get_by_text("Đã cập nhật alias.", exact=True)).to_have_count(0)
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == source_digest
    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute(
            "SELECT raw_label FROM project_aliases WHERE raw_label='ALIAS-SOURCE-TECHNICAL'"
        ).fetchone() == ("ALIAS-SOURCE-TECHNICAL",)
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0] == 0
