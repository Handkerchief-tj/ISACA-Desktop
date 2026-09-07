"""Filesystem locations used by the installed and development desktop app."""

from __future__ import annotations

import os
from pathlib import Path


def user_data_root() -> Path:
    """Return the writable per-user ISACA data directory."""

    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "ISACA"


def ensure_user_directories() -> dict[str, Path]:
    """Create and return stable model, cache, log and run directories."""

    root = user_data_root()
    paths = {
        "root": root,
        "models": root / "models",
        "cache": root / "cache",
        "logs": root / "logs",
        "runs": root / "runs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def create_project_layout(root: str | Path) -> Path:
    """Create the directory layout shared with the official SLiCAP editor."""

    project = Path(root).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    for name in ("sch", "cir", "img", "lib", "results", "runs", "txt"):
        (project / name).mkdir(exist_ok=True)
    return project
