"""Unit tests for the four paper Section III-A graph manipulations."""

import sympy as sp

from sfg_prototype import (
    GraphVertex,
    LocalZeroPath,
    LocalizedSymbolicZero,
    MetaEdge,
    SignalFlowGraph,
)
from sfg_prototype.simplify import (
    apply_graph_manipulation,
    propose_open_loop_root_removals,
    propose_signal_path_removals,
    propose_subgraph_substitutions,
    propose_local_zero_substitutions,
    propose_vertex_pair_contractions,
)


def _edge(source: str, target: str, domain: str, value: sp.Expr) -> MetaEdge:
    return MetaEdge(source, target, domain, value, {}, ())


def _graph(*vertices: str) -> SignalFlowGraph:
    graph = SignalFlowGraph(title="test", file_path=None)
    for name in vertices:
        kind = "voltage" if name.endswith(".v") else "current"
        graph.vertices[name] = GraphVertex(name=name, node=name.rsplit(".", 1)[0], kind=kind)
    return graph


def test_rsp_removes_one_summing_input_without_mutating_original_graph() -> None:
    graph = _graph("a.v", "b.v", "sum.i")
    graph.meta_edges[("a.v", "sum.i", "admittance")] = _edge("a.v", "sum.i", "admittance", sp.Symbol("g1"))
    graph.meta_edges[("b.v", "sum.i", "admittance")] = _edge("b.v", "sum.i", "admittance", sp.Symbol("g2"))

    manipulation = propose_signal_path_removals(graph)[0]
    result = apply_graph_manipulation(graph, manipulation)

    assert manipulation.kind == "RSP"
    assert manipulation.key in graph.meta_edges
    assert manipulation.key not in result.graph.meta_edges


def test_rr_removes_one_extreme_order_partition_without_mutating_original_graph() -> None:
    g, c, s = sp.symbols("g c s")
    graph = _graph("a.v", "sum.i")
    key = ("a.v", "sum.i", "admittance")
    graph.meta_edges[key] = _edge("a.v", "sum.i", "admittance", g + s * c)

    manipulation = next(item for item in propose_open_loop_root_removals(graph) if item.new_value == g)
    result = apply_graph_manipulation(graph, manipulation)

    assert manipulation.kind == "RR"
    assert graph.meta_edges[key].value == g + s * c
    assert result.graph.meta_edges[key].value == g


def test_vpc_contracts_a_connected_voltage_current_node_pair_on_a_copy() -> None:
    graph = _graph("a.v", "a.i", "b.v", "b.i")
    edges = (
        _edge("a.i", "a.v", "impedance", sp.Symbol("Za")),
        _edge("b.i", "b.v", "impedance", sp.Symbol("Zb")),
        _edge("a.v", "b.i", "admittance", sp.Symbol("Yab")),
        _edge("b.v", "a.i", "admittance", sp.Symbol("Yba")),
    )
    graph.meta_edges = {(edge.source, edge.target, edge.domain): edge for edge in edges}

    manipulation = propose_vertex_pair_contractions(graph)[0]
    result = apply_graph_manipulation(graph, manipulation)

    assert manipulation.kind == "VPC"
    assert len(graph.vertices) == 4
    assert len(result.graph.vertices) == 2


def test_ss_replaces_a_simple_chain_and_preserves_the_original_graph() -> None:
    graph = _graph("a.v", "middle.i", "out.v")
    first = _edge("a.v", "middle.i", "admittance", sp.Symbol("g"))
    second = _edge("middle.i", "out.v", "impedance", sp.Symbol("Z"))
    graph.meta_edges = {
        (first.source, first.target, first.domain): first,
        (second.source, second.target, second.domain): second,
    }

    manipulation = propose_subgraph_substitutions(graph)[0]
    result = apply_graph_manipulation(graph, manipulation)

    assert manipulation.kind == "SS"
    assert len(graph.meta_edges) == 2
    assert len(result.graph.meta_edges) == 1
    replacement = next(iter(result.graph.meta_edges.values()))
    assert sp.simplify(replacement.value - sp.Symbol("g") * sp.Symbol("Z")) == 0


def test_strict_local_zero_region_proposes_an_exact_ss_replacement() -> None:
    graph = _graph("in.v", "middle.i", "out.i")
    g1, g2, gd = sp.symbols("g1 g2 gd")
    first = _edge("in.v", "middle.i", "admittance", g1)
    second = _edge("middle.i", "out.i", "admittance", g2)
    direct = _edge("in.v", "out.i", "admittance", gd)
    graph.meta_edges = {
        (edge.source, edge.target, edge.domain): edge
        for edge in (first, second, direct)
    }
    paths = (
        LocalZeroPath(
            vertices=("in.v", "middle.i", "out.i"),
            edges=(("in.v", "middle.i", "admittance"), ("middle.i", "out.i", "admittance")),
            gain=g1 * g2,
            mason_cofactor=sp.Integer(1),
            weighted_gain=g1 * g2,
        ),
        LocalZeroPath(
            vertices=("in.v", "out.i"),
            edges=(("in.v", "out.i", "admittance"),),
            gain=gd,
            mason_cofactor=sp.Integer(1),
            weighted_gain=gd,
        ),
    )
    zero = LocalizedSymbolicZero(
        cluster_index=1,
        vertex="out.i",
        expression=sp.Symbol("z"),
        characteristic_equation=g1 * g2 + gd,
        numeric_value=-1 + 0j,
        frequency_hz=1.0,
        reference_value=-1 + 0j,
        relative_root_error=0.0,
        cut_start="in.v",
        cut_end="out.i",
        cut_kind="complementary",
        paths=paths,
        cancellation_residual=0.0,
        method="local Mason numerator",
        equivalent_transfer=g1 * g2 + gd,
        status="resolved",
    )

    manipulation = propose_local_zero_substitutions(graph, (zero,))[0]
    result = apply_graph_manipulation(graph, manipulation)

    assert manipulation.kind == "SS"
    assert len(graph.meta_edges) == 3
    assert len(result.graph.meta_edges) == 1
    replacement = next(iter(result.graph.meta_edges.values()))
    assert sp.simplify(replacement.value - (g1 * g2 + gd)) == 0
