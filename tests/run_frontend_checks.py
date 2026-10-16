"""Build and verify the local production frontend before running consumers."""

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_INPUTS = (
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "index.html",
    "src/App.tsx",
)
RELEASE_RUNNER = Path(__file__).resolve().with_name("run_release_checks.py")


class FrontendCheckError(RuntimeError):
    """A committed frontend prerequisite or emitted asset graph is invalid."""


class _AssetTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append(values["src"] or "")
        if tag == "link" and values.get("href"):
            rel = (values.get("rel") or "").split()
            if any(value in {"stylesheet", "modulepreload", "icon"} for value in rel):
                self.references.append(values["href"] or "")


def _asset_path(reference: str, *, output: Path, referring_file: Path) -> Path | None:
    if not reference or reference.startswith(("data:", "#")):
        return None
    reference = reference.split("?", 1)[0].split("#", 1)[0]
    if not reference:
        return None
    if reference.startswith("/"):
        if not reference.startswith("/app/assets/"):
            raise FrontendCheckError(f"frontend asset reference is outside /app/assets/: {reference}")
        relative = reference.removeprefix("/app/assets/")
        candidate = output / "assets" / relative
    else:
        candidate = referring_file.parent / reference
    resolved = candidate.resolve()
    assets_root = (output / "assets").resolve()
    if resolved != assets_root and assets_root not in resolved.parents:
        raise FrontendCheckError(f"frontend asset reference escapes frontend_dist/assets: {reference}")
    return resolved


def validate_artifacts(root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Validate index and every referenced/imported local asset without building."""
    output = root / "src/wkly_rp_qc_v2/frontend_dist"
    index = output / "index.html"
    if not index.is_file():
        raise FrontendCheckError(f"Missing generated frontend artifact: {index}")
    if not (output / "assets").is_dir():
        raise FrontendCheckError(f"Missing generated frontend asset directory: {output / 'assets'}")

    parser = _AssetTags()
    parser.feed(index.read_text(encoding="utf-8"))
    pending = [_asset_path(ref, output=output, referring_file=index) for ref in parser.references]
    visited: list[Path] = []
    while pending:
        asset = pending.pop()
        if asset is None or asset in visited:
            continue
        if not asset.is_file():
            raise FrontendCheckError(f"Missing referenced frontend asset: {asset.relative_to(output)}")
        visited.append(asset)
        text = asset.read_text(encoding="utf-8", errors="strict")
        refs: list[str] = []
        if asset.suffix in {".js", ".mjs", ".css"}:
            refs.extend(re.findall(r"(?:from\s*|import\s*(?:\(\s*)?)['\"]([^'\"]+)['\"]", text))
            refs.extend(re.findall(r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", text))
        for reference in refs:
            # Package-local module specifiers in a built bundle should be relative or
            # app-absolute. Bare imports would require a runtime Node/module resolver.
            if not reference.startswith((".", "/", "data:", "#")):
                raise FrontendCheckError(f"Unbundled frontend import requires runtime resolution: {reference}")
            pending.append(_asset_path(reference, output=output, referring_file=asset))
    return tuple(visited)


def _validate_inputs(root: Path) -> Path:
    frontend = root / "frontend"
    for relative in FRONTEND_INPUTS:
        path = frontend / relative
        if not path.is_file():
            raise FrontendCheckError(f"Missing committed frontend build input: frontend/{relative}")
    try:
        manifest: Any = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FrontendCheckError(f"Invalid frontend/package.json: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("scripts"), dict) or not manifest["scripts"].get("build"):
        raise FrontendCheckError("frontend/package.json must define scripts.build")
    return frontend


def build_frontend(root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Install exact locked npm dependencies, build, then validate emitted assets."""
    frontend = _validate_inputs(Path(root).resolve())
    subprocess.run(["npm", "ci"], cwd=frontend, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    return validate_artifacts(Path(root).resolve())


class _OutcomeGuard:
    def __init__(self) -> None:
        self.collected = 0
        self.executed = 0
        self.skipped: list[str] = []

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "call":
            self.executed += 1
        if report.skipped or getattr(report, "wasxfail", False):
            self.skipped.append(report.nodeid)


def _pytest_targets(targets: list[str]) -> int:
    if not targets:
        print("No pytest targets were provided after --pytest.", file=sys.stderr)
        return 2
    guard = _OutcomeGuard()
    result = int(pytest.main(targets, plugins=[guard]))
    if result != 0:
        return result
    if guard.collected == 0 or guard.executed == 0:
        print(
            f"Frontend checks require collected and executed tests; collected={guard.collected}, executed={guard.executed}.",
            file=sys.stderr,
        )
        return int(pytest.ExitCode.NO_TESTS_COLLECTED)
    if guard.skipped:
        print(f"Required frontend checks were skipped or xfailed: {', '.join(guard.skipped)}", file=sys.stderr)
        return int(pytest.ExitCode.TESTS_FAILED)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--pytest", nargs=argparse.REMAINDER, metavar="TARGET")
    modes.add_argument("--release-all", action="store_true")
    modes.add_argument("--check-artifacts", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.check_artifacts:
        try:
            assets = validate_artifacts()
        except (FrontendCheckError, OSError) as error:
            print(f"Frontend artifact readiness failed: {error}", file=sys.stderr)
            return 1
        print(f"Frontend artifact graph ready: {len(assets)} local assets")
        return 0

    if args.release_all and not RELEASE_RUNNER.is_file():
        print(f"Strict release runner is not available yet: {RELEASE_RUNNER}", file=sys.stderr)
        return 1

    try:
        build_frontend()
    except (FrontendCheckError, OSError, subprocess.CalledProcessError) as error:
        print(f"Frontend preparation failed: {error}", file=sys.stderr)
        return int(error.returncode) if isinstance(error, subprocess.CalledProcessError) else 1

    if args.release_all:
        completed = subprocess.run([sys.executable, str(RELEASE_RUNNER), "--all"], cwd=REPO_ROOT, check=False)
        return completed.returncode
    return _pytest_targets(args.pytest or [])


if __name__ == "__main__":
    raise SystemExit(main())
