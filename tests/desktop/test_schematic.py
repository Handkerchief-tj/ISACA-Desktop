from __future__ import annotations

import json
import subprocess
import sys

from SLiCAP.schematic.schematic_data import SchematicData

from isaca_api.catalog import device_catalog
from isaca_api.official_schematic import official_netlist_from_schematic
from isaca_api.schematic import schematic_to_netlist
from isaca_api.slicap_schematic import internal_to_slicap_schematic, slicap_schematic_to_internal
from isaca_api.models import (
    AnalysisPorts,
    PinRef,
    Point,
    SchematicComponent,
    SchematicDocument,
    SchematicWire,
)


def test_internal_schematic_exports_expected_rc_netlist(rc_schematic) -> None:
    netlist, diagnostics = schematic_to_netlist(rc_schematic)
    errors = [item for item in diagnostics if item.level == "error"]
    assert errors == []
    assert "V1 in 0 V value={Vin}" in netlist
    assert "R1 in out R value={R}" in netlist
    assert "C1 out 0 C value={C}" in netlist
    assert ".param C=1u R=1k" in netlist
    assert ".source V1" in netlist
    assert ".detector V_out" in netlist


def test_native_slicap_round_trip_preserves_electrical_topology(rc_schematic) -> None:
    native, export_diagnostics = internal_to_slicap_schematic(rc_schematic)
    assert [item for item in export_diagnostics if item.level == "error"] == []
    parsed = SchematicData.from_json(json.dumps(native))
    assert len(parsed.components) == len(rc_schematic.components)
    assert len(parsed.wires) == len(rc_schematic.wires)

    restored, import_diagnostics = slicap_schematic_to_internal(native)
    assert [item for item in import_diagnostics if item.level == "error"] == []
    restored_netlist, restored_diagnostics = schematic_to_netlist(restored)
    assert [item for item in restored_diagnostics if item.level == "error"] == []
    assert "R1 in out R value={R}" in restored_netlist
    assert "C1 out 0 C value={C}" in restored_netlist
    assert restored.analysis.source == "V1"
    assert restored.analysis.detector == "V_out"


def test_unknown_native_objects_are_preserved_read_only(rc_schematic) -> None:
    native, _ = internal_to_slicap_schematic(rc_schematic)
    native["future_field"] = {"value": 7}
    native["components"].append({
        "symbol_name": "FUTURE_DEVICE",
        "instance_id": "Z1",
        "x": 0,
        "y": 0,
    })
    restored, diagnostics = slicap_schematic_to_internal(native)
    exported, _ = internal_to_slicap_schematic(restored)
    assert exported["future_field"] == {"value": 7}
    assert any(component["instance_id"] == "Z1" for component in exported["components"])
    assert any(item.code == "slicap_symbol_read_only" for item in diagnostics)


def test_native_junctions_and_wire_waypoints_survive_round_trip(rc_schematic) -> None:
    native, _ = internal_to_slicap_schematic(rc_schematic)
    native["junctions"] = [{"x": 250.0, "y": 300.0}]
    restored, diagnostics = slicap_schematic_to_internal(native)
    assert [item for item in diagnostics if item.level == "error"] == []
    assert any(component.device == "JUNCTION" for component in restored.components)
    assert any(wire.waypoints for wire in restored.wires)

    exported, export_diagnostics = internal_to_slicap_schematic(restored)
    assert [item for item in export_diagnostics if item.level == "error"] == []
    assert {"x": 250.0, "y": 300.0} in exported["junctions"]


def test_native_file_is_netlisted_by_official_slicap_cli(tmp_path, rc_schematic) -> None:
    native, diagnostics = internal_to_slicap_schematic(rc_schematic)
    assert [item for item in diagnostics if item.level == "error"] == []
    schematic_path = tmp_path / "rc.slicap_sch"
    netlist_path = tmp_path / "rc.cir"
    schematic_path.write_text(json.dumps(native), encoding="utf-8")
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
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    netlist = netlist_path.read_text(encoding="utf-8")
    assert "V1 in 0 V value={Vin}" in netlist
    assert "R1 in out R value={R}" in netlist
    assert "C1 out 0 C value={C}" in netlist
    assert ".source V1" in netlist
    assert ".detector V_out" in netlist


