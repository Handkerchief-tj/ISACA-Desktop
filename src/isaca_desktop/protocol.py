"""Versioned JSON and NDJSON contracts for isolated desktop workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkerRequest(BaseModel):
    """One deterministic request passed from the GUI to a worker process."""

    schema_version: Literal["1.0"] = "1.0"
    action: Literal["analysis", "export_schematic"]
    job_id: str
    run_root: str
    result_path: str
    cancel_path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkerEvent(BaseModel):
    """One progress or diagnostic event emitted on worker stdout."""

    schema_version: Literal["1.0"] = "1.0"
    event: Literal["started", "progress", "completed", "failed", "cancelled"]
    stage: str
    message: str
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    data: dict[str, Any] = Field(default_factory=dict)


def write_request(request: WorkerRequest, path: str | Path) -> Path:
    """Persist a worker request atomically enough for local process handoff."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(request.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def emit_event(event: WorkerEvent) -> None:
    """Write one unbuffered NDJSON event for QProcess consumers."""

    print(event.model_dump_json(), file=sys.__stdout__ or sys.stdout, flush=True)


def desktop_command(request_path: str | Path) -> tuple[str, list[str]]:
    """Return the executable and arguments for source and frozen builds."""

    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return sys.executable, ["--worker", str(request_path)]
    return sys.executable, ["-m", "isaca_desktop", "--worker", str(request_path)]
