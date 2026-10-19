import hashlib
import sqlite3
from unittest.mock import patch

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from wkly_rp_qc_v2.store import (
    get_job,
    get_workbook_version,
    list_finding_reviews,
    list_snapshots,
)


@pytest.mark.parametrize("app_server", ["saved_reviews"], indirect=True)
def test_restart_existing_data_and_legacy_delivery(
    page: Page, app_server, app_url: str, external_attempts
):
    assert app_server["seeded_jobs"]
    assert {job["state"] for job in app_server["seeded_jobs"]} == {
        "accepted", "rejected", "clarification_needed"
    }
    assert all(job["status"] == "completed" and job["result_sha256"] for job in app_server["seeded_jobs"])
    assert all(len(job["reviews"]) == 1 for job in app_server["seeded_jobs"])
    result_digests = {
        job["job_id"]: hashlib.sha256(job["result_path"].read_bytes()).hexdigest()
        for job in app_server["seeded_jobs"]
    }
    assert all(result_digests[job["job_id"]] == job["result_sha256"] for job in app_server["seeded_jobs"])
    before = app_server["saved_digests"]
    row_baseline = [
        (job["job_id"], job["status"], job["created_at_utc"], job["result_sha256"], job["reviews"])
        for job in app_server["seeded_jobs"]
    ]
    workbook_id = app_server["workbook_id"]
    jobs = app_server["seeded_jobs"]

    page.goto(f"{app_url}/app/workbooks/{workbook_id}")
    expect(page.get_by_role("heading", name="two-weeks.xlsx")).to_be_visible()
    expect(page.get_by_text("BC Tuần 31.08 - 05.09", exact=True)).to_be_visible()

    for job in jobs:
        response = page.request.get(f"{app_url}/jobs/{job['job_id']}")
        assert response.status == 200
        assert "completed" in response.text()
        assert job["state"] in response.text()
        for suffix, content_type in (
            ("export.csv", "text/csv"),
            ("export.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("review.md", "text/markdown"),
        ):
            download = page.request.get(f"{app_url}/jobs/{job['job_id']}/{suffix}")
            assert download.status == 200
            assert content_type in download.headers["content-type"]

    app_server["restart"]()
    page.reload()
    expect(page.get_by_role("heading", name="two-weeks.xlsx")).to_be_visible()
    expect(page.get_by_text("BC Tuần 31.08 - 05.09", exact=True)).to_be_visible()

    for job in jobs:
        response = page.request.get(f"{app_url}/jobs/{job['job_id']}")
        assert response.status == 200
        assert "completed" in response.text()
        assert job["note"] in response.text()
        result = page.request.get(f"{app_url}/jobs/{job['job_id']}/export.csv")
        assert result.status == 200

    after_rows = []
    for job in jobs:
        saved = get_job(app_server["database"], job["job_id"])
        assert saved is not None
        after_rows.append((
            job["job_id"], saved["status"], saved["created_at_utc"], saved["result"]["sha256"],
            list_finding_reviews(app_server["database"], job["job_id"]),
        ))
    assert after_rows == row_baseline
    assert {
        job["job_id"]: hashlib.sha256(job["result_path"].read_bytes()).hexdigest()
        for job in jobs
    } == result_digests
    assert len(jobs) == 3
    assert app_server["saved_digests"] == before
    assert get_workbook_version(app_server["database"], workbook_id) == app_server["workbook_record"]
    assert list_snapshots(app_server["database"]) == app_server["snapshot_records"]
    for key, digest in before.items():
        if key.startswith("authorized/"):
            assert hashlib.sha256(app_server["authorized_source_paths"][key].read_bytes()).hexdigest() == digest
            assert hashlib.sha256(app_server["authorized_copy_paths"][key].read_bytes()).hexdigest() == digest
        elif key == "technical/workbook":
            assert hashlib.sha256(app_server["workbook_path"].read_bytes()).hexdigest() == digest
        else:
            technical_path = app_server["data_root"] / key.removeprefix("technical/")
            assert hashlib.sha256(technical_path.read_bytes()).hexdigest() == digest
    with sqlite3.connect(app_server["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0] == len(jobs)
        assert connection.execute("SELECT COUNT(*) FROM finding_reviews").fetchone()[0] == len(jobs)


def test_legacy_delivery_failures_keep_status_and_mime_contracts(
    page: Page, app_url: str, external_attempts
):
    page.goto(f"{app_url}/app/")
    assert page.request.get(f"{app_url}/api/not-a-route").status == 404
    assert page.request.get(f"{app_url}/app/assets/no-such-asset.js").status == 404
    assert page.request.get(f"{app_url}/jobs/987654").status == 404
    assert page.request.get(f"{app_url}/jobs/987654/export.csv").status == 404
    assert page.request.post(f"{app_url}/jobs/987654/export.csv").status == 405


def test_injected_server_startup_failure_leaves_no_server_or_worker(app_server):
    def stop_before_ready(server):
        server.should_exit = True

    with (
        patch.object(uvicorn.Server, "run", autospec=True, side_effect=stop_before_ready),
        pytest.raises(RuntimeError, match="stopped before becoming ready"),
    ):
        app_server["restart"]()
    assert app_server["lifecycle_state"]() == {"server_running": False, "worker_running": False}
