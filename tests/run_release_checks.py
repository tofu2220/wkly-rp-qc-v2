"""Fail-closed exact pytest/unittest regression gate for release evidence.

Maintainer invocation from a clean checkout: run the checked frontend builder
first, then this script's ``--backend`` mode. ``--all`` requires that same
production build and adds browser/package targets.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
REPLAY_NAME = "20261012T031440747089Z"
PACKET_STAGE = "review_recorded"
FIXTURE_ENV = "WKLY_RP_QC_FIXTURE_ROOT"
INTERNAL_NEGATIVE_RUN = "GSD_RELEASE_CHECKS_INTERNAL"
RAW_INPUTS = (
    "extraction_response.json",
    "assessment_response.json",
    "run_notes.md",
)


def required_acceptance_inputs(
    fixture_root: Path | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    environ: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Resolve all source, replay, packet, candidate, and reference files; raise on any gap."""
    environment = os.environ if environ is None else environ
    root = Path(fixture_root or environment.get(FIXTURE_ENV, repo_root)).expanduser().resolve()
    path = lambda variable, fallback: Path(environment.get(variable, str(root / fallback))).expanduser().resolve()
    replay_run = path("WKLY_RP_QC_REPLAY_RUN", f"results/smart-parking/{REPLAY_NAME}")
    inputs = {
        "workbook": path("WKLY_RP_QC_SAMPLE_WORKBOOK", "docs/Khối SMC_Báo cáo tuần chi tiết.xlsx"),
        "normalized": path("WKLY_RP_QC_SAMPLE_NORMALIZED", "download/1-raw/normalized_rows.json"),
        "replay_run": replay_run,
        "replay_root": replay_run.parent,
        "packet": path("WKLY_RP_QC_DISPOSITION_PACKET", "docs/phase-4-evaluation/review-dispositions-v1.md"),
        "candidate_manifest": path("WKLY_RP_QC_CANDIDATE_MANIFEST", "docs/phase-4-evaluation/candidate-cases.json"),
        "business_review_reference": path("WKLY_RP_QC_BUSINESS_REFERENCE", "docs/phase-4-evaluation/business-review-form.md"),
        "draft_reference": path("WKLY_RP_QC_DRAFT_REFERENCE", "docs/phase-4-evaluation/gpt-6.1-draft-review.md"),
    }
    for filename in RAW_INPUTS:
        inputs[f"replay_{filename}"] = replay_run / filename

    # Reuse the frozen packet's complete, exact input inventory rather than
    # maintaining a second, inevitably drifting list in the release runner.
    import test_disposition_packet

    for relative in test_disposition_packet.EXPECTED_HASHES:
        inputs[f"packet_input:{relative}"] = root / relative
    directories = {"replay_run", "replay_root"}
    missing = [
        f"{name}: {value}"
        for name, value in inputs.items()
        if not (value.is_dir() if name in directories else value.is_file())
    ]
    if missing:
        raise FileNotFoundError("Required acceptance inputs are missing:\n - " + "\n - ".join(missing))
    return inputs


def run_packet_gate() -> int:
    """Always validate the packet's real stage and actual user confirmation."""
    import test_disposition_packet

    return test_disposition_packet.run_artifact_gate(PACKET_STAGE, require_confirmation=True)


def pytest_evidence_passes(
    collected_ids: list[str],
    executed_ids: list[str],
    outcomes: dict[str, dict[str, str]],
    exit_code: int,
) -> bool:
    """Validate count, identity, and every setup/call/teardown outcome without parsing output."""
    expected_phases = {"setup", "call", "teardown"}
    return (
        exit_code == int(pytest.ExitCode.OK)
        and bool(collected_ids)
        and len(collected_ids) == len(set(collected_ids))
        and collected_ids == executed_ids
        and len(collected_ids) == len(executed_ids)
        and set(outcomes) == set(collected_ids)
        and all(phases == {phase: "passed" for phase in expected_phases} for phases in outcomes.values())
    )


