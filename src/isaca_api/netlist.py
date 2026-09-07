"""Normalize SLiCAP netlists and expose strict parameter diagnostics."""

from __future__ import annotations

import re

from .models import CircuitDocument, Diagnostic, DiagnosticLevel, NormalizeRequest
from .parameters import collect_parameter_specs


_ELEMENT_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\s+\S+\s+\S+")


def _content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("*")]


def _has_title(lines: list[str]) -> bool:
    if not lines:
        return False
    first = lines[0]
    if first.startswith('"') and first.endswith('"'):
        return True
    if first.startswith("."):
        return False
    return _ELEMENT_LINE.match(first) is None


def normalize_netlist(request: NormalizeRequest) -> CircuitDocument:
    """Add required framing and describe parameters without changing equations."""

    raw = request.netlist_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = raw.splitlines() if raw else []
    content = _content_lines(raw)
    diagnostics: list[Diagnostic] = []
    title = request.title or (content[0].strip('"') if _has_title(content) else "Untitled circuit")
    if not _has_title(content):
        title_line = f'"{title}"' if " " in title else title
        lines.insert(0, title_line)
        diagnostics.append(Diagnostic(level=DiagnosticLevel.INFO, code="title_added", message="A SLiCAP title line was added."))
    if not any(line.strip().lower() == ".end" for line in lines):
        lines.append(".end")
        diagnostics.append(Diagnostic(level=DiagnosticLevel.WARNING, code="end_added", message="Missing .end was added."))
    normalized = "\n".join(lines).strip() + "\n"
    if not any(line.strip().lower().startswith(".source") for line in lines):
        diagnostics.append(Diagnostic(level=DiagnosticLevel.WARNING, code="source_missing", message="No .source directive is defined."))
    if not any(line.strip().lower().startswith(".detector") for line in lines):
        diagnostics.append(Diagnostic(level=DiagnosticLevel.WARNING, code="detector_missing", message="No .detector directive is defined."))
    specs = collect_parameter_specs(
        normalized,
        request.parameter_overrides,
        request.use_slicap_defaults,
    )
    missing = [spec.name for spec in specs if spec.required_for_numeric and spec.numeric_value is None]
    if missing:
        diagnostics.append(
            Diagnostic(
                level=DiagnosticLevel.WARNING,
                code="numeric_parameters_unresolved",
                message="Numeric analysis requires values for: " + ", ".join(missing),
            )
        )
    defaults_used = [spec.name for spec in specs if spec.source.value == "slicap_default"]
    if defaults_used:
        diagnostics.append(
            Diagnostic(
                level=DiagnosticLevel.WARNING,
                code="slicap_defaults_used",
                message=(
                    "SLiCAP 5.2.1 defaults were explicitly enabled for: "
                    + ", ".join(defaults_used)
                    + ". Several built-in defaults are zero and may be unsuitable as physical device data."
                ),
            )
        )
    return CircuitDocument(
        title=title,
        input_kind="netlist",
        netlist_text=normalized,
        parameters=specs,
        diagnostics=diagnostics,
        provenance={"normalizer": "isaca-1.0", "slicap_target": "5.2.1"},
    )
