"""Tests for error-controlled dominant-product-term transfer simplification."""

import sympy as sp

from sfg_prototype import (
    PerformanceSpecification,
    SubrangeSymbolicTransfer,
    simplify_symbolic_transfer_terms,
)


def test_transfer_term_pruning_removes_only_frequency_invisible_term() -> None:
    s = sp.Symbol("s")
    epsilon = sp.Symbol("epsilon")
    transfer = (1 + epsilon * s) / (1 + s)
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1 + epsilon * s,
        denominator=1 + s,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={epsilon: 1e-9},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
        parameter_tolerances={epsilon: 0.5},
        max_corner_parameters=1,
    )

    assert result is not None
    assert sp.simplify(result.transfer - 1 / (1 + s)) == 0
    assert result.retained_term_count == 3
    assert result.full_term_count == 4
    assert result.nominal_error.accepted
    assert result.corner_count == 2
    assert result.corner_max_norm_error is not None
    assert result.corner_max_norm_error <= 1.0
    assert result.discarded_parameters == ("epsilon",)


def test_transfer_term_pruning_ranks_retained_parameter_influence() -> None:
    s = sp.Symbol("s")
    gain, tau, epsilon = sp.symbols("gain tau epsilon")
    transfer = (gain + epsilon * s) / (1 + tau * s)
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=gain + epsilon * s,
        denominator=1 + tau * s,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={gain: 2.0, tau: 0.01, epsilon: 1e-9},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
    )

    assert result is not None
    assert sp.simplify(result.transfer - gain / (1 + tau * s)) == 0
    assert result.discarded_parameters == ("epsilon",)
    assert tuple(item.parameter for item in result.parameter_influences) == (
        "gain",
        "tau",
    )
    assert result.parameter_influences[0].max_normalized_sensitivity == 1.0
    assert 0.0 < result.parameter_influences[1].max_normalized_sensitivity < 1.0


def test_transfer_term_pruning_can_remove_same_order_terms_jointly() -> None:
    s = sp.Symbol("s")
    a, b = sp.symbols("a b")
    transfer = (1 + a * s - b * s) / (1 + s)
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1 + a * s - b * s,
        denominator=1 + s,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={a: 1.0, b: 1.0},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
    )

    assert result is not None
    assert sp.simplify(result.transfer - 1 / (1 + s)) == 0
    assert result.pruning_steps[0].joint
    assert result.pruning_steps[0].side == "numerator"
    assert result.pruning_steps[0].laplace_order == 1
    assert set(result.pruning_steps[0].removed_terms) == {a * s, -b * s}
    assert result.discarded_parameters == ("a", "b")


def test_transfer_joint_removal_respects_parameter_corners() -> None:
    s = sp.Symbol("s")
    a, b = sp.symbols("a b")
    transfer = (1 + a * s - b * s) / (1 + s)
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1 + a * s - b * s,
        denominator=1 + s,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={a: 1.0, b: 1.0},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
        default_parameter_tolerance=0.1,
        max_corner_parameters=2,
    )

    assert result is not None
    assert result.representation == "factored-equivalent"
    assert result.pruning_steps == ()
    assert result.discarded_parameters == ()
    assert sp.simplify(result.transfer - transfer) == 0
    assert result.corner_count == 4
    assert result.corner_max_norm_error is not None
    assert result.corner_max_norm_error <= 1.0


def test_transfer_joint_removal_keeps_dominant_term_of_same_order() -> None:
    s = sp.Symbol("s")
    dominant, e1, e2, e3 = sp.symbols("dominant e1 e2 e3")
    transfer = (1 + dominant * s + e1 * s + e2 * s + e3 * s) / (1 + s)
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1 + dominant * s + e1 * s + e2 * s + e3 * s,
        denominator=1 + s,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={dominant: 0.01, e1: 1e-9, e2: 2e-9, e3: 3e-9},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
        max_steps=1,
        max_joint_size=4,
    )

    assert result is not None
    assert sp.simplify(result.transfer - (1 + dominant * s) / (1 + s)) == 0
    assert result.pruning_steps[0].joint
    assert set(result.pruning_steps[0].removed_terms) == {e1 * s, e2 * s, e3 * s}
    assert dominant in result.transfer.free_symbols


def test_transfer_readability_factors_repeated_parameter_groups_without_pruning() -> None:
    s = sp.Symbol("s")
    cl, cmu, cx, gm, gpi, gx = sp.symbols("cl cmu cx gm gpi gx")
    denominator = (
        cl * gpi
        + cl * gx
        + cmu * gm
        + cmu * gpi
        + cmu * gx
        + cx * gpi
        + cx * gx
    )
    transfer = 1 / denominator
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1,
        denominator=denominator,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={cl: 1, cmu: 2, cx: 3, gm: 4, gpi: 5, gx: 6},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
    )

    assert result is not None
    assert result.representation == "factored-equivalent"
    assert sp.simplify(result.transfer - transfer) == 0
    assert result.denominator.has(gpi + gx)
    assert result.denominator.has(cl + cmu + cx)
    assert result.retained_term_count == result.full_term_count
    assert result.readability_operations_after < result.readability_operations_before


def test_transfer_readability_handles_polynomial_above_pruning_term_limit() -> None:
    s = sp.Symbol("s")
    a, b, c, d, e, f = sp.symbols("a b c d e f")
    denominator = sp.expand((a + b) * (c + d) * s**2 + (a + b) * (e + f) * s + 1)
    transfer = 1 / denominator
    exact = SubrangeSymbolicTransfer(
        cluster_index=1,
        lower_frequency_hz=1.0,
        upper_frequency_hz=10.0,
        representative_frequency_hz=sp.sqrt(10),
        transfer=transfer,
        numerator=1,
        denominator=denominator,
        poles=(),
        zeros=(),
        vertex_count=2,
        edge_count=1,
    )

    result = simplify_symbolic_transfer_terms(
        exact,
        frequencies=(1.0, 3.0, 10.0),
        reference_expression=transfer,
        substitutions={a: 1, b: 2, c: 3, d: 4, e: 5, f: 6},
        performance_specification=PerformanceSpecification(
            magnitude_error_db=0.01,
            phase_error_deg=0.1,
        ),
        max_terms=3,
    )

    assert result is not None
    assert result.representation == "factored-equivalent"
    assert result.pruning_steps == ()
    assert sp.simplify(result.transfer - transfer) == 0
    assert result.denominator.has(a + b)
    assert result.readability_operations_after < result.readability_operations_before
