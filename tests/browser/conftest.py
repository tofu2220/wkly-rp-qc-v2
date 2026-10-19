import hashlib
import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from test_jobs import job_fixture

from wkly_rp_qc_v2.jobs import JobWorker
from wkly_rp_qc_v2.store import (
    create_job,
    get_job,
    get_workbook_version,
    list_finding_reviews,
    list_snapshots,
    save_finding_review,
)
from wkly_rp_qc_v2.web import WebSettings, create_app


if Path("/etc/NIXOS").exists() and (node := shutil.which("node")):
    os.environ.setdefault("PLAYWRIGHT_NODEJS_PATH", node)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    if Path("/etc/NIXOS").exists():
        chromium = shutil.which("chromium")
        if chromium:
            return {**browser_type_launch_args, "executable_path": chromium}
    return browser_type_launch_args


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "block"}


@pytest.fixture
def app_server(request):
    with tempfile.TemporaryDirectory(prefix="wkly-rp-browser-") as temporary:
        root = Path(temporary)
        data_root = root / "server-data"
        seeded_jobs = []
        saved_digests = {}
        authorized_copy_paths = {}
        authorized_source_paths = {}
        workbook_path = data_root / "two-weeks.xlsx"

        if getattr(request, "param", None) == "saved_reviews":
            database, storage, _, base_job_id = job_fixture(data_root)
            replay_root = data_root / "replays"
            settings = WebSettings(database, storage, (replay_root,))
            worker = JobWorker(database, storage, (replay_root,))
            states = (
                ("accepted", None),
                ("rejected", "Technical rejection fixture"),
                ("clarification_needed", "Technical clarification fixture"),
            )
            base_job = get_job(database, base_job_id)
            assert base_job is not None
            job_ids = [base_job_id]
            for _ in states[1:]:
                job_ids.append(create_job(
                    database,
                    workbook_id=base_job["workbook_id"],
                    previous_snapshot_id=base_job["previous_snapshot_id"],
                    current_snapshot_id=base_job["current_snapshot_id"],
                    project_label=base_job["project_label"],
                    role=base_job["role"],
                    aliases=base_job["aliases"],
                    mode=base_job["mode"],
                    replay_bundle_id=base_job["replay_bundle_id"],
                ))

            for job_id, (state, note) in zip(job_ids, states, strict=True):
                with patch(
                    "wkly_rp_qc_v2.cli.complete_json",
                    side_effect=AssertionError("provider called while seeding restart fixture"),
                ) as seed_provider, patch(
                    "wkly_rp_qc_v2.openrouter.urllib.request.urlopen",
                    side_effect=AssertionError("network called while seeding restart fixture"),
                ) as seed_network:
                    worker.process_job(job_id)
                    seed_provider.assert_not_called()
                    seed_network.assert_not_called()
                save_finding_review(database, job_id, "P001", 0, state, note)
                job = get_job(database, job_id)
                assert job is not None and job["status"] == "completed" and job["result"]
                seeded_jobs.append({
                    "job_id": job_id,
                    "state": state,
                    "note": note or "",
                    "status": job["status"],
                    "created_at_utc": job["created_at_utc"],
                    "result_sha256": job["result"]["sha256"],
                    "result_path": Path(job["result"]["path"]),
                    "reviews": list_finding_reviews(database, job_id),
                })

            # Copy the authorized acceptance inputs into isolated temporary storage and
            # retain source/copy hashes. These copies are evidence only; the technical
            # job fixture above is the only data loaded by the server.
            from run_release_checks import required_acceptance_inputs

            authorized_inputs = required_acceptance_inputs()
            for label, source in authorized_inputs.items():
                if source.is_dir():
                    for member in sorted(path for path in source.rglob("*") if path.is_file()):
                        relative = member.relative_to(source)
                        destination = root / "authorized-inputs" / label / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(member, destination)
                        digest = hashlib.sha256(member.read_bytes()).hexdigest()
                        key = f"authorized/{label}/{relative.as_posix()}"
                        saved_digests[key] = digest
                        authorized_copy_paths[key] = destination
                        authorized_source_paths[key] = member
                        assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
                else:
                    destination = root / "authorized-inputs" / label / source.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    digest = hashlib.sha256(source.read_bytes()).hexdigest()
                    key = f"authorized/{label}"
                    saved_digests[key] = digest
                    authorized_copy_paths[key] = destination
                    authorized_source_paths[key] = source
                    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest

            saved_digests["technical/workbook"] = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
            for relative_root in ("normalized", "replays"):
                folder = data_root / relative_root
                for path in sorted(item for item in folder.rglob("*") if item.is_file()):
                    saved_digests[f"technical/{path.relative_to(data_root).as_posix()}"] = (
                        hashlib.sha256(path.read_bytes()).hexdigest()
                    )
        else:
            data_root.mkdir(parents=True, exist_ok=True)
            settings = WebSettings(data_root / "store.sqlite3", data_root / "files", ())

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        current = {"app": None, "server": None, "thread": None, "worker": None}

        def stop_server():
            server = current["server"]
            thread = current["thread"]
            app = current["app"]
            if server is not None and thread is not None and thread.is_alive():
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    server.force_exit = True
                    thread.join(timeout=2)
                assert not thread.is_alive(), "isolated Uvicorn server survived fixture teardown"
            worker = getattr(getattr(app, "state", None), "job_worker", None)
            worker_thread = getattr(worker, "_thread", None)
            assert worker_thread is None or not worker_thread.is_alive(), "job worker survived fixture teardown"
            current.update({"app": None, "server": None, "thread": None, "worker": None})

        def start_server():
            app = create_app(settings)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            thread = threading.Thread(target=server.run, name="isolated-browser-server", daemon=True)
            current.update({"app": app, "server": server, "thread": thread, "worker": app.state.job_worker})
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if server.started:
                        return
                    if not thread.is_alive():
                        raise RuntimeError("isolated Uvicorn server stopped before becoming ready")
                    time.sleep(0.05)
                raise RuntimeError("isolated Uvicorn server did not become ready")
            except BaseException:
                stop_server()
                raise

        def restart_server():
            stop_server()
            start_server()

        def lifecycle_state():
            thread = current["thread"]
            worker = current["worker"]
            worker_thread = getattr(worker, "_thread", None)
            return {
                "server_running": bool(thread and thread.is_alive()),
                "worker_running": bool(worker_thread and worker_thread.is_alive()),
            }

        with ExitStack() as guards:
            provider = guards.enter_context(patch(
                "wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")
            ))
            network = guards.enter_context(patch(
                "wkly_rp_qc_v2.openrouter.urllib.request.urlopen",
                side_effect=AssertionError("network called"),
            ))
            base_url = f"http://127.0.0.1:{port}"
            try:
                start_server()
                yield {
                    "base_url": base_url,
                    "database": settings.database_path,
                    "storage": settings.storage_dir,
                    "data_root": data_root,
                    "worker": current["worker"],
                    "restart": restart_server,
                    "stop": stop_server,
                    "lifecycle_state": lifecycle_state,
                    "seeded_jobs": seeded_jobs,
                    "saved_digests": saved_digests,
                    "authorized_copy_paths": authorized_copy_paths,
                    "authorized_source_paths": authorized_source_paths,
                    "workbook_id": seeded_jobs and get_job(settings.database_path, seeded_jobs[0]["job_id"])["workbook_id"],
                    "workbook_path": workbook_path,
                    "workbook_record": seeded_jobs and get_workbook_version(
                        settings.database_path,
                        get_job(settings.database_path, seeded_jobs[0]["job_id"])["workbook_id"],
                    ),
                    "snapshot_records": seeded_jobs and list_snapshots(settings.database_path),
                }
            finally:
                stop_server()
                provider.assert_not_called()
                network.assert_not_called()


@pytest.fixture
def app_url(app_server):
    return app_server["base_url"]


@pytest.fixture
def external_attempts(page, app_url):
    attempts = []
    local_origin = urlsplit(app_url)

    def guard(route):
        request_url = urlsplit(route.request.url)
        if (request_url.scheme, request_url.netloc) != (local_origin.scheme, local_origin.netloc):
            attempts.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    page.route("**/*", guard)
    yield attempts
    assert attempts == [], f"browser attempted external requests: {attempts}"
