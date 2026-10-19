import json
import os
from pathlib import Path, PurePosixPath
import select
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

import httpx

from run_frontend_checks import validate_artifacts
from test_jobs import job_fixture
from test_web import workbook_bytes
from wkly_rp_qc_v2.jobs import JobWorker


ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
SERVER_PROGRAM = r'''\
import asyncio, sys, threading, time
from pathlib import Path
from unittest.mock import patch
import uvicorn
import wkly_rp_qc_v2
from wkly_rp_qc_v2.web import WebSettings, create_app

root = Path(sys.argv[1])
port = int(sys.argv[2])
print("IMPORT_PATH=" + str(Path(wkly_rp_qc_v2.__file__).resolve()), flush=True)
with patch("wkly_rp_qc_v2.cli.complete_json", side_effect=AssertionError("provider called")) as provider, patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=AssertionError("network called")) as network:
    settings = WebSettings(root / "app.sqlite3", root / "storage", (root / "replays",))
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not server.started:
        if not thread.is_alive():
            raise RuntimeError("server stopped before ready")
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("installed server readiness timeout")
    print("READY", flush=True)
    sys.stdin.readline()
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
    if thread.is_alive():
        raise RuntimeError("installed server did not stop")
    provider.assert_not_called()
    network.assert_not_called()
'''


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        if UV is None:
            self.fail("uv is required to build the planned wheel and sdist artifacts")

    def assert_safe_members(self, names: list[str]) -> None:
        for name in names:
            path = PurePosixPath(name)
            self.assertFalse(path.is_absolute() or ".." in path.parts, f"unsafe archive member: {name}")
            lowered = name.casefold()
            self.assertNotIn("node_modules", lowered)
            self.assertNotIn(".env", lowered)
            self.assertFalse(lowered.endswith((".xlsx", ".sqlite3", ".db")), name)

    def wheel_package_files(self, wheel: Path) -> tuple[dict[str, bytes], list[str]]:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            self.assert_safe_members(names)
            files = {
                name.removeprefix("wkly_rp_qc_v2/"): archive.read(name)
                for name in names
                if name.startswith("wkly_rp_qc_v2/") and not name.endswith("/")
            }
        self.assertIn("frontend_dist/index.html", files)
        return files, names

    def sdist_package_files(self, sdist: Path, destination: Path) -> tuple[dict[str, bytes], list[str]]:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            self.assert_safe_members(names)
            files: dict[str, bytes] = {}
            for member in members:
                path = PurePosixPath(member.name)
                if not member.isfile():
                    continue
                full_path = destination / Path(*path.parts)
                full_path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                content = source.read()
                full_path.write_bytes(content)
                if "src" not in path.parts or "wkly_rp_qc_v2" not in path.parts:
                    continue
                package_index = path.parts.index("wkly_rp_qc_v2")
                relative = PurePosixPath(*path.parts[package_index + 1:]).as_posix()
                files[relative] = content
        self.assertIn("frontend_dist/index.html", files)
        return files, names

    def validate_staged_graph(self, package_files: dict[str, bytes], root: Path) -> None:
        package_root = root / "src/wkly_rp_qc_v2"
        package_root.mkdir(parents=True, exist_ok=True)
        for relative, content in package_files.items():
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        graph = validate_artifacts(root)
        self.assertGreater(len(graph), 0)

    def install_and_smoke(self, wheel: Path, root: Path, sample_upload: bytes) -> None:
        root.mkdir(parents=True, exist_ok=True)
        environment = root / "venv"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["UV_PROJECT_ENVIRONMENT"] = str(environment)
        subprocess.run([UV, "sync", "--locked", "--project", str(ROOT), "--no-install-project", "--no-dev"],
                       cwd=root, env=env, check=True, timeout=180)
        python = environment / "bin/python"
        subprocess.run([UV, "pip", "install", "--offline", "--python", str(python), "--no-deps", str(wheel)],
                       cwd=root, env=env, check=True, timeout=60)

        import_probe = subprocess.run(
            [str(python), "-c", "import wkly_rp_qc_v2; print(wkly_rp_qc_v2.__file__)"],
            cwd=root, env=env, check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        self.assertTrue(import_probe.startswith(str(environment)), import_probe)
        self.assertNotIn(str(ROOT), import_probe)
        self.assertIsNone(shutil.which("node", path=""))

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server_root = root / "runtime-data"
        server_root.mkdir()
        database, storage, _, legacy_job_id = job_fixture(server_root)
        JobWorker(database, storage, (server_root / "replays",)).process_job(legacy_job_id)
        child_env = dict(env)
        child_env["PATH"] = str(root / "empty-path")
        process = subprocess.Popen(
            [str(python), "-c", SERVER_PROGRAM, str(server_root), str(port)],
            cwd=root, env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        try:
            ready_lines: list[str] = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"installed server exited early: {process.stderr.read()}")
                if select.select([process.stdout], [], [], 0.2)[0]:
                    line = process.stdout.readline().strip()
                    ready_lines.append(line)
                    if line == "READY":
                        break
            else:
                self.fail("installed server did not become ready")
            reported_import = next(line.split("=", 1)[1] for line in ready_lines if line.startswith("IMPORT_PATH="))
            self.assertTrue(reported_import.startswith(str(environment)), reported_import)
            self.assertNotIn(str(ROOT), reported_import)

            base_url = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base_url, timeout=10) as client:
                uploaded = client.post("/api/workbooks", files={
                    "workbook": ("technical.xlsx", sample_upload,
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                })
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                workbook_id = uploaded.json()["workbook_id"]
                preview = client.get(f"/api/workbooks/{workbook_id}")
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["workbook_id"], workbook_id)
                app_page = client.get("/app/")
                self.assertEqual(app_page.status_code, 200)
                self.assertEqual(app_page.headers["content-type"], "text/html; charset=utf-8")
                script = __import__("re").search(r'src="/app/assets/([^\"]+\.js)"', app_page.text)
                self.assertIsNotNone(script, app_page.text)
                asset = client.get(f"/app/assets/{script.group(1)}")
                self.assertEqual(asset.status_code, 200)
                self.assertIn("javascript", asset.headers["content-type"])
                legacy_job = client.get(f"/jobs/{legacy_job_id}")
                self.assertEqual(legacy_job.status_code, 200)
                self.assertIn("text/html", legacy_job.headers["content-type"])
                export = client.get(f"/jobs/{legacy_job_id}/export.csv")
                self.assertEqual(export.status_code, 200)
                self.assertIn("text/csv", export.headers["content-type"])
                self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
                spreadsheet = client.get(f"/jobs/{legacy_job_id}/export.xlsx")
                self.assertEqual(spreadsheet.status_code, 200)
                self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", spreadsheet.headers["content-type"])
                markdown = client.get(f"/jobs/{legacy_job_id}/review.md")
                self.assertEqual(markdown.status_code, 200)
                self.assertIn("text/markdown", markdown.headers["content-type"])
                missing_asset = client.get("/app/assets/no-such-file.js")
                self.assertEqual(missing_asset.status_code, 404)
                missing_static = client.get("/static/no-such-file.css")
                self.assertEqual(missing_static.status_code, 404)
                missing_api = client.get("/api/no-such-route")
                self.assertEqual(missing_api.status_code, 404)
                self.assertIn("application/json", missing_api.headers["content-type"])
                missing_job = client.get("/jobs/999999")
                missing_download = client.get("/jobs/999999/export.csv")
                unsupported_method = client.post(f"/jobs/{legacy_job_id}/export.csv")
                self.assertEqual(missing_job.status_code, 404)
                self.assertEqual(missing_download.status_code, 404)
                self.assertEqual(unsupported_method.status_code, 405)

            import sqlite3
            with sqlite3.connect(server_root / "app.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM workbook_versions").fetchone()[0], 2)
        finally:
            if process.poll() is None:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    process.wait(timeout=15)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.read())

    def test_wheel_sdist_rebuild_and_installed_runtime_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix="wkly-package-artifact-") as temporary:
            temp = Path(temporary)
            archives = temp / "archives"
            archives.mkdir()
            subprocess.run([UV, "build", "--out-dir", str(archives)], cwd=ROOT, check=True, timeout=180)
            wheel = next(archives.glob("*.whl"))
            sdist = next(archives.glob("*.tar.gz"))
            wheel_files, wheel_members = self.wheel_package_files(wheel)
            self.assertTrue(any(name.endswith("/frontend_dist/index.html") for name in wheel_members))
            self.validate_staged_graph(wheel_files, temp / "wheel-staged")

            sdist_files, sdist_members = self.sdist_package_files(sdist, temp / "sdist-extract")
            self.assertTrue(any(name.endswith("/src/wkly_rp_qc_v2/frontend_dist/index.html") for name in sdist_members))
            self.validate_staged_graph(sdist_files, temp / "sdist-staged")

            source_dir = temp / "sdist-extract"
            project_dir = next(source_dir.rglob("pyproject.toml")).parent
            rebuilt_dir = temp / "rebuilt-wheel"
            rebuilt_dir.mkdir()
            tools = temp / "no-node-path"
            tools.mkdir()
            quiet_env = dict(os.environ)
            quiet_env["PATH"] = str(tools)
            quiet_env.pop("PYTHONPATH", None)
            subprocess.run([UV, "build", "--wheel", "--out-dir", str(rebuilt_dir)],
                           cwd=project_dir, env=quiet_env, check=True, timeout=180)
            rebuilt_wheel = next(rebuilt_dir.glob("*.whl"))
            rebuilt_files, rebuilt_members = self.wheel_package_files(rebuilt_wheel)
            self.validate_staged_graph(rebuilt_files, temp / "rebuilt-staged")
            self.assertNotIn("node_modules", " ".join(sdist_members).casefold())

            sample_upload = workbook_bytes()
            for index, installed_wheel in enumerate((wheel, rebuilt_wheel), start=1):
                with self.subTest(wheel=index):
                    self.install_and_smoke(installed_wheel, temp / f"installed-{index}", sample_upload)


if __name__ == "__main__":
    unittest.main()
