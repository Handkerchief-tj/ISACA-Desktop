"""Regression tests for the SLiCAP network-to-SFG boundary."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import sympy as sp

import pytest

from sfg_prototype import (
    BaselineEquivalenceError,
    build_signal_flow_graph,
    flatten_netlist,
    graph_transfer_function,
    run_reference_pipeline,
    validate_graph_equivalence,
)
from sfg_prototype.analysis import analyze_meta_edge_candidates, analyze_paper_topology, paper_complexity
from sfg_prototype.pipeline import _paper_simplified_complexity


FIXTURES = Path(__file__).parent / "fixtures"


def test_graph_preserves_netlist_source_and_detector_metadata() -> None:
    network = flatten_netlist(FIXTURES / "source_follower_symbolic.cir")
    graph = build_signal_flow_graph(network, strict=True)
    transfer, source, detector, detector_vertex = graph_transfer_function(graph)
    expected = sp.Symbol("Rs") * sp.Symbol("gm") / (
        1 + sp.Symbol("Rs") * (sp.Symbol("gm") + sp.Symbol("go"))
    )

    assert graph.source_name == "Vin"
    assert graph.detector_name == "V_source"
    assert source == "Vin"
    assert detector == "V_source"
    assert detector_vertex == "source.v"
    assert sp.simplify(transfer - expected) == 0


def test_zero_voltage_current_sensor_is_preserved() -> None:
    network = flatten_netlist(FIXTURES / "controlled_sources.cir")
    graph = build_signal_flow_graph(network, strict=True)

    assert graph.deferred_elements == []
    assert graph.current_references["Vx"] == "I_Vx"
    assert graph.current_reference_directions["Vx"] == ("sense", "0")
    assert ("I_Vx", "sense.i", "current") in graph.meta_edges
    assert ("I_Vx", "fout.i", "current") in graph.meta_edges
    assert ("I_Vx", "hvout.v", "voltage") in graph.meta_edges


def test_floating_voltage_source_matches_supernode_solution() -> None:
    network = flatten_netlist(FIXTURES / "floating_vsource.cir")
    graph = build_signal_flow_graph(network, strict=True)
    transfer, _source, _detector, _vertex = graph_transfer_function(
        graph,
        substitutions={"Vin": 1, "R1": 1000, "R2": 2000},
    )

    assert sp.simplify(transfer - sp.Rational(1, 3)) == 0
    assert "I_Vin" in graph.vertices
    assert graph.voltage_constraints == []


def test_baseline_gate_rejects_a_changed_unsimplified_graph() -> None:
    pipeline = run_reference_pipeline(
        FIXTURES / "rc_lowpass_numeric.cir",
        frequency_range_hz=(1.0, 1.0e6),
        enable_subrange_transfers=False,
    )
    changed = deepcopy(pipeline.graph)
    key = ("in.v", "out.i", "admittance")
    edge = changed.meta_edges[key]
    changed.meta_edges[key] = replace(edge, value=2 * edge.value)

    with pytest.raises(BaselineEquivalenceError):
        validate_graph_equivalence(
            changed,
            pipeline,
            frequencies=(1.0, 1.0e3, 1.0e6),
        )


def test_fast_eq10_complexity_matches_full_topology_analysis() -> None:
    pipeline = run_reference_pipeline(
        FIXTURES / "rc_lowpass_numeric.cir",
        frequency_range_hz=(1.0, 1.0e6),
        enable_subrange_transfers=False,
    )
    candidates = tuple(
        analyze_meta_edge_candidates(
            pipeline.graph,
            substitutions=pipeline.network.numeric_substitutions(),
        )
    )
    topology = analyze_paper_topology(
        pipeline.graph,
        candidates,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
    )
    expected = paper_complexity(
        pipeline.graph,
        candidates,
        topology,
        use_simplified=True,
    )
    actual = _paper_simplified_complexity(pipeline.graph, candidates)

    assert actual.value == expected.value
    assert actual.open_loop_roots == expected.open_loop_roots
    assert actual.summing_points == expected.summing_points


@pytest.mark.parametrize(
    ("fixture", "detector", "expected"),
    (
        ("current_control_direction.cir", "V_fout", -2),
        ("current_control_direction.cir", "V_hvout", 2),
        ("vcvs_chain.cir", "V_out", -6),
        ("independent_current_source.cir", "V_out", 1000),
    ),
)
def test_source_direction_and_voltage_constraints(
    fixture: str,
    detector: str,
    expected: int,
) -> None:
    network = flatten_netlist(FIXTURES / fixture)
    graph = build_signal_flow_graph(network, strict=True)
    transfer, _source, _detector, _vertex = graph_transfer_function(
        graph,
        source=network.source[0],
        detector=detector,
    )

    assert sp.simplify(transfer - expected) == 0
