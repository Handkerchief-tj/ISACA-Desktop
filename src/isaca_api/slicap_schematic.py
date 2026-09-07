"""Bidirectional adapter for the native SLiCAP 5.2.1 schematic JSON format."""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from typing import Any

from .catalog import DEVICE_CATALOG
from .models import (
    AnalysisPorts,
    Diagnostic,
    DiagnosticLevel,
    PinRef,
    Point,
    SchematicComponent,
    SchematicDocument,
    SchematicWire,
)


_SYMBOL_TO_DEVICE = {
    "R": "R",
    "R0": "R",
    "C": "C",
    "L": "L",
    "V": "V",
    "I": "I",
    "VCCS": "G",
    "VCVS": "E",
    "CCCS": "F",
    "CCVS": "H",
    "M": "M",
    "QV": "QV",
    "0": "GROUND",
    "port": "PORT",
}

_KNOWN_TOP_LEVEL = {
    "components",
    "wires",
    "junctions",
    "free_texts",
    "hyperlinks",
    "commands",
    "libs",
    "images",
    "border",
    "latex_fragments",
    "parameters",
    "analysis_items",
    "shapes",
    "model_defs",
    "properties",
}


def _round_point(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], 6), round(point[1], 6)


def _transform_pin(component: dict[str, Any], pin: tuple[float, float]) -> tuple[float, float]:
    """Map one symbol-local pin to SLiCAP scene coordinates."""

    x, y = pin
    if component.get("h_flip", False):
        x = -x
    if component.get("v_flip", False):
        y = -y
    angle = math.radians(float(component.get("rotation", 0.0)))
    rotated_x = x * math.cos(angle) - y * math.sin(angle)
    rotated_y = x * math.sin(angle) + y * math.cos(angle)
    return _round_point((rotated_x + float(component["x"]), rotated_y + float(component["y"])))


