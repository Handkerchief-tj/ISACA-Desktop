"""Version-checked access to the SLiCAP APIs used by the prototype.

The analysis engine intentionally keeps the few unavoidable private SLiCAP
hooks in this module. The rest of the package imports them from here, making
future SLiCAP migrations auditable and local.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import SLiCAP.SLiCAPconfigure as ini
import SLiCAP.SLiCAPyacc as yacc_module
from SLiCAP.SLiCAPexecute import _makeAllMatrices as make_all_matrices
from SLiCAP.SLiCAPinstruction import instruction as instruction_type
from SLiCAP.SLiCAPlex import _replaceScaleFactors as replace_scale_factors
from SLiCAP.SLiCAPmath import fullSubs as full_subs
from SLiCAP.SLiCAPprotos import circuit as circuit_type


class UnsupportedSLiCAPVersion(RuntimeError):
    """Raised when the installed SLiCAP version is outside the tested range."""


@dataclass(frozen=True)
class SLiCAPRuntimeInfo:
    """Describe the installed SLiCAP version and required private hooks."""

    version: str
    supported: bool
    missing_hooks: tuple[str, ...]


def _version_tuple(value: str) -> tuple[int, int, int]:
    """Return the numeric release prefix without another runtime dependency."""

    parts: list[int] = []
    for token in value.split("."):
        digits = "".join(char for char in token if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    return tuple((parts + [0, 0, 0])[:3])


def runtime_info() -> SLiCAPRuntimeInfo:
    """Inspect the active SLiCAP runtime without initializing a project."""

    try:
        installed = version("SLiCAP")
    except PackageNotFoundError:
        installed = "0.0.0"
    required: dict[str, Any] = {
        "SLiCAPyacc._checkCircuit": getattr(yacc_module, "_checkCircuit", None),
        "SLiCAPyacc._initializeParser": getattr(yacc_module, "_initializeParser", None),
        "SLiCAPyacc._updateCirData": getattr(yacc_module, "_updateCirData", None),
        "SLiCAPexecute._makeAllMatrices": make_all_matrices,
        "SLiCAPlex._replaceScaleFactors": replace_scale_factors,
    }
    missing = tuple(name for name, hook in required.items() if not callable(hook))
    current = _version_tuple(installed)
    supported = (4, 0, 8) <= current < (5, 3, 0) and not missing
    return SLiCAPRuntimeInfo(installed, supported, missing)


def ensure_supported_runtime() -> SLiCAPRuntimeInfo:
    """Fail early with a useful message for an untested SLiCAP installation."""

    info = runtime_info()
    if not info.supported:
        hooks = ", ".join(info.missing_hooks) or "none"
        raise UnsupportedSLiCAPVersion(
            "sfg-prototype supports SLiCAP >=4.0.8,<5.3; "
            f"found {info.version}. Missing hooks: {hooks}."
        )
    return info


ensure_supported_runtime()

check_circuit = yacc_module._checkCircuit
initialize_parser = yacc_module._initializeParser
update_circuit_data = yacc_module._updateCirData
