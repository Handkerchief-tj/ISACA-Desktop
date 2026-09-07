"""Isolated QProcess jobs with durable state, progress and bounded cancellation."""

from __future__ import annotations

import codecs
import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .protocol import WorkerEvent, WorkerRequest, desktop_command, write_request


class WorkerController(QObject):
    """Run one worker without blocking the UI and retain its lifecycle on disk."""

    event_received = Signal(object)
    log_received = Signal(str)
    completed = Signal(str, object)
    failed = Signal(str)
    cancelled = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.started.connect(lambda: self._persist_state("running"))
        self._process.errorOccurred.connect(self._process_error)
        self._process.finished.connect(self._finished)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._force_cancel)
        self._stdout_buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._request: WorkerRequest | None = None
        self._cancel_requested = False

    @property
    def busy(self) -> bool:
        return self._request is not None

    def start(self, action: str, payload: dict, run_root: str | Path) -> str:
        """Save the request and asynchronously start the selected worker."""
        if self.busy:
            raise RuntimeError("An ISACA worker is already running.")
        job_id = uuid4().hex
        root = Path(run_root).expanduser().resolve()
        task_dir = root / job_id
        task_dir.mkdir(parents=True, exist_ok=False)
        if action == "export_schematic":
            source = Path(payload["input_path"])
            snapshot = source.read_bytes()
            snapshot_path = task_dir / "input_snapshot.slicap_sch"
            snapshot_path.write_bytes(snapshot)
            payload = {**payload, "input_sha256": hashlib.sha256(snapshot).hexdigest(),
                       "snapshot_path": str(snapshot_path)}
        request_path = task_dir / "desktop-request.json"
        request = WorkerRequest(
            action=action, job_id=job_id, run_root=str(root),
            result_path=str(task_dir / "desktop-result.json"),
            cancel_path=str(task_dir / "cancel.request"), payload=payload,
        )
        write_request(request, request_path)
        program, arguments = desktop_command(request_path)
        environment = QProcessEnvironment.systemEnvironment()
        source_root = str(Path(__file__).resolve().parents[1])
        previous = environment.value("PYTHONPATH")
        environment.insert("PYTHONPATH", source_root + (os.pathsep + previous if previous else ""))
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        self._process.setProcessEnvironment(environment)
        self._stdout_buffer = ""
        self._decoder.reset()
        self._request = request
        self._cancel_requested = False
        self._kill_timer.stop()
        self._persist_state("queued")
        self._process.setWorkingDirectory(str(task_dir))
        self.busy_changed.emit(True)
        self._process.start(program, arguments)
        return job_id

    def cancel(self) -> None:
        """Request a cooperative stop; forcibly stop an unresponsive computation."""
        if not self.busy or self._cancel_requested:
            return
        self._cancel_requested = True
        Path(self._request.cancel_path).touch()
        self.log_received.emit("正在取消任务...")
        self._kill_timer.start(3000)

    def _force_cancel(self) -> None:
        if self.busy and self._cancel_requested:
            self._process.kill()

    def _persist_state(self, status: str, **details) -> None:
        if self._request is None:
            return
        path = Path(self._request.result_path).with_name("desktop-state.json")
        payload = {
            "id": self._request.job_id, "action": self._request.action,
            "status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _read_stdout(self) -> None:
        raw = bytes(self._process.readAllStandardOutput())
        self._append_log("worker-stdout.ndjson", raw)
        self._stdout_buffer += self._decoder.decode(raw)
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._consume_stdout_line(line.strip())

    def _consume_stdout_line(self, line: str) -> None:
        if not line:
            return
        try:
            event = WorkerEvent.model_validate_json(line)
        except ValueError:
            self.log_received.emit(line)
            return
        self.event_received.emit(event)
        self.log_received.emit(event.message)

    def _read_stderr(self) -> None:
        raw = bytes(self._process.readAllStandardError())
        self._append_log("worker-stderr.log", raw)
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            self.log_received.emit(text)

    def _append_log(self, name: str, raw: bytes) -> None:
        """Persist worker streams so failures can be inspected after closing the UI."""
        if not raw or self._request is None:
            return
        try:
            with Path(self._request.result_path).with_name(name).open("ab") as handle:
                handle.write(raw)
        except OSError as error:
            self.log_received.emit(f"Cannot persist task log: {error}")

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and self.busy:
            message = self._process.errorString()
            self._persist_state("failed", error=message)
            self._release()
            self.failed.emit(message)

    def _release(self) -> None:
        self._kill_timer.stop()
        self._request = None
        self._cancel_requested = False
        self.busy_changed.emit(False)

    def _finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_stdout()
        self._read_stderr()
        self._stdout_buffer += self._decoder.decode(b"", final=True)
        self._consume_stdout_line(self._stdout_buffer.strip())
        self._stdout_buffer = ""
        request = self._request
        if request is None:
            return
        if self._cancel_requested:
            self._persist_state("cancelled", exit_code=exit_code)
            result_path = Path(request.result_path)
            result_path.write_text(json.dumps({"status": "cancelled", "action": request.action}), encoding="utf-8")
            self._release()
            self.cancelled.emit()
            return
        try:
            envelope = json.loads(Path(request.result_path).read_text(encoding="utf-8"))
            if exit_code != 0 or envelope.get("status") != "completed":
                raise RuntimeError(envelope.get("error") or f"Worker exited with code {exit_code}.")
        except (OSError, ValueError, RuntimeError) as error:
            message = f"Worker failed (exit {exit_code}): {error}"
            self._persist_state("failed", error=message, exit_code=exit_code)
            self._release()
            self.failed.emit(message)
            return
        self._persist_state("completed", exit_code=exit_code)
        self._release()
        self.completed.emit(request.action, envelope.get("result", {}))