def _component_pin_positions(component: dict[str, Any]) -> dict[str, tuple[float, float]]:
    if component.get("symbol_name") == "__junction__":
        return {"junction": _round_point((float(component["x"]), float(component["y"])))}
    symbol_name = component.get("symbol_name", "")
    definition = next(
        (item for item in DEVICE_CATALOG.values() if item.get("symbol") == symbol_name),
        None,
    )
    pins = definition.get("pin_positions", {}) if definition else {}
    return {
        name: _transform_pin(component, (float(position["x"]), float(position["y"])))
        for name, position in pins.items()
    }


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    tolerance: float = 1e-6,
) -> bool:
    cross = (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (end[0] - start[0])
    if abs(cross) > tolerance:
        return False
    return (
        min(start[0], end[0]) - tolerance <= point[0] <= max(start[0], end[0]) + tolerance
        and min(start[1], end[1]) - tolerance <= point[1] <= max(start[1], end[1]) + tolerance
    )


class _CoordinateUnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[float, float], tuple[float, float]] = {}

    def add(self, item: tuple[float, float]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[float, float]) -> tuple[float, float]:
        self.add(item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: tuple[float, float], right: tuple[float, float]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _analysis_from_native(items: list[dict[str, Any]]) -> AnalysisPorts:
    if not items:
        return AnalysisPorts()
    item = items[0]
    sources = list(item.get("source", []))
    detectors = list(item.get("detector", []))
    lgrefs = list(item.get("lgref", []))
    detector = None
    if detectors:
        detector_type, detector_ref = detectors[0]
        detector = f"{detector_type}_{detector_ref}"
    return AnalysisPorts(
        source=sources[0] if sources else None,
        detector=detector,
        lgref=lgrefs[0] if lgrefs else None,
    )


def slicap_schematic_to_internal(raw: dict[str, Any]) -> tuple[SchematicDocument, list[Diagnostic]]:
    """Convert SLiCAP 5.2.1 JSON into the editable internal schematic model."""

    diagnostics: list[Diagnostic] = []
    components: list[SchematicComponent] = []
    native_by_id: dict[str, dict[str, Any]] = {}
    internal_by_id: dict[str, SchematicComponent] = {}
    unknown_components: list[dict[str, Any]] = []
    pin_at_position: dict[tuple[float, float], list[PinRef]] = defaultdict(list)

    for native in raw.get("components", []):
        symbol = native.get("symbol_name", "")
        device = _SYMBOL_TO_DEVICE.get(symbol)
        if device is None:
            unknown_components.append(deepcopy(native))
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    code="slicap_symbol_read_only",
                    message=f"Symbol {symbol!r} is preserved but is not editable in the web schematic.",
                    location=native.get("instance_id"),
                )
            )
            continue
        instance_id = str(native.get("instance_id", symbol))
        properties: dict[str, Any] = {}
        params = {str(key): str(value) for key, value in native.get("params", {}).items()}
        if device in {"GROUND", "PORT"}:
            properties["name"] = params.get("name", "0" if device == "GROUND" else instance_id)
        component = SchematicComponent(
            id=instance_id,
            refdes=instance_id,
            device=device,
            position=Point(x=float(native.get("x", 0)), y=float(native.get("y", 0))),
            rotation=int(float(native.get("rotation", 0))) % 360,
            model=str(native.get("model", "")) or None,
            parameters={} if device in {"GROUND", "PORT"} else params,
            control_ref=(native.get("refs") or [None])[0],
            properties=properties,
            passthrough={
                "h_flip": bool(native.get("h_flip", False)),
                "v_flip": bool(native.get("v_flip", False)),
                "prop_display": deepcopy(native.get("prop_display", {})),
                "prop_offsets": deepcopy(native.get("prop_offsets", {})),
                "symbol_name": symbol,
            },
        )
        components.append(component)
        native_by_id[instance_id] = native
        internal_by_id[instance_id] = component
        for pin_name, position in _component_pin_positions(native).items():
            pin_at_position[position].append(PinRef(component_id=instance_id, pin_id=pin_name))

    for index, native in enumerate(raw.get("junctions", []), start=1):
        instance_id = f"junction-{index}"
        component = SchematicComponent(
            id=instance_id,
            refdes=f"J{index}",
            device="JUNCTION",
            position=Point(x=float(native.get("x", 0)), y=float(native.get("y", 0))),
        )
        components.append(component)
        internal_by_id[instance_id] = component
        position = _round_point((component.position.x, component.position.y))
        pin_at_position[position].append(PinRef(component_id=instance_id, pin_id="junction"))

    union_find = _CoordinateUnionFind()
    wire_records: list[tuple[list[tuple[float, float]], dict[str, Any]]] = []
    candidates = set(pin_at_position)
    for native_wire in raw.get("wires", []):
        points = [_round_point((float(point[0]), float(point[1]))) for point in native_wire.get("points", [])]
        if len(points) < 2:
            continue
        candidates.update(points)
        wire_records.append((points, native_wire))
    for point in candidates:
        union_find.add(point)
    for points, _ in wire_records:
        for start, end in zip(points, points[1:]):
            union_find.union(start, end)
            for candidate in candidates:
                if _point_on_segment(candidate, start, end):
                    union_find.union(start, candidate)

    net_names: dict[tuple[float, float], set[str]] = defaultdict(set)
    for points, native_wire in wire_records:
        name = native_wire.get("user_net_name") or native_wire.get("net_name")
        if name:
            net_names[union_find.find(points[0])].add(str(name))

    wires: list[SchematicWire] = []
    implicit_junctions: dict[tuple[float, float], PinRef] = {}

    def endpoint_ref(position: tuple[float, float]) -> PinRef:
        references = pin_at_position.get(position, [])
        if references:
            return sorted(
                references,
                key=lambda item: (internal_by_id[item.component_id].device == "JUNCTION", item.component_id),
            )[0]
        if position in implicit_junctions:
            return implicit_junctions[position]
        sequence = len([item for item in components if item.device == "JUNCTION"]) + 1
        instance_id = f"junction-{sequence}"
        component = SchematicComponent(
            id=instance_id,
            refdes=f"J{sequence}",
            device="JUNCTION",
            position=Point(x=position[0], y=position[1]),
        )
        components.append(component)
        internal_by_id[instance_id] = component
        reference = PinRef(component_id=instance_id, pin_id="junction")
        implicit_junctions[position] = reference
        pin_at_position[position].append(reference)
        return reference

    for wire_index, (points, native_wire) in enumerate(wire_records, start=1):
        root = union_find.find(points[0])
        names = net_names.get(root, set())
        if len(names) > 1:
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="slicap_conflicting_net_names",
                    message="Native schematic net has conflicting names: " + ", ".join(sorted(names)),
                )
            )
        net_name = sorted(names)[0] if names else None
        wires.append(
            SchematicWire(
                id=f"W{wire_index}",
                source=endpoint_ref(points[0]),
                target=endpoint_ref(points[-1]),
                waypoints=[Point(x=point[0], y=point[1]) for point in points[1:-1]],
                net_name=net_name,
            )
        )

    parameter_values: dict[str, str] = {}
    for block in raw.get("parameters", []):
        for pair in block.get("params", []):
            if len(pair) >= 2:
                parameter_values[str(pair[0])] = str(pair[1])

    properties = raw.get("properties", {})
    passthrough = {
        "slicap_unknown_top_level": {key: deepcopy(value) for key, value in raw.items() if key not in _KNOWN_TOP_LEVEL},
        "slicap_unknown_components": unknown_components,
        "slicap_read_only": {
            key: deepcopy(raw.get(key))
            for key in _KNOWN_TOP_LEVEL - {"components", "wires", "parameters", "analysis_items", "properties"}
            if key in raw
        },
        "slicap_properties": deepcopy(properties),
    }
    return (
        SchematicDocument(
            title=str(properties.get("title") or "Untitled circuit"),
            components=components,
            wires=wires,
            parameters=parameter_values,
            analysis=_analysis_from_native(raw.get("analysis_items", [])),
            passthrough=passthrough,
        ),
        diagnostics,
    )


