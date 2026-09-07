"""Regression tests for the paper's Eq. (6), (7), (11), and (12) policy."""

import cmath
import math
from types import SimpleNamespace

import pytest
import sympy as sp

from sfg_prototype import (
    GraphPerformance,
    PerformanceSpecification,
    SignalFlowGraph,
    SimplificationConfig,
    cluster_roots,
    compare_graph_performance,
    error_policy_report,
    split_error_specification,
)
from sfg_prototype.analysis import FrequencySample, root_observability_check
from sfg_prototype.pipeline import (
    GraphPerformanceSample,
    _best_readable_rational_factorization,
    _paper_ranking_value,
    _use_exhaustive_paper_candidates,
)
from sfg_prototype.pipeline import _frequency_grid


def test_presimplification_scales_every_active_error_boundary() -> None:
    user_spec = PerformanceSpecification(
        error_norm="bode_linf",
        relative_error_limit=0.05,
        magnitude_error_db=2.0,
        phase_error_deg=5.0,
    )

    pre_spec = user_spec.scaled(1.0e-4)

    assert pre_spec.relative_error_limit == pytest.approx(5.0e-6)
    assert pre_spec.magnitude_error_db == pytest.approx(2.0e-4)
    assert pre_spec.phase_error_deg == pytest.approx(5.0e-4)


def test_bode_infinity_norm_uses_user_boundary_function() -> None:
    value = 10 ** (1.0 / 20.0) * cmath.exp(1j * math.radians(2.5))
    performance = GraphPerformance(
        transfer=sp.nan,
        source="Vin",
        detector="Vout",
        detector_vertex="out.v",
        samples=(GraphPerformanceSample(frequency_hz=1.0e3, value=value),),
    )
    reference = (FrequencySample(frequency_hz=1.0e3, value=1.0 + 0.0j),)

    error = compare_graph_performance(
        performance,
        reference,
        error_budget=0.05,
        error_norm="bode_linf",
        magnitude_error_db=2.0,
        phase_error_deg=5.0,
    )

    assert error.accepted
    assert error.max_magnitude_error_db == pytest.approx(1.0)
    assert error.max_phase_error_deg == pytest.approx(2.5)
    assert error.norm_error == pytest.approx(0.5)
    assert error.worst_magnitude_frequency_hz == pytest.approx(1.0e3)
    assert error.worst_phase_frequency_hz == pytest.approx(1.0e3)


def test_root_subranges_keep_the_complete_user_error_boundary() -> None:
    clusters = cluster_roots(
        poles=(-2j * math.pi * 1.0e3, -2j * math.pi * 1.0e6),
        zeros=(),
        relative_tolerance=0.1,
    )

    specs = split_error_specification(
        clusters,
        total_error_budget=0.05,
        frequency_range_hz=(10.0, 1.0e9),
    )

    assert len(specs) == 2
    assert [spec.error_budget for spec in specs] == pytest.approx([0.05, 0.05])
    assert specs[0].upper_frequency_hz == pytest.approx(math.sqrt(1.0e3 * 1.0e6))
    assert specs[1].lower_frequency_hz == pytest.approx(math.sqrt(1.0e3 * 1.0e6))


def test_root_clustering_does_not_chain_distant_endpoints() -> None:
    """Every root in a paper cluster must remain within epsilon of its endpoints."""
    clusters = cluster_roots(
        poles=(-100.0, -180.0, -324.0),
        zeros=(),
        relative_tolerance=0.5,
    )

    assert [len(cluster.roots) for cluster in clusters] == [2, 1]
    assert clusters[0].min_magnitude == pytest.approx(100.0)
    assert clusters[0].max_magnitude == pytest.approx(180.0)
    assert clusters[1].min_magnitude == pytest.approx(324.0)


def test_paper_mode_does_not_apply_hidden_target_root_reduction() -> None:
    """Default root interpretation must operate on the accepted G_j graph."""
    config = SimplificationConfig()

    assert config.target_root_use_frequency_reduction is False
    assert config.root_feedback_loop_gain_threshold == pytest.approx(1.0)


