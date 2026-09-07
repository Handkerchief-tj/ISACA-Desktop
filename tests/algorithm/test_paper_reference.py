"""Golden tests for the paper's single-transistor numerical reference."""

import math
from pathlib import Path

import pytest
import sympy as sp

import sfg_prototype.pipeline as pipeline_module
from sfg_prototype import (
    SimplificationConfig,
    compute_numeric_descriptor_reference,
    compute_numeric_reference,
    run_reference_pipeline,
    sample_descriptor_frequency_response,
    sample_frequency_response,
    simplify_netlist,
    validate_graph_equivalence,
)


FIXTURE = Path(__file__).parent / "fixtures" / "paper_single_transistor.cir"
RC_FIXTURE = Path(__file__).parent / "fixtures" / "rc_lowpass_numeric.cir"


def _nearest_root_error(actual: tuple[complex, ...], expected: tuple[complex, ...]) -> float:
    """Return the worst greedy relative error between two small root sets."""
    remaining = list(actual)
    errors = []
    for target in expected:
        index = min(range(len(remaining)), key=lambda item: abs(remaining[item] - target))
        value = remaining.pop(index)
        errors.append(abs(value - target) / max(abs(target), 1.0))
    return max(errors, default=0.0)


def test_paper_closed_loop_roots_and_formula_boundaries() -> None:
    result = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        frequency_range_hz=(10.0, 1.0e11),
        enable_subrange_transfers=False,
    )
    poles_hz = sorted((root.real / (2 * math.pi) for root in result.reference.poles), key=abs)
    zeros_hz = sorted((root.real / (2 * math.pi) for root in result.reference.zeros), key=abs)

    expected = (-334.0, -34.6e3, 6.34e6, -1.52e9, -13.6e9)
    actual = (poles_hz[0], poles_hz[1], zeros_hz[0], zeros_hz[1], poles_hz[2])
    for measured, target in zip(actual, expected):
        assert abs(abs(measured) - abs(target)) / abs(target) < 0.01

    assert len(result.clusters) == 4
    boundaries = [spec.upper_frequency_hz for spec in result.error_specs[:-1]]
    assert boundaries == pytest.approx((3399.85, 468596.7, 98.23256e6), rel=2e-4)

    _performance, baseline_error = validate_graph_equivalence(
        result.graph,
        result,
        frequencies=(10.0, *boundaries, 1.0e11),
        substitutions=result.network.numeric_substitutions(),
    )
    assert baseline_error.max_relative_error < 1e-12


def test_numeric_sfg_reference_matches_slicap_roots() -> None:
    symbolic = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        enable_subrange_transfers=False,
    )
    numeric = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        enable_subrange_transfers=False,
        reference_mode="numeric_sfg",
    )

    assert numeric.reference.method == "numeric-coefficient-sfg"
    assert _nearest_root_error(numeric.reference.poles, symbolic.reference.poles) < 1e-10
    assert _nearest_root_error(numeric.reference.zeros, symbolic.reference.zeros) < 1e-10


def test_numeric_descriptor_reference_matches_slicap_without_determinant_expansion() -> None:
    symbolic = compute_numeric_reference(FIXTURE)
    descriptor = compute_numeric_descriptor_reference(FIXTURE)

    assert descriptor.method == "numeric-mna-descriptor"
    assert descriptor.conductance_matrix.shape == (8, 8)
    assert descriptor.dynamic_matrix.shape == (8, 8)
    assert descriptor.input_vector.shape == (8, 1)
    assert descriptor.detector_vector.shape == (1, 8)
    assert descriptor.cancelled_roots == ()
    assert _nearest_root_error(descriptor.poles, symbolic.poles) < 1e-10
    assert _nearest_root_error(descriptor.zeros, symbolic.zeros) < 1e-10

    frequencies = (10.0, 1.0e3, 1.0e6, 1.0e10)
    symbolic_samples = sample_frequency_response(symbolic.laplace, frequencies)
    descriptor_samples = sample_descriptor_frequency_response(descriptor, frequencies)
    for actual, expected in zip(descriptor_samples, symbolic_samples):
        error = abs(actual.value - expected.value) / max(abs(expected.value), 1e-30)
        assert error < 1e-10


