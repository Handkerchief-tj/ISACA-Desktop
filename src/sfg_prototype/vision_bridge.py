"""Compile netLens topology output into a validated SLiCAP netlist.

netLens currently emits a structural HSPICE ``.subckt``: instance name,
ordered pins, and a model hint.  SLiCAP additionally needs explicit device
models, source/detector directives, and small-signal parameters.  This module
keeps that impedance mismatch in one auditable adapter instead of teaching
either parser the other tool's syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_PASSIVE_MODELS = {"r": ("R", 2), "c": ("C", 2), "l": ("L", 2)}
_SOURCE_MODELS = {"v": ("V", 2), "i": ("I", 2)}
_MOS_MODELS = {"nmos4": "nmos", "pmos4": "pmos"}
_BJT_MODELS = {"npn": "npn", "pnp": "pnp"}
_MOS_PARAMETERS = ("gm", "gb", "go", "cgs", "cdg", "cgb", "cdb", "csb")
_BJT_PARAMETERS = ("gm", "go", "gpi", "cpi", "cbx", "cbc", "rb")


@dataclass(frozen=True)
class VisionBridgeDiagnostic:
    """One machine-readable compatibility finding produced by the bridge."""

    severity: str
    code: str
    message: str
    component: str | None = None


@dataclass(frozen=True)
class VisionComponent:
    """Normalized component reconstructed from the netLens structural netlist."""

    refdes: str
    raw_refdes: str
    kind: str
    pins: tuple[str, ...]
    model_hint: str


@dataclass(frozen=True)
class VisionCircuitIR:
    """Tool-neutral circuit topology at the vision/analysis boundary."""

    title: str
    components: tuple[VisionComponent, ...]

    @property
    def nodes(self) -> tuple[str, ...]:
        """Return unique nodes in first-seen order."""
        result: list[str] = []
        for component in self.components:
            for node in component.pins:
                if node not in result:
                    result.append(node)
        return tuple(result)


@dataclass
class VisionBridgeConfig:
    """User/model information that cannot be recovered from image topology alone."""

    source_node: str | None = None
    detector_node: str | None = None
    source_name: str = "Vin"
    supply_nodes: tuple[str, ...] = ("VDD", "VSS", "VCC", "VEE")
    parameter_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VisionBridgeResult:
    """Compiled SLiCAP text plus readiness and compatibility diagnostics."""

    circuit: VisionCircuitIR
    netlist_text: str
    diagnostics: tuple[VisionBridgeDiagnostic, ...]
    unresolved_parameters: tuple[str, ...]
    source_ref: str | None
    detector: str | None

    @property
    def symbolically_ready(self) -> bool:
        """Whether SLiCAP can analyze the topology symbolically after parsing."""
        return not any(item.severity == "error" for item in self.diagnostics)

    @property
    def numerically_ready(self) -> bool:
        """Whether reference roots can be computed without more parameter values."""
        return self.symbolically_ready and not self.unresolved_parameters

    def write(self, output_path: str | Path) -> Path:
        """Write the generated ``.cir`` file and return its absolute path."""
        path = Path(output_path).expanduser().resolve()
        if path.suffix.lower() != ".cir":
            raise ValueError(f"Expected a .cir output path, got: {path.name}")
        if not path.parent.exists():
            raise FileNotFoundError(f"Output directory does not exist: {path.parent}")
        path.write_text(self.netlist_text, encoding="utf-8")
        return path


def parse_netlens_sp(source: str | Path, *, from_text: bool = False) -> tuple[VisionCircuitIR, tuple[VisionBridgeDiagnostic, ...]]:
    """Parse the structural HSPICE subset emitted by ``HSPICEGenerator``."""
    text = str(source) if from_text else Path(source).expanduser().resolve().read_text(encoding="utf-8")
    title = "netlens_circuit"
    components: list[VisionComponent] = []
    diagnostics: list[VisionBridgeDiagnostic] = []
    counters: dict[str, int] = {}
    used_refdes: set[str] = set()

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split(";", 1)[0].strip()
        if not line or line.startswith("*"):
            continue
        tokens = line.split()
        directive = tokens[0].lower()
        if directive == ".subckt":
            if len(tokens) >= 2:
                title = tokens[1]
            continue
        if directive in {".ends", ".end"}:
            continue
        if directive.startswith("."):
            diagnostics.append(VisionBridgeDiagnostic(
                "warning",
                "IGNORED_DIRECTIVE",
                f"Ignored unsupported HSPICE directive on line {line_number}: {tokens[0]}",
            ))
            continue
        if len(tokens) < 4:
            diagnostics.append(VisionBridgeDiagnostic(
                "error",
                "MALFORMED_COMPONENT",
                f"Line {line_number} has no complete instance/pin/model record.",
                tokens[0] if tokens else None,
            ))
            continue

        model_hint = tokens[-1].lower()
        expected = _expected_pin_count(model_hint)
        if expected is None:
            diagnostics.append(VisionBridgeDiagnostic(
                "error",
                "UNSUPPORTED_MODEL",
                f"Model '{tokens[-1]}' is not supported by the symbolic bridge.",
                tokens[0],
            ))
            continue
        pins = tuple(_normalize_node(item) for item in tokens[1:-1])
        if len(pins) != expected:
            diagnostics.append(VisionBridgeDiagnostic(
                "error",
                "PIN_COUNT_MISMATCH",
                f"Model '{tokens[-1]}' expects {expected} pins, received {len(pins)}.",
                tokens[0],
            ))
            continue

        prefix = _model_prefix(model_hint)
        counters[prefix] = counters.get(prefix, 0) + 1
        refdes = _normalize_refdes(tokens[0], prefix, counters[prefix])
        if refdes in used_refdes:
            original = refdes
            suffix = 2
            while f"{original}_{suffix}" in used_refdes:
                suffix += 1
            refdes = f"{original}_{suffix}"
            diagnostics.append(VisionBridgeDiagnostic(
                "warning",
                "DUPLICATE_REFDES_RENAMED",
                f"Renamed duplicate instance '{tokens[0]}' to '{refdes}'.",
                refdes,
            ))
        used_refdes.add(refdes)
        components.append(VisionComponent(
            refdes=refdes,
            raw_refdes=tokens[0],
            kind=_model_kind(model_hint),
            pins=pins,
            model_hint=model_hint,
        ))

    if not components:
        diagnostics.append(VisionBridgeDiagnostic(
            "error", "EMPTY_TOPOLOGY", "The netLens netlist contains no supported components."
        ))
    return VisionCircuitIR(title=title, components=tuple(components)), tuple(diagnostics)


def compile_slicap_netlist(
    circuit: VisionCircuitIR,
    config: VisionBridgeConfig | None = None,
    diagnostics: Iterable[VisionBridgeDiagnostic] = (),
) -> VisionBridgeResult:
    """Compile normalized vision topology into SLiCAP ``.cir`` syntax."""
    config = config or VisionBridgeConfig()
    findings = list(diagnostics)
    nodes = circuit.nodes
    source_node = _resolve_named_node(config.source_node, nodes, ("VIN", "IN"))
    detector_node = _resolve_named_node(config.detector_node, nodes, ("VOUT", "OUT"))

    if source_node is None:
        findings.append(VisionBridgeDiagnostic(
            "error",
            "SOURCE_NODE_REQUIRED",
            "No unambiguous input node was found; provide VisionBridgeConfig.source_node.",
        ))
    elif config.source_node is None:
        findings.append(VisionBridgeDiagnostic(
            "warning", "SOURCE_NODE_INFERRED", f"Inferred input node '{source_node}'."
        ))
    if detector_node is None:
        findings.append(VisionBridgeDiagnostic(
            "error",
            "DETECTOR_NODE_REQUIRED",
            "No unambiguous output node was found; provide VisionBridgeConfig.detector_node.",
        ))
    elif config.detector_node is None:
        findings.append(VisionBridgeDiagnostic(
            "warning", "DETECTOR_NODE_INFERRED", f"Inferred output node '{detector_node}'."
        ))

    parameter_values = {_sanitize_parameter(str(key)): value for key, value in config.parameter_values.items()}
    unresolved: list[str] = []
    lines = [_format_title(circuit.title)]
    source_ref = _normalize_refdes(config.source_name, "V", 1) if source_node else None

    existing_sources: dict[str, VisionComponent] = {}
    for component in circuit.components:
        if component.kind == "voltage" and "0" in component.pins:
            non_ground = component.pins[0] if component.pins[1] == "0" else component.pins[1]
            existing_sources[non_ground.casefold()] = component

    selected_source = existing_sources.get(source_node.casefold()) if source_node else None
    if selected_source is not None:
        source_ref = selected_source.refdes
    elif source_node is not None:
        lines.append(f"{source_ref} {source_node} 0 V value={{{source_ref}}}")

    for supply in config.supply_nodes:
        actual = _find_node_case_insensitive(supply, nodes)
        if actual is None or actual in {"0", source_node} or actual.casefold() in existing_sources:
            continue
        supply_ref = _unique_refdes(_normalize_refdes(actual, "V", 1), {
            component.refdes for component in circuit.components
        } | ({source_ref} if source_ref else set()))
        lines.append(f"{supply_ref} {actual} 0 V value=0 dc={{{actual}}}")
        findings.append(VisionBridgeDiagnostic(
            "warning",
            "SUPPLY_ASSUMED_AC_GROUND",
            f"Inserted zero small-signal supply source for node '{actual}'.",
            supply_ref,
        ))

    for component in circuit.components:
        if component.kind in {"resistor", "capacitor", "inductor"}:
            model = {"resistor": "R", "capacitor": "C", "inductor": "L"}[component.kind]
            value_name = _sanitize_parameter(component.refdes)
            lines.append(
                f"{component.refdes} {' '.join(component.pins)} {model} value={{{value_name}}}"
            )
            _record_unresolved(value_name, parameter_values, unresolved)
        elif component.kind in {"voltage", "current"}:
            model = "V" if component.kind == "voltage" else "I"
            if component.refdes == source_ref:
                lines.append(
                    f"{component.refdes} {' '.join(component.pins)} {model} value={{{component.refdes}}}"
                )
            else:
                lines.append(
                    f"{component.refdes} {' '.join(component.pins)} {model} value=0 dc={{{component.refdes}}}"
                )
        elif component.kind in {"nmos", "pmos"}:
            assignments = []
            for name in _MOS_PARAMETERS:
                symbol = _device_parameter(name, component.refdes)
                assignments.append(f"{name}={{{symbol}}}")
                _record_unresolved(symbol, parameter_values, unresolved)
            lines.append(
                f"{component.refdes} {' '.join(component.pins)} M " + " ".join(assignments)
            )
            if component.kind == "pmos":
                findings.append(VisionBridgeDiagnostic(
                    "warning",
                    "PMOS_SIGN_REQUIRES_OPERATING_POINT",
                    "PMOS small-signal parameter signs must come from the selected operating-point convention.",
                    component.refdes,
                ))
        elif component.kind in {"npn", "pnp"}:
            assignments = []
            for name in _BJT_PARAMETERS:
                symbol = _device_parameter(name, component.refdes)
                assignments.append(f"{name}={{{symbol}}}")
                _record_unresolved(symbol, parameter_values, unresolved)
            lines.append(
                f"{component.refdes} {' '.join(component.pins)} QV " + " ".join(assignments)
            )
            if component.kind == "pnp":
                findings.append(VisionBridgeDiagnostic(
                    "warning",
                    "PNP_SIGN_REQUIRES_OPERATING_POINT",
                    "PNP small-signal parameter signs must come from the selected operating-point convention.",
                    component.refdes,
                ))

    used_values = sorted(set(parameter_values) - {source_ref or ""})
    if used_values:
        lines.append(".param " + " ".join(
            f"{name}={parameter_values[name]}" for name in used_values
        ))
    if source_ref:
        lines.append(f".source {source_ref}")
    if detector_node:
        lines.append(f".detector V_{detector_node}")
    lines.append(".end")

    for parameter in unresolved:
        findings.append(VisionBridgeDiagnostic(
            "warning",
            "NUMERIC_PARAMETER_REQUIRED",
            f"Parameter '{parameter}' is symbolic only; numeric root clustering requires a value.",
        ))
    return VisionBridgeResult(
        circuit=circuit,
        netlist_text="\n".join(lines) + "\n",
        diagnostics=tuple(findings),
        unresolved_parameters=tuple(unresolved),
        source_ref=source_ref,
        detector=f"V_{detector_node}" if detector_node else None,
    )


def convert_netlens_sp_to_slicap(
    source: str | Path,
    config: VisionBridgeConfig | None = None,
    *,
    from_text: bool = False,
) -> VisionBridgeResult:
    """Parse one netLens ``.sp`` output and compile its SLiCAP representation."""
    circuit, diagnostics = parse_netlens_sp(source, from_text=from_text)
    return compile_slicap_netlist(circuit, config=config, diagnostics=diagnostics)


def bridge_report(result: VisionBridgeResult) -> str:
    """Render a concise Markdown compatibility and readiness report."""
    lines = [
        "# Vision-to-SLiCAP Bridge Report",
        "",
        f"- title: `{result.circuit.title}`",
        f"- components: `{len(result.circuit.components)}`",
        f"- symbolic ready: `{result.symbolically_ready}`",
        f"- numeric ready: `{result.numerically_ready}`",
        f"- source: `{result.source_ref}`",
        f"- detector: `{result.detector}`",
        f"- unresolved numeric parameters: `{len(result.unresolved_parameters)}`",
        "",
        "## Diagnostics",
    ]
    if not result.diagnostics:
        lines.append("- none")
    else:
        for item in result.diagnostics:
            component = f" (`{item.component}`)" if item.component else ""
            lines.append(f"- **{item.severity}** `{item.code}`{component}: {item.message}")
    return "\n".join(lines) + "\n"


def _expected_pin_count(model_hint: str) -> int | None:
    if model_hint in _PASSIVE_MODELS:
        return _PASSIVE_MODELS[model_hint][1]
    if model_hint in _SOURCE_MODELS:
        return _SOURCE_MODELS[model_hint][1]
    if model_hint in _MOS_MODELS:
        return 4
    if model_hint in _BJT_MODELS:
        return 4
    return None


def _model_prefix(model_hint: str) -> str:
    if model_hint in _PASSIVE_MODELS:
        return _PASSIVE_MODELS[model_hint][0]
    if model_hint in _SOURCE_MODELS:
        return _SOURCE_MODELS[model_hint][0]
    if model_hint in _MOS_MODELS:
        return "M"
    if model_hint in _BJT_MODELS:
        return "Q"
    return "X"


def _model_kind(model_hint: str) -> str:
    return {
        "r": "resistor",
        "c": "capacitor",
        "l": "inductor",
        "v": "voltage",
        "i": "current",
        **_MOS_MODELS,
        **_BJT_MODELS,
    }[model_hint]


def _normalize_node(node: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", node.strip())
    return "0" if value.casefold() in {"0", "gnd"} else value or "unnamed"


def _normalize_refdes(raw: str, prefix: str, fallback_index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        return f"{prefix}{fallback_index}"
    if cleaned[0].upper() != prefix.upper():
        return f"{prefix}_{cleaned}"
    return prefix + cleaned[1:]


def _unique_refdes(candidate: str, used: set[str]) -> str:
    result = candidate
    index = 2
    while result in used:
        result = f"{candidate}_{index}"
        index += 1
    return result


def _sanitize_parameter(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"p_{cleaned or 'value'}"
    return cleaned


def _device_parameter(name: str, refdes: str) -> str:
    return _sanitize_parameter(f"{name}_{refdes}")


def _record_unresolved(name: str, values: dict[str, Any], unresolved: list[str]) -> None:
    if name not in values and name not in unresolved:
        unresolved.append(name)


def _find_node_case_insensitive(name: str, nodes: Iterable[str]) -> str | None:
    target = name.casefold()
    matches = [node for node in nodes if node.casefold() == target]
    return matches[0] if len(matches) == 1 else None


def _resolve_named_node(explicit: str | None, nodes: tuple[str, ...], candidates: tuple[str, ...]) -> str | None:
    if explicit is not None:
        return _find_node_case_insensitive(explicit, nodes)
    matches = [
        node for node in nodes
        if any(node.casefold() == candidate.casefold() for candidate in candidates)
    ]
    return matches[0] if len(matches) == 1 else None


def _format_title(title: str) -> str:
    cleaned = title.strip() or "netlens_circuit"
    return f'"{cleaned}"' if any(character.isspace() for character in cleaned) else cleaned