def _is_browser_nodeid(nodeid: str) -> bool:
    path = Path(nodeid.split("::", 1)[0])
    parts = path.parts
    return any(parts[index : index + 2] == ("tests", "browser") for index in range(len(parts) - 1))


_PYTEST_EVIDENCE_PLUGIN = '''
import json
import os
from pathlib import Path

class EvidencePlugin:
    def __init__(self):
        self.ids = []
        self.mapping = {}
        self.outcomes = {}

    def pytest_collection_finish(self, session):
        self.mapping = {
            item.nodeid: str(item.path) + item.nodeid[item.nodeid.find("::"):]
            for item in session.items
        }
        self.ids = list(self.mapping.values())

    def pytest_runtest_logreport(self, report):
        nodeid = self.mapping.get(report.nodeid)
        if nodeid is None:
            return
        phases = self.outcomes.setdefault(nodeid, {})
        if report.skipped or getattr(report, "wasxfail", False):
            phases[report.when] = "skip" if report.skipped else "xfail"
        elif report.failed:
            phases[report.when] = "failed"
        else:
            phases[report.when] = "passed"

    def pytest_sessionfinish(self, session, exitstatus):
        payload = {"nodeids": self.ids, "outcomes": self.outcomes}
        Path(os.environ["GSD_RELEASE_EVIDENCE_REPORT"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

evidence_plugin = EvidencePlugin()
pytest_collection_finish = evidence_plugin.pytest_collection_finish
pytest_runtest_logreport = evidence_plugin.pytest_runtest_logreport
pytest_sessionfinish = evidence_plugin.pytest_sessionfinish
'''


