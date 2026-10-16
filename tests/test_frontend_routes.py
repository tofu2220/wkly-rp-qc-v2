import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wkly_rp_qc_v2.web as web
from test_jobs import job_fixture
from test_web import ASGITestClient


class _PackageRoot:
    def __init__(self, missing_frontend: Path):
        self.missing_frontend = missing_frontend

    def __truediv__(self, name: str) -> Path:
        if name == "frontend_dist":
            return self.missing_frontend
        return Path(web.__file__).parent / name


class _PackageFile:
    def __init__(self, package_root: _PackageRoot):
        self.parent = package_root


class FrontendRouteTests(unittest.TestCase):
    def make_client(self, root: Path):
        settings = web.WebSettings(root / "store.sqlite3", root / "files", ())
        return ASGITestClient(web.create_app(settings))

    def test_legacy_home_links_to_explicit_react_entry(self):
        with tempfile.TemporaryDirectory() as temporary, self.make_client(Path(temporary)) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/app/"', response.text)

    def test_only_declared_app_navigation_shapes_serve_react_and_assets_keep_mime(self):
        with tempfile.TemporaryDirectory() as temporary, self.make_client(Path(temporary)) as client:
            home = client.get("/app/")
            workbook = client.get("/app/workbooks/987")
            self.assertEqual(home.status_code, 200)
            self.assertEqual(home.headers["content-type"], "text/html; charset=utf-8")
            self.assertEqual(home.headers["cache-control"], "no-cache")
            self.assertEqual(workbook.status_code, 200)
            self.assertEqual(client.get("/app/jobs/1").status_code, 404)
            self.assertEqual(client.get("/app/assets/unknown.js").status_code, 404)
            self.assertEqual(client.get("/app/assets/unknown.css").status_code, 404)
            asset_root = Path(web.__file__).parent / "frontend_dist/assets"
            javascript = next(asset_root.glob("*.js"))
            asset = client.get(f"/app/assets/{javascript.name}")
            self.assertEqual(asset.status_code, 200)
            self.assertIn(asset.headers["content-type"], {"text/javascript; charset=utf-8", "application/javascript"})
            self.assertEqual(client.post("/app/").status_code, 405)

    def test_api_and_legacy_job_download_failures_are_not_replaced_by_react_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, storage, _, job_id = job_fixture(root)
            with ASGITestClient(web.create_app(web.WebSettings(database, storage, ()))) as client:
                unknown_api = client.get("/api/does-not-exist")
                missing_job = client.get("/jobs/987654")
                missing_download = client.get("/jobs/987654/export.csv")
                incomplete_export = client.get(f"/jobs/{job_id}/export.csv")
                method_error = client.post("/jobs/987654/export.csv")
            self.assertEqual(unknown_api.status_code, 404)
            self.assertIn("application/json", unknown_api.headers["content-type"])
            self.assertEqual(missing_job.status_code, 404)
            self.assertEqual(missing_download.status_code, 404)
            self.assertEqual(incomplete_export.status_code, 409)
            self.assertNotIn("<html", unknown_api.text.lower())
            self.assertEqual(method_error.status_code, 405)

    def test_missing_frontend_build_returns_503_without_breaking_legacy_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_dist = root / "frontend_dist"
            package_root = _PackageRoot(missing_dist)
            path_factory = lambda value: _PackageFile(package_root) if value == web.__file__ else Path(value)
            with patch.object(web, "Path", side_effect=path_factory):
                with self.make_client(root) as client:
                    missing = client.get("/app/")
                    home = client.get("/")
            self.assertEqual(missing.status_code, 503)
            self.assertIn("build frontend assets first", missing.json()["detail"])
            self.assertEqual(home.status_code, 200)


if __name__ == "__main__":
    unittest.main()
