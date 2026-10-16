import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_frontend_checks


class FrontendBuildRunnerTests(unittest.TestCase):
    def make_root(self, directory: str) -> Path:
        root = Path(directory)
        frontend = root / "frontend"
        (frontend / "src").mkdir(parents=True)
        (frontend / "package.json").write_text(
            json.dumps({"scripts": {"build": "tsc --noEmit && vite build"}}), encoding="utf-8"
        )
        (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
        (frontend / "tsconfig.json").write_text("{}", encoding="utf-8")
        (frontend / "index.html").write_text("<script type=\"module\" src=\"/app/assets/app.js\"></script>", encoding="utf-8")
        (frontend / "src" / "App.tsx").write_text("export default {}", encoding="utf-8")
        return root

    def write_output(self, root: Path) -> None:
        output = root / "src/wkly_rp_qc_v2/frontend_dist/assets"
        output.mkdir(parents=True, exist_ok=True)
        (root / "src/wkly_rp_qc_v2/frontend_dist/index.html").write_text(
            '<script type="module" src="/app/assets/app.js"></script>', encoding="utf-8"
        )
        (output / "app.js").write_text('import "./style.css";', encoding="utf-8")
        (output / "style.css").write_text("body { color: black; }", encoding="utf-8")

    def test_builds_missing_generated_outputs_before_artifact_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            commands = []

            def run(command, *, cwd, check):
                commands.append((command, cwd, check))
                if command[-1] == "build":
                    self.write_output(root)

            with patch.object(run_frontend_checks.subprocess, "run", side_effect=run):
                run_frontend_checks.build_frontend(root)

            self.assertEqual([item[0] for item in commands], [["npm", "ci"], ["npm", "run", "build"]])
            self.assertTrue(all(item[1] == root / "frontend" and item[2] for item in commands))

    def test_missing_committed_source_inputs_fail_before_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            (root / "frontend/package-lock.json").unlink()
            with patch.object(run_frontend_checks.subprocess, "run") as run:
                with self.assertRaisesRegex(run_frontend_checks.FrontendCheckError, "package-lock.json"):
                    run_frontend_checks.build_frontend(root)
            run.assert_not_called()

    def test_missing_build_script_fails_before_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            (root / "frontend/package.json").write_text(json.dumps({"scripts": {}}), encoding="utf-8")
            with patch.object(run_frontend_checks.subprocess, "run") as run:
                with self.assertRaisesRegex(run_frontend_checks.FrontendCheckError, "scripts.build"):
                    run_frontend_checks.build_frontend(root)
            run.assert_not_called()

    def test_failed_locked_install_stops_before_build_or_consumers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            with patch.object(run_frontend_checks.subprocess, "run", side_effect=run_frontend_checks.subprocess.CalledProcessError(1, ["npm", "ci"])) as run:
                with self.assertRaises(run_frontend_checks.subprocess.CalledProcessError):
                    run_frontend_checks.build_frontend(root)
            self.assertEqual(run.call_count, 1)

    def test_failed_build_does_not_use_existing_or_stale_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            self.write_output(root)
            with patch.object(
                run_frontend_checks.subprocess, "run",
                side_effect=[None, run_frontend_checks.subprocess.CalledProcessError(1, ["npm", "run", "build"])],
            ) as run:
                with self.assertRaises(run_frontend_checks.subprocess.CalledProcessError):
                    run_frontend_checks.build_frontend(root)
            self.assertEqual(run.call_count, 2)

    def test_missing_or_broken_emitted_graph_fails_after_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            with patch.object(run_frontend_checks.subprocess, "run", side_effect=lambda *args, **kwargs: None):
                with self.assertRaisesRegex(run_frontend_checks.FrontendCheckError, "frontend_dist/index.html"):
                    run_frontend_checks.build_frontend(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            (root / "src/wkly_rp_qc_v2/frontend_dist/assets").mkdir(parents=True)
            (root / "src/wkly_rp_qc_v2/frontend_dist/index.html").write_text(
                '<script type="module" src="/app/assets/missing.js"></script>', encoding="utf-8"
            )
            with patch.object(run_frontend_checks.subprocess, "run", side_effect=lambda *args, **kwargs: None):
                with self.assertRaisesRegex(run_frontend_checks.FrontendCheckError, "missing.js"):
                    run_frontend_checks.build_frontend(root)

    def test_pytest_preserves_no_tests_and_failed_tests_exit_codes(self):
        with patch.object(run_frontend_checks, "build_frontend"), patch.object(
            run_frontend_checks.pytest, "main", return_value=5
        ) as pytest_main:
            self.assertEqual(run_frontend_checks.main(["--pytest", "tests/no_tests.py"]), 5)
            pytest_main.assert_called_once()

        with patch.object(run_frontend_checks, "build_frontend"), patch.object(
            run_frontend_checks.pytest, "main", return_value=1
        ):
            self.assertEqual(run_frontend_checks.main(["--pytest", "tests/test_bad.py"]), 1)

    def test_pytest_rejects_skipped_or_xfailed_targets_even_if_pytest_returns_zero(self):
        class FakeReport:
            when = "call"
            skipped = True
            wasxfail = False
            nodeid = "tests/test_target.py::test_target"

        class Plugin:
            def __init__(self):
                self.reports = []

            def pytest_runtest_logreport(self, report):
                self.reports.append(report)

        plugin = Plugin()

        def pytest_main(args, plugins):
            plugins[0].pytest_runtest_logreport(FakeReport())
            return 0

        with patch.object(run_frontend_checks, "build_frontend"), patch.object(
            run_frontend_checks.pytest, "main", side_effect=pytest_main
        ):
            self.assertNotEqual(run_frontend_checks.main(["--pytest", "tests/test_target.py"]), 0)

    def test_release_all_requires_the_later_strict_runner_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            with patch.object(run_frontend_checks, "REPO_ROOT", root), patch.object(
                run_frontend_checks.subprocess, "run"
            ) as run:
                self.assertNotEqual(run_frontend_checks.main(["--release-all"]), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
