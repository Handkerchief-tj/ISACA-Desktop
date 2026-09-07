"""Tests for physically traceable local summing-point root expressions."""

import math

import sympy as sp

from sfg_prototype import (
    GraphVertex,
    LocalZeroPath,
    MetaEdge,
    PaperTopologyMetrics,
    SignalFlowGraph,
    SummingPointRoot,
    derive_local_summing_root_expression,
    derive_local_summing_zero_expression,
    derive_target_root_symbolic_approximations,
    validate_localized_zero_corners,
)
from sfg_prototype.analysis import RootCluster, RootLocalization, RootSample


s = sp.Symbol("s")
Gin, gx, gpi, gm, go, Gl = sp.symbols("Gin gx gpi gm go Gl")
cmu, cpi, cx, Cl = sp.symbols("cmu cpi cx Cl")

VALUES = {
    Gin: 1e-3,
    gx: 2e-3,
    gpi: 30e-6,
    gm: 100e-3,
    go: 3e-6,
    Gl: 1e-3,
    cmu: 2.5e-9,
    cpi: 12.5e-9,
    cx: 35e-15,
    Cl: 100e-9,
}


def _graph(*vertices: str) -> SignalFlowGraph:
    graph = SignalFlowGraph(title="local-root", file_path=None)
    for name in vertices:
        kind = "voltage" if name.endswith(".v") else "current"
        graph.vertices[name] = GraphVertex(name=name, node=name.rsplit(".", 1)[0], kind=kind)
    return graph


def _add_edge(graph: SignalFlowGraph, source: str, target: str, domain: str, value: sp.Expr) -> None:
    edge = MetaEdge(source, target, domain, value, {}, ())
    graph.meta_edges[(source, target, domain)] = edge


def _root(cluster: int, vertex: str, expression: sp.Expr) -> SummingPointRoot:
    value = complex(sp.N(expression.subs(VALUES)))
    return SummingPointRoot(
        vertex=vertex,
        value=value,
        frequency_hz=abs(value) / (2 * math.pi),
        path_edges=((), ()),
        cluster_index=cluster,
        observable=True,
        dominant=True,
        root_source="closed-loop pole localized at summing point",
    )


def _assert_term_pruning_is_bounded(result) -> None:
    """Check that optional product-term pruning preserves the selected local root."""
    if result.simplified_expression is None:
        assert result.retained_term_count == result.full_term_count
        return
    original = complex(sp.N(result.expression.subs(VALUES)))
    simplified = complex(sp.N(result.simplified_expression.subs(VALUES)))
    assert abs(simplified - original) / abs(original) <= 0.05 + 1e-12
    assert result.retained_term_count < result.full_term_count


def _zero_topology() -> PaperTopologyMetrics:
    return PaperTopologyMetrics(
        source_ref="input",
        source_vertex="input.v",
        detector="V_sum",
        detector_vertex="sum.i",
        forward_paths=(),
        feedback_loops=(),
        graph_cuts=(),
        summing_vertices=(),
        observable_edges=frozenset(),
        candidate_root_edges=frozenset(),
    )


def _two_path_zero_graph() -> tuple[SignalFlowGraph, SummingPointRoot, sp.Expr]:
    graph = _graph("input.v", "x.i", "x.v", "sum.i")
    direct = ("input.v", "sum.i", "admittance")
    first = ("input.v", "x.i", "admittance")
    second = ("x.i", "x.v", "impedance")
    third = ("x.v", "sum.i", "admittance")
    _add_edge(graph, *direct, s * cx)
    _add_edge(graph, *first, gx)
    _add_edge(graph, *second, 1 / (s * (cmu + cpi)))
    _add_edge(graph, *third, s * cmu)
    expected = -cmu * gx / (cx * (cmu + cpi))
    value = complex(sp.N(expected.subs(VALUES)))
    root = SummingPointRoot(
        vertex="sum.i",
        value=value,
        frequency_hz=abs(value) / (2 * math.pi),
        path_edges=((direct,), (first, second, third)),
        cluster_index=4,
        observable=True,
        dominant=True,
        root_source="closed-loop zero localized at summing point",
    )
    return graph, root, expected


