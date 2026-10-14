import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from .assessment import validate_extraction
from .core import AssessmentValidationError, validate_assessment_payload
from .policy import load_policy, resolve_policy
from .replay import load_saved_payload
from .replay_catalog import eligible_replays, normalized_input_for_workbook
from .report import write_reports, write_run_notes
from .store import (
    get_job,
    get_snapshot,
    list_jobs,
    save_job_result,
    set_job_status,
)
from .web_service import WebSettings, scope_preview


class JobWorker:
    def __init__(self, database_path: Path, storage_dir: Path, replay_roots: tuple[Path, ...]) -> None:
        self.database_path = Path(database_path)
        self.storage_dir = Path(storage_dir)
        self.replay_roots = tuple(Path(root) for root in replay_roots)
        self._queue: Queue[int | None] = Queue()
        self._queued: set[int] = set()
        self._lock = Lock()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        for status, error in (
            ("running", "The local server stopped while this job was running."),
            ("pending", "The local server stopped before this queued job started."),
        ):
            for job in list_jobs(self.database_path, status=status):
                set_job_status(self.database_path, job["job_id"], "interrupted", error=error)
        self._thread = Thread(target=self._run, name="weekly-report-job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        self._queue.put(None)
        self._queue.join()
        self._thread.join()
        self._thread = None

    def enqueue(self, job_id: int) -> None:
        job = get_job(self.database_path, job_id)
        if job is None or job["status"] != "pending":
            raise ValueError("Only pending jobs can be queued")
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
        self._queue.put(job_id)

    def resume(self, job_id: int) -> None:
        job = get_job(self.database_path, job_id)
        if job is None or job["status"] not in {"interrupted", "failed"}:
            raise ValueError("Only interrupted or failed jobs can be resumed")
        self.start()
        set_job_status(self.database_path, job_id, "pending", progress=0, coverage={}, error=None)
        self.enqueue(job_id)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self.process_job(job_id)
            finally:
                if job_id is not None:
                    with self._lock:
                        self._queued.discard(job_id)
                self._queue.task_done()

    def _run_dir(self, job_id: int) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.storage_dir / "runs" / str(job_id) / stamp
        path.mkdir(parents=True, exist_ok=False)
        return path

    def process_job(self, job_id: int) -> None:
        job = get_job(self.database_path, job_id)
        if job is None or job["status"] != "pending":
            raise ValueError("Only pending jobs can be processed")
        set_job_status(self.database_path, job_id, "running", progress=5, coverage={"stage": "scope"})
        run_dir = None
        metadata = {
            "created_at_utc": datetime.now(UTC).isoformat(),
            "model": "not called (offline replay)",
            "usage": {},
            "verification_mode": "offline_replay",
            "role": job["role"],
            "job_id": job_id,
        }
        scope = {"selection_warnings": []}
        stage = "scope preparation"
        try:
            if job["result"] is not None:
                result_path = Path(job["result"]["path"])
                if not result_path.is_file() or hashlib.sha256(result_path.read_bytes()).hexdigest() != job["result"]["sha256"]:
                    raise ValueError("Existing completed job artifact is missing or has a different hash")
                set_job_status(
                    self.database_path, job_id, "completed", progress=100, coverage=job["coverage"]
                )
                return
            previous = get_snapshot(self.database_path, job["previous_snapshot_id"])
            current = get_snapshot(self.database_path, job["current_snapshot_id"])
            if previous is None or current is None:
                raise ValueError("Selected report snapshot is unavailable")
            input_path = normalized_input_for_workbook(
                self.database_path, self.storage_dir, job["workbook_id"]
            )
            prepared = scope_preview(
                WebSettings(self.database_path, self.storage_dir, self.replay_roots),
                previous_snapshot_id=job["previous_snapshot_id"],
                current_snapshot_id=job["current_snapshot_id"],
                project=job["project_label"],
                role=job["role"],
                aliases=job["aliases"],
            )
            scope = prepared.scope
            normalized_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
            metadata.update({
                "input_sha256": normalized_hash,
                "project": job["project_label"],
                "previous_sheet": scope["previous_sheet"],
                "current_sheet": scope["current_sheet"],
                "requested_aliases": job["aliases"],
                "period_context": prepared.period_context,
            })

            bundles = eligible_replays(
                input_path, scope, job["role"], job["aliases"], self.replay_roots
            )
            bundle = next((item for item in bundles if item["bundle_id"] == job["replay_bundle_id"]), None)
            if bundle is None:
                set_job_status(
                    self.database_path, job_id, "no_ai_result", progress=100,
                    coverage={"result_state": "no_ai_result_available", "commitments": 0,
                              "findings": 0, "available_checks": [], "unavailable_checks": ["offline replay"]},
                )
                return

            metadata.update(bundle["provenance"])
            scope["selection_warnings"].extend(bundle["provenance"].get("provenance_warnings", []))
            metadata["policy_version"] = "demo-policy-v1"
            stage = "replay extraction response"
            extraction_payload = load_saved_payload(bundle["response_dir"] / "extraction_response.json")
            stage = "replay extraction validation"
            commitments = validate_extraction(extraction_payload, prepared.sources)

            set_job_status(self.database_path, job_id, "running", progress=35, coverage={"stage": stage})
            stage = "replay assessment response"
            assessment_payload = load_saved_payload(bundle["response_dir"] / "assessment_response.json")
            stage = "replay assessment validation"
            validated = validate_assessment_payload(assessment_payload, commitments, prepared.sources)
            metadata["citation_repairs"] = validated.citation_repairs
            set_job_status(self.database_path, job_id, "running", progress=65, coverage={"stage": stage})

            stage = "replay policy resolution"
            registry = load_policy()
            metadata["policy_version"] = registry["version"]
            metadata["policy_sha256"] = hashlib.sha256(json.dumps(
                registry, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()).hexdigest()
            result = resolve_policy(
                validated.assessments, commitments, prepared.sources, scope,
                role=job["role"], registry=registry,
            )
            assessments = result.assessments
            metadata["policy_resolution"] = result.resolutions
            metadata["policy_questions"] = [
                question for assessment in assessments for question in assessment["policy_questions"]
            ]
            set_job_status(self.database_path, job_id, "running", progress=85, coverage={"stage": stage})

            stage = "replay report writing"
            run_dir = self._run_dir(job_id)
            write_reports(run_dir, scope, prepared.sources, commitments, assessments, metadata)
            findings = sum(len(assessment["findings"]) for assessment in assessments)
            coverage = {
                "result_state": "completed_with_findings" if findings else "completed_no_findings",
                "commitments": len(commitments),
                "affected_commitments": sum(bool(item["findings"]) for item in assessments),
                "findings": findings,
                "available_checks": ["historical offline replay", "strict source/quote validation", "local policy"],
                "unavailable_checks": ["fresh AI evaluation", "unimplemented semantic rules"],
            }
            save_job_result(self.database_path, job_id, str(run_dir / "comparison.json"), metadata)
            set_job_status(self.database_path, job_id, "completed", progress=100, coverage=coverage)
        except Exception as error:  # noqa: BLE001 — persist diagnostics for any job failure
            if isinstance(error, AssessmentValidationError):
                metadata["citation_repairs"] = error.citation_repairs
            failure = {"stage": stage, "error_type": type(error).__name__, "error": str(error)}
            try:
                run_dir = run_dir or self._run_dir(job_id)
                for name in ("comparison.json", "comparison.csv", "mentor_review.md"):
                    (run_dir / name).unlink(missing_ok=True)
                (run_dir / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                write_run_notes(run_dir, metadata, scope, [], failure)
            finally:
                set_job_status(
                    self.database_path, job_id, "failed", progress=100, error=f"{stage}: {error}",
                    coverage={"result_state": "analysis_failed", "stage": stage,
                              "diagnostics_path": str(run_dir) if run_dir else None},
                )