def _native_component(component: SchematicComponent) -> dict[str, Any]:
    catalog = DEVICE_CATALOG[component.device]
    symbol_name = str(component.passthrough.get("symbol_name") or catalog["symbol"])
    params = dict(component.parameters)
    if component.device in {"GROUND", "PORT"}:
        params = {"name": str(component.properties.get("name", "0" if component.device == "GROUND" else component.refdes))}
    native = {
        "symbol_name": symbol_name,
        "instance_id": component.refdes,
        "x": component.position.x,
        "y": component.position.y,
        "rotation": component.rotation,
        "h_flip": bool(component.passthrough.get("h_flip", False)),
        "v_flip": bool(component.passthrough.get("v_flip", False)),
        "params": params,
        "model": component.model if component.model is not None else (catalog.get("model") or ""),
        "refs": [component.control_ref] if component.control_ref else [],
        "prop_display": deepcopy(component.passthrough.get("prop_display", {"refdes": [True, False]})),
        "prop_offsets": deepcopy(component.passthrough.get("prop_offsets", {})),
    }
    return native


def _native_detector(detector: str | None) -> list[list[str]]:
    if not detector:
        return []
    if "_" in detector:
        kind, reference = detector.split("_", 1)
        if kind.upper() in {"V", "I"}:
            return [[kind.upper(), reference]]
    return [["V", detector]]


def _routed_points(
    source: tuple[float, float],
    target: tuple[float, float],
    waypoints: list[Point],
    lane: int,
) -> list[list[float]]:
    """Create an orthogonal route instead of a long pin-crossing straight wire."""

    if waypoints:
        return [list(source), *[[point.x, point.y] for point in waypoints], list(target)]
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    offset = 30 + 15 * lane
    if abs(dx) < 1e-6 and abs(dy) > 40:
        offset_x = source[0] + offset
        return [list(source), [offset_x, source[1]], [offset_x, target[1]], list(target)]
    if abs(dy) < 1e-6:
        return [list(source), list(target)]
    if abs(dx) > 1e-6 and abs(dy) > 1e-6:
        middle_x = round(((source[0] + target[0]) / 2 + 15 * lane) / 5) * 5
        return [list(source), [middle_x, source[1]], [middle_x, target[1]], list(target)]
    return [list(source), list(target)]