def test_numeric_descriptor_reference_handles_first_order_rc() -> None:
    descriptor = compute_numeric_descriptor_reference(RC_FIXTURE)

    assert descriptor.poles == pytest.approx((-1000.0 + 0.0j,), rel=1e-12)
    assert descriptor.zeros == ()
    assert descriptor.raw_finite_modes == descriptor.poles


def test_descriptor_reference_mode_drives_root_clustering() -> None:
    symbolic = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        enable_subrange_transfers=False,
    )
    descriptor = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        enable_subrange_transfers=False,
        reference_mode="descriptor_mna",
    )

    assert descriptor.reference.method == "numeric-mna-descriptor"
    assert _nearest_root_error(descriptor.reference.poles, symbolic.reference.poles) < 1e-10
    assert _nearest_root_error(descriptor.reference.zeros, symbolic.reference.zeros) < 1e-10
    assert [cluster.center_frequency_hz for cluster in descriptor.clusters] == pytest.approx(
        [cluster.center_frequency_hz for cluster in symbolic.clusters],
        rel=1e-10,
    )


def test_descriptor_reference_mode_supports_end_to_end_error_checks() -> None:
    result = simplify_netlist(
        RC_FIXTURE,
        config=SimplificationConfig(
            reference_mode="descriptor_mna",
            enable_transfer_term_pruning=False,
            points_per_subrange=4,
            max_steps=1,
            max_steps_per_subrange=1,
            max_candidates_per_round=4,
        ),
    )

    assert result.pipeline.reference.method == "numeric-mna-descriptor"
    assert result.baseline_error.max_relative_error < 1e-12
    assert result.final_error.accepted
    target_roots = result.subrange_results[0].target_root_approximations
    assert len(target_roots) == 1
    assert target_roots[0].kind == "pole"
    assert target_roots[0].status == "resolved"
    assert target_roots[0].relative_root_error is not None
    assert target_roots[0].relative_root_error < 1e-12


def test_paper_closed_loop_roots_are_classified_as_olr_or_spr() -> None:
    result = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        frequency_range_hz=(10.0, 1.0e11),
        enable_subrange_transfers=False,
    )

    observable_olr = {
        (item.cluster_index, item.root_kind, item.source, item.target)
        for item in result.localizations
        if item.observable
    }
    assert (3, "zero", "1_Q1.v", "net2.i") in observable_olr
    assert (4, "pole", "net1.i", "net1.v") in observable_olr
    hidden_olr = {
        (item.cluster_index, item.root_kind, item.source, item.target)
        for item in result.localizations
        if not item.observable
    }
    assert (1, "pole", "net2.i", "net2.v") in hidden_olr
    assert (2, "pole", "1_Q1.i", "1_Q1.v") in hidden_olr

    dominant_spr = {
        (item.cluster_index, item.vertex)
        for item in result.summing_point_roots
        if item.observable and item.dominant
    }
    assert dominant_spr == {(1, "1_Q1.i"), (2, "1_Q1.i"), (4, "net2.i")}


def test_paper_demo_resolves_all_target_roots_and_recovers_equation_24() -> None:
    result = simplify_netlist(
        FIXTURE,
        config=SimplificationConfig(
            cluster_tolerance=0.9,
            total_error_budget=0.05,
            magnitude_error_db=2.0,
            phase_error_deg=5.0,
            frequency_range_hz=(10.0, 1.0e11),
            points_per_subrange=4,
            max_steps=0,
            max_steps_per_subrange=0,
            max_candidates_per_round=0,
            enable_transfer_term_pruning=False,
            enable_symbolic_spr_expressions=True,
            # With graph operations disabled, explicitly exercise the optional
            # diagnostic dominant-term view rather than the default paper path.
            target_root_use_frequency_reduction=True,
        ),
    )

    target_roots = tuple(
        root
        for subrange in result.subrange_results
        for root in subrange.target_root_approximations
    )
    assert len(target_roots) == 5
    assert all(root.status == "resolved" for root in target_roots)
    first_pole = next(
        root
        for root in target_roots
        if root.cluster_index == 1 and root.kind == "pole"
    )
    assert sp.count_ops(first_pole.expression) < 30
    assert "(Cl + cmu + cx)" in sp.sstr(first_pole.expression)
    second_pole = next(
        root
        for root in target_roots
        if root.cluster_index == 2 and root.kind == "pole"
    )
    gm, gpi, gx, cmu, cpi, cx, Cl = sp.symbols("gm gpi gx cmu cpi cx Cl")
    equation_22 = -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
        (cmu + cpi) * (cmu + cx + Cl)
    )
    assert sp.simplify(second_pole.expression - equation_22) == 0
    assert second_pole.relative_root_error is not None
    assert second_pole.relative_root_error > 0.3
    high_zero = next(
        root
        for root in target_roots
        if root.kind == "zero" and root.reference_value.real < 0
    )
    expected = -gx * cmu / (cx * (cmu + cpi))
    assert high_zero.category == "SPR"
    assert high_zero.method == "local Mason numerator"
    assert sp.simplify(high_zero.expression - expected) == 0
    assert high_zero.relative_root_error is not None
    assert high_zero.relative_root_error < 0.05
    assert all(subrange.error.accepted for subrange in result.subrange_results)


