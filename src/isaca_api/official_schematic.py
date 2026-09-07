"""Headless access to the official SLiCAP 5.2.1 schematic exporter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from SLiCAP.schematic.schematic_data import SchematicData

from .models import Diagnostic, DiagnosticLevel


_ERROR_CODES = {
    "missing connection": "official_missing_connection",
    "missing reference": "official_missing_reference",
    "missing model": "official_missing_model",
    "missing value": "official_missing_value",
    "no symbol definition": "official_symbol_missing",
}


def _cli_diagnostics(stderr: str) -> list[Diagnostic]:
    """Translate official CLI messages into stable API diagnostics."""

    diagnostics: list[Diagnostic] = []
    for raw_line in stderr.splitlines():
        message = raw_line.strip().lstrip("-").strip()
        if not message or message.startswith("Netlist not generated"):
            continue
        lowered = message.lower()
        code = next(
            (value for phrase, value in _ERROR_CODES.items() if phrase in lowered),
            "official_netlist_error",
        )
        location = message.split(":", 1)[0] if ":" in message else None
        diagnostics.append(
            Diagnostic(
                level=DiagnosticLevel.ERROR,
                code=code,
                message=message,
                location=location,
            )
        )
    return diagnostics


def official_netlist_from_schematic(
    schematic: dict[str, Any],
    work_root: str | Path,
    *,
    timeout_seconds: float = 45.0,
) -> tuple[str | None, list[Diagnostic]]:
    """Generate a netlist by invoking SLiCAP's supported headless CLI."""

    try:
        # Validate the public JSON data model before launching Qt.
        SchematicData.from_json(json.dumps(schematic))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, [
            Diagnostic(
                level=DiagnosticLevel.ERROR,
                code="official_schematic_invalid",
                message=f"Invalid SLiCAP schematic data: {exc}",
            )
        ]

    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="slicap-sch-", dir=root) as directory:
        temporary = Path(directory)
        schematic_path = temporary / "circuit.slicap_sch"
        netlist_path = temporary / "circuit.cir"
        schematic_path.write_text(json.dumps(schematic, indent=2), encoding="utf-8")

        environment = os.environ.copy()
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "SLiCAP.schematic.cli",
                    "netlist",
                    str(schematic_path),
                    "-o",
                    str(netlist_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return None, [
                Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="official_netlist_timeout",
                    message=f"SLiCAP netlist export exceeded {timeout_seconds:g} seconds.",
                )
            ]

        if completed.returncode != 0 or not netlist_path.is_file():
            diagnostics = _cli_diagnostics(completed.stderr)
            if not diagnostics:
                diagnostics.append(
                    Diagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="official_netlist_error",
                        message=completed.stderr.strip() or completed.stdout.strip() or "SLiCAP netlist export failed.",
                    )
                )
            return None, diagnostics

        return netlist_path.read_text(encoding="utf-8"), []