def internal_to_slicap_schematic(document: SchematicDocument) -> tuple[dict[str, Any], list[Diagnostic]]:
    """Convert the internal model into native SLiCAP 5.2.1 schematic JSON."""

    diagnostics: list[Diagnostic] = []
    native_components: list[dict[str, Any]] = []
    native_by_internal_id: dict[str, dict[str, Any]] = {}
    for component in document.components:
        catalog = DEVICE_CATALOG.get(component.device, {})
        if component.device == "JUNCTION":
            native_by_internal_id[component.id] = {
                "symbol_name": "__junction__",
                "x": component.position.x,
                "y": component.position.y,
            }
            continue
        if component.device == "X" or not catalog.get("symbol"):
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="slicap_subcircuit_export_unsupported",
                    message=(
                        f"{component.refdes} can be exported to .cir, but the web editor cannot "
                        "yet create a native SLiCAP 5.2.1 subcircuit symbol without its symbol library."
                    ),
                    location=component.id,
                )
            )
            continue
        native = _native_component(component)
        native_components.append(native)
        native_by_internal_id[component.id] = native
    wires: list[dict[str, Any]] = []
    for wire_index, wire in enumerate(document.wires):
        source_component = native_by_internal_id.get(wire.source.component_id)
        target_component = native_by_internal_id.get(wire.target.component_id)
        if source_component is None or target_component is None:
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="invalid_wire_component",
                    message="Cannot export a wire whose component is missing.",
                    location=wire.id,
                )
            )
            continue
        source_position = _component_pin_positions(source_component).get(wire.source.pin_id)
        target_position = _component_pin_positions(target_component).get(wire.target.pin_id)
        if source_position is None or target_position is None:
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="invalid_wire_pin",
                    message="Cannot export a wire whose pin is unknown to SLiCAP 5.2.1.",
                    location=wire.id,
                )
            )
            continue
        points = _routed_points(source_position, target_position, wire.waypoints, wire_index)
        wires.append(
            {
                "points": points,
                "net_name": wire.net_name,
                "display_name": bool(wire.net_name),
                "label_offset": [0.0, -3.0],
                "net_locked": bool(wire.net_name),
                "user_net_name": wire.net_name,
                "show_dc_voltage": False,
                "dc_label_offset": [0.0, 6.0],
            }
        )

    passthrough = document.passthrough
    native: dict[str, Any] = deepcopy(passthrough.get("slicap_unknown_top_level", {}))
    read_only = passthrough.get("slicap_read_only", {})
    for key in _KNOWN_TOP_LEVEL - {"components", "wires", "parameters", "analysis_items", "properties"}:
        native[key] = deepcopy(read_only.get(key, [] if key != "border" else None))
    native["components"] = native_components + deepcopy(passthrough.get("slicap_unknown_components", []))
    native["junctions"] = [
        {"x": component.position.x, "y": component.position.y}
        for component in document.components
        if component.device == "JUNCTION"
    ]
    native["wires"] = wires
    native["parameters"] = []
    if document.parameters:
        native["parameters"].append(
            {
                "x": 0.0,
                "y": 0.0,
                "params": [[name, value] for name, value in sorted(document.parameters.items())],
                "preamble_path": "",
                "display_width": 200,
                "display_height": 80,
                "show": True,
            }
        )
    native["analysis_items"] = [
        {
            "x": 0.0,
            "y": 40.0,
            "source": [document.analysis.source] if document.analysis.source else [],
            "detector": _native_detector(document.analysis.detector),
            "lgref": [document.analysis.lgref] if document.analysis.lgref else [],
            "show": True,
        }
    ]
    properties = deepcopy(passthrough.get("slicap_properties", {}))
    properties.update(
        {
            "title": document.title,
            "author": properties.get("author", ""),
            "project": properties.get("project", ""),
            "created": properties.get("created", ""),
            "last_modified": properties.get("last_modified", ""),
            "page_size": properties.get("page_size", "A4"),
            "page_width_mm": properties.get("page_width_mm", 210.0),
            "page_height_mm": properties.get("page_height_mm", 297.0),
            "is_subcircuit": properties.get("is_subcircuit", False),
            "subcircuit_ports": properties.get("subcircuit_ports", []),
            "subcircuit_params": properties.get("subcircuit_params", []),
            "control_section": properties.get("control_section", ""),
        }
    )
    native["properties"] = properties
    return native, diagnostics


def pinned_symbol_pins() -> dict[str, dict[str, tuple[float, float]]]:
    """Return official pin positions for compatibility tests."""

    return {
        item["symbol"]: {
            name: (float(position["x"]), float(position["y"]))
            for name, position in item["pin_positions"].items()
        }
        for item in DEVICE_CATALOG.values()
        if item.get("symbol")
    }