def test_per_run_analysis_cache_reuses_identical_graph_work(monkeypatch) -> None:
    result = run_reference_pipeline(
        FIXTURE,
        cluster_tolerance=0.9,
        total_error_budget=0.05,
        frequency_range_hz=(10.0, 1.0e11),
        enable_subrange_transfers=False,
    )
    config = SimplificationConfig(frequency_range_hz=(10.0, 1.0e11))
    substitutions = result.network.numeric_substitutions()
    cache = pipeline_module._ManipulationAnalysisCache()
    calls = {"candidates": 0, "topology": 0, "performance": 0, "symbolic": 0}

    original_candidates = pipeline_module.analyze_meta_edge_candidates
    original_topology = pipeline_module.analyze_paper_topology
    original_performance = pipeline_module.evaluate_graph_performance
    original_symbolic = pipeline_module.solve_subrange_symbolic_transfer

    def counted_candidates(*args, **kwargs):
        calls["candidates"] += 1
        return original_candidates(*args, **kwargs)

    def counted_topology(*args, **kwargs):
        calls["topology"] += 1
        return original_topology(*args, **kwargs)

    def counted_performance(*args, **kwargs):
        calls["performance"] += 1
        return original_performance(*args, **kwargs)

    def counted_symbolic(*args, **kwargs):
        calls["symbolic"] += 1
        return original_symbolic(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "analyze_meta_edge_candidates", counted_candidates)
    monkeypatch.setattr(pipeline_module, "analyze_paper_topology", counted_topology)
    monkeypatch.setattr(pipeline_module, "evaluate_graph_performance", counted_performance)
    monkeypatch.setattr(pipeline_module, "solve_subrange_symbolic_transfer", counted_symbolic)

    first_analysis = pipeline_module._cached_complexity_analysis(
        cache,
        result.graph,
        result,
        config,
        substitutions,
    )
    second_analysis = pipeline_module._cached_complexity_analysis(
        cache,
        result.graph,
        result,
        config,
        substitutions,
    )
    first_performance = pipeline_module._cached_numeric_graph_performance(
        cache,
        result.graph,
        result,
        frequencies=(10.0, 1.0e3),
        substitutions=substitutions,
    )
    second_performance = pipeline_module._cached_numeric_graph_performance(
        cache,
        result.graph,
        result,
        frequencies=(10.0, 1.0e3),
        substitutions=substitutions,
    )
    first_symbolic = pipeline_module._cached_subrange_symbolic_transfer(
        cache,
        result.graph,
        result.error_specs[0],
        source=result.reference.source,
        detector=result.reference.detector,
        substitutions=substitutions,
        relative_threshold=0.0,
        edge_relative_threshold=0.0,
        max_symbolic_root_degree=2,
    )
    second_symbolic = pipeline_module._cached_subrange_symbolic_transfer(
        cache,
        result.graph,
        result.error_specs[1],
        source=result.reference.source,
        detector=result.reference.detector,
        substitutions=substitutions,
        relative_threshold=0.0,
        edge_relative_threshold=0.0,
        max_symbolic_root_degree=2,
    )

    assert first_analysis is second_analysis
    assert first_performance is second_performance
    assert first_symbolic.cluster_index != second_symbolic.cluster_index
    assert first_symbolic.transfer == second_symbolic.transfer
    assert calls == {"candidates": 1, "topology": 1, "performance": 1, "symbolic": 1}