def test_observability_thresholds_reject_invalid_values() -> None:
    graph = SignalFlowGraph(title="observability", file_path=None)

    with pytest.raises(ValueError, match="dominance_ratio"):
        root_observability_check(graph, (), None, dominance_ratio=-1.0)
    with pytest.raises(ValueError, match="feedback_interaction_threshold"):
        root_observability_check(
            graph,
            (),
            None,
            feedback_interaction_threshold=-1.0,
        )


def test_paper_candidate_generation_is_exhaustive_only_for_bounded_graphs() -> None:
    graph = SignalFlowGraph(title="candidate-mode", file_path=None)
    graph.meta_edges = {(str(index), "target", "current"): None for index in range(3)}

    assert _use_exhaustive_paper_candidates(
        graph,
        SimplificationConfig(
            candidate_generation_mode="auto",
            exhaustive_candidate_edge_limit=3,
        ),
    )
    assert not _use_exhaustive_paper_candidates(
        graph,
        SimplificationConfig(
            candidate_generation_mode="auto",
            exhaustive_candidate_edge_limit=2,
        ),
    )
    assert _use_exhaustive_paper_candidates(
        graph,
        SimplificationConfig(candidate_generation_mode="exhaustive"),
    )
    assert not _use_exhaustive_paper_candidates(
        graph,
        SimplificationConfig(candidate_generation_mode="bounded"),
    )


def test_paper_candidate_generation_rejects_invalid_configuration() -> None:
    graph = SignalFlowGraph(title="candidate-mode", file_path=None)

    with pytest.raises(ValueError, match="candidate_generation_mode"):
        _use_exhaustive_paper_candidates(
            graph,
            SimplificationConfig(candidate_generation_mode="unknown"),
        )
    with pytest.raises(ValueError, match="non-negative"):
        _use_exhaustive_paper_candidates(
            graph,
            SimplificationConfig(exhaustive_candidate_edge_limit=-1),
        )


def test_error_policy_report_discloses_engineering_candidate_thresholds() -> None:
    result = SimpleNamespace(
        config=SimplificationConfig(),
        pipeline=SimpleNamespace(error_specs=()),
    )

    report = error_policy_report(result)

    assert "candidate generation mode: `auto`" in report
    assert "bounded-mode RR partition threshold" in report
    assert "feedback loop-gain threshold: `1.000000e+00`" in report
    assert "root-location deviation is reported only" in report


def test_rational_readability_compacts_repeated_physical_parameter_groups() -> None:
    Gin, gx, gpi, Gl, go, Cl, cmu, cx, gm = sp.symbols(
        "Gin gx gpi Gl go Cl cmu cx gm"
    )
    shared_conductance = Gin * gpi + Gin * gx + gpi * gx
    expanded = -(
        (Gl + go)
        * shared_conductance
        / sp.expand(
            (Cl + cmu + cx) * shared_conductance
            + cmu * gm * (Gin + gx)
        )
    )

    readable = _best_readable_rational_factorization(expanded)

    assert sp.simplify(readable - expanded) == 0
    assert sp.count_ops(readable) < sp.count_ops(expanded)
    assert "(Cl + cmu + cx)" in sp.sstr(readable)
    assert "(Gin + gx)" in sp.sstr(readable)


def test_quality_concerned_ranking_prefers_larger_complexity_reduction() -> None:
    low_quality = _paper_ranking_value(error=1.0e-9, quality=1.0, alpha=0.0)
    high_quality = _paper_ranking_value(error=1.0, quality=4.0, alpha=0.0)

    assert high_quality < low_quality


def test_frequency_grid_can_make_paper_sampling_boundaries_explicit() -> None:
    class Reference:
        poles = (-2j * math.pi * 1.0e3,)
        zeros = ()

    clusters = cluster_roots(Reference.poles, Reference.zeros, relative_tolerance=0.1)
    spec = split_error_specification(clusters, frequency_range_hz=(10.0, 1.0e6))[0]
    closed = _frequency_grid((spec,), Reference, 4, (10.0, 1.0e6), include_boundaries=True)
    interior = _frequency_grid((spec,), Reference, 4, (10.0, 1.0e6), include_boundaries=False)

    assert closed[0] == pytest.approx(10.0)
    assert closed[-1] == pytest.approx(1.0e6)
    assert interior[0] > 10.0
    assert interior[-1] < 1.0e6
