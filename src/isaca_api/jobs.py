"""Small persistent local job queue for long-running symbolic analyses."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from .models import AnalysisJob, AnalysisRequest
from .slicap_adapter import SLiCAP521Adapter


class AnalysisJobManager:
    """Execute analyses in the background and persist each state transition."""

    def __init__(self, run_root: str | Path, max_workers: int = 2):
        self.run_root = Path(run_root).resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.adapter = SLiCAP521Adapter(self.run_root)
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="isaca-analysis")
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.RLock()

    def submit(self, request: AnalysisRequest) -> AnalysisJob:
        """Queue one analysis and return its initial state."""

        job = AnalysisJob(id=uuid4().hex, status="queued")
        with self._lock:
            self._jobs[job.id] = job
            self._persist(job)
        self.executor.submit(self._execute, job.id, request)
        return job.model_copy(deep=True)

    def get(self, job_id: str) -> AnalysisJob | None:
        """Return a snapshot of one job, loading persisted state if needed."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return job.model_copy(deep=True)
        state_path = self.run_root / job_id / "job.json"
        if not state_path.is_file():
            return None
        return AnalysisJob.model_validate_json(state_path.read_text(encoding="utf-8"))

    def artifact(self, job_id: str, name: str) -> Path | None:
        """Resolve one artifact without allowing traversal outside its job."""

        root = (self.run_root / job_id / "artifacts").resolve()
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def shutdown(self) -> None:
        """Finish active jobs and release worker threads during API shutdown."""

        self.executor.shutdown(wait=True, cancel_futures=True)

    def _execute(self, job_id: str, request: AnalysisRequest) -> None:
        self._update(AnalysisJob(id=job_id, status="running"))
        try:
            result = self.adapter.analyze(job_id, request)
        except Exception as error:
            self._update(AnalysisJob(id=job_id, status="failed", error=str(error)))
            return
        self._update(AnalysisJob(id=job_id, status="completed", result=result))

    def _update(self, job: AnalysisJob) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._persist(job)

    def _persist(self, job: AnalysisJob) -> None:
        directory = self.run_root / job.id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.json").write_text(
            json.dumps(job.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
