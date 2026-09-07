from __future__ import annotations

from pathlib import Path

from sfg_prototype import (
    VisionBridgeConfig,
    bridge_report,
    convert_netlens_sp_to_slicap,
    flatten_netlist,
)


NETLENS_COMMON_SOURCE = """
.subckt common_source
r1 VDD VOUT r
m1 VOUT VIN gnd gnd nmos4
.ends
"""


def _numeric_parameters() -> dict[str, str]:
    return {
        "R1": "10k",
        "gm_M1": "1m",
        "gb_M1": "0",
        "go_M1": "10u",
        "cgs_M1": "1p",
        "cdg_M1": "100f",
        "cgb_M1": "0",
        "cdb_M1": "200f",
        "csb_M1": "0",
    }


def test_netlens_topology_compiles_to_symbolic_slicap() -> None:
    result = convert_netlens_sp_to_slicap(
        NETLENS_COMMON_SOURCE,
        VisionBridgeConfig(source_node="VIN", detector_node="VOUT"),
        from_text=True,
    )

    assert result.symbolically_ready
    assert not result.numerically_ready
    assert result.source_ref == "Vin"
    assert result.detector == "V_VOUT"
    assert "Vin VIN 0 V value={Vin}" in result.netlist_text
    assert "VDD VDD 0 V value=0 dc={VDD}" in result.netlist_text
    assert "R1 VDD VOUT R value={R1}" in result.netlist_text
    assert "M1 VOUT VIN 0 0 M gm={gm_M1}" in result.netlist_text
    assert "R1" in result.unresolved_parameters
    assert "gm_M1" in result.unresolved_parameters
    assert "NUMERIC_PARAMETER_REQUIRED" in bridge_report(result)


def test_complete_bridge_output_is_accepted_by_slicap(tmp_path: Path) -> None:
    result = convert_netlens_sp_to_slicap(
        NETLENS_COMMON_SOURCE,
        VisionBridgeConfig(
            source_node="VIN",
            detector_node="VOUT",
            parameter_values=_numeric_parameters(),
        ),
        from_text=True,
    )
    assert result.symbolically_ready
    assert result.numerically_ready

    output_path = result.write(tmp_path / "vision_common_source.cir")
    network = flatten_netlist(output_path)

    assert network.title == "common_source"
    assert network.source == ["Vin", None]
    assert network.detector == ["V_VOUT", None]
    assert "R1" in network.elements
    assert any(refdes.endswith("_M1") for refdes in network.elements)


def test_missing_source_and_detector_are_explicit_errors() -> None:
    result = convert_netlens_sp_to_slicap(
        ".subckt anonymous\nr1 net1 net2 r\n.ends\n",
        from_text=True,
    )
    codes = {item.code for item in result.diagnostics}

    assert not result.symbolically_ready
    assert "SOURCE_NODE_REQUIRED" in codes
    assert "DETECTOR_NODE_REQUIRED" in codes


def test_unsupported_visual_component_does_not_silently_pass() -> None:
    result = convert_netlens_sp_to_slicap(
        ".subckt digital\nxi1 in out inverter\n.ends\n",
        VisionBridgeConfig(source_node="in", detector_node="out"),
        from_text=True,
    )
    codes = {item.code for item in result.diagnostics}

    assert not result.symbolically_ready
    assert "UNSUPPORTED_MODEL" in codes
    assert "EMPTY_TOPOLOGY" in codes