def _run_pytest(targets: list[str], evidence: Path) -> dict[str, Any]:
    plugin = evidence / "gsd_release_pytest_plugin.py"
    plugin.write_text(_PYTEST_EVIDENCE_PLUGIN, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(evidence), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    collection_report = evidence / "pytest-collection.json"
    env["GSD_RELEASE_EVIDENCE_REPORT"] = str(collection_report)
    try:
        collection = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "gsd_release_pytest_plugin", "--collect-only", "-q", *targets],
            cwd=REPO_ROOT, env=env, capture_output=True, check=False, timeout=900,
        )
    except subprocess.TimeoutExpired as error:
        (evidence / "pytest-collection.stdout.log").write_bytes(_raw_bytes(error.stdout))
        (evidence / "pytest-collection.stderr.log").write_bytes(_raw_bytes(error.stderr))
        return {
            "collection_exit_code": 124,
            "collected_ids": [],
            "executed_ids": [],
            "collected_count": 0,
            "executed_count": 0,
            "outcomes": {},
            "error": "collection subprocess timed out and was terminated",
        }
    (evidence / "pytest-collection.stdout.log").write_bytes(_raw_bytes(collection.stdout))
    (evidence / "pytest-collection.stderr.log").write_bytes(_raw_bytes(collection.stderr))
    collect_code = collection.returncode
    try:
        collected_ids = json.loads(collection_report.read_text(encoding="utf-8"))["nodeids"]
    except (OSError, json.JSONDecodeError, KeyError):
        collected_ids = []
    if collect_code != int(pytest.ExitCode.OK) or not collected_ids:
        return {
            "collection_exit_code": collect_code,
            "collected_ids": collected_ids,
            "executed_ids": [],
            "collected_count": len(collected_ids),
            "executed_count": 0,
            "outcomes": {},
            "error": "collection failed or selected zero tests",
        }

    groups = {
        "backend": [nodeid for nodeid in collected_ids if not _is_browser_nodeid(nodeid)],
        "browser": [nodeid for nodeid in collected_ids if _is_browser_nodeid(nodeid)],
    }
    groups = {name: nodeids for name, nodeids in groups.items() if nodeids}
    outcomes: dict[str, dict[str, str]] = {}
    executed_nodeids: list[str] = []
    execution_nodeids: list[str] = []
    execution_codes: dict[str, int] = {}
    execution_groups: dict[str, dict[str, Any]] = {}
    execution_ids_match = True
    stdout_logs: list[bytes] = []
    stderr_logs: list[bytes] = []
    for name, nodeids in groups.items():
        execution_report = evidence / f"pytest-execution-{name}.json"
        env["GSD_RELEASE_EVIDENCE_REPORT"] = str(execution_report)
        try:
            execution = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "gsd_release_pytest_plugin", "-q", "-s", *nodeids],
                cwd=REPO_ROOT, env=env, capture_output=True, check=False, timeout=900,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _raw_bytes(error.stdout)
            stderr = _raw_bytes(error.stderr)
            (evidence / f"pytest-execution-{name}.stdout.log").write_bytes(stdout)
            (evidence / f"pytest-execution-{name}.stderr.log").write_bytes(stderr)
            stdout_logs.append(f"=== {name} (timed out) ===\n".encode() + stdout)
            stderr_logs.append(f"=== {name} (timed out) ===\n".encode() + stderr)
            execution_codes[name] = 124
            execution_ids_match = False
            continue

        stdout = _raw_bytes(execution.stdout)
        stderr = _raw_bytes(execution.stderr)
        (evidence / f"pytest-execution-{name}.stdout.log").write_bytes(stdout)
        (evidence / f"pytest-execution-{name}.stderr.log").write_bytes(stderr)
        stdout_logs.append(f"=== {name} ===\n".encode() + stdout)
        stderr_logs.append(f"=== {name} ===\n".encode() + stderr)
        execution_codes[name] = execution.returncode
        try:
            execution_data = json.loads(execution_report.read_text(encoding="utf-8"))
            group_nodeids = execution_data["nodeids"]
            group_outcomes = execution_data["outcomes"]
        except (OSError, json.JSONDecodeError, KeyError):
            group_nodeids, group_outcomes = [], {}
        execution_ids_match = execution_ids_match and group_nodeids == nodeids
        execution_groups[name] = {"expected_ids": nodeids, "executed_ids": group_nodeids}
        execution_nodeids.extend(group_nodeids)
        outcomes.update(group_outcomes)
        executed_nodeids.extend(nodeid for nodeid, phases in group_outcomes.items() if "call" in phases)

    (evidence / "pytest-execution.stdout.log").write_bytes(b"\n".join(stdout_logs))
    (evidence / "pytest-execution.stderr.log").write_bytes(b"\n".join(stderr_logs))
    run_code = 0 if execution_codes and all(code == int(pytest.ExitCode.OK) for code in execution_codes.values()) else 1
    executed_ids = [nodeid for nodeid in collected_ids if nodeid in executed_nodeids]
    execution_ids_match = (
        execution_ids_match
        and len(execution_nodeids) == len(collected_ids)
        and set(execution_nodeids) == set(collected_ids)
    )
    executed = [nodeid for nodeid in collected_ids if "call" in outcomes.get(nodeid, {})]
    mandatory_bad = {
        nodeid: phases for nodeid, phases in outcomes.items()
        if any(value in {"failed", "skip", "xfail"} for value in phases.values())
    }
    return {
        "collection_exit_code": collect_code,
        "execution_exit_code": run_code,
        "collected_ids": collected_ids,
        "executed_ids": executed,
        "collected_count": len(collected_ids),
        "executed_count": len(executed),
        "outcomes": outcomes,
        "bad_outcomes": mandatory_bad,
        "execution_codes": execution_codes,
        "execution_groups": execution_groups,
        "exact_ids_match": execution_ids_match and collected_ids == executed_ids == executed,
        "passed": (
            execution_ids_match
            and collected_ids == executed_ids
            and not mandatory_bad
            and pytest_evidence_passes(collected_ids, executed, outcomes, run_code)
        ),
    }


def _raw_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", "surrogateescape")


class _RecordingResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.test_ids: list[str] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.test_ids.append(test.id())
        super().startTest(test)