def test_two_signed_forward_paths_recover_paper_equation_24() -> None:
    graph, root, expected = _two_path_zero_graph()

    result = derive_local_summing_zero_expression(graph, root, _zero_topology(), VALUES)

    assert result.status == "resolved"
    assert result.method == "local Mason numerator"
    assert sp.simplify(result.expression - expected) == 0
    assert sp.simplify(result.characteristic_equation - (cmu * cx * s + cmu * gx + cpi * cx * s)) == 0
    assert len(result.paths) == 2
    assert result.relative_root_error is not None
    assert result.relative_root_error < 1e-12


def test_local_system_matrix_fallback_recovers_the_same_zero() -> None:
    graph, root, expected = _two_path_zero_graph()

    result = derive_local_summing_zero_expression(
        graph,
        root,
        _zero_topology(),
        VALUES,
        strategy="matrix",
    )

    assert result.status == "resolved"
    assert result.method == "local Rosenbrock-style system matrix"
    assert sp.simplify(result.expression - expected) == 0

    automatic = derive_local_summing_zero_expression(
        graph,
        root,
        _zero_topology(),
        VALUES,
        max_paths=1,
        strategy="auto",
    )
    assert automatic.status == "resolved"
    assert automatic.method == "local Rosenbrock-style system matrix"
    assert sp.simplify(automatic.expression - expected) == 0


def test_local_zero_corners_are_checked_only_when_tolerances_are_explicit() -> None:
    graph, root, _expected = _two_path_zero_graph()
    result = derive_local_summing_zero_expression(graph, root, _zero_topology(), VALUES)
    reference_numerator = cx * (cmu + cpi) * s + cmu * gx

    unchecked = validate_localized_zero_corners(
        result,
        reference_numerator,
        VALUES,
        parameter_tolerances=None,
    )
    checked = validate_localized_zero_corners(
        result,
        reference_numerator,
        VALUES,
        parameter_tolerances={gx: 0.1, cmu: 0.1, cpi: 0.1, cx: 0.1},
        max_corner_parameters=4,
    )

    assert unchecked.corner_max_relative_error is None
    assert checked.status == "resolved"
    assert checked.corner_max_relative_error is not None
    assert checked.corner_max_relative_error < 1e-12


def test_local_mason_numerator_preserves_a_complex_zero_pair() -> None:
    damping, omega = sp.symbols("damping omega")
    values = {damping: sp.Rational(1, 2), omega: 1000}
    graph = _graph("input.v", "branch.i", "sum.i")
    direct = ("input.v", "sum.i", "admittance")
    first = ("input.v", "branch.i", "admittance")
    second = ("branch.i", "sum.i", "admittance")
    _add_edge(graph, *direct, s**2 + 2 * damping * omega * s)
    _add_edge(graph, *first, omega**2)
    _add_edge(graph, *second, sp.Integer(1))
    expected = -500 + 500 * sp.sqrt(3) * sp.I
    target = complex(sp.N(expected))
    root = SummingPointRoot(
        vertex="sum.i",
        value=target,
        frequency_hz=abs(target) / (2 * math.pi),
        path_edges=((direct,), (first, second)),
        cluster_index=1,
        observable=True,
        dominant=True,
        root_source="closed-loop zero localized at summing point",
    )

    result = derive_local_summing_zero_expression(
        graph,
        root,
        _zero_topology(),
        values,
    )

    assert result.status == "resolved"
    assert result.numeric_value is not None
    assert abs(result.numeric_value - target) / abs(target) < 1e-12
    assert result.numeric_value.real < 0
    assert result.numeric_value.imag > 0


def test_matrix_fallback_does_not_report_a_cleared_denominator_pole_as_zero() -> None:
    a, b, k = sp.symbols("a b k")
    values = {a: 1, b: 10, k: 1}
    graph = _graph("input.v", "branch.i", "sum.i")
    direct = ("input.v", "sum.i", "admittance")
    first = ("input.v", "branch.i", "admittance")
    second = ("branch.i", "sum.i", "admittance")
    _add_edge(graph, *direct, (s + a) / (s + b))
    _add_edge(graph, *first, k)
    _add_edge(graph, *second, sp.Integer(1))
    expected = -(a + k * b) / (1 + k)
    target = complex(sp.N(expected.subs(values)))
    root = SummingPointRoot(
        vertex="sum.i",
        value=target,
        frequency_hz=abs(target) / (2 * math.pi),
        path_edges=((direct,), (first, second)),
        cluster_index=1,
        observable=True,
        dominant=True,
        root_source="closed-loop zero localized at summing point",
    )

    result = derive_local_summing_zero_expression(
        graph,
        root,
        _zero_topology(),
        values,
        strategy="matrix",
    )

    assert result.status == "resolved"
    assert sp.simplify(result.expression - expected) == 0
    assert abs(result.numeric_value + 10) > 1


