"""Stable entry point used by the Windows standalone build."""

from __future__ import annotations

import os
from pathlib import Path


def _configure_packaged_runtime() -> None:
    """Keep Qt WebEngine diagnostics out of the read-only install directory."""

    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    log_directory = local_app_data / "ISACA" / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CHROME_LOG_FILE", str(log_directory / "qtwebengine.log"))


_configure_packaged_runtime()

from isaca_desktop.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