def _run_unittest(evidence: Path) -> dict[str, Any]:
    sys.path[:0] = [str(TESTS_DIR), str(REPO_ROOT / "src")]
    suite = unittest.defaultTestLoader.discover(str(TESTS_DIR), pattern="test_*.py", top_level_dir=str(TESTS_DIR))
    output = io.StringIO()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        saved_stdout, saved_stderr = os.dup(1), os.dup(2)
        try:
            os.dup2(stdout.fileno(), 1)
            os.dup2(stderr.fileno(), 2)
            result = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=_RecordingResult).run(suite)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        stdout.seek(0)
        stderr.seek(0)
        stdout_raw, stderr_raw = stdout.read(), stderr.read()
    (evidence / "unittest.log").write_bytes(output.getvalue().encode("utf-8", "surrogateescape"))
    (evidence / "unittest.stdout.log").write_bytes(stdout_raw)
    (evidence / "unittest.stderr.log").write_bytes(stderr_raw)
    return {
        "tests_run": result.testsRun,
        "test_ids": result.test_ids,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skips": [test.id() for test, _ in result.skipped],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
        "passed": (
            result.testsRun > 0
            and result.testsRun == len(result.test_ids)
            and len(result.test_ids) == len(set(result.test_ids))
            and result.wasSuccessful()
            and not result.skipped
            and not result.expectedFailures
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--backend", action="store_true")
    modes.add_argument("--all", action="store_true")
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = args.evidence_dir.resolve() if args.evidence_dir else Path(tempfile.mkdtemp(prefix="wkly-release-checks-"))
    evidence.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"mode": "all" if args.all else "backend", "evidence_dir": str(evidence)}
    try:
        required = required_acceptance_inputs(args.fixture_root)
        manifest["required_inputs"] = {key: str(value) for key, value in required.items()}
        run_packet_gate()
    except (FileNotFoundError, OSError, ValueError) as error:
        manifest.update({"passed": False, "preflight_error": str(error)})
        (evidence / "preflight.stderr.log").write_bytes((str(error) + "\n").encode("utf-8", "surrogateescape"))
        (evidence / "evidence.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Release preflight failed: {error}", file=sys.stderr)
        print(f"Raw/structured evidence: {evidence}", file=sys.stderr)
        return 1

    os.environ["DISPOSITION_EXPECT_STAGE"] = PACKET_STAGE
    os.environ["DISPOSITION_REQUIRE_CONFIRMATION"] = "1"
    if args.fixture_root is not None:
        os.environ[FIXTURE_ENV] = str(args.fixture_root.resolve())
    if args.all:
        try:
            from run_frontend_checks import validate_artifacts

            validate_artifacts()
        except (ImportError, OSError, RuntimeError) as error:
            manifest.update({"passed": False, "artifact_error": str(error)})
            (evidence / "evidence.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Release artifact preflight failed: {error}", file=sys.stderr)
            print(f"Evidence: {evidence}", file=sys.stderr)
            return 1

    targets = [str(TESTS_DIR)] if args.all else sorted(str(path) for path in TESTS_DIR.glob("test_*.py"))
    pytest_evidence = _run_pytest(targets, evidence)
    unittest_evidence = _run_unittest(evidence)
    manifest.update({"pytest": pytest_evidence, "unittest": unittest_evidence})
    manifest["passed"] = bool(pytest_evidence.get("passed")) and bool(unittest_evidence.get("passed"))
    (evidence / "evidence.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Release checks {'passed' if manifest['passed'] else 'FAILED'}; evidence: {evidence}")
    print(
        f"pytest collected/executed={pytest_evidence.get('collected_count', 0)}/"
        f"{pytest_evidence.get('executed_count', 0)}; unittest={unittest_evidence['tests_run']}"
        f" failed={len(unittest_evidence['failures'])} errors={len(unittest_evidence['errors'])}"
        f" skipped={len(unittest_evidence['skips'])}"
    )
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
