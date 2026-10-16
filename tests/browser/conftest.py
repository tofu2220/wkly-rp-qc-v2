import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
import pytest
import uvicorn

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
def app_server():
    with tempfile.TemporaryDirectory(prefix="wkly-rp-browser-") as temporary:
        root = Path(temporary)
        settings = WebSettings(root / "store.sqlite3", root / "files", ())
        app = create_app(settings)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="isolated-browser-server", daemon=True)
        with ExitStack() as guards:
            provider = guards.enter_context(patch(
                "wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")
            ))
            network = guards.enter_context(patch(
                "wkly_rp_qc_v2.openrouter.urllib.request.urlopen",
                side_effect=AssertionError("network called"),
            ))
            thread.start()
            base_url = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if server.started:
                        break
                    if not thread.is_alive():
                        raise RuntimeError("isolated Uvicorn server stopped before becoming ready")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("isolated Uvicorn server did not become ready")
                yield {
                    "base_url": base_url,
                    "database": settings.database_path,
                    "worker": app.state.job_worker,
                }
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    server.force_exit = True
                    thread.join(timeout=2)
                assert not thread.is_alive(), "isolated Uvicorn server survived fixture teardown"
                worker = app.state.job_worker._thread
                assert worker is None or not worker.is_alive(), "job worker survived fixture teardown"
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