def test_single_local_loop_recovers_paper_equation_22() -> None:
    graph = _graph("n1p.i", "n1p.v", "n2.i", "n2.v")
    _add_edge(graph, "n1p.i", "n1p.v", "impedance", 1 / (gx + gpi + s * (cmu + cpi)))
    _add_edge(graph, "n1p.v", "n2.i", "admittance", -gm)
    _add_edge(graph, "n2.i", "n2.v", "impedance", 1 / (s * (cmu + cx + Cl)))
    _add_edge(graph, "n2.v", "n1p.i", "admittance", s * cmu)
    expected = -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
        (cmu + cpi) * (cmu + cx + Cl)
    )

    result = derive_local_summing_root_expression(graph, _root(2, "n1p.i", expected), VALUES)

    assert result is not None
    assert sp.simplify(result.expression - expected) == 0
    assert len(result.loop_gains) == 1
    assert result.relative_frequency_error is not None
    assert result.relative_frequency_error < 1e-12
    _assert_term_pruning_is_bounded(result)


def test_two_local_loops_produce_a_mason_pole_near_the_first_closed_loop_root() -> None:
    graph = _graph("n1.i", "n1.v", "n1p.i", "n1p.v", "n2.i", "n2.v")
    _add_edge(graph, "n1.i", "n1.v", "impedance", 1 / (Gin + gx))
    _add_edge(graph, "n1.v", "n1p.i", "admittance", gx)
    _add_edge(graph, "n1p.i", "n1p.v", "impedance", 1 / (gx + gpi))
    _add_edge(graph, "n1p.v", "n1.i", "admittance", gx)
    _add_edge(graph, "n1p.v", "n2.i", "admittance", -gm)
    _add_edge(graph, "n2.i", "n2.v", "impedance", 1 / (go + Gl + s * (cmu + cx + Cl)))
    _add_edge(graph, "n2.v", "n1p.i", "admittance", s * cmu)
    reference = -2 * math.pi * 333.72
    root = SummingPointRoot(
        vertex="n1p.i",
        value=complex(reference),
        frequency_hz=333.72,
        path_edges=((), ()),
        cluster_index=1,
        observable=True,
        dominant=True,
        root_source="closed-loop pole localized at summing point",
    )

    result = derive_local_summing_root_expression(graph, root, VALUES)

    assert result is not None
    assert len(result.loop_gains) == 2
    assert set(result.scc_vertices) == {"n1.i", "n1.v", "n1p.i", "n1p.v", "n2.i", "n2.v"}
    assert result.frequency_hz is not None
    assert abs(result.frequency_hz - 333.72) / 333.72 < 0.05
    _assert_term_pruning_is_bounded(result)


def test_readability_selection_recovers_equation_22_from_two_cluster2_loops() -> None:
    graph = _graph("n1.i", "n1.v", "n1p.i", "n1p.v", "n2.i", "n2.v")
    _add_edge(graph, "n1.i", "n1.v", "impedance", 1 / (Gin + gx))
    _add_edge(graph, "n1.v", "n1p.i", "admittance", gx)
    _add_edge(graph, "n1p.i", "n1p.v", "impedance", 1 / (gx + gpi + s * (cmu + cpi)))
    _add_edge(graph, "n1p.v", "n1.i", "admittance", gx)
    _add_edge(graph, "n1p.v", "n2.i", "admittance", -gm)
    _add_edge(graph, "n2.i", "n2.v", "impedance", 1 / (s * (cmu + cx + Cl)))
    _add_edge(graph, "n2.v", "n1p.i", "admittance", s * cmu)
    expected = -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
        (cmu + cpi) * (cmu + cx + Cl)
    )
    reference = -2 * math.pi * 34636.74
    root = SummingPointRoot(
        vertex="n1p.i",
        value=complex(reference),
        frequency_hz=34636.74,
        path_edges=((), ()),
        cluster_index=2,
        observable=True,
        dominant=True,
        root_source="closed-loop pole localized at summing point",
    )

    result = derive_local_summing_root_expression(graph, root, VALUES)

    assert result is not None
    assert result.method == "dominant local-loop approximation"
    assert len(result.loop_gains) == 1
    assert sp.simplify(result.expression - expected) == 0
    assert result.relative_frequency_error is not None
    assert result.relative_frequency_error < 0.5
    _assert_term_pruning_is_bounded(result)

    robust = derive_local_summing_root_expression(
        graph,
        root,
        VALUES,
        default_parameter_tolerance=0.1,
        max_corner_parameters=4,
    )
    assert robust is not None
    assert robust.method == "local Mason characteristic"
    assert robust.corner_max_relative_error == 0.0
    assert {name for name, _value in robust.parameter_sensitivities} >= {"gm", "gx"}
    _assert_term_pruning_is_bounded(robust)
    if robust.simplified_corner_max_relative_error is not None:
        assert robust.simplified_corner_max_relative_error <= 0.05 + 1e-12


