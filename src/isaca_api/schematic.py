"""Connectivity resolution and deterministic SLiCAP netlist export."""

from __future__ import annotations

from collections import defaultdict

from .catalog import DEVICE_CATALOG
from .models import Diagnostic, DiagnosticLevel, SchematicDocument
from .netlist import normalize_netlist
from .models import NormalizeRequest


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _pin_key(component_id: str, pin_id: str) -> str:
    return f"{component_id}:{pin_id}"


def _component_pins(component) -> list[str]:
    catalog = DEVICE_CATALOG.get(component.device)
    if catalog is None:
        return list(component.properties.get("pins", []))
    if component.device == "X":
        return list(component.properties.get("pins", []))
    return list(catalog["pins"])


def resolve_nets(document: SchematicDocument) -> tuple[dict[str, str], list[Diagnostic]]:
    """Resolve wire-connected pins using union-find and deterministic names."""

    union_find = _UnionFind()
    diagnostics: list[Diagnostic] = []
    components = {component.id: component for component in document.components}
    pin_keys: set[str] = set()
    for component in document.components:
        if component.device not in DEVICE_CATALOG:
            diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="unknown_device", message=f"Unsupported device {component.device}.", location=component.id))
        for pin in _component_pins(component):
            key = _pin_key(component.id, pin)
            pin_keys.add(key)
            union_find.add(key)

    named_roots: dict[str, set[str]] = defaultdict(set)
    connected: set[str] = set()
    for wire in document.wires:
        source = _pin_key(wire.source.component_id, wire.source.pin_id)
        target = _pin_key(wire.target.component_id, wire.target.pin_id)
        if source not in pin_keys or target not in pin_keys:
            diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="invalid_wire_endpoint", message="Wire references a missing component pin.", location=wire.id))
            continue
        union_find.union(source, target)
        connected.update((source, target))

    for component in document.components:
        pins = _component_pins(component)
        if component.device == "GROUND" and pins:
            named_roots[union_find.find(_pin_key(component.id, pins[0]))].add("0")
        if component.device == "PORT" and pins:
            name = str(component.properties.get("name", component.refdes)).strip()
            if name:
                named_roots[union_find.find(_pin_key(component.id, pins[0]))].add(name)
    for wire in document.wires:
        if wire.net_name:
            key = _pin_key(wire.source.component_id, wire.source.pin_id)
            if key in pin_keys:
                named_roots[union_find.find(key)].add(wire.net_name)

    root_members: dict[str, list[str]] = defaultdict(list)
    for key in sorted(pin_keys):
        root_members[union_find.find(key)].append(key)
    resolved: dict[str, str] = {}
    auto_index = 1
    for root, members in sorted(root_members.items(), key=lambda item: item[1][0]):
        names = named_roots.get(root, set())
        if len(names) > 1:
            diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="conflicting_net_names", message="Connected net has conflicting names: " + ", ".join(sorted(names)), location=root))
        if "0" in names:
            name = "0"
        elif names:
            name = sorted(names)[0]
        else:
            name = f"N{auto_index:03d}"
            auto_index += 1
        for member in members:
            resolved[member] = name

    for key in sorted(pin_keys - connected):
        component_id, _ = key.split(":", 1)
        if components[component_id].device not in {"GROUND", "PORT", "JUNCTION"}:
            diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="dangling_pin", message="Component pin is not connected.", location=key))
    return resolved, diagnostics


def _format_parameters(parameters: dict[str, str]) -> str:
    formatted: list[str] = []
    for key, value in parameters.items():
        text = str(value).strip()
        if not text:
            continue
        if text == "?" or (text.startswith("{") and text.endswith("}")):
            formatted.append(f"{key}={text}")
        else:
            formatted.append(f"{key}={{{text}}}")
    return " ".join(formatted)


def schematic_to_netlist(document: SchematicDocument) -> tuple[str, list[Diagnostic]]:
    """Export the supported internal schematic model to a SLiCAP `.cir` netlist."""

    nets, diagnostics = resolve_nets(document)
    refdes_seen: set[str] = set()
    lines = [f'"{document.title}"' if " " in document.title else document.title, ""]
    for component in document.components:
        if component.device in {"GROUND", "PORT", "JUNCTION"}:
            continue
        if component.refdes in refdes_seen:
            diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="duplicate_refdes", message=f"Duplicate reference designator {component.refdes}.", location=component.id))
            continue
        refdes_seen.add(component.refdes)
        catalog = DEVICE_CATALOG.get(component.device)
        if catalog is None:
            continue
        for parameter_name, parameter_value in component.parameters.items():
            if str(parameter_value).strip() == "?":
                diagnostics.append(
                    Diagnostic(
                        level=DiagnosticLevel.ERROR,
                        code="component_parameter_unresolved",
                        message=f"{component.refdes}.{parameter_name} still has the '?' placeholder.",
                        location=component.id,
                    )
                )
        nodes = [nets.get(_pin_key(component.id, pin), "?") for pin in _component_pins(component)]
        model = component.model or catalog.get("model")
        fields = [component.refdes, *nodes]
        if component.device in {"F", "H"}:
            if not component.control_ref:
                diagnostics.append(Diagnostic(level=DiagnosticLevel.ERROR, code="control_ref_missing", message=f"{component.refdes} requires a controlling branch reference.", location=component.id))
            fields.append(component.control_ref or "?")
        if model:
            fields.append(model)
        params = _format_parameters(component.parameters)
        if params:
            fields.append(params)
        lines.append(" ".join(fields))

    if document.parameters:
        lines.extend(("", ".param " + " ".join(f"{name}={value}" for name, value in sorted(document.parameters.items()))))
    if document.analysis.source:
        lines.append(f".source {document.analysis.source}")
    else:
        diagnostics.append(Diagnostic(level=DiagnosticLevel.WARNING, code="source_missing", message="No analysis source is selected."))
    if document.analysis.detector:
        lines.append(f".detector {document.analysis.detector}")
    else:
        diagnostics.append(Diagnostic(level=DiagnosticLevel.WARNING, code="detector_missing", message="No analysis detector is selected."))
    if document.analysis.lgref:
        lines.append(f".lgref {document.analysis.lgref}")
    lines.append(".end")
    normalized = normalize_netlist(NormalizeRequest(netlist_text="\n".join(lines))).netlist_text
    return normalized, diagnostics