def test_official_core_devices_export_through_slicap_cli(tmp_path) -> None:
    """Exercise official pins, models, parameters and current-control refs."""

    catalog = device_catalog()
    specifications = [
        ("vctrl", "VCTRL", "V", {"value": "1"}, None),
        ("l1", "L1", "L", {"value": "1m"}, None),
        ("g1", "G1", "G", {"value": "gain"}, None),
        ("e1", "E1", "E", {"value": "gain"}, None),
        ("f1", "F1", "F", {"value": "gain"}, "VCTRL"),
        ("h1", "H1", "H", {"value": "gain"}, "VCTRL"),
        ("m1", "M1", "M", {"gm": "gm", "go": "go"}, None),
        ("q1", "Q1", "QV", {"gm": "gm", "go": "go"}, None),
    ]
    components: list[SchematicComponent] = []
    wires: list[SchematicWire] = []
    net_index = 1

    for row, (component_id, refdes, device, parameters, control_ref) in enumerate(specifications):
        origin = Point(x=200.0, y=100.0 + row * 140.0)
        components.append(
            SchematicComponent(
                id=component_id,
                refdes=refdes,
                device=device,
                position=origin,
                model=catalog[device]["model"],
                parameters=parameters,
                control_ref=control_ref,
            )
        )
        for pin_id, coordinates in catalog[device]["pin_positions"].items():
            port_id = f"port-{net_index}"
            pin_x = origin.x + coordinates["x"]
            pin_y = origin.y + coordinates["y"]
            offset = -60.0 if coordinates["x"] <= 0 else 60.0
            components.append(
                SchematicComponent(
                    id=port_id,
                    refdes=f"P{net_index}",
                    device="PORT",
                    position=Point(x=pin_x + offset, y=pin_y),
                    properties={"name": f"n{net_index}"},
                )
            )
            wires.append(
                SchematicWire(
                    id=f"W{net_index}",
                    source=PinRef(component_id=component_id, pin_id=pin_id),
                    target=PinRef(component_id=port_id, pin_id="port"),
                )
            )
            net_index += 1

    document = SchematicDocument(
        title="Official core devices",
        components=components,
        wires=wires,
        parameters={"gain": "2", "gm": "1m", "go": "1u"},
    )
    native, conversion_diagnostics = internal_to_slicap_schematic(document)
    assert [item for item in conversion_diagnostics if item.level == "error"] == []

    netlist, export_diagnostics = official_netlist_from_schematic(native, tmp_path)
    assert export_diagnostics == []
    assert netlist is not None
    lines = {line.split()[0]: line for line in netlist.splitlines() if line and not line.startswith(".")}
    for refdes, model in {
        "VCTRL": "V",
        "L1": "L",
        "G1": "G",
        "E1": "E",
        "F1": "F",
        "H1": "H",
        "M1": "M",
        "Q1": "QV",
    }.items():
        assert f" {model} " in lines[refdes]
    assert " VCTRL " in lines["F1"]
    assert " VCTRL " in lines["H1"]
    assert "gm={gm}" in lines["M1"]
    assert "gm={gm}" in lines["Q1"]


def test_subcircuit_block_exports_to_cir_and_reports_native_limit() -> None:
    document = SchematicDocument(
        title="Subcircuit wrapper",
        components=[
            SchematicComponent(
                id="x1",
                refdes="X1",
                device="X",
                position=Point(x=0, y=0),
                model="gain_stage",
                properties={"pins": ["in", "out"]},
            )
        ],
        analysis=AnalysisPorts(source="Vin", detector="V_out"),
    )
    netlist, netlist_diagnostics = schematic_to_netlist(document)
    assert "X1 N001 N002 gain_stage" in netlist
    assert not any(item.code == "unknown_device" for item in netlist_diagnostics)

    _, native_diagnostics = internal_to_slicap_schematic(document)
    assert any(item.code == "slicap_subcircuit_export_unsupported" for item in native_diagnostics)