def test_target_root_module_unifies_spr_and_open_loop_zero_expressions() -> None:
    graph = _graph("n1p.i", "n1p.v", "n2.i", "n2.v", "z.v", "z.i")
    _add_edge(graph, "n1p.i", "n1p.v", "impedance", 1 / (gx + gpi + s * (cmu + cpi)))
    _add_edge(graph, "n1p.v", "n2.i", "admittance", -gm)
    _add_edge(graph, "n2.i", "n2.v", "impedance", 1 / (s * (cmu + cx + Cl)))
    _add_edge(graph, "n2.v", "n1p.i", "admittance", s * cmu)
    _add_edge(graph, "z.v", "z.i", "admittance", s * cmu - gm)
    pole_expression = -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
        (cmu + cpi) * (cmu + cx + Cl)
    )
    pole_value = complex(sp.N(pole_expression.subs(VALUES)))
    zero_expression = gm / cmu
    zero_value = complex(sp.N(zero_expression.subs(VALUES)))
    clusters = (
        RootCluster(1, (RootSample("pole", pole_value),), abs(pole_value), abs(pole_value)),
        RootCluster(2, (RootSample("zero", zero_value),), abs(zero_value), abs(zero_value)),
    )
    summing_root = _root(1, "n1p.i", pole_expression)
    localization = RootLocalization(
        source="z.v",
        target="z.i",
        domain="admittance",
        root_kind="zero",
        root_value=zero_value,
        root_frequency_hz=abs(zero_value) / (2 * math.pi),
        cluster_index=2,
        frequency_error_ratio=0.0,
        observable=True,
        on_forward_path=True,
        category="OLR-O",
    )

    results = derive_target_root_symbolic_approximations(
        graph,
        clusters,
        (localization,),
        (summing_root,),
        substitutions=VALUES,
        max_relative_root_error=1e-10,
        enforce_relative_root_error=True,
    )

    assert len(results) == 2
    assert results[0].status == "resolved"
    assert results[0].category == "SPR"
    assert sp.simplify(results[0].expression - pole_expression) == 0
    assert results[1].status == "resolved"
    assert results[1].category == "OLR-O"
    assert sp.simplify(results[1].expression - zero_expression) == 0


def test_paper_mode_reports_root_shift_without_rejecting_the_symbolic_root() -> None:
    graph = _graph("z.v", "z.i")
    _add_edge(graph, "z.v", "z.i", "admittance", 1 / (s + 100))
    reference_root = complex(-70.0)
    cluster = RootCluster(
        1,
        (RootSample("pole", reference_root),),
        abs(reference_root),
        abs(reference_root),
    )
    localization = RootLocalization(
        source="z.v",
        target="z.i",
        domain="admittance",
        root_kind="pole",
        root_value=complex(-100.0),
        root_frequency_hz=100.0 / (2 * math.pi),
        cluster_index=1,
        frequency_error_ratio=30.0 / 70.0,
        observable=True,
        on_forward_path=True,
        category="OLR-O",
    )

    paper_mode = derive_target_root_symbolic_approximations(
        graph,
        (cluster,),
        (localization,),
        (),
        max_relative_root_error=0.05,
    )
    location_control = derive_target_root_symbolic_approximations(
        graph,
        (cluster,),
        (localization,),
        (),
        max_relative_root_error=0.05,
        enforce_relative_root_error=True,
    )

    assert paper_mode[0].status == "resolved"
    assert math.isclose(paper_mode[0].relative_root_error, 30.0 / 70.0)
    assert location_control[0].status == "outside_error_limit"
