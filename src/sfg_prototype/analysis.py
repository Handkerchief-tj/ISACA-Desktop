#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analysis helpers for the SFG-based simplification pipeline.

This module deliberately reuses SLiCAP's native MNA solver for the reference
transfer function, poles and zeros. The SFG code is used for the paper-specific
graph stages: root clustering, meta-edge/root localization, and later
error-controlled simplification.
"""

from __future__ import annotations

import math
import os
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import combinations, product
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import sympy as sp
from scipy.linalg import eig

from .slicap_compat import (
    check_circuit as _checkCircuit,
    ini,
    initialize_parser as _initializeParser,
    instruction_type as instruction,
    make_all_matrices as _makeAllMatrices,
    update_circuit_data as _updateCirData,
    yacc_module as _yacc_module,
)
from .network import LinearNetwork, evaluate_numeric_expression, format_numeric_expression, flatten_netlist
from .sfg import MetaEdge, SignalFlowGraph, build_signal_flow_graph


@dataclass(frozen=True)
class NumericReferenceResult:
    """Reference transfer function and numerical poles/zeros."""

    title: str | None
    file_path: str
    source: str
    detector: str
    laplace: sp.Expr
    numerator: sp.Expr
    denominator: sp.Expr
    poles: tuple[complex, ...]
    zeros: tuple[complex, ...]
    dc_value: complex | None
    method: str = "slicap-symbolic-mna"


@dataclass(frozen=True)
class DescriptorMatrixResult:
    """Numerical MNA descriptor pencil and its finite transfer roots."""

    title: str | None
    file_path: str
    source: str
    detector: str
    dependent_variables: tuple[str, ...]
    conductance_matrix: np.ndarray
    dynamic_matrix: np.ndarray
    input_vector: np.ndarray
    detector_vector: np.ndarray
    poles: tuple[complex, ...]
    zeros: tuple[complex, ...]
    cancelled_roots: tuple[complex, ...]
    raw_finite_modes: tuple[complex, ...]
    raw_finite_zeros: tuple[complex, ...]
    infinite_mode_count: int
    infinite_zero_count: int
    method: str = "numeric-mna-descriptor"


@dataclass(frozen=True)
class FrequencySample:
    """Reference transfer-function value at one frequency point."""

    frequency_hz: float
    value: complex

    @property
    def magnitude(self) -> float:
        """Return the magnitude at this frequency point."""
        return abs(self.value)

    @property
    def phase_deg(self) -> float:
        """Return the phase in degrees at this frequency point."""
        return float(np.angle(self.value, deg=True))


@dataclass(frozen=True)
class RootSample:
    """One closed-loop pole or zero used by the clustering stage."""

    kind: str
    value: complex

    @property
    def magnitude(self) -> float:
        """Return the root magnitude in rad/s."""
        return abs(self.value)

    @property
    def frequency_hz(self) -> float:
        """Return the root magnitude converted to Hz."""
        return abs(self.value) / (2 * np.pi)


@dataclass(frozen=True)
class RootCluster:
    """A group of numerically nearby poles/zeros."""

    index: int
    roots: tuple[RootSample, ...]
    min_magnitude: float
    max_magnitude: float

    @property
    def center_magnitude(self) -> float:
        """Return the geometric center of the cluster in rad/s."""
        if self.min_magnitude == 0 or self.max_magnitude == 0:
            return 0.0
        return float(np.sqrt(self.min_magnitude * self.max_magnitude))

    @property
    def center_frequency_hz(self) -> float:
        """Return the geometric center of the cluster in Hz."""
        return self.center_magnitude / (2 * np.pi)

    @property
    def pole_count(self) -> int:
        """Return the number of poles in this cluster."""
        return sum(1 for root in self.roots if root.kind == "pole")

    @property
    def zero_count(self) -> int:
        """Return the number of zeros in this cluster."""
        return sum(1 for root in self.roots if root.kind == "zero")


@dataclass(frozen=True)
class ErrorSubSpecification:
    """Full user error boundary restricted to one root-cluster frequency range."""

    cluster_index: int
    error_budget: float
    lower_frequency_hz: float
    upper_frequency_hz: float
    root_count: int
    raw_lower_frequency_hz: float | None = None
    raw_upper_frequency_hz: float | None = None
    user_lower_frequency_hz: float | None = None
    user_upper_frequency_hz: float | None = None


@dataclass(frozen=True)
class CandidateRoot:
    """A root contributed by one SFG meta-edge expression."""

    kind: str
    value: complex
    frequency_hz: float


@dataclass(frozen=True)
class MetaEdgeCandidate:
    """A meta-edge candidate for later ranking and simplification."""

    source: str
    target: str
    domain: str
    symbolic_value: sp.Expr
    numeric_value: sp.Expr
    order_partitions: dict[int | None, sp.Expr]
    roots: tuple[CandidateRoot, ...]
    contribution_count: int
    dynamic_order_span: int
    structural_score: float


@dataclass(frozen=True)
class GraphTopology:
    """Reachability information used by the paper's topology analysis stage."""

    source_ref: str
    source_vertex: str
    detector: str
    detector_vertex: str
    reachable_from_source: frozenset[str]
    can_reach_detector: frozenset[str]
    observable_edges: frozenset[tuple[str, str, str]]


@dataclass(frozen=True)
class SignalPath:
    """One source-to-detector forward path in the SFG."""

    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class FeedbackLoop:
    """One directed feedback loop in the SFG."""

    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class GraphCut:
    """A valid graph cut candidate from paper Section IV-B."""

    start_vertex: str
    end_vertex: str
    length: int
    vertices: frozenset[str]
    edges: frozenset[tuple[str, str, str]]
    cut_kind: str = "complementary"


@dataclass(frozen=True)
class SummingVertex:
    """One vertex where multiple incoming SFG branches are summed."""

    vertex: str
    incoming_edges: tuple[tuple[str, str, str], ...]

    @property
    def input_count(self) -> int:
        """Return the number of incoming terms."""
        return len(self.incoming_edges)


@dataclass(frozen=True)
class PaperTopologyMetrics:
    """Topology objects used by paper Fig.12 steps 7-9."""

    source_ref: str
    source_vertex: str
    detector: str
    detector_vertex: str
    forward_paths: tuple[SignalPath, ...]
    feedback_loops: tuple[FeedbackLoop, ...]
    graph_cuts: tuple[GraphCut, ...]
    summing_vertices: tuple[SummingVertex, ...]
    observable_edges: frozenset[tuple[str, str, str]]
    candidate_root_edges: frozenset[tuple[str, str, str]]


@dataclass(frozen=True)
class PaperComplexity:
    """Paper Eq.(9)/(10) graph-complexity terms."""

    open_loop_roots: int
    summing_points: int
    forward_paths: int
    feedback_loops: int
    use_simplified: bool = False

    @property
    def full(self) -> int:
        """Return C(N) from paper Eq.(9)."""
        return self.open_loop_roots + self.summing_points + self.forward_paths + self.feedback_loops

    @property
    def simplified(self) -> int:
        """Return Cs(N) from paper Eq.(10)."""
        return self.open_loop_roots + self.summing_points

    @property
    def value(self) -> int:
        """Return the active complexity value."""
        return self.simplified if self.use_simplified else self.full


@dataclass(frozen=True)
class SFGSubgraph:
    """A root-cluster subgraph G_j used by paper Fig.12 step 9.1."""

    cluster_index: int
    lower_frequency_hz: float
    upper_frequency_hz: float
    vertices: frozenset[str]
    edges: frozenset[tuple[str, str, str]]
    candidate_edges: frozenset[tuple[str, str, str]]


@dataclass(frozen=True)
class SummingPointRoot:
    """A candidate root generated by intersecting signals at a summing vertex."""

    vertex: str
    value: complex
    frequency_hz: float
    path_edges: tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]
    expression: sp.Expr | None = None
    crossing_equation: sp.Expr | None = None
    cluster_index: int | None = None
    frequency_error_ratio: float | None = None
    observable: bool = False
    dominant: bool = False
    root_source: str = "summing-point root candidate"
    category: str = "SPR"


@dataclass(frozen=True)
class RootLocalization:
    """Maps one candidate edge root to a closed-loop root cluster."""

    source: str
    target: str
    domain: str
    root_kind: str
    root_value: complex
    root_frequency_hz: float
    cluster_index: int | None
    frequency_error_ratio: float | None
    observable: bool
    on_forward_path: bool
    root_source: str = "open-loop meta-edge root"
    category: str = "OLR-NO"


@dataclass(frozen=True)
class SimplificationRanking:
    """First-pass ranking information for one meta-edge candidate."""

    source: str
    target: str
    domain: str
    observable_root_count: int
    localized_root_count: int
    preservation_score: float
    deletion_priority: float
    reason: str


@dataclass(frozen=True)
class SymbolicRootExpression:
    """One pole/zero expression extracted from a subrange transfer function."""

    kind: str
    expression: sp.Expr | None
    numeric_value: complex | None
    frequency_hz: float | None
    polynomial_degree: int
    exact: bool


@dataclass(frozen=True)
class SubrangeSymbolicTransfer:
    """Symbolic transfer function obtained from one frequency-reduced G_j."""

    cluster_index: int
    lower_frequency_hz: float
    upper_frequency_hz: float
    representative_frequency_hz: float
    transfer: sp.Expr
    numerator: sp.Expr
    denominator: sp.Expr
    poles: tuple[SymbolicRootExpression, ...]
    zeros: tuple[SymbolicRootExpression, ...]
    vertex_count: int
    edge_count: int
    success: bool = True
    error: str | None = None


@dataclass(frozen=True)
class LocalizedSymbolicRoot:
    """A physically traceable root expression extracted from one local SFG loop."""

    cluster_index: int
    vertex: str
    kind: str
    expression: sp.Expr
    characteristic_equation: sp.Expr
    loop_gains: tuple[sp.Expr, ...]
    scc_vertices: tuple[str, ...]
    numeric_value: complex | None
    frequency_hz: float | None
    reference_value: complex
    relative_frequency_error: float | None
    loop_edges: tuple[tuple[tuple[str, str, str], ...], ...] = ()
    method: str = "local Mason characteristic"
    full_loop_count: int = 0
    parameter_sensitivities: tuple[tuple[str, float], ...] = ()
    corner_max_relative_error: float | None = None
    simplified_expression: sp.Expr | None = None
    simplified_numeric_value: complex | None = None
    simplified_frequency_hz: float | None = None
    simplified_relative_frequency_error: float | None = None
    simplified_corner_max_relative_error: float | None = None
    full_term_count: int = 0
    retained_term_count: int = 0


@dataclass(frozen=True)
class LocalZeroPath:
    """One signed forward path contributing to a local summing-point zero."""

    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str, str], ...]
    gain: sp.Expr
    mason_cofactor: sp.Expr
    weighted_gain: sp.Expr


@dataclass(frozen=True)
class LocalizedSymbolicZero:
    """A closed-loop zero explained by cancellation inside one local SFG region."""

    cluster_index: int
    vertex: str
    expression: sp.Expr | None
    characteristic_equation: sp.Expr | None
    numeric_value: complex | None
    frequency_hz: float | None
    reference_value: complex
    relative_root_error: float | None
    cut_start: str | None
    cut_end: str | None
    cut_kind: str
    paths: tuple[LocalZeroPath, ...]
    cancellation_residual: float | None
    method: str
    equivalent_transfer: sp.Expr | None = None
    corner_max_relative_error: float | None = None
    parameter_participation: tuple[tuple[str, float], ...] = ()
    parameters: tuple[str, ...] = ()
    status: str = "unresolved"
    failure_reason: str | None = None


@dataclass(frozen=True)
class TargetRootSymbolicApproximation:
    """Best physically traceable symbolic expression for one closed-loop root."""

    cluster_index: int
    root_index: int
    kind: str
    reference_value: complex
    reference_frequency_hz: float
    category: str
    expression: sp.Expr | None
    characteristic_equation: sp.Expr | None
    numeric_value: complex | None
    frequency_hz: float | None
    relative_root_error: float | None
    method: str
    location: str | None
    parameters: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class PaperAlgorithmConfig:
    """Configuration for the paper Fig.12 analysis stages."""

    cluster_tolerance: float = 0.9
    total_error_budget: float = 0.05
    strict_graph: bool = False
    max_paths: int = 256
    max_loops: int = 512
    max_cuts: int = 256
    strict_cuts_only: bool = False
    frequency_range_hz: tuple[float, float] | None = None
    magnitude_error_db: float | None = None
    phase_error_deg: float | None = None
    use_simplified_complexity: bool = False
    enable_subrange_transfers: bool = True
    enable_symbolic_spr_expressions: bool = False
    subrange_term_relative_threshold: float = 0.1
    subrange_edge_relative_threshold: float = 1e-5
    max_symbolic_root_degree: int = 2


@dataclass(frozen=True)
class PaperAlgorithmState:
    """Single state object for the paper Fig.12 algorithm after N -> G."""

    config: PaperAlgorithmConfig
    pipeline: "ReferencePipelineResult"
    complexity: PaperComplexity
    strict_graph_cuts: tuple[GraphCut, ...]
    fallback_graph_cuts: tuple[GraphCut, ...]


@dataclass(frozen=True)
class ReferencePipelineResult:
    """Combined result of reference analysis, clustering and SFG candidate scan."""

    network: LinearNetwork
    graph: SignalFlowGraph
    reference: NumericReferenceResult | DescriptorMatrixResult
    clusters: tuple[RootCluster, ...]
    error_specs: tuple[ErrorSubSpecification, ...]
    candidates: tuple[MetaEdgeCandidate, ...]
    topology: GraphTopology
    localizations: tuple[RootLocalization, ...]
    rankings: tuple[SimplificationRanking, ...]
    topology_metrics: PaperTopologyMetrics | None = None
    subgraphs: tuple[SFGSubgraph, ...] = ()
    summing_point_roots: tuple[SummingPointRoot, ...] = ()
    subrange_transfers: tuple[SubrangeSymbolicTransfer, ...] = ()


def compute_numeric_reference(
    file_path: str | os.PathLike[str],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    source: str | None = None,
    detector: str | None = None,
) -> NumericReferenceResult:
    """Call SLiCAP's native solver and return the reference H(s), poles and zeros."""
    path = Path(file_path).expanduser().resolve()
    circuit = _load_circuit(path)
    substitutions = substitutions or {}
    circuit.parDefs.update(_parse_substitutions(substitutions))
    circuit = _updateCirData(circuit)

    source_name = source or _first_defined(circuit.source)
    detector_name = detector or _first_defined(circuit.detector)
    if source_name is None:
        raise ValueError("Numeric reference analysis requires a source; define .source or pass source=...")
    if detector_name is None:
        raise ValueError("Numeric reference analysis requires a detector; define .detector or pass detector=...")

    laplace_instr = _base_instruction(circuit, source_name, detector_name)
    laplace_instr.setDataType("laplace")
    laplace_result = laplace_instr.execute()

    pz_instr = _base_instruction(circuit, source_name, detector_name)
    pz_instr.setDataType("pz")
    pz_result = pz_instr.execute()

    numer, denom = sp.cancel(laplace_result.laplace).as_numer_denom()
    dc_value = None if pz_result.DCvalue == [] else complex(sp.N(pz_result.DCvalue))
    return NumericReferenceResult(
        title=circuit.title,
        file_path=str(path),
        source=source_name,
        detector=detector_name,
        laplace=sp.simplify(laplace_result.laplace),
        numerator=sp.simplify(numer),
        denominator=sp.simplify(denom),
        poles=tuple(complex(value) for value in pz_result.poles),
        zeros=tuple(complex(value) for value in pz_result.zeros),
        dc_value=dc_value,
        method="slicap-symbolic-mna",
    )


def compute_numeric_descriptor_reference(
    file_path: str | os.PathLike[str],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    source: str | None = None,
    detector: str | None = None,
    cancellation_tolerance: float = 1e-7,
) -> DescriptorMatrixResult:
    """Find transfer poles/zeros from the numerical MNA matrix pencil.

    SLiCAP supplies ``M(s) x = b u``. For linear RLC small-signal models this
    matrix is affine in the Laplace variable and can be written as
    ``M(s) = G + s C``. Finite eigenvalues of ``(-G, C)`` are circuit modes;
    finite eigenvalues of the SISO Rosenbrock pencil are transmission zeros.
    Numerically coincident mode/zero pairs are removed as pole-zero
    cancellations, without expanding a symbolic determinant or transfer
    function.
    """
    if cancellation_tolerance <= 0:
        raise ValueError("cancellation_tolerance must be positive.")

    path = Path(file_path).expanduser().resolve()
    circuit = _load_circuit(path)
    circuit.parDefs.update(_parse_substitutions(substitutions or {}))
    circuit = _updateCirData(circuit)

    source_name = source or _first_defined(circuit.source)
    detector_name = detector or _first_defined(circuit.detector)
    if source_name is None:
        raise ValueError("Descriptor analysis requires a source; define .source or pass source=...")
    if detector_name is None:
        raise ValueError("Descriptor analysis requires a detector; define .detector or pass detector=...")

    matrix_instr = _base_instruction(circuit, source_name, detector_name)
    matrix_instr.parDefs = circuit.parDefs
    matrix_instr = _makeAllMatrices(matrix_instr, reduce=False)
    laplace = ini.laplace
    matrix = sp.Matrix(matrix_instr.M)
    conductance = matrix.subs(laplace, 0)
    dynamic = matrix.diff(laplace)
    residual = matrix - conductance - laplace * dynamic
    nonlinear_entries = [
        (row, col, sp.simplify(residual[row, col]))
        for row in range(residual.rows)
        for col in range(residual.cols)
        if sp.simplify(residual[row, col]) != 0
    ]
    if nonlinear_entries:
        row, col, value = nonlinear_entries[0]
        raise ValueError(
            "Descriptor analysis currently requires an affine MNA pencil "
            f"M(s)=G+sC; entry ({row}, {col}) has residual {value}."
        )

    dependent_variables = tuple(str(value) for value in matrix_instr.Dv)
    detector_vector = _descriptor_detector_vector(
        dependent_variables,
        matrix_instr.detector,
    )
    g_matrix = _descriptor_complex_array(conductance, "conductance matrix G")
    c_matrix = _descriptor_complex_array(dynamic, "dynamic matrix C")
    input_vector = _descriptor_complex_array(matrix_instr.Iv, "input vector B")

    raw_modes, infinite_mode_count = _finite_generalized_eigenvalues(-g_matrix, c_matrix)
    system_constant = np.block(
        [
            [g_matrix, -input_vector],
            [detector_vector, np.zeros((1, 1), dtype=complex)],
        ]
    )
    system_dynamic = np.block(
        [
            [c_matrix, np.zeros((c_matrix.shape[0], 1), dtype=complex)],
            [np.zeros((1, c_matrix.shape[1]), dtype=complex), np.zeros((1, 1), dtype=complex)],
        ]
    )
    raw_zeros, infinite_zero_count = _finite_generalized_eigenvalues(
        -system_constant,
        system_dynamic,
    )
    poles, zeros, cancelled = _cancel_numeric_root_pairs(
        raw_modes,
        raw_zeros,
        relative_tolerance=cancellation_tolerance,
    )
    return DescriptorMatrixResult(
        title=circuit.title,
        file_path=str(path),
        source=source_name,
        detector=detector_name,
        dependent_variables=dependent_variables,
        conductance_matrix=g_matrix,
        dynamic_matrix=c_matrix,
        input_vector=input_vector,
        detector_vector=detector_vector,
        poles=tuple(poles),
        zeros=tuple(zeros),
        cancelled_roots=tuple(cancelled),
        raw_finite_modes=tuple(raw_modes),
        raw_finite_zeros=tuple(raw_zeros),
        infinite_mode_count=infinite_mode_count,
        infinite_zero_count=infinite_zero_count,
    )


def _descriptor_detector_vector(
    dependent_variables: tuple[str, ...],
    detector: list[str | None] | str,
) -> np.ndarray:
    """Build the SISO output row from SLiCAP's detector definition."""
    detector_terms = [detector, None] if isinstance(detector, str) else detector
    row = np.zeros((1, len(dependent_variables)), dtype=complex)
    for sign, name in zip((1.0, -1.0), detector_terms):
        if name is None:
            continue
        try:
            index = dependent_variables.index(str(name))
        except ValueError as error:
            raise ValueError(f"Detector variable '{name}' is absent from the MNA variables.") from error
        row[0, index] += sign
    return row


def _descriptor_complex_array(value: Any, label: str) -> np.ndarray:
    """Convert a fully substituted SLiCAP matrix to a complex NumPy array."""
    symbolic = sp.Matrix(value)
    unresolved = sorted(
        {symbol for entry in symbolic for symbol in entry.free_symbols},
        key=str,
    )
    if unresolved:
        names = ", ".join(str(symbol) for symbol in unresolved)
        raise ValueError(f"{label} has unresolved parameters: {names}")
    try:
        return np.asarray(symbolic.tolist(), dtype=complex)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Could not convert {label} to numerical coefficients.") from error


def _finite_generalized_eigenvalues(
    constant: np.ndarray,
    dynamic: np.ndarray,
) -> tuple[list[complex], int]:
    """Return sorted finite roots of ``constant - root*dynamic``."""
    values = eig(constant, dynamic, right=False, check_finite=True)
    finite = [_clean_numeric_root(value) for value in values if np.isfinite(value)]
    finite.sort(key=lambda value: (abs(value), value.real, value.imag))
    return finite, int(len(values) - len(finite))


def _clean_numeric_root(value: complex, tolerance: float = 1e-10) -> complex:
    """Remove insignificant real or imaginary roundoff from one root."""
    result = complex(value)
    scale = max(1.0, abs(result))
    real = 0.0 if abs(result.real) <= tolerance * scale else result.real
    imag = 0.0 if abs(result.imag) <= tolerance * scale else result.imag
    return complex(real, imag)


def _cancel_numeric_root_pairs(
    poles: Iterable[complex],
    zeros: Iterable[complex],
    relative_tolerance: float,
) -> tuple[list[complex], list[complex], list[complex]]:
    """Remove numerically coincident pole/zero pairs from a nonminimal pencil."""
    remaining_poles = list(poles)
    remaining_zeros: list[complex] = []
    cancelled: list[complex] = []
    for zero in zeros:
        if not remaining_poles:
            remaining_zeros.append(zero)
            continue
        distances = [abs(zero - pole) / max(1.0, abs(zero), abs(pole)) for pole in remaining_poles]
        index = int(np.argmin(distances))
        if distances[index] <= relative_tolerance:
            pole = remaining_poles.pop(index)
            cancelled.append(_clean_numeric_root((zero + pole) / 2))
        else:
            remaining_zeros.append(zero)
    remaining_poles.sort(key=lambda value: (abs(value), value.real, value.imag))
    remaining_zeros.sort(key=lambda value: (abs(value), value.real, value.imag))
    cancelled.sort(key=lambda value: (abs(value), value.real, value.imag))
    return remaining_poles, remaining_zeros, cancelled


def compute_numeric_sfg_reference(
    graph: SignalFlowGraph,
    substitutions: dict[str | sp.Symbol, Any],
    source: str | None = None,
    detector: str | None = None,
) -> NumericReferenceResult:
    """Build a numerical-coefficient H(s) without expanding symbolic parameters.

    This mode supports root clustering for larger circuits whose fully symbolic
    SLiCAP transfer function is too expensive. It is not an independent MNA/SFG
    equivalence reference, so callers must preserve the ``method`` label.
    """
    numeric_graph = deepcopy(graph)
    parsed = _parse_substitutions(substitutions)
    numeric_graph.meta_edges = {
        key: replace(
            edge,
            value=_rationalized_numeric_expression(edge.value, parsed),
            order_partitions={
                order: _rationalized_numeric_expression(value, parsed)
                for order, value in edge.order_partitions.items()
            },
        )
        for key, edge in numeric_graph.meta_edges.items()
    }
    numeric_graph.source_drives = [
        replace(drive, value=_rationalized_numeric_expression(drive.value, parsed))
        for drive in numeric_graph.source_drives
    ]
    numeric_graph.voltage_constraints = [
        replace(constraint, value=_rationalized_numeric_expression(constraint.value, parsed))
        for constraint in numeric_graph.voltage_constraints
    ]
    transfer, source_name, detector_name, _detector_vertex = solve_sfg_symbolic_transfer(
        numeric_graph,
        source=source,
        detector=detector,
    )
    remaining = transfer.free_symbols - {ini.laplace}
    if remaining:
        names = ", ".join(sorted(str(symbol) for symbol in remaining))
        raise ValueError(f"Numeric SFG reference has unresolved parameters: {names}")
    numerator, denominator = sp.cancel(transfer).as_numer_denom()
    poles = tuple(_numeric_polynomial_roots(denominator))
    zeros = tuple(_numeric_polynomial_roots(numerator))
    try:
        dc_expr = sp.limit(transfer, ini.laplace, 0)
        dc_value = complex(sp.N(dc_expr)) if dc_expr.is_finite else None
    except (TypeError, ValueError):
        dc_value = None
    return NumericReferenceResult(
        title=graph.title,
        file_path=str(graph.file_path or ""),
        source=source_name,
        detector=detector_name,
        laplace=sp.factor(transfer),
        numerator=sp.factor(numerator),
        denominator=sp.factor(denominator),
        poles=poles,
        zeros=zeros,
        dc_value=dc_value,
        method="numeric-coefficient-sfg",
    )


def _numeric_polynomial_roots(expression: sp.Expr) -> list[complex]:
    """Return roots of a univariate numerical polynomial in the Laplace variable."""
    polynomial = sp.Poly(sp.expand(expression), ini.laplace)
    if polynomial.degree() <= 0:
        return []
    coefficients = np.asarray(
        [complex(sp.N(value, 16)) for value in polynomial.all_coeffs()],
        dtype=complex,
    )
    return [complex(value) for value in np.roots(coefficients)]


def _rationalized_numeric_expression(
    expression: sp.Expr,
    substitutions: dict[sp.Symbol, sp.Expr],
) -> sp.Expr:
    """Substitute a design point while retaining exact decimal cancellation."""
    value = sp.sympify(expression).subs(substitutions)
    return sp.cancel(sp.nsimplify(value, rational=True))


def run_reference_pipeline(
    file_path: str | os.PathLike[str],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    source: str | None = None,
    detector: str | None = None,
    cluster_tolerance: float = 0.1,
    total_error_budget: float = 0.01,
    strict_graph: bool = False,
    max_paths: int = 256,
    max_loops: int = 512,
    max_cuts: int = 256,
    strict_cuts_only: bool = False,
    root_observability_dominance_ratio: float = 10.0,
    root_feedback_loop_gain_threshold: float = 1.0,
    frequency_range_hz: tuple[float, float] | None = None,
    enable_subrange_transfers: bool = True,
    enable_symbolic_spr_expressions: bool = False,
    subrange_term_relative_threshold: float = 0.1,
    subrange_edge_relative_threshold: float = 1e-5,
    max_symbolic_root_degree: int = 2,
    reference_mode: str = "slicap_symbolic",
) -> ReferencePipelineResult:
    """Run the paper-oriented stages that precede actual graph simplification."""
    network = flatten_netlist(file_path)
    substitution_map = network.numeric_substitutions(extra=substitutions)
    graph = build_signal_flow_graph(network, strict=strict_graph)
    if reference_mode == "slicap_symbolic":
        reference = compute_numeric_reference(
            file_path,
            substitutions=substitutions,
            source=source,
            detector=detector,
        )
    elif reference_mode == "numeric_sfg":
        reference = compute_numeric_sfg_reference(
            graph,
            substitution_map,
            source=source,
            detector=detector,
        )
    elif reference_mode == "descriptor_mna":
        reference = compute_numeric_descriptor_reference(
            file_path,
            substitutions=substitutions,
            source=source,
            detector=detector,
        )
    else:
        raise ValueError(
            "reference_mode must be 'slicap_symbolic', 'numeric_sfg', or 'descriptor_mna'."
        )
    clusters = tuple(cluster_roots(reference.poles, reference.zeros, relative_tolerance=cluster_tolerance))
    error_specs = tuple(
        split_error_specification(
            clusters,
            total_error_budget=total_error_budget,
            frequency_range_hz=frequency_range_hz,
        )
    )
    candidates = tuple(analyze_meta_edge_candidates(graph, substitutions=substitution_map))
    topology = analyze_graph_topology(graph, source=reference.source, detector=reference.detector)
    topology_metrics = analyze_paper_topology(
        graph,
        candidates,
        source=reference.source,
        detector=reference.detector,
        max_paths=max_paths,
        max_loops=max_loops,
        max_cuts=max_cuts,
        strict_cuts_only=strict_cuts_only,
    )
    localizations = tuple(localize_candidate_roots(candidates, clusters, topology, error_specs))
    summing_point_roots = tuple(
        localize_closed_loop_summing_roots(
            graph,
            reference,
            clusters,
            localizations,
            topology_metrics,
            substitution_map,
        )
    )
    if enable_symbolic_spr_expressions:
        summing_point_roots = tuple(
            _refine_summing_point_symbolic_expressions(
                graph,
                summing_point_roots,
                localizations,
                substitution_map,
            )
        )
    subgraphs = tuple(create_root_subgraphs(graph, error_specs, candidates, topology_metrics, summing_point_roots))
    localizations = tuple(
        root_observability_check(
            graph,
            localizations,
            topology_metrics,
            substitution_map,
            summing_point_roots=summing_point_roots,
            dominance_ratio=root_observability_dominance_ratio,
            feedback_interaction_threshold=root_feedback_loop_gain_threshold,
        )
    )
    rankings = tuple(rank_meta_edge_candidates(candidates, localizations, topology=topology))
    subrange_transfers = (
        tuple(
            analyze_subrange_symbolic_transfers(
                graph,
                error_specs,
                source=reference.source,
                detector=reference.detector,
                substitutions=substitution_map,
                relative_threshold=subrange_term_relative_threshold,
                edge_relative_threshold=subrange_edge_relative_threshold,
                max_symbolic_root_degree=max_symbolic_root_degree,
            )
        )
        if enable_subrange_transfers else ()
    )
    return ReferencePipelineResult(
        network=network,
        graph=graph,
        reference=reference,
        clusters=clusters,
        error_specs=error_specs,
        candidates=candidates,
        topology=topology,
        localizations=localizations,
        rankings=rankings,
        topology_metrics=topology_metrics,
        subgraphs=subgraphs,
        summing_point_roots=summing_point_roots,
        subrange_transfers=subrange_transfers,
    )


def run_paper_algorithm(
    file_path: str | os.PathLike[str],
    config: PaperAlgorithmConfig | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    source: str | None = None,
    detector: str | None = None,
) -> PaperAlgorithmState:
    """Run the paper Fig.12 analysis stages and return one coherent state."""
    config = config or PaperAlgorithmConfig()
    pipeline = run_reference_pipeline(
        file_path,
        substitutions=substitutions,
        source=source,
        detector=detector,
        cluster_tolerance=config.cluster_tolerance,
        total_error_budget=config.total_error_budget,
        strict_graph=config.strict_graph,
        max_paths=config.max_paths,
        max_loops=config.max_loops,
        max_cuts=config.max_cuts,
        strict_cuts_only=config.strict_cuts_only,
        frequency_range_hz=config.frequency_range_hz,
        enable_subrange_transfers=config.enable_subrange_transfers,
        enable_symbolic_spr_expressions=config.enable_symbolic_spr_expressions,
        subrange_term_relative_threshold=config.subrange_term_relative_threshold,
        subrange_edge_relative_threshold=config.subrange_edge_relative_threshold,
        max_symbolic_root_degree=config.max_symbolic_root_degree,
    )
    topology = pipeline.topology_metrics
    if topology is None:
        strict_cuts: tuple[GraphCut, ...] = ()
        fallback_cuts: tuple[GraphCut, ...] = ()
    else:
        strict_cuts = tuple(cut for cut in topology.graph_cuts if cut.cut_kind == "complementary")
        fallback_cuts = tuple(cut for cut in topology.graph_cuts if cut.cut_kind != "complementary")
    complexity = paper_complexity(
        pipeline.graph,
        pipeline.candidates,
        topology,
        use_simplified=config.use_simplified_complexity,
    )
    return PaperAlgorithmState(
        config=config,
        pipeline=pipeline,
        complexity=complexity,
        strict_graph_cuts=strict_cuts,
        fallback_graph_cuts=fallback_cuts,
    )


def sample_frequency_response(
    laplace: sp.Expr,
    frequencies_hz: Iterable[float],
) -> list[FrequencySample]:
    """Sample a reference transfer function at selected frequencies."""
    samples: list[FrequencySample] = []
    for frequency in frequencies_hz:
        value = complex(sp.N(laplace.subs(ini.laplace, 2 * sp.pi * sp.I * frequency)))
        samples.append(FrequencySample(float(frequency), value))
    return samples


def sample_descriptor_frequency_response(
    descriptor: DescriptorMatrixResult,
    frequencies_hz: Iterable[float],
) -> list[FrequencySample]:
    """Evaluate ``H(jw)=L(G+jwC)^-1B`` without constructing symbolic H(s)."""
    samples: list[FrequencySample] = []
    for frequency in frequencies_hz:
        frequency = float(frequency)
        if frequency < 0:
            raise ValueError("Frequency samples must be non-negative.")
        laplace_value = 2j * np.pi * frequency
        matrix = descriptor.conductance_matrix + laplace_value * descriptor.dynamic_matrix
        try:
            state = np.linalg.solve(matrix, descriptor.input_vector)
        except np.linalg.LinAlgError as error:
            raise ValueError(f"Descriptor matrix is singular at {frequency} Hz.") from error
        value = complex((descriptor.detector_vector @ state)[0, 0])
        samples.append(FrequencySample(frequency, value))
    return samples


def cluster_roots(
    poles: Iterable[complex],
    zeros: Iterable[complex],
    relative_tolerance: float = 0.1,
) -> list[RootCluster]:
    """Cluster roots by magnitude while enforcing the paper's full cluster span."""
    if relative_tolerance < 0:
        raise ValueError("relative_tolerance must be non-negative.")
    roots = [RootSample("pole", complex(value)) for value in poles]
    roots.extend(RootSample("zero", complex(value)) for value in zeros)
    roots.sort(key=lambda item: item.magnitude)
    if not roots:
        return []

    groups: list[list[RootSample]] = [[roots[0]]]
    for root in roots[1:]:
        cluster_minimum = groups[-1][0]
        scale = max(cluster_minimum.magnitude, root.magnitude, 1e-300)
        if abs(root.magnitude - cluster_minimum.magnitude) / scale <= relative_tolerance:
            groups[-1].append(root)
        else:
            groups.append([root])

    clusters: list[RootCluster] = []
    for index, group in enumerate(groups, start=1):
        magnitudes = [root.magnitude for root in group]
        clusters.append(
            RootCluster(
                index=index,
                roots=tuple(group),
                min_magnitude=min(magnitudes),
                max_magnitude=max(magnitudes),
            )
        )
    return clusters


def split_error_specification(
    clusters: Iterable[RootCluster],
    total_error_budget: float = 0.01,
    frequency_range_hz: tuple[float, float] | None = None,
) -> list[ErrorSubSpecification]:
    """Restrict the user error specification to root-cluster ranges using Eq.(13)-(14)."""
    clusters = list(clusters)
    if total_error_budget < 0:
        raise ValueError("total_error_budget must be non-negative.")
    if not clusters:
        return []
    user_lower: float | None = None
    user_upper: float | None = None
    if frequency_range_hz is not None:
        user_lower, user_upper = map(float, frequency_range_hz)
        if user_lower <= 0 or user_upper <= user_lower:
            raise ValueError("frequency_range_hz must be a positive (min, max) tuple with max > min.")
    # Root clustering restricts the frequency domain; it does not divide the
    # user-specified accuracy among clusters. Each subrange keeps the full bound.
    budget = total_error_budget
    boundaries = _cluster_frequency_boundaries(clusters)
    specs: list[ErrorSubSpecification] = []
    for index, cluster in enumerate(clusters):
        raw_lower, raw_upper = boundaries[index]
        lower = raw_lower
        upper = raw_upper
        if user_lower is not None and user_upper is not None:
            lower = max(lower, user_lower)
            upper = min(upper, user_upper)
        if upper <= lower:
            # The root cluster lies outside the user-requested error range.
            continue
        specs.append(
            ErrorSubSpecification(
                cluster_index=cluster.index,
                error_budget=budget,
                lower_frequency_hz=lower,
                upper_frequency_hz=upper,
                root_count=len(cluster.roots),
                raw_lower_frequency_hz=raw_lower,
                raw_upper_frequency_hz=raw_upper,
                user_lower_frequency_hz=user_lower,
                user_upper_frequency_hz=user_upper,
            )
        )
    return specs


def analyze_meta_edge_candidates(
    graph: SignalFlowGraph,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
) -> list[MetaEdgeCandidate]:
    """Analyze every meta-edge as a candidate for later ranking/simplification."""
    candidates = [
        _analyze_meta_edge_candidate(edge, substitutions=substitutions)
        for edge in graph.meta_edges.values()
    ]
    candidates.sort(key=lambda item: item.structural_score, reverse=True)
    return candidates


def analyze_graph_topology(
    graph: SignalFlowGraph,
    source: str | None = None,
    detector: str | None = None,
) -> GraphTopology:
    """Analyze source-to-detector reachability of the SFG."""
    source_drive = _select_source_drive(graph, source)
    detector_name = detector or _first_available_detector_vertex(graph)
    detector_vertex = _detector_to_vertex(graph, detector_name)
    adjacency, reverse_adjacency = _graph_adjacency(graph)
    reachable = frozenset(_reachable_vertices(adjacency, {source_drive.vertex_name}))
    can_reach_detector = frozenset(_reachable_vertices(reverse_adjacency, {detector_vertex}))
    observable_edges = frozenset(
        key for key, edge in graph.meta_edges.items()
        if edge.source in reachable and edge.target in can_reach_detector
    )
    return GraphTopology(
        source_ref=source_drive.source_ref,
        source_vertex=source_drive.vertex_name,
        detector=detector_name,
        detector_vertex=detector_vertex,
        reachable_from_source=reachable,
        can_reach_detector=can_reach_detector,
        observable_edges=observable_edges,
    )


def analyze_paper_topology(
    graph: SignalFlowGraph,
    candidates: Iterable[MetaEdgeCandidate] | None = None,
    source: str | None = None,
    detector: str | None = None,
    max_paths: int = 256,
    max_loops: int = 512,
    max_cuts: int = 256,
    strict_cuts_only: bool = False,
) -> PaperTopologyMetrics:
    """Analyze forward paths, loops and summing vertices for Fig.12 steps 7-9."""
    source_drive = _select_source_drive(graph, source)
    detector_name = detector or _first_available_detector_vertex(graph)
    detector_vertex = _detector_to_vertex(graph, detector_name)
    adjacency, reverse_adjacency = _graph_adjacency(graph)
    edge_lookup = _edge_lookup(graph)
    forward_paths = tuple(_enumerate_forward_paths(
        adjacency,
        edge_lookup,
        source_drive.vertex_name,
        detector_vertex,
        max_paths=max_paths,
    ))
    feedback_loops = tuple(_enumerate_feedback_loops(
        adjacency,
        edge_lookup,
        max_loops=max_loops,
    ))
    graph_cuts = tuple(
        _enumerate_graph_cuts(
            adjacency,
            reverse_adjacency,
            edge_lookup,
            max_cuts=max_cuts,
            include_fallback=not strict_cuts_only,
        )
    )
    incoming_edges: dict[str, list[tuple[str, str, str]]] = {}
    for key, edge in graph.meta_edges.items():
        incoming_edges.setdefault(edge.target, []).append(key)
    summing_vertices = tuple(
        SummingVertex(vertex, tuple(sorted(edges)))
        for vertex, edges in sorted(incoming_edges.items())
        if len(edges) > 1
    )
    reachable = _reachable_vertices(adjacency, {source_drive.vertex_name})
    can_reach_detector = _reachable_vertices(reverse_adjacency, {detector_vertex})
    observable_edges = frozenset(
        key for key, edge in graph.meta_edges.items()
        if edge.source in reachable and edge.target in can_reach_detector
    )
    if candidates is None:
        candidate_root_edges = frozenset(
            key for key, edge in graph.meta_edges.items()
            if _has_candidate_root(edge.value)
        )
    else:
        candidate_root_edges = frozenset(
            (candidate.source, candidate.target, candidate.domain)
            for candidate in candidates
            if candidate.roots
        )
    return PaperTopologyMetrics(
        source_ref=source_drive.source_ref,
        source_vertex=source_drive.vertex_name,
        detector=detector_name,
        detector_vertex=detector_vertex,
        forward_paths=forward_paths,
        feedback_loops=feedback_loops,
        graph_cuts=graph_cuts,
        summing_vertices=summing_vertices,
        observable_edges=observable_edges,
        candidate_root_edges=candidate_root_edges,
    )


def create_root_subgraphs(
    graph: SignalFlowGraph,
    error_specs: Iterable[ErrorSubSpecification],
    candidates: Iterable[MetaEdgeCandidate],
    topology: PaperTopologyMetrics,
    summing_point_roots: Iterable[SummingPointRoot] | None = None,
) -> list[SFGSubgraph]:
    """Create root-cluster subgraphs G_j from candidate roots and observability."""
    candidates = tuple(candidates)
    summing_point_roots = tuple(summing_point_roots or ())
    subgraphs: list[SFGSubgraph] = []
    for spec in error_specs:
        candidate_edges: set[tuple[str, str, str]] = set()
        root_edges: set[tuple[str, str, str]] = set()
        vertices: set[str] = set()
        edges: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            key = (candidate.source, candidate.target, candidate.domain)
            if not _candidate_in_frequency_band(candidate, spec):
                continue
            candidate_edges.add(key)
            root_edges.add(key)
            vertices.update([candidate.source, candidate.target])
            edges.add(key)
            for path in topology.forward_paths:
                if key in path.edges:
                    vertices.update(path.vertices)
                    edges.update(path.edges)
            for loop in topology.feedback_loops:
                if key in loop.edges:
                    vertices.update(loop.vertices)
                    edges.update(loop.edges)
        for root in summing_point_roots:
            if root.cluster_index != spec.cluster_index:
                continue
            for path_edges in root.path_edges:
                root_edges.update(path_edges)
                edges.update(path_edges)
                for edge_key in path_edges:
                    edge = graph.meta_edges.get(edge_key)
                    if edge is not None:
                        vertices.update([edge.source, edge.target])
        for cut in topology.graph_cuts:
            if root_edges and not cut.edges.isdisjoint(root_edges):
                vertices.update(cut.vertices)
                edges.update(cut.edges)
        subgraphs.append(
            SFGSubgraph(
                cluster_index=spec.cluster_index,
                lower_frequency_hz=spec.lower_frequency_hz,
                upper_frequency_hz=spec.upper_frequency_hz,
                vertices=frozenset(vertices),
                edges=frozenset(edges),
                candidate_edges=frozenset(candidate_edges),
            )
        )
    return subgraphs


def paper_complexity(
    graph: SignalFlowGraph,
    candidates: Iterable[MetaEdgeCandidate] | None = None,
    topology: PaperTopologyMetrics | None = None,
    source: str | None = None,
    detector: str | None = None,
    use_simplified: bool = False,
) -> PaperComplexity:
    """Compute paper Eq.(9) or Eq.(10) graph complexity terms."""
    if candidates is None:
        candidates = analyze_meta_edge_candidates(graph)
    candidates = tuple(candidates)
    if topology is None:
        topology = analyze_paper_topology(graph, candidates, source=source, detector=detector)
    open_loop_roots = sum(len(candidate.roots) for candidate in candidates)
    return PaperComplexity(
        open_loop_roots=open_loop_roots,
        summing_points=len(topology.summing_vertices),
        forward_paths=len(topology.forward_paths),
        feedback_loops=len(topology.feedback_loops),
        use_simplified=use_simplified,
    )


def frequency_reduced_graph(
    graph: SignalFlowGraph,
    spec: ErrorSubSpecification,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    relative_threshold: float = 0.1,
    edge_relative_threshold: float = 1e-5,
) -> SignalFlowGraph:
    """Create a subrange graph by keeping dominant order partitions at the band center."""
    substitutions = substitutions or {}
    reduced = deepcopy(graph)
    frequency_hz = _subrange_center_frequency(spec)
    for key, edge in list(reduced.meta_edges.items()):
        if edge.domain == "voltage" or edge.source.endswith(".src"):
            continue
        value = _frequency_reduced_edge_value(edge, frequency_hz, substitutions, relative_threshold)
        reduced.meta_edges[key] = replace(
            edge,
            value=sp.simplify(value),
            order_partitions=_partition_expression_for_domain(value, edge.domain),
        )
    _prune_weak_subrange_edges(reduced, frequency_hz, substitutions, edge_relative_threshold)
    return reduced


def analyze_subrange_symbolic_transfers(
    graph: SignalFlowGraph,
    error_specs: Iterable[ErrorSubSpecification],
    source: str | None = None,
    detector: str | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    relative_threshold: float = 0.1,
    edge_relative_threshold: float = 1e-5,
    max_symbolic_root_degree: int = 2,
) -> list[SubrangeSymbolicTransfer]:
    """Solve symbolic transfer functions for all frequency-reduced subgraphs."""
    results: list[SubrangeSymbolicTransfer] = []
    for spec in error_specs:
        results.append(
            solve_subrange_symbolic_transfer(
                graph,
                spec,
                source=source,
                detector=detector,
                substitutions=substitutions,
                relative_threshold=relative_threshold,
                edge_relative_threshold=edge_relative_threshold,
                max_symbolic_root_degree=max_symbolic_root_degree,
            )
        )
    return results


def solve_subrange_symbolic_transfer(
    graph: SignalFlowGraph,
    spec: ErrorSubSpecification,
    source: str | None = None,
    detector: str | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    relative_threshold: float = 0.1,
    edge_relative_threshold: float = 1e-5,
    max_symbolic_root_degree: int = 2,
) -> SubrangeSymbolicTransfer:
    """Create G_j, solve H_j(s), and extract symbolic pole/zero candidates."""
    representative = _subrange_center_frequency(spec)
    reduced = frequency_reduced_graph(
        graph,
        spec,
        substitutions=substitutions,
        relative_threshold=relative_threshold,
        edge_relative_threshold=edge_relative_threshold,
    )
    try:
        transfer, _source_ref, _detector_name, _detector_vertex = solve_sfg_symbolic_transfer(
            reduced,
            source=source,
            detector=detector,
        )
        numerator, denominator = sp.cancel(transfer).as_numer_denom()
        numerator = sp.factor(sp.simplify(numerator))
        denominator = sp.factor(sp.simplify(denominator))
        zeros = tuple(
            _symbolic_roots_from_polynomial(
                numerator,
                "zero",
                substitutions=substitutions,
                max_symbolic_degree=max_symbolic_root_degree,
            )
        )
        poles = tuple(
            _symbolic_roots_from_polynomial(
                denominator,
                "pole",
                substitutions=substitutions,
                max_symbolic_degree=max_symbolic_root_degree,
            )
        )
        return SubrangeSymbolicTransfer(
            cluster_index=spec.cluster_index,
            lower_frequency_hz=spec.lower_frequency_hz,
            upper_frequency_hz=spec.upper_frequency_hz,
            representative_frequency_hz=representative,
            transfer=sp.factor(sp.simplify(transfer)),
            numerator=numerator,
            denominator=denominator,
            poles=poles,
            zeros=zeros,
            vertex_count=len(reduced.vertices),
            edge_count=len(reduced.meta_edges),
        )
    except Exception as exc:
        return SubrangeSymbolicTransfer(
            cluster_index=spec.cluster_index,
            lower_frequency_hz=spec.lower_frequency_hz,
            upper_frequency_hz=spec.upper_frequency_hz,
            representative_frequency_hz=representative,
            transfer=sp.nan,
            numerator=sp.nan,
            denominator=sp.nan,
            poles=(),
            zeros=(),
            vertex_count=len(reduced.vertices),
            edge_count=len(reduced.meta_edges),
            success=False,
            error=str(exc),
        )


def solve_cluster_approximated_symbolic_transfer(
    graph: SignalFlowGraph,
    spec: ErrorSubSpecification,
    localizations: Iterable[RootLocalization],
    source: str | None = None,
    detector: str | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    edge_relative_threshold: float = 1e-5,
    max_symbolic_root_degree: int = 2,
) -> SubrangeSymbolicTransfer:
    """Solve a paper-style G_j formed from root-cluster edge approximations."""
    representative = _subrange_center_frequency(spec)
    reduced = deepcopy(graph)
    edge_root_clusters = _edge_root_cluster_map(localizations)
    for key, edge in list(reduced.meta_edges.items()):
        if edge.domain == "voltage" or edge.source.endswith(".src"):
            continue
        value = _cluster_approximated_edge_value(
            sp.sympify(edge.value),
            spec.cluster_index,
            edge_root_clusters.get(key),
        )
        reduced.meta_edges[key] = replace(
            edge,
            value=sp.simplify(value),
            order_partitions=_partition_expression_for_domain(value, edge.domain),
        )
    _prune_weak_subrange_edges(reduced, representative, substitutions or {}, edge_relative_threshold)
    try:
        transfer, _source_ref, _detector_name, _detector_vertex = solve_sfg_symbolic_transfer(
            reduced,
            source=source,
            detector=detector,
        )
        numerator, denominator = sp.cancel(transfer).as_numer_denom()
        numerator = sp.factor(sp.simplify(numerator))
        denominator = sp.factor(sp.simplify(denominator))
        zeros = tuple(
            _symbolic_roots_from_polynomial(
                numerator,
                "zero",
                substitutions=substitutions,
                max_symbolic_degree=max_symbolic_root_degree,
            )
        )
        poles = tuple(
            _symbolic_roots_from_polynomial(
                denominator,
                "pole",
                substitutions=substitutions,
                max_symbolic_degree=max_symbolic_root_degree,
            )
        )
        return SubrangeSymbolicTransfer(
            cluster_index=spec.cluster_index,
            lower_frequency_hz=spec.lower_frequency_hz,
            upper_frequency_hz=spec.upper_frequency_hz,
            representative_frequency_hz=representative,
            transfer=sp.factor(sp.simplify(transfer)),
            numerator=numerator,
            denominator=denominator,
            poles=poles,
            zeros=zeros,
            vertex_count=len(reduced.vertices),
            edge_count=len(reduced.meta_edges),
        )
    except Exception as exc:
        return SubrangeSymbolicTransfer(
            cluster_index=spec.cluster_index,
            lower_frequency_hz=spec.lower_frequency_hz,
            upper_frequency_hz=spec.upper_frequency_hz,
            representative_frequency_hz=representative,
            transfer=sp.nan,
            numerator=sp.nan,
            denominator=sp.nan,
            poles=(),
            zeros=(),
            vertex_count=len(reduced.vertices),
            edge_count=len(reduced.meta_edges),
            success=False,
            error=str(exc),
        )


def solve_sfg_symbolic_transfer(
    graph: SignalFlowGraph,
    source: str | None = None,
    detector: str | None = None,
) -> tuple[sp.Expr, str, str, str]:
    """Solve an SFG symbolically using equations x_t=sum(g_e*x_s)."""
    drive = _select_source_drive(graph, source)
    detector_name = detector or _first_available_detector_vertex(graph)
    detector_vertex = _detector_to_vertex(graph, detector_name)
    relevant_vertices = _vertices_relevant_to_detector(graph, detector_vertex)
    relevant_vertices.add(drive.vertex_name)
    symbols = {name: sp.Symbol(_safe_symbol_name(name)) for name in sorted(relevant_vertices) if name in graph.vertices}
    if detector_vertex not in symbols:
        raise ValueError(f"Detector vertex '{detector_vertex}' is not in the relevant SFG subgraph.")

    equations: list[sp.Expr] = []
    source_vertices = {item.vertex_name for item in graph.source_drives if item.vertex_name in symbols}
    for item in graph.source_drives:
        if item.vertex_name not in symbols:
            continue
        equations.append(symbols[item.vertex_name] - sp.sympify(item.value))

    incoming: dict[str, list[MetaEdge]] = {}
    for edge in graph.meta_edges.values():
        if edge.target in symbols:
            incoming.setdefault(edge.target, []).append(edge)
    for target, edges in incoming.items():
        if target in source_vertices:
            continue
        voltage_edges = [edge for edge in edges if edge.domain == "voltage"]
        signal_edges = [edge for edge in edges if edge.domain != "voltage"]
        for edge_group in (signal_edges, voltage_edges):
            if not edge_group:
                continue
            expr = symbols[target]
            for edge in edge_group:
                if edge.source not in symbols:
                    continue
                expr -= sp.sympify(edge.value) * symbols[edge.source]
            equations.append(sp.simplify(expr))

    solution = _solve_linear_sfg_equations(equations, list(symbols.values()))
    detector_expr = sp.simplify(solution[symbols[detector_vertex]])
    drive_value = sp.sympify(drive.value)
    if drive_value == 0:
        transfer = detector_expr
    else:
        transfer = sp.cancel(sp.simplify(detector_expr / drive_value))
    return sp.factor(transfer), drive.source_ref, detector_name, detector_vertex


def symbolic_transfer_roots(
    transfer: sp.Expr,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    max_symbolic_degree: int = 2,
) -> tuple[tuple[SymbolicRootExpression, ...], tuple[SymbolicRootExpression, ...]]:
    """Extract symbolic poles and zeroes from an arbitrary rational transfer function."""
    numerator, denominator = sp.cancel(sp.sympify(transfer)).as_numer_denom()
    poles = tuple(
        _symbolic_roots_from_polynomial(
            denominator,
            "pole",
            substitutions=substitutions,
            max_symbolic_degree=max_symbolic_degree,
        )
    )
    zeros = tuple(
        _symbolic_roots_from_polynomial(
            numerator,
            "zero",
            substitutions=substitutions,
            max_symbolic_degree=max_symbolic_degree,
        )
    )
    return poles, zeros


def derive_target_root_symbolic_approximations(
    graph: SignalFlowGraph,
    clusters: Iterable[RootCluster],
    localizations: Iterable[RootLocalization],
    summing_point_roots: Iterable[SummingPointRoot],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    max_relative_root_error: float = 0.5,
    enforce_relative_root_error: bool = False,
    max_local_loops: int = 12,
    parameter_tolerances: dict[str | sp.Symbol, float] | None = None,
    default_parameter_tolerance: float | None = None,
    max_corner_parameters: int = 6,
    localized_spr_expressions: Iterable[LocalizedSymbolicRoot] | None = None,
    localized_spr_zero_expressions: Iterable[LocalizedSymbolicZero] | None = None,
    summing_root_graph: SignalFlowGraph | None = None,
    root_context_localizations: Iterable[RootLocalization] | None = None,
    root_index_start: int = 1,
) -> tuple[TargetRootSymbolicApproximation, ...]:
    """Select one local symbolic expression for every clustered closed-loop root.

    Observable open-loop roots are extracted directly from the corresponding
    meta-edge numerator or denominator. Dominant summing-point poles are
    derived from a local Mason characteristic. Candidates are attached to the
    nearest closed-loop root of the same kind and selected by readability.

    The paper prototype accepts graph manipulations from transfer-performance
    error on the imaginary axis, so root-location error is diagnostic by
    default. Set ``enforce_relative_root_error`` only for the optional
    pole/zero-location control discussed in paper Section III-E.
    """
    if max_relative_root_error < 0:
        raise ValueError("max_relative_root_error must be non-negative.")
    substitutions = substitutions or {}
    summing_root_graph = summing_root_graph or graph
    localizations = tuple(localizations)
    root_context_localizations = (
        localizations
        if root_context_localizations is None
        else tuple(root_context_localizations)
    )
    summing_point_roots = tuple(summing_point_roots)
    edge_root_clusters = _edge_root_cluster_map(root_context_localizations)
    target_rows: list[tuple[int, int, RootSample]] = []
    root_index = int(root_index_start)
    for cluster in clusters:
        for root in cluster.roots:
            target_rows.append((root_index, cluster.index, root))
            root_index += 1
    candidates: dict[int, list[TargetRootSymbolicApproximation]] = {
        index: [] for index, _cluster_index, _root in target_rows
    }

    for localization in localizations:
        if not localization.observable or localization.cluster_index is None:
            continue
        target = _nearest_target_root_row(
            target_rows,
            localization.cluster_index,
            localization.root_kind,
            localization.root_value,
        )
        if target is None:
            continue
        edge = graph.meta_edges.get((localization.source, localization.target, localization.domain))
        if edge is None:
            continue
        numerator, denominator = sp.cancel(sp.sympify(edge.value)).as_numer_denom()
        characteristic = numerator if localization.root_kind == "zero" else denominator
        expression = _nearest_symbolic_root_expression(
            characteristic,
            localization.root_value,
            substitutions,
        )
        if expression is None:
            continue
        numeric_value = _numeric_root_value(expression, substitutions)
        if numeric_value is None:
            continue
        target_index, cluster_index, reference_root = target
        error = _relative_complex_root_error(numeric_value, reference_root.value)
        candidates[target_index].append(
            TargetRootSymbolicApproximation(
                cluster_index=cluster_index,
                root_index=target_index,
                kind=reference_root.kind,
                reference_value=reference_root.value,
                reference_frequency_hz=reference_root.frequency_hz,
                category="OLR-O",
                expression=sp.factor(expression),
                characteristic_equation=sp.factor(characteristic),
                numeric_value=numeric_value,
                frequency_hz=abs(numeric_value) / (2 * np.pi),
                relative_root_error=error,
                method="open-loop meta-edge root",
                location=f"{localization.source} -> {localization.target} ({localization.domain})",
                parameters=_root_expression_parameters(expression),
                status="candidate",
            )
        )

    for local_zero in tuple(localized_spr_zero_expressions or ()):
        if (
            local_zero.status != "resolved"
            or local_zero.expression is None
            or local_zero.numeric_value is None
        ):
            continue
        target = _nearest_target_root_row(
            target_rows,
            local_zero.cluster_index,
            "zero",
            local_zero.reference_value,
        )
        if target is None:
            continue
        target_index, cluster_index, reference_root = target
        candidates[target_index].append(
            TargetRootSymbolicApproximation(
                cluster_index=cluster_index,
                root_index=target_index,
                kind="zero",
                reference_value=reference_root.value,
                reference_frequency_hz=reference_root.frequency_hz,
                category="SPR",
                expression=sp.factor(local_zero.expression),
                characteristic_equation=local_zero.characteristic_equation,
                numeric_value=local_zero.numeric_value,
                frequency_hz=local_zero.frequency_hz,
                relative_root_error=local_zero.relative_root_error,
                method=local_zero.method,
                location=f"{local_zero.cut_start} -> {local_zero.cut_end} at {local_zero.vertex}",
                parameters=local_zero.parameters,
                status="candidate",
            )
        )

    precomputed_spr = tuple(localized_spr_expressions or ())
    for root in summing_point_roots:
        if (
            not root.observable
            or not root.dominant
            or root.cluster_index is None
            or "pole" not in root.root_source
        ):
            continue
        target = _nearest_target_root_row(
            target_rows,
            root.cluster_index,
            "pole",
            root.value,
        )
        if target is None:
            continue
        target_index, cluster_index, reference_root = target
        local_candidates = [
            item
            for item in precomputed_spr
            if item.cluster_index == root.cluster_index and item.vertex == root.vertex
        ]
        if not local_candidates:
            graph_local = derive_local_summing_root_expression(
                graph,
                root,
                substitutions=substitutions,
                max_loops=max_local_loops,
                max_relative_frequency_error=(
                    max_relative_root_error if enforce_relative_root_error else math.inf
                ),
                parameter_tolerances=parameter_tolerances,
                default_parameter_tolerance=default_parameter_tolerance,
                max_corner_parameters=max_corner_parameters,
            )
            if graph_local is not None:
                local_candidates.append(graph_local)
        if not local_candidates:
            continue
        for local in local_candidates:
            cluster_candidate = _cluster_approximated_local_root_candidate(
                summing_root_graph,
                root,
                local,
                edge_root_clusters,
                substitutions,
            )
            if cluster_candidate is None:
                continue
            expression, characteristic, numeric_value = cluster_candidate
            candidates[target_index].append(
                TargetRootSymbolicApproximation(
                    cluster_index=cluster_index,
                    root_index=target_index,
                    kind=reference_root.kind,
                    reference_value=reference_root.value,
                    reference_frequency_hz=reference_root.frequency_hz,
                    category="SPR",
                    expression=sp.factor(expression),
                    characteristic_equation=sp.factor(characteristic),
                    numeric_value=numeric_value,
                    frequency_hz=abs(numeric_value) / (2 * np.pi),
                    relative_root_error=_relative_complex_root_error(
                        numeric_value,
                        reference_root.value,
                    ),
                    method="cluster-approximated dominant local-loop characteristic",
                    location=root.vertex,
                    parameters=_root_expression_parameters(expression),
                    status="candidate",
                )
            )
        seen_expressions: set[str] = set()
        for local in local_candidates:
            expression = (
                local.expression
                if local.simplified_expression is None
                else local.simplified_expression
            )
            numeric_value = (
                local.numeric_value
                if local.simplified_numeric_value is None
                else local.simplified_numeric_value
            )
            if numeric_value is None:
                continue
            expression_key = sp.srepr(sp.factor(expression))
            if expression_key in seen_expressions:
                continue
            seen_expressions.add(expression_key)
            error = _relative_complex_root_error(numeric_value, reference_root.value)
            candidates[target_index].append(
                TargetRootSymbolicApproximation(
                    cluster_index=cluster_index,
                    root_index=target_index,
                    kind=reference_root.kind,
                    reference_value=reference_root.value,
                    reference_frequency_hz=reference_root.frequency_hz,
                    category="SPR",
                    expression=sp.factor(expression),
                    characteristic_equation=local.characteristic_equation,
                    numeric_value=numeric_value,
                    frequency_hz=abs(numeric_value) / (2 * np.pi),
                    relative_root_error=error,
                    method=local.method,
                    location=root.vertex,
                    parameters=_root_expression_parameters(expression),
                    status="candidate",
                )
            )

    results: list[TargetRootSymbolicApproximation] = []
    for target_index, cluster_index, reference_root in target_rows:
        root_candidates = candidates[target_index]
        accepted = root_candidates
        if enforce_relative_root_error:
            accepted = [
                item
                for item in root_candidates
                if item.relative_root_error is not None
                and item.relative_root_error <= max_relative_root_error
            ]
        if accepted:
            selected = min(accepted, key=_target_root_candidate_selection_key)
            results.append(replace(selected, status="resolved"))
        elif root_candidates:
            selected = min(
                root_candidates,
                key=lambda item: item.relative_root_error if item.relative_root_error is not None else math.inf,
            )
            results.append(replace(selected, status="outside_error_limit"))
        else:
            results.append(
                TargetRootSymbolicApproximation(
                    cluster_index=cluster_index,
                    root_index=target_index,
                    kind=reference_root.kind,
                    reference_value=reference_root.value,
                    reference_frequency_hz=reference_root.frequency_hz,
                    category="UNRESOLVED",
                    expression=None,
                    characteristic_equation=None,
                    numeric_value=None,
                    frequency_hz=None,
                    relative_root_error=None,
                    method="no local symbolic candidate",
                    location=None,
                    parameters=(),
                    status="unresolved",
                )
            )
    return tuple(results)


def _target_root_candidate_selection_key(
    item: TargetRootSymbolicApproximation,
) -> tuple[int, float, float]:
    """Prefer paper subrange topology, then readability, then root proximity."""

    paper_topology_priority = int(
        item.method != "cluster-approximated dominant local-loop characteristic"
    )
    operation_count = (
        int(sp.count_ops(item.expression))
        if item.expression is not None
        else math.inf
    )
    root_error = (
        item.relative_root_error
        if item.relative_root_error is not None
        else math.inf
    )
    return paper_topology_priority, operation_count, root_error


def _cluster_approximated_local_root_candidate(
    graph: SignalFlowGraph,
    root: SummingPointRoot,
    local: LocalizedSymbolicRoot,
    edge_root_clusters: dict[tuple[str, str, str], int],
    substitutions: dict[str | sp.Symbol, Any],
) -> tuple[sp.Expr, sp.Expr, complex] | None:
    """Apply paper-style cluster asymptotes to the selected dominant local loops."""

    if root.cluster_index is None or not local.loop_edges:
        return None
    loops: list[FeedbackLoop] = []
    loop_gains: list[sp.Expr] = []
    for loop_edges in local.loop_edges:
        if not loop_edges:
            return None
        loops.append(
            FeedbackLoop(
                vertices=(loop_edges[0][0], *(edge[1] for edge in loop_edges)),
                edges=loop_edges,
            )
        )
        gain = sp.Integer(1)
        for edge_key in loop_edges:
            edge = graph.meta_edges.get(edge_key)
            if edge is None:
                return None
            gain *= _cluster_approximated_edge_value(
                sp.sympify(edge.value),
                root.cluster_index,
                edge_root_clusters.get(edge_key),
            )
        loop_gains.append(sp.factor(sp.cancel(gain)))
    delta = _mason_characteristic_equation(tuple(loops), tuple(loop_gains))
    characteristic = sp.factor(sp.together(delta).as_numer_denom()[0])
    laplace = _laplace_symbol_for(characteristic) or ini.laplace
    try:
        polynomial = sp.Poly(sp.expand(characteristic), laplace)
    except sp.PolynomialError:
        return None
    physical_equation = _physical_local_characteristic(delta, tuple(loop_gains))
    expression = _nearest_local_characteristic_root(
        physical_equation,
        polynomial,
        root.value,
        substitutions,
    )
    if expression is None:
        return None
    numeric_value = _numeric_root_value(expression, substitutions)
    if numeric_value is None:
        return None
    return sp.factor(expression), sp.factor(physical_equation), numeric_value


def _nearest_target_root_row(
    targets: Iterable[tuple[int, int, RootSample]],
    cluster_index: int,
    kind: str,
    value: complex,
) -> tuple[int, int, RootSample] | None:
    """Return the nearest closed-loop target root in one cluster."""
    matches = [
        target
        for target in targets
        if target[1] == cluster_index and target[2].kind == kind
    ]
    if not matches:
        return None
    return min(matches, key=lambda target: abs(target[2].value - value))


def _relative_complex_root_error(value: complex, reference: complex) -> float:
    """Return scale-safe relative error in the complex root plane."""
    return float(abs(value - reference) / max(abs(reference), 1.0))


def _root_expression_parameters(expression: sp.Expr) -> tuple[str, ...]:
    """List physical parameters occurring in a root expression."""
    return tuple(
        str(symbol)
        for symbol in sorted(sp.sympify(expression).free_symbols, key=str)
        if str(symbol) != str(ini.laplace)
    )


def derive_local_summing_root_expression(
    graph: SignalFlowGraph,
    root: SummingPointRoot,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    max_loops: int = 12,
    max_relative_frequency_error: float = 0.5,
    parameter_tolerances: dict[str | sp.Symbol, float] | None = None,
    default_parameter_tolerance: float | None = None,
    max_corner_parameters: int = 6,
    max_corner_relative_error: float | None = None,
    max_reported_sensitivities: int = 8,
    enable_term_pruning: bool = True,
    max_term_pruning_steps: int = 12,
    max_term_count: int = 64,
    term_relative_frequency_error: float = 0.05,
    term_corner_relative_error: float = 0.05,
) -> LocalizedSymbolicRoot | None:
    """Derive a readable SPR pole from the local feedback component around a summing vertex.

    The method forms the local Mason characteristic ``Delta(s)`` from all simple
    loops in the strongly connected component that contains ``root.vertex``.
    It then selects the symbolic characteristic root nearest to the closed-loop
    reference root. This keeps the result tied to graph topology rather than to
    circuit-specific parameter names.
    """
    if root.cluster_index is None or "pole" not in root.root_source:
        return None
    component = _strongly_connected_component(graph, root.vertex)
    if len(component) < 2:
        return None
    adjacency, _reverse = _graph_adjacency(graph)
    induced_adjacency = {
        vertex: set(adjacency.get(vertex, set())) & component
        for vertex in component
    }
    loops = _enumerate_feedback_loops(
        induced_adjacency,
        _edge_lookup(graph),
        max_loops=max_loops + 1,
    )
    if not loops or len(loops) > max_loops:
        return None
    all_loop_gains = tuple(_feedback_loop_gain(graph, loop) for loop in loops)
    full_indices = tuple(range(len(loops)))
    full_candidate = _local_characteristic_candidate(
        tuple(loops),
        all_loop_gains,
        root,
        substitutions,
    )
    if full_candidate is None:
        return None
    full_expression = full_candidate[1]
    all_sensitivities = _normalized_root_parameter_sensitivities(
        full_expression,
        substitutions,
    )
    corners = _parameter_corner_substitutions(
        substitutions,
        parameter_tolerances,
        all_sensitivities,
        default_tolerance=default_parameter_tolerance,
        max_corner_parameters=max_corner_parameters,
    )
    corner_limit = (
        max_relative_frequency_error
        if max_corner_relative_error is None
        else max_corner_relative_error
    )
    full_corner_values = _root_corner_values(full_expression, corners)
    subset_indices: list[tuple[int, ...]] = [full_indices]
    subset_indices.extend((index,) for index in range(len(loops)))
    if len(loops) <= 6:
        subset_indices.extend(combinations(range(len(loops)), 2))
    candidates: list[
        tuple[int, float, float | None, tuple[FeedbackLoop, ...], tuple[sp.Expr, ...], sp.Expr, sp.Expr, complex, float]
    ] = []
    seen_subsets: set[tuple[int, ...]] = set()
    for indices in subset_indices:
        indices = tuple(indices)
        if indices in seen_subsets:
            continue
        seen_subsets.add(indices)
        selected_loops = tuple(loops[index] for index in indices)
        selected_gains = tuple(all_loop_gains[index] for index in indices)
        candidate = _local_characteristic_candidate(
            selected_loops,
            selected_gains,
            root,
            substitutions,
        )
        if candidate is None:
            continue
        physical_equation, expression, numeric_value, frequency_hz, relative_error = candidate
        corner_error = _root_corner_relative_error(
            expression,
            full_expression,
            corners,
            reference_values=full_corner_values,
        )
        if indices != full_indices:
            if relative_error > max_relative_frequency_error:
                continue
            if corner_error is not None and corner_error > corner_limit:
                continue
        candidates.append(
            (
                int(sp.count_ops(expression)),
                relative_error,
                corner_error,
                selected_loops,
                selected_gains,
                physical_equation,
                expression,
                numeric_value,
                frequency_hz,
            )
        )
    if not candidates:
        return None
    (
        _complexity,
        relative_error,
        corner_error,
        selected_loops,
        loop_gains,
        physical_equation,
        expression,
        numeric_value,
        frequency_hz,
    ) = min(
        candidates,
        key=lambda item: (
            item[0],
            max(item[1], item[2] or 0.0),
            len(item[3]),
        ),
    )
    method = (
        "local Mason characteristic"
        if len(selected_loops) == len(loops)
        else "dominant local-loop approximation"
    )
    simplified_expression = None
    simplified_numeric_value = None
    simplified_frequency_hz = None
    simplified_relative_error = None
    simplified_corner_error = None
    full_term_count = _rational_additive_term_count(expression)
    retained_term_count = full_term_count
    if enable_term_pruning:
        term_result = _prune_symbolic_root_terms(
            expression,
            full_expression,
            root,
            substitutions,
            corners,
            max_total_relative_frequency_error=max_relative_frequency_error,
            max_term_relative_error=term_relative_frequency_error,
            max_total_corner_relative_error=corner_limit,
            max_term_corner_relative_error=term_corner_relative_error,
            max_steps=max_term_pruning_steps,
            max_terms=max_term_count,
        )
        if term_result is not None:
            (
                simplified_expression,
                simplified_numeric_value,
                simplified_frequency_hz,
                simplified_relative_error,
                simplified_corner_error,
                full_term_count,
                retained_term_count,
            ) = term_result
    return LocalizedSymbolicRoot(
        cluster_index=root.cluster_index,
        vertex=root.vertex,
        kind="pole",
        expression=expression,
        characteristic_equation=physical_equation,
        loop_gains=loop_gains,
        scc_vertices=tuple(sorted(component)),
        numeric_value=numeric_value,
        frequency_hz=frequency_hz,
        reference_value=root.value,
        relative_frequency_error=relative_error,
        loop_edges=tuple(loop.edges for loop in selected_loops),
        method=method,
        full_loop_count=len(loops),
        parameter_sensitivities=tuple(all_sensitivities[:max_reported_sensitivities]),
        corner_max_relative_error=corner_error,
        simplified_expression=simplified_expression,
        simplified_numeric_value=simplified_numeric_value,
        simplified_frequency_hz=simplified_frequency_hz,
        simplified_relative_frequency_error=simplified_relative_error,
        simplified_corner_max_relative_error=simplified_corner_error,
        full_term_count=full_term_count,
        retained_term_count=retained_term_count,
    )


def derive_local_summing_zero_expression(
    graph: SignalFlowGraph,
    root: SummingPointRoot,
    topology: PaperTopologyMetrics,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    max_paths: int = 64,
    max_loops: int = 64,
    max_vertices: int = 12,
    max_symbolic_operations: int = 5000,
    max_relative_root_error: float = 0.05,
    strategy: str = "auto",
) -> LocalizedSymbolicZero:
    """Explain one closed-loop zero by a local Mason numerator or matrix fallback.

    The two paths attached to ``root`` identify a divergence/reconvergence
    region. All signed forward paths in that region are combined with their
    Mason cofactors, ``N(s)=sum(P_k*Delta_k)``. This is the zero counterpart of
    :func:`derive_local_summing_root_expression`, which builds a pole equation
    from local feedback loops.
    """
    if strategy not in {"auto", "paths", "matrix"}:
        raise ValueError("strategy must be 'auto', 'paths', or 'matrix'.")
    if root.cluster_index is None or "zero" not in root.root_source:
        return _unresolved_local_zero(root, "root is not a clustered summing-point zero")
    region = _local_zero_region_from_path_pair(root.path_edges, root.vertex)
    if region is None:
        return _unresolved_local_zero(root, "could not derive a divergence/reconvergence region")
    start, end, vertices = region
    cut_kind = _local_zero_cut_kind(topology, start, end, vertices)
    cancellation = _local_zero_cancellation_residual(
        graph,
        root.vertex,
        root.value,
        substitutions or {},
    )
    if len(vertices) > max_vertices:
        return _unresolved_local_zero(
            root,
            f"local region has {len(vertices)} vertices; limit is {max_vertices}",
            start=start,
            end=end,
            cut_kind=cut_kind,
            cancellation_residual=cancellation,
        )

    path_failure: str | None = None
    if strategy != "matrix":
        candidate, path_failure = _local_zero_from_mason_paths(
            graph,
            root,
            start,
            end,
            vertices,
            substitutions or {},
            max_paths=max_paths,
            max_loops=max_loops,
            max_symbolic_operations=max_symbolic_operations,
        )
        if candidate is not None:
            equation, expression, numeric_value, paths, equivalent_transfer = candidate
            return _finalize_local_zero(
                root,
                expression,
                equation,
                numeric_value,
                paths,
                start,
                end,
                cut_kind,
                cancellation,
                "local Mason numerator",
                max_relative_root_error,
                equivalent_transfer=equivalent_transfer,
                parameter_participation=tuple(
                    _normalized_root_parameter_sensitivities(expression, substitutions or {})[:8]
                ),
            )
        if strategy == "paths":
            return _unresolved_local_zero(
                root,
                path_failure or "local Mason numerator failed",
                start=start,
                end=end,
                cut_kind=cut_kind,
                cancellation_residual=cancellation,
            )

    matrix_candidate, matrix_failure = _local_zero_from_system_matrix(
        graph,
        root,
        start,
        end,
        vertices,
        substitutions or {},
        max_symbolic_operations=max_symbolic_operations,
    )
    if matrix_candidate is not None:
        equation, expression, numeric_value, equivalent_transfer, participation = matrix_candidate
        return _finalize_local_zero(
            root,
            expression,
            equation,
            numeric_value,
            (),
            start,
            end,
            cut_kind,
            cancellation,
            "local Rosenbrock-style system matrix",
            max_relative_root_error,
            equivalent_transfer=equivalent_transfer,
            parameter_participation=participation,
        )
    reasons = "; ".join(value for value in (path_failure, matrix_failure) if value)
    return _unresolved_local_zero(
        root,
        reasons or "no local zero expression found",
        start=start,
        end=end,
        cut_kind=cut_kind,
        cancellation_residual=cancellation,
    )


def _local_zero_from_mason_paths(
    graph: SignalFlowGraph,
    root: SummingPointRoot,
    start: str,
    end: str,
    vertices: frozenset[str],
    substitutions: dict[str | sp.Symbol, Any],
    max_paths: int,
    max_loops: int,
    max_symbolic_operations: int,
) -> tuple[
    tuple[sp.Expr, sp.Expr, complex, tuple[LocalZeroPath, ...], sp.Expr] | None,
    str | None,
]:
    """Build ``sum(P_k*Delta_k)`` in one bounded local graph region."""
    adjacency, _reverse = _graph_adjacency(graph)
    induced = {
        vertex: set(adjacency.get(vertex, set())) & set(vertices)
        for vertex in vertices
    }
    edge_lookup = _edge_lookup(graph)
    paths = _enumerate_forward_paths(induced, edge_lookup, start, end, max_paths=max_paths + 1)
    if len(paths) < 2:
        return None, "local region contains fewer than two forward paths"
    if len(paths) > max_paths:
        return None, f"local path count exceeds {max_paths}"
    loops = _enumerate_feedback_loops(induced, edge_lookup, max_loops=max_loops + 1)
    if len(loops) > max_loops:
        return None, f"local feedback-loop count exceeds {max_loops}"
    loop_gains = tuple(_feedback_loop_gain(graph, loop) for loop in loops)
    full_delta = _mason_characteristic_equation(tuple(loops), loop_gains)
    loop_vertex_sets = tuple(set(loop.vertices[:-1]) for loop in loops)
    local_paths: list[LocalZeroPath] = []
    for path in paths:
        gain = _signed_path_gain(graph, path.edges)
        path_vertices = set(path.vertices)
        retained_indices = tuple(
            index
            for index, loop_vertices in enumerate(loop_vertex_sets)
            if not path_vertices.intersection(loop_vertices)
        )
        retained_loops = tuple(loops[index] for index in retained_indices)
        retained_gains = tuple(loop_gains[index] for index in retained_indices)
        cofactor = _mason_characteristic_equation(retained_loops, retained_gains)
        local_paths.append(
            LocalZeroPath(
                vertices=path.vertices,
                edges=path.edges,
                gain=sp.factor(sp.cancel(gain)),
                mason_cofactor=sp.factor(sp.cancel(cofactor)),
                weighted_gain=sp.factor(sp.cancel(gain * cofactor)),
            )
        )
    summed = sp.Add(*(item.weighted_gain for item in local_paths))
    numerator, _denominator = sp.cancel(sp.together(summed)).as_numer_denom()
    equation = sp.factor(sp.simplify(numerator))
    if equation == 0:
        return None, "local Mason numerator cancels identically"
    if int(sp.count_ops(equation)) > max_symbolic_operations:
        return None, f"local Mason numerator exceeds {max_symbolic_operations} operations"
    expression = _nearest_symbolic_root_expression(equation, root.value, substitutions)
    if expression is None:
        return None, "local Mason numerator has no readable matching root"
    numeric_value = _numeric_root_value(expression, substitutions)
    if numeric_value is None:
        return None, "local Mason root could not be evaluated numerically"
    equivalent_transfer = sp.factor(sp.cancel(summed / full_delta))
    return (equation, expression, numeric_value, tuple(local_paths), equivalent_transfer), None


def _local_zero_from_system_matrix(
    graph: SignalFlowGraph,
    root: SummingPointRoot,
    start: str,
    end: str,
    vertices: frozenset[str],
    substitutions: dict[str | sp.Symbol, Any],
    max_symbolic_operations: int,
) -> tuple[
    tuple[sp.Expr, sp.Expr, complex, sp.Expr, tuple[tuple[str, float], ...]] | None,
    str | None,
]:
    """Extract a local transfer numerator from an augmented SFG system matrix."""
    states = tuple(sorted(set(vertices) - {start}))
    if end not in states:
        return None, "local matrix output is not an internal state"
    indices = {name: index for index, name in enumerate(states)}
    matrix = sp.eye(len(states))
    input_vector = sp.zeros(len(states), 1)
    for edge in graph.meta_edges.values():
        if edge.target not in indices or edge.source not in vertices:
            continue
        if edge.domain == "voltage":
            return None, "local matrix fallback does not absorb voltage constraints"
        row = indices[edge.target]
        value = sp.sympify(edge.value)
        if edge.source == start:
            input_vector[row, 0] += value
        elif edge.source in indices:
            matrix[row, indices[edge.source]] -= value
    detector = sp.zeros(1, len(states))
    detector[0, indices[end]] = 1
    augmented = matrix.row_join(-input_vector).col_join(
        detector.row_join(sp.zeros(1, 1))
    )
    try:
        determinant = augmented.det(method="bareiss")
        state_determinant = matrix.det(method="bareiss")
    except Exception as error:
        return None, f"local system-matrix determinant failed: {error}"
    numerator, _denominator = sp.cancel(sp.together(determinant)).as_numer_denom()
    equation = sp.factor(sp.simplify(numerator))
    if equation == 0:
        return None, "local system-matrix numerator cancels identically"
    if int(sp.count_ops(equation)) > max_symbolic_operations:
        return None, f"local system-matrix numerator exceeds {max_symbolic_operations} operations"
    expression = _nearest_symbolic_root_expression(equation, root.value, substitutions)
    if expression is None:
        return None, "local system matrix has no readable matching root"
    numeric_value = _numeric_root_value(expression, substitutions)
    if numeric_value is None:
        return None, "local system-matrix root could not be evaluated numerically"
    if state_determinant == 0:
        return None, "local state matrix is structurally singular"
    equivalent_transfer = sp.factor(sp.cancel(determinant / state_determinant))
    participation = _local_system_matrix_parameter_participation(
        augmented,
        root.value,
        substitutions,
    )
    return (equation, expression, numeric_value, equivalent_transfer, participation), None


def _local_zero_region_from_path_pair(
    path_pair: tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]],
    end_vertex: str,
) -> tuple[str, str, frozenset[str]] | None:
    """Return the smallest divergence/reconvergence region implied by two paths."""
    left, right = path_pair
    if not left or not right:
        return None
    common = 0
    while common < min(len(left), len(right)) and left[common] == right[common]:
        common += 1
    if common == len(left) or common == len(right):
        return None
    start = left[common][0]
    left_suffix = left[common:]
    right_suffix = right[common:]
    if left_suffix[-1][1] != end_vertex or right_suffix[-1][1] != end_vertex:
        return None
    vertices = {start, end_vertex}
    for source, target, _domain in left_suffix + right_suffix:
        vertices.update((source, target))
    return start, end_vertex, frozenset(vertices)


def _local_zero_cut_kind(
    topology: PaperTopologyMetrics,
    start: str,
    end: str,
    vertices: frozenset[str],
) -> str:
    """Return the strongest graph-cut classification matching one local region."""
    matches = [
        cut
        for cut in topology.graph_cuts
        if cut.start_vertex == start
        and cut.end_vertex == end
        and vertices.issubset(cut.vertices)
    ]
    if not matches:
        return "fallback"
    if any(cut.cut_kind == "complementary" for cut in matches):
        return "complementary"
    return min(matches, key=lambda cut: cut.length).cut_kind


def _signed_path_gain(
    graph: SignalFlowGraph,
    edges: Iterable[tuple[str, str, str]],
) -> sp.Expr:
    """Multiply signed meta-edge gains along one path."""
    gain = sp.Integer(1)
    for key in edges:
        edge = graph.meta_edges.get(key)
        if edge is None:
            return sp.Integer(0)
        gain *= sp.sympify(edge.value)
    return sp.cancel(gain)


def _local_zero_cancellation_residual(
    graph: SignalFlowGraph,
    vertex: str,
    laplace_value: complex,
    substitutions: dict[str | sp.Symbol, Any],
) -> float | None:
    """Measure cancellation of actual incoming signals at a target complex root."""
    try:
        values = _numeric_graph_vertex_values_at_s(graph, laplace_value, substitutions)
        contributions: list[complex] = []
        for edge in graph.meta_edges.values():
            if edge.target != vertex or edge.domain == "voltage":
                continue
            source_value = values.get(edge.source, 0.0 + 0.0j)
            edge_value = _numeric_sfg_expression_at_s(edge.value, laplace_value, substitutions)
            contributions.append(source_value * edge_value)
        scale = sum(abs(value) for value in contributions)
        if scale <= 1e-300:
            return None
        return float(abs(sum(contributions)) / scale)
    except Exception:
        return None


def _numeric_graph_vertex_values_at_s(
    graph: SignalFlowGraph,
    laplace_value: complex,
    substitutions: dict[str | sp.Symbol, Any],
) -> dict[str, complex]:
    """Solve every SFG vertex at an arbitrary complex Laplace value."""
    names = sorted(graph.vertices)
    indices = {name: index for index, name in enumerate(names)}
    source_vertices = {item.vertex_name for item in graph.source_drives}
    rows: list[tuple[str, str, tuple[MetaEdge, ...]]] = []
    for item in graph.source_drives:
        if item.vertex_name in indices:
            rows.append(("source", item.vertex_name, ()))
    incoming: dict[str, list[MetaEdge]] = {}
    for edge in graph.meta_edges.values():
        incoming.setdefault(edge.target, []).append(edge)
    for target, edges in incoming.items():
        if target not in indices or target in source_vertices:
            continue
        signal_edges = tuple(edge for edge in edges if edge.domain != "voltage")
        voltage_edges = tuple(edge for edge in edges if edge.domain == "voltage")
        if signal_edges:
            rows.append(("equation", target, signal_edges))
        if voltage_edges:
            rows.append(("equation", target, voltage_edges))
    for name in _analysis_kcl_zero_vertices(graph):
        rows.append(("zero", name, ()))
    matrix = np.zeros((len(rows), len(names)), dtype=complex)
    vector = np.zeros(len(rows), dtype=complex)
    source_by_vertex = {item.vertex_name: item for item in graph.source_drives}
    for row_index, (kind, target, edges) in enumerate(rows):
        matrix[row_index, indices[target]] = 1.0
        if kind == "source":
            vector[row_index] = _numeric_sfg_expression_at_s(
                source_by_vertex[target].value,
                laplace_value,
                substitutions,
            )
        elif kind == "equation":
            for edge in edges:
                if edge.source in indices:
                    matrix[row_index, indices[edge.source]] -= _numeric_sfg_expression_at_s(
                        edge.value,
                        laplace_value,
                        substitutions,
                    )
    if matrix.shape[0] == matrix.shape[1]:
        solution = np.linalg.solve(matrix, vector)
    else:
        solution, *_ = np.linalg.lstsq(matrix, vector, rcond=None)
    return {name: complex(solution[index]) for name, index in indices.items()}


def _numeric_sfg_expression_at_s(
    expression: Any,
    laplace_value: complex,
    substitutions: dict[str | sp.Symbol, Any],
) -> complex:
    """Evaluate one symbolic SFG gain at an arbitrary complex ``s``."""
    numeric = evaluate_numeric_expression(expression, substitutions, keep_laplace=True).numeric
    return complex(sp.N(numeric.subs(ini.laplace, laplace_value)))


def _local_system_matrix_parameter_participation(
    matrix: sp.Matrix,
    root_value: complex,
    substitutions: dict[str | sp.Symbol, Any],
    limit: int = 8,
) -> tuple[tuple[str, float], ...]:
    """Rank parameters with left/right null-vector zero sensitivities."""
    parsed = _parse_substitutions(substitutions)
    parameters = sorted(
        set().union(*(entry.free_symbols for entry in matrix)) - {ini.laplace},
        key=str,
    )
    if not parameters:
        return ()
    try:
        numeric = np.asarray(
            sp.Matrix(matrix).subs(parsed).subs(ini.laplace, root_value).evalf().tolist(),
            dtype=complex,
        )
        left_vectors, _singular_values, right_h = np.linalg.svd(numeric)
        left = left_vectors[:, -1]
        right = right_h.conj().T[:, -1]
        derivative_s = np.asarray(
            matrix.diff(ini.laplace).subs(parsed).subs(ini.laplace, root_value).evalf().tolist(),
            dtype=complex,
        )
        denominator = np.vdot(left, derivative_s @ right)
        if abs(denominator) <= 1e-300:
            return ()
        rows: list[tuple[str, float]] = []
        for parameter in parameters:
            derivative_p = np.asarray(
                matrix.diff(parameter).subs(parsed).subs(ini.laplace, root_value).evalf().tolist(),
                dtype=complex,
            )
            dz_dp = -np.vdot(left, derivative_p @ right) / denominator
            nominal = complex(sp.N(parsed.get(parameter, parameter)))
            normalized = abs(dz_dp * nominal / max(abs(root_value), 1e-300))
            if np.isfinite(normalized):
                rows.append((str(parameter), float(normalized)))
        rows.sort(key=lambda item: item[1], reverse=True)
        return tuple(rows[:limit])
    except Exception:
        return ()


def _finalize_local_zero(
    root: SummingPointRoot,
    expression: sp.Expr,
    equation: sp.Expr,
    numeric_value: complex,
    paths: tuple[LocalZeroPath, ...],
    start: str,
    end: str,
    cut_kind: str,
    cancellation_residual: float | None,
    method: str,
    max_relative_root_error: float,
    equivalent_transfer: sp.Expr | None = None,
    parameter_participation: tuple[tuple[str, float], ...] = (),
) -> LocalizedSymbolicZero:
    """Apply root-error and half-plane acceptance to one local zero candidate."""
    error = _relative_complex_root_error(numeric_value, root.value)
    half_plane_ok = _same_root_half_plane(numeric_value, root.value)
    status = "resolved" if error <= max_relative_root_error and half_plane_ok else "outside_error_limit"
    reason = None
    if not half_plane_ok:
        reason = "candidate changes the zero half-plane"
    elif error > max_relative_root_error:
        reason = f"relative root error {error:.6e} exceeds {max_relative_root_error:.6e}"
    return LocalizedSymbolicZero(
        cluster_index=int(root.cluster_index or 0),
        vertex=root.vertex,
        expression=sp.factor(sp.simplify(expression)),
        characteristic_equation=sp.factor(sp.simplify(equation)),
        numeric_value=numeric_value,
        frequency_hz=abs(numeric_value) / (2 * np.pi),
        reference_value=root.value,
        relative_root_error=error,
        cut_start=start,
        cut_end=end,
        cut_kind=cut_kind,
        paths=paths,
        cancellation_residual=cancellation_residual,
        method=method,
        equivalent_transfer=equivalent_transfer,
        parameter_participation=parameter_participation,
        parameters=_root_expression_parameters(expression),
        status=status,
        failure_reason=reason,
    )


def _same_root_half_plane(candidate: complex, reference: complex) -> bool:
    """Return True when a candidate preserves the target root half-plane."""
    scale = max(abs(candidate), abs(reference), 1.0)
    tolerance = 1e-10 * scale
    if abs(candidate.real) <= tolerance or abs(reference.real) <= tolerance:
        return True
    return (candidate.real > 0) == (reference.real > 0)


def _unresolved_local_zero(
    root: SummingPointRoot,
    reason: str,
    start: str | None = None,
    end: str | None = None,
    cut_kind: str = "none",
    cancellation_residual: float | None = None,
) -> LocalizedSymbolicZero:
    """Create an auditable unresolved local-zero result."""
    return LocalizedSymbolicZero(
        cluster_index=int(root.cluster_index or 0),
        vertex=root.vertex,
        expression=None,
        characteristic_equation=None,
        numeric_value=None,
        frequency_hz=None,
        reference_value=root.value,
        relative_root_error=None,
        cut_start=start,
        cut_end=end,
        cut_kind=cut_kind,
        paths=(),
        cancellation_residual=cancellation_residual,
        method="unresolved local zero",
        equivalent_transfer=None,
        parameter_participation=(),
        status="unresolved",
        failure_reason=reason,
    )


def validate_localized_zero_corners(
    zero: LocalizedSymbolicZero,
    reference_numerator: sp.Expr,
    substitutions: dict[str | sp.Symbol, Any],
    parameter_tolerances: dict[str | sp.Symbol, float] | None,
    max_corner_parameters: int = 6,
    max_relative_root_error: float = 0.05,
) -> LocalizedSymbolicZero:
    """Validate one local zero against full-transfer zeroes at explicit corners."""
    if (
        zero.expression is None
        or zero.status == "unresolved"
        or not parameter_tolerances
    ):
        return zero
    sensitivities = _normalized_root_parameter_sensitivities(
        zero.expression,
        substitutions,
    )
    corners = _parameter_corner_substitutions(
        substitutions,
        parameter_tolerances,
        sensitivities,
        default_tolerance=None,
        max_corner_parameters=max_corner_parameters,
    )
    if not corners:
        return zero
    laplace = _laplace_symbol_for(reference_numerator) or ini.laplace
    errors: list[float] = []
    for corner in corners:
        candidate = _numeric_root_value(zero.expression, corner)
        if candidate is None:
            errors.append(math.inf)
            continue
        roots = _numeric_roots_for_polynomial(
            reference_numerator,
            laplace,
            corner,
        )
        same_half_plane = [root for root in roots if _same_root_half_plane(root, candidate)]
        if not same_half_plane:
            errors.append(math.inf)
            continue
        reference = min(same_half_plane, key=lambda value: abs(value - candidate))
        errors.append(_relative_complex_root_error(candidate, reference))
    corner_error = max(errors, default=None)
    if corner_error is None or corner_error <= max_relative_root_error:
        return replace(zero, corner_max_relative_error=corner_error)
    reason = (
        f"parameter-corner root error {corner_error:.6e} exceeds "
        f"{max_relative_root_error:.6e}"
    )
    return replace(
        zero,
        corner_max_relative_error=corner_error,
        status="outside_error_limit",
        failure_reason=reason,
    )


def _local_characteristic_candidate(
    loops: tuple[FeedbackLoop, ...],
    loop_gains: tuple[sp.Expr, ...],
    root: SummingPointRoot,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[sp.Expr, sp.Expr, complex, float, float] | None:
    """Build and evaluate one local-loop subset as a symbolic root candidate."""
    delta = _mason_characteristic_equation(loops, loop_gains)
    polynomial_expression = sp.together(delta).as_numer_denom()[0]
    laplace = _laplace_symbol_for(polynomial_expression) or ini.laplace
    try:
        polynomial = sp.Poly(sp.expand(polynomial_expression), laplace)
    except sp.PolynomialError:
        return None
    if polynomial.degree() <= 0:
        return None
    physical_equation = _physical_local_characteristic(delta, loop_gains)
    expression = _nearest_local_characteristic_root(
        physical_equation,
        polynomial,
        root.value,
        substitutions,
    )
    if expression is None:
        return None
    numeric_value = _numeric_root_value(expression, substitutions)
    if numeric_value is None:
        return None
    frequency_hz = abs(numeric_value) / (2 * np.pi)
    if root.frequency_hz <= 0:
        return None
    relative_error = abs(frequency_hz - root.frequency_hz) / root.frequency_hz
    return physical_equation, expression, numeric_value, frequency_hz, relative_error


def _normalized_root_parameter_sensitivities(
    expression: sp.Expr,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> list[tuple[str, float]]:
    """Rank parameters by the normalized differential sensitivity of a local root."""
    substitution_map = _symbol_substitution_map(substitutions)
    expression = sp.sympify(expression)
    sensitivities: list[tuple[str, float]] = []
    for symbol in sorted(expression.free_symbols, key=str):
        if str(symbol) == str(ini.laplace) or symbol not in substitution_map:
            continue
        nominal = substitution_map[symbol]
        if nominal == 0:
            continue
        try:
            normalized = sp.diff(expression, symbol) * symbol / expression
            value = complex(sp.N(normalized.subs(substitution_map)))
        except Exception:
            continue
        magnitude = abs(value)
        if np.isfinite(magnitude):
            sensitivities.append((str(symbol), float(magnitude)))
    return sorted(sensitivities, key=lambda item: (-item[1], item[0]))


def _parameter_corner_substitutions(
    substitutions: dict[str | sp.Symbol, Any] | None,
    tolerances: dict[str | sp.Symbol, float] | None,
    sensitivities: Iterable[tuple[str, float]],
    default_tolerance: float | None,
    max_corner_parameters: int,
) -> tuple[dict[sp.Symbol, sp.Expr], ...]:
    """Create sensitivity-prioritized min/max parameter corners around the design point."""
    if max_corner_parameters <= 0:
        return ()
    nominal = _symbol_substitution_map(substitutions)
    sensitivity_order = [sp.Symbol(name) for name, _value in sensitivities]
    default_value = 0.0 if default_tolerance is None else abs(float(default_tolerance))
    tolerance_map = {
        symbol: default_value
        for symbol in sensitivity_order
        if symbol in nominal and default_value > 0
    }
    tolerance_map.update(
        {
            sp.Symbol(str(key)): abs(float(value))
            for key, value in (tolerances or {}).items()
            if abs(float(value)) > 0
        }
    )
    if not tolerance_map:
        return ()
    selected = [
        symbol
        for symbol in sensitivity_order
        if symbol in tolerance_map and symbol in nominal and tolerance_map[symbol] > 0
    ][:max_corner_parameters]
    if not selected:
        return ()
    corners: list[dict[sp.Symbol, sp.Expr]] = []
    for signs in product((-1.0, 1.0), repeat=len(selected)):
        corner = dict(nominal)
        for symbol, sign in zip(selected, signs):
            corner[symbol] = nominal[symbol] * (1.0 + sign * tolerance_map[symbol])
        corners.append(corner)
    return tuple(corners)


def _root_corner_relative_error(
    candidate: sp.Expr,
    reference: sp.Expr,
    corners: Iterable[dict[sp.Symbol, sp.Expr]],
    reference_values: tuple[complex, ...] | None = None,
) -> float | None:
    """Return the worst complex root error over supplied parameter corners."""
    corner_maps = tuple(corners)
    if not corner_maps:
        return None
    if reference_values is None:
        reference_values = _root_corner_values(reference, corner_maps)
    if reference_values is None or len(reference_values) != len(corner_maps):
        return math.inf
    candidate_values = _root_corner_values(candidate, corner_maps)
    if candidate_values is None:
        return math.inf
    errors: list[float] = []
    for candidate_value, reference_value in zip(candidate_values, reference_values):
        denominator = max(abs(reference_value), 1e-300)
        error = abs(candidate_value - reference_value) / denominator
        if not np.isfinite(error):
            return math.inf
        errors.append(float(error))
    return None if not errors else max(errors)


def _root_corner_values(
    expression: sp.Expr,
    corners: Iterable[dict[sp.Symbol, sp.Expr]],
) -> tuple[complex, ...] | None:
    """Evaluate one symbolic root once at each supplied parameter corner."""
    values: list[complex] = []
    for corner in corners:
        try:
            value = complex(sp.N(sp.sympify(expression).subs(corner)))
        except Exception:
            return None
        if not np.isfinite(value.real) or not np.isfinite(value.imag):
            return None
        values.append(value)
    return tuple(values)


def _rational_additive_terms(expression: sp.Expr) -> tuple[list[sp.Expr], list[sp.Expr]]:
    """Expand a rational root expression into numerator and denominator product terms."""
    numerator, denominator = sp.cancel(sp.sympify(expression)).as_numer_denom()
    return (
        list(sp.Add.make_args(sp.expand(numerator))),
        list(sp.Add.make_args(sp.expand(denominator))),
    )


def _rational_additive_term_count(expression: sp.Expr) -> int:
    """Count additive product terms in a rational symbolic root expression."""
    numerator_terms, denominator_terms = _rational_additive_terms(expression)
    return len(numerator_terms) + len(denominator_terms)


def _prune_symbolic_root_terms(
    expression: sp.Expr,
    reference_expression: sp.Expr,
    root: SummingPointRoot,
    substitutions: dict[str | sp.Symbol, Any] | None,
    corners: Iterable[dict[sp.Symbol, sp.Expr]],
    max_total_relative_frequency_error: float,
    max_term_relative_error: float,
    max_total_corner_relative_error: float,
    max_term_corner_relative_error: float,
    max_steps: int,
    max_terms: int,
) -> tuple[sp.Expr, complex, float, float, float | None, int, int] | None:
    """Greedily remove non-dominant product terms under design-point and corner limits.

    This is a postprocessing step: it never changes the selected local loops.
    Each trial removes one expanded additive term from the root numerator or
    denominator and is accepted only when the resulting root remains within the
    supplied error limits.
    """
    numerator_terms, denominator_terms = _rational_additive_terms(expression)
    full_term_count = len(numerator_terms) + len(denominator_terms)
    if full_term_count <= 2 or full_term_count > max_terms or max_steps <= 0:
        return None

    corner_maps = tuple(corners)
    current_expression = sp.factor(sp.Add(*numerator_terms)) / sp.factor(
        sp.Add(*denominator_terms)
    )
    current_value = _numeric_root_value(current_expression, substitutions)
    if current_value is None or root.frequency_hz <= 0:
        return None
    source_value = current_value
    current_frequency = abs(current_value) / (2 * np.pi)
    current_error = abs(current_frequency - root.frequency_hz) / root.frequency_hz
    full_reference_values = _root_corner_values(reference_expression, corner_maps)
    source_reference_values = _root_corner_values(expression, corner_maps)
    current_corner_error = _root_corner_relative_error(
        current_expression,
        reference_expression,
        corner_maps,
        reference_values=full_reference_values,
    )

    for _step in range(max_steps):
        trials: list[
            tuple[
                int,
                float,
                float | None,
                list[sp.Expr],
                list[sp.Expr],
                sp.Expr,
                complex,
                float,
            ]
        ] = []
        for side, terms in (("numerator", numerator_terms), ("denominator", denominator_terms)):
            if len(terms) <= 1:
                continue
            for index in range(len(terms)):
                trial_numerator = list(numerator_terms)
                trial_denominator = list(denominator_terms)
                if side == "numerator":
                    trial_numerator.pop(index)
                else:
                    trial_denominator.pop(index)
                trial_expression = sp.factor(sp.Add(*trial_numerator)) / sp.factor(
                    sp.Add(*trial_denominator)
                )
                trial_value = _numeric_root_value(trial_expression, substitutions)
                if trial_value is None:
                    continue
                trial_frequency = abs(trial_value) / (2 * np.pi)
                trial_error = abs(trial_frequency - root.frequency_hz) / root.frequency_hz
                term_error = abs(trial_value - source_value) / max(abs(source_value), 1e-300)
                if (
                    trial_error > max_total_relative_frequency_error
                    or term_error > max_term_relative_error
                ):
                    continue
                trial_corner_error = _root_corner_relative_error(
                    trial_expression,
                    reference_expression,
                    corner_maps,
                    reference_values=full_reference_values,
                )
                if (
                    trial_corner_error is not None
                    and trial_corner_error > max_total_corner_relative_error
                ):
                    continue
                term_corner_error = _root_corner_relative_error(
                    trial_expression,
                    expression,
                    corner_maps,
                    reference_values=source_reference_values,
                )
                if (
                    term_corner_error is not None
                    and term_corner_error > max_term_corner_relative_error
                ):
                    continue
                trials.append(
                    (
                        int(sp.count_ops(trial_expression)),
                        trial_error,
                        trial_corner_error,
                        trial_numerator,
                        trial_denominator,
                        trial_expression,
                        trial_value,
                        trial_frequency,
                    )
                )
        if not trials:
            break
        best = min(
            trials,
            key=lambda item: (
                item[0],
                max(item[1], item[2] or 0.0),
            ),
        )
        (
            _complexity,
            current_error,
            current_corner_error,
            numerator_terms,
            denominator_terms,
            current_expression,
            current_value,
            current_frequency,
        ) = best

    retained_term_count = len(numerator_terms) + len(denominator_terms)
    if retained_term_count >= full_term_count:
        return None
    return (
        sp.factor(current_expression),
        current_value,
        current_frequency,
        current_error,
        current_corner_error,
        full_term_count,
        retained_term_count,
    )


def _strongly_connected_component(graph: SignalFlowGraph, vertex: str) -> set[str]:
    """Return the maximal strongly connected vertex set containing ``vertex``."""
    adjacency, reverse_adjacency = _graph_adjacency(graph)
    if vertex not in adjacency:
        return set()
    forward = _reachable_vertices(adjacency, {vertex})
    backward = _reachable_vertices(reverse_adjacency, {vertex})
    return forward & backward


def _feedback_loop_gain(graph: SignalFlowGraph, loop: FeedbackLoop) -> sp.Expr:
    """Return one loop gain while summing any parallel edges on each hop."""
    gain = sp.Integer(1)
    for source, target in zip(loop.vertices, loop.vertices[1:]):
        parallel = [
            sp.sympify(edge.value)
            for edge in graph.meta_edges.values()
            if edge.source == source and edge.target == target
        ]
        if not parallel:
            return sp.Integer(0)
        gain *= sp.Add(*parallel)
    return sp.factor(sp.cancel(gain))


def _mason_characteristic_equation(
    loops: tuple[FeedbackLoop, ...] | list[FeedbackLoop],
    loop_gains: tuple[sp.Expr, ...],
) -> sp.Expr:
    """Build Mason's ``Delta=1-sum(L)+sum(L_i L_j)-...`` for local loops."""
    terms: list[sp.Expr] = [sp.Integer(1)]
    loop_vertices = [set(loop.vertices[:-1]) for loop in loops]
    for count in range(1, len(loops) + 1):
        for indices in combinations(range(len(loops)), count):
            used: set[str] = set()
            disjoint = True
            for index in indices:
                if used.intersection(loop_vertices[index]):
                    disjoint = False
                    break
                used.update(loop_vertices[index])
            if not disjoint:
                continue
            product = sp.prod(loop_gains[index] for index in indices)
            terms.append(product if count % 2 == 0 else -product)
    return sp.Add(*terms, evaluate=False)


def _physical_local_characteristic(
    delta: sp.Expr,
    loop_gains: tuple[sp.Expr, ...],
) -> sp.Expr:
    """Keep low-order local characteristics in a physically grouped form."""
    if len(loop_gains) == 1:
        numerator, denominator = sp.cancel(loop_gains[0]).as_numer_denom()
        return sp.Add(sp.factor(denominator), -sp.factor(numerator), evaluate=False)
    if len(loop_gains) == 2:
        laplace = _laplace_symbol_for(delta) or ini.laplace
        static = [gain for gain in loop_gains if laplace not in gain.free_symbols]
        dynamic = [gain for gain in loop_gains if laplace in gain.free_symbols]
        if len(static) == 1 and len(dynamic) == 1:
            numerator, denominator = sp.cancel(dynamic[0]).as_numer_denom()
            background = sp.Add(sp.Integer(1), -static[0], evaluate=False)
            return sp.Add(
                sp.Mul(background, sp.factor(denominator), evaluate=False),
                -sp.factor(numerator),
                evaluate=False,
            )
    return delta


def _nearest_local_characteristic_root(
    physical_equation: sp.Expr,
    polynomial: sp.Poly,
    reference_value: complex,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> sp.Expr | None:
    """Select the local characteristic root nearest to one closed-loop root."""
    laplace = polynomial.gens[0]
    candidates: list[tuple[float, sp.Expr]] = []
    if polynomial.degree() == 1 and sp.sympify(physical_equation).is_polynomial(laplace):
        constant = physical_equation.subs(laplace, 0)
        slope = sp.diff(physical_equation, laplace).subs(laplace, 0)
        if slope != 0:
            expression = -constant / slope
            numeric = _numeric_root_value(expression, substitutions)
            if numeric is not None:
                candidates.append((abs(numeric - reference_value), expression))
    if not candidates:
        for item in _symbolic_roots_from_polynomial(
            polynomial.as_expr(),
            "pole",
            substitutions=substitutions,
            max_symbolic_degree=2,
        ):
            if item.expression is None or item.numeric_value is None:
                continue
            candidates.append((abs(item.numeric_value - reference_value), item.expression))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def detect_summing_point_roots(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    symbolic_expressions: bool = False,
) -> list[SummingPointRoot]:
    """Detect summing-point root candidates by intersecting incoming path signals."""
    substitutions = substitutions or {}
    adjacency, _reverse_adjacency = _graph_adjacency(graph)
    edge_lookup = _edge_lookup(graph)
    incoming_by_target: dict[str, list[tuple[tuple[str, str, str], MetaEdge]]] = {}
    for key, edge in graph.meta_edges.items():
        incoming_by_target.setdefault(edge.target, []).append((key, edge))
    roots: list[SummingPointRoot] = []
    for summing in topology.summing_vertices:
        path_gains: list[tuple[tuple[tuple[str, str, str], ...], sp.Expr, sp.Expr]] = []
        for incoming_key, incoming_edge in incoming_by_target.get(summing.vertex, []):
            source_paths = _enumerate_forward_paths(
                adjacency,
                edge_lookup,
                topology.source_vertex,
                incoming_edge.source,
                max_paths=64,
            )
            for path in source_paths:
                prefix_gain = _path_gain_expr_to_vertex(graph, path, incoming_edge.source, substitutions)
                if prefix_gain is None:
                    continue
                edge_gain = evaluate_numeric_expression(incoming_edge.value, substitutions, keep_laplace=True).numeric
                total_gain = sp.cancel(sp.simplify(prefix_gain * edge_gain))
                if symbolic_expressions:
                    symbolic_prefix_gain = _path_gain_raw_expr_to_vertex(graph, path, incoming_edge.source)
                    symbolic_edge_gain = sp.sympify(incoming_edge.value)
                else:
                    symbolic_prefix_gain = None
                    symbolic_edge_gain = edge_gain
                if symbolic_prefix_gain is None:
                    symbolic_total_gain = total_gain
                else:
                    symbolic_total_gain = sp.factor(symbolic_prefix_gain * symbolic_edge_gain)
                path_gains.append((path.edges + (incoming_key,), total_gain, symbolic_total_gain))
        if len(path_gains) < 2:
            continue
        for left_index in range(len(path_gains)):
            for right_index in range(left_index + 1, len(path_gains)):
                left_edges, left_gain, left_symbolic_gain = path_gains[left_index]
                right_edges, right_gain, right_symbolic_gain = path_gains[right_index]
                difference = sp.simplify(left_gain - right_gain)
                symbolic_difference = (
                    sp.simplify(left_symbolic_gain - right_symbolic_gain)
                    if symbolic_expressions else None
                )
                for root in _roots_from_polynomial_like(difference):
                    if abs(root) == 0:
                        continue
                    symbolic_root = (
                        _nearest_symbolic_root_expression(symbolic_difference, root, substitutions)
                        if symbolic_difference is not None else None
                    )
                    roots.append(
                        SummingPointRoot(
                            vertex=summing.vertex,
                            value=root,
                            frequency_hz=abs(root) / (2 * np.pi),
                            path_edges=(left_edges, right_edges),
                            expression=symbolic_root,
                            crossing_equation=(
                                None if symbolic_difference is None
                                else sp.factor(sp.simplify(symbolic_difference))
                            ),
                        )
                    )
    return _dedupe_summing_point_roots(roots)


def localize_summing_point_roots(
    roots: Iterable[SummingPointRoot],
    clusters: Iterable[RootCluster],
    error_specs: Iterable[ErrorSubSpecification],
) -> list[SummingPointRoot]:
    """Assign summing-point root candidates to root clusters/subranges."""
    clusters = tuple(clusters)
    specs_by_cluster = {spec.cluster_index: spec for spec in error_specs}
    localized: list[SummingPointRoot] = []
    for root in roots:
        cluster, ratio = _match_root_cluster(root.frequency_hz, clusters, specs_by_cluster)
        localized.append(
            replace(
                root,
                cluster_index=None if cluster is None else cluster.index,
                frequency_error_ratio=ratio,
            )
        )
    localized = _mark_dominant_summing_point_roots(localized, clusters)
    localized.sort(key=lambda item: (item.cluster_index is None, item.cluster_index or 0, item.frequency_hz))
    return localized


def localize_closed_loop_summing_roots(
    graph: SignalFlowGraph,
    reference: NumericReferenceResult,
    clusters: Iterable[RootCluster],
    open_loop_localizations: Iterable[RootLocalization],
    topology: PaperTopologyMetrics,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
) -> list[SummingPointRoot]:
    """Localize closed-loop roots not explained by observable open-loop roots at summing vertices."""
    substitutions = substitutions or {}
    clusters = tuple(clusters)
    observable_open_loop: list[RootLocalization] = []
    for item in open_loop_localizations:
        if not item.observable:
            continue
        if item.root_kind == "pole":
            edge_key = (item.source, item.target, item.domain)
            loop_gain = _feedback_loop_gain_envelope_containing_edge(
                graph,
                topology,
                edge_key,
                item.root_frequency_hz,
                substitutions,
            )
            if loop_gain >= 1.0:
                continue
        observable_open_loop.append(item)
    matched_open_loop: set[int] = set()
    roots = [RootSample("pole", complex(value)) for value in reference.poles]
    roots.extend(RootSample("zero", complex(value)) for value in reference.zeros)
    roots.sort(key=lambda item: item.magnitude)
    localized: list[SummingPointRoot] = []
    for root in roots:
        cluster, _ratio = _match_root_cluster(root.frequency_hz, clusters, {})
        if cluster is None:
            continue
        open_loop_match = _nearest_unmatched_open_loop_root(
            root,
            cluster.index,
            observable_open_loop,
            matched_open_loop,
        )
        if open_loop_match is not None:
            matched_open_loop.add(open_loop_match)
            continue
        location = _best_summing_point_takeover(
            graph,
            topology,
            root.frequency_hz,
            substitutions,
        )
        if location is None:
            continue
        vertex, path_edges = location
        localized.append(
            SummingPointRoot(
                vertex=vertex,
                value=root.value,
                frequency_hz=root.frequency_hz,
                path_edges=path_edges,
                cluster_index=cluster.index,
                frequency_error_ratio=0.0,
                observable=True,
                dominant=True,
                root_source=f"closed-loop {root.kind} localized at summing point",
                category="SPR",
            )
        )
    return localized


def _nearest_unmatched_open_loop_root(
    root: RootSample,
    cluster_index: int,
    localizations: list[RootLocalization],
    matched: set[int],
    relative_frequency_tolerance: float = 0.25,
) -> int | None:
    """Match one closed-loop root to the nearest observable open-loop root of the same kind."""
    candidates = [
        (abs(math.log(max(item.root_frequency_hz, 1e-300) / max(root.frequency_hz, 1e-300))), index)
        for index, item in enumerate(localizations)
        if index not in matched
        and item.cluster_index == cluster_index
        and item.root_kind == root.kind
    ]
    if not candidates:
        return None
    _distance, index = min(candidates)
    item = localizations[index]
    relative_difference = abs(item.root_frequency_hz - root.frequency_hz) / max(
        item.root_frequency_hz,
        root.frequency_hz,
        1e-300,
    )
    if relative_difference > relative_frequency_tolerance:
        return None
    return index


def _best_summing_point_takeover(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> tuple[
    str,
    tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]],
] | None:
    """Find the summing vertex whose incoming signals exchange dominance near a root."""
    adjacency, _reverse_adjacency = _graph_adjacency(graph)
    edge_lookup = _edge_lookup(graph)
    incoming: dict[str, list[tuple[tuple[str, str, str], MetaEdge]]] = {}
    for key, edge in graph.meta_edges.items():
        if edge.domain == "voltage" or edge.source.endswith(".src"):
            continue
        incoming.setdefault(edge.target, []).append((key, edge))
    use_state_solver = len(graph.vertices) > 16
    if use_state_solver:
        scale = math.sqrt(10.0)
        sample_frequencies = (
            frequency_hz / scale,
            frequency_hz,
            frequency_hz * scale,
        )
        vertex_samples = tuple(
            _numeric_graph_vertex_values(graph, sample_frequency, substitutions)
            for sample_frequency in sample_frequencies
        )
    best: tuple[
        float,
        str,
        tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]],
    ] | None = None
    for summing in topology.summing_vertices:
        edges = incoming.get(summing.vertex, [])
        for left_index in range(len(edges)):
            for right_index in range(left_index + 1, len(edges)):
                left_key, left_edge = edges[left_index]
                right_key, right_edge = edges[right_index]
                if use_state_solver:
                    left_levels = _incoming_edge_signal_levels(
                        left_edge,
                        sample_frequencies,
                        vertex_samples,
                        substitutions,
                    )
                    right_levels = _incoming_edge_signal_levels(
                        right_edge,
                        sample_frequencies,
                        vertex_samples,
                        substitutions,
                    )
                else:
                    left_levels = _incoming_path_signal_levels(
                        graph,
                        topology.source_vertex,
                        left_edge,
                        frequency_hz,
                        substitutions,
                    )
                    right_levels = _incoming_path_signal_levels(
                        graph,
                        topology.source_vertex,
                        right_edge,
                        frequency_hz,
                        substitutions,
                    )
                if left_levels[1] <= 0 or right_levels[1] <= 0:
                    continue
                amplitude_mismatch = abs(math.log10(left_levels[1] / right_levels[1]))
                left_slope = 20.0 * math.log10(max(left_levels[2], 1e-300) / max(left_levels[0], 1e-300))
                right_slope = 20.0 * math.log10(max(right_levels[2], 1e-300) / max(right_levels[0], 1e-300))
                slope_difference = abs(left_slope - right_slope)
                if slope_difference <= 1e-9:
                    continue
                score = (amplitude_mismatch + 1.0) / slope_difference
                if best is None or score < best[0]:
                    path_pair = (
                        _representative_path_to_incoming(adjacency, edge_lookup, topology.source_vertex, left_key),
                        _representative_path_to_incoming(adjacency, edge_lookup, topology.source_vertex, right_key),
                    )
                    best = (score, summing.vertex, path_pair)
    if best is None:
        return None
    return best[1], best[2]


def _incoming_edge_signal_levels(
    incoming_edge: MetaEdge,
    frequencies_hz: tuple[float, float, float],
    vertex_samples: tuple[dict[str, complex], dict[str, complex], dict[str, complex]],
    substitutions: dict[str | sp.Symbol, Any],
) -> tuple[float, float, float]:
    """Evaluate one edge's actual summing-point contribution at three frequencies."""
    return tuple(
        abs(
            values.get(incoming_edge.source, 0.0 + 0.0j)
            * _numeric_sfg_expression(incoming_edge.value, frequency, substitutions)
        )
        for frequency, values in zip(frequencies_hz, vertex_samples)
    )


def _incoming_path_signal_levels(
    graph: SignalFlowGraph,
    source_vertex: str,
    incoming_edge: MetaEdge,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> tuple[float, float, float]:
    """Evaluate bounded simple-path contributions for small paper-style graphs."""
    scale = math.sqrt(10.0)
    return tuple(
        _incoming_path_signal_magnitude(
            graph,
            source_vertex,
            incoming_edge,
            frequency_hz * factor,
            substitutions,
        )
        for factor in (1.0 / scale, 1.0, scale)
    )


def _incoming_path_signal_magnitude(
    graph: SignalFlowGraph,
    source_vertex: str,
    incoming_edge: MetaEdge,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Sum bounded simple paths feeding one edge, matching the paper example semantics."""
    adjacency, _reverse_adjacency = _graph_adjacency(graph)
    edge_lookup = _edge_lookup(graph)
    paths = _enumerate_forward_paths(
        adjacency,
        edge_lookup,
        source_vertex,
        incoming_edge.source,
        max_paths=64,
    )
    prefix = 1.0 + 0.0j if incoming_edge.source == source_vertex else 0.0 + 0.0j
    for path in paths:
        value = 1.0 + 0.0j
        for edge_key in path.edges:
            edge = graph.meta_edges.get(edge_key)
            if edge is None:
                value = 0.0 + 0.0j
                break
            value *= _numeric_sfg_expression(edge.value, frequency_hz, substitutions)
        prefix += value
    edge_value = _numeric_sfg_expression(incoming_edge.value, frequency_hz, substitutions)
    return abs(prefix * edge_value)


def _numeric_graph_vertex_values(
    graph: SignalFlowGraph,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> dict[str, complex]:
    """Solve all SFG vertex values once at a selected frequency."""
    names = sorted(graph.vertices)
    indices = {name: index for index, name in enumerate(names)}
    source_vertices = {item.vertex_name for item in graph.source_drives}
    rows: list[tuple[str, str | None, tuple[MetaEdge, ...]]] = []
    for item in graph.source_drives:
        if item.vertex_name in indices:
            rows.append(("source", item.vertex_name, ()))
    incoming: dict[str, list[MetaEdge]] = {}
    for edge in graph.meta_edges.values():
        incoming.setdefault(edge.target, []).append(edge)
    for target, edges in incoming.items():
        if target not in indices or target in source_vertices:
            continue
        voltage_edges = tuple(edge for edge in edges if edge.domain == "voltage")
        signal_edges = tuple(edge for edge in edges if edge.domain != "voltage")
        if signal_edges:
            rows.append(("equation", target, signal_edges))
        if voltage_edges:
            rows.append(("equation", target, voltage_edges))
    for name in _analysis_kcl_zero_vertices(graph):
        rows.append(("zero", name, ()))

    matrix = np.zeros((len(rows), len(names)), dtype=complex)
    vector = np.zeros(len(rows), dtype=complex)
    source_by_vertex = {item.vertex_name: item for item in graph.source_drives}
    for row_index, (kind, target, edges) in enumerate(rows):
        if target is None:
            continue
        target_index = indices[target]
        matrix[row_index, target_index] = 1.0
        if kind == "source":
            vector[row_index] = _numeric_sfg_expression(
                source_by_vertex[target].value,
                frequency_hz,
                substitutions,
            )
        elif kind == "equation":
            for edge in edges:
                if edge.source in indices:
                    matrix[row_index, indices[edge.source]] -= _numeric_sfg_expression(
                        edge.value,
                        frequency_hz,
                        substitutions,
                    )
    if matrix.shape[0] == matrix.shape[1]:
        solution = np.linalg.solve(matrix, vector)
    else:
        solution, *_ = np.linalg.lstsq(matrix, vector, rcond=None)
    return {name: complex(solution[index]) for name, index in indices.items()}


def _analysis_kcl_zero_vertices(graph: SignalFlowGraph) -> tuple[str, ...]:
    """Return current vertices constrained to zero by ideal voltage relations."""
    impedance_sources = {
        edge.source
        for edge in graph.meta_edges.values()
        if edge.domain == "impedance"
    }
    return tuple(
        name
        for name, vertex in sorted(graph.vertices.items())
        if vertex.kind == "current"
        and name.endswith(".i")
        and name not in impedance_sources
    )


def _numeric_sfg_expression(
    expression: Any,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> complex:
    """Evaluate one SFG gain at ``s=j2*pi*f``."""
    numeric = evaluate_numeric_expression(
        expression,
        substitutions,
        keep_laplace=True,
    ).numeric
    numeric = numeric.subs(ini.laplace, 2 * sp.pi * sp.I * frequency_hz)
    return complex(sp.N(numeric))


def _representative_path_to_incoming(
    adjacency: dict[str, set[str]],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
    source_vertex: str,
    incoming_key: tuple[str, str, str],
) -> tuple[tuple[str, str, str], ...]:
    """Return one shortest source path followed by the selected incoming edge."""
    incoming_source = incoming_key[0]
    paths = _enumerate_forward_paths(
        adjacency,
        edge_lookup,
        source_vertex,
        incoming_source,
        max_paths=64,
    )
    if not paths:
        return (incoming_key,) if incoming_source == source_vertex else ()
    path = min(paths, key=lambda item: len(item.edges))
    return path.edges + (incoming_key,)


def localize_candidate_roots(
    candidates: Iterable[MetaEdgeCandidate],
    clusters: Iterable[RootCluster],
    topology: GraphTopology,
    error_specs: Iterable[ErrorSubSpecification] | None = None,
) -> list[RootLocalization]:
    """Assign candidate roots to root clusters and mark output observability."""
    clusters = tuple(clusters)
    specs_by_cluster = {spec.cluster_index: spec for spec in (error_specs or [])}
    localizations: list[RootLocalization] = []
    for candidate in candidates:
        edge_key = (candidate.source, candidate.target, candidate.domain)
        on_forward_path = edge_key in topology.observable_edges
        for root in candidate.roots:
            cluster, error_ratio = _match_root_cluster(root.frequency_hz, clusters, specs_by_cluster)
            localizations.append(
                RootLocalization(
                    source=candidate.source,
                    target=candidate.target,
                    domain=candidate.domain,
                    root_kind=root.kind,
                    root_value=root.value,
                    root_frequency_hz=root.frequency_hz,
                    cluster_index=None if cluster is None else cluster.index,
                    frequency_error_ratio=error_ratio,
                    observable=on_forward_path and cluster is not None,
                    on_forward_path=on_forward_path,
                    category="OLR-O" if on_forward_path and cluster is not None else "OLR-NO",
                )
            )
    localizations.sort(key=lambda item: (item.cluster_index is None, item.cluster_index or 0, item.root_frequency_hz))
    return localizations


def root_observability_check(
    graph: SignalFlowGraph,
    localizations: Iterable[RootLocalization],
    topology: PaperTopologyMetrics,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    dominance_ratio: float = 10.0,
    summing_point_roots: Iterable[SummingPointRoot] | None = None,
    feedback_interaction_threshold: float = 1.0,
) -> list[RootLocalization]:
    """Check root observability by downstream summing-point signal dominance."""
    if dominance_ratio < 0:
        raise ValueError("dominance_ratio must be non-negative")
    if feedback_interaction_threshold < 0:
        raise ValueError("feedback_interaction_threshold must be non-negative")
    substitutions = substitutions or {}
    summing_vertices = {item.vertex for item in topology.summing_vertices}
    spr_by_cluster: dict[int, list[SummingPointRoot]] = {}
    for root in summing_point_roots or ():
        if root.cluster_index is not None and root.observable:
            spr_by_cluster.setdefault(root.cluster_index, []).append(root)
    checked: list[RootLocalization] = []
    for item in localizations:
        key = (item.source, item.target, item.domain)
        if item.cluster_index is None or key not in topology.observable_edges:
            checked.append(replace(item, observable=False, category="OLR-NO"))
            continue
        if (
            item.root_kind == "pole"
            and item.cluster_index in spr_by_cluster
            and _feedback_loop_gain_envelope_containing_edge(
                graph,
                topology,
                key,
                item.root_frequency_hz,
                substitutions,
            )
            >= feedback_interaction_threshold
        ):
            checked.append(replace(item, observable=False, category="OLR-NO"))
            continue
        candidate_paths = [path for path in topology.forward_paths if key in path.edges]
        if not candidate_paths:
            checked.append(replace(item, observable=False, category="OLR-NO"))
            continue
        observable = False
        for path in candidate_paths:
            downstream_summing = _first_downstream_summing_vertex(path, key, summing_vertices)
            if downstream_summing is None:
                observable = True
                break
            candidate_level = _path_gain_to_vertex(graph, path, downstream_summing, item.root_frequency_hz, substitutions)
            competitor_levels = [
                _path_gain_to_vertex(graph, other, downstream_summing, item.root_frequency_hz, substitutions)
                for other in topology.forward_paths
                if other is not path and downstream_summing in other.vertices
            ]
            strongest_competitor = max(competitor_levels, default=0.0)
            if candidate_level > 0 and strongest_competitor <= dominance_ratio * candidate_level:
                observable = True
                break
        checked.append(replace(item, observable=observable, category="OLR-O" if observable else "OLR-NO"))
    return checked


def summing_point_root_observability_check(
    graph: SignalFlowGraph,
    roots: Iterable[SummingPointRoot],
    topology: PaperTopologyMetrics,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    dominance_ratio: float = 10.0,
) -> list[SummingPointRoot]:
    """Mark summing-point root candidates that can propagate to the detector."""
    substitutions = substitutions or {}
    roots = list(roots)
    checked: list[SummingPointRoot] = []
    for root in roots:
        if root.cluster_index is None:
            checked.append(replace(root, observable=False))
            continue
        carrying_paths = [
            path
            for path in topology.forward_paths
            if root.vertex in path.vertices and any(edge in path.edges for pair in root.path_edges for edge in pair)
        ]
        if not carrying_paths:
            checked.append(replace(root, observable=False))
            continue
        observable = False
        for path in carrying_paths:
            candidate_level = _path_gain_to_vertex(
                graph,
                path,
                topology.detector_vertex,
                root.frequency_hz,
                substitutions,
            )
            competitor_levels = [
                _path_gain_to_vertex(graph, other, topology.detector_vertex, root.frequency_hz, substitutions)
                for other in topology.forward_paths
                if other is not path
            ]
            strongest_competitor = max(competitor_levels, default=0.0)
            if candidate_level > 0 and strongest_competitor <= dominance_ratio * candidate_level:
                observable = True
                break
        checked.append(replace(root, observable=observable))
    checked.sort(key=lambda item: (not item.observable, item.cluster_index is None, item.cluster_index or 0, item.frequency_hz))
    return checked


def rank_meta_edge_candidates(
    candidates: Iterable[MetaEdgeCandidate],
    localizations: Iterable[RootLocalization],
    topology: GraphTopology | None = None,
) -> list[SimplificationRanking]:
    """Create a first-pass preservation/deletion ranking for meta-edge candidates."""
    localizations_by_edge: dict[tuple[str, str, str], list[RootLocalization]] = {}
    for item in localizations:
        localizations_by_edge.setdefault((item.source, item.target, item.domain), []).append(item)

    rankings: list[SimplificationRanking] = []
    for candidate in candidates:
        key = (candidate.source, candidate.target, candidate.domain)
        edge_localizations = localizations_by_edge.get(key, [])
        observable_count = sum(1 for item in edge_localizations if item.observable)
        localized_count = sum(1 for item in edge_localizations if item.cluster_index is not None)
        preservation_score = candidate.structural_score + 4 * observable_count + 2 * localized_count
        on_forward_path = topology is not None and key in topology.observable_edges
        if on_forward_path:
            preservation_score += 10
        if candidate.domain == "voltage" or candidate.source.endswith(".src"):
            preservation_score += 100
            reason = "source_or_voltage_constraint"
        elif observable_count:
            reason = "observable_localized_root"
        elif on_forward_path:
            reason = "observable_static_path"
        elif candidate.roots:
            reason = "unobservable_or_unmatched_root"
        else:
            reason = "static_or_no_local_root"
        deletion_priority = 1 / (1 + preservation_score)
        rankings.append(
            SimplificationRanking(
                source=candidate.source,
                target=candidate.target,
                domain=candidate.domain,
                observable_root_count=observable_count,
                localized_root_count=localized_count,
                preservation_score=float(preservation_score),
                deletion_priority=float(deletion_priority),
                reason=reason,
            )
        )
    rankings.sort(key=lambda item: item.deletion_priority, reverse=True)
    return rankings


def root_cluster_report(clusters: Iterable[RootCluster]) -> str:
    """Format root clustering results as Markdown."""
    lines = ["# Root Clusters", ""]
    for cluster in clusters:
        lines.append(f"## Cluster {cluster.index}")
        lines.append(f"- min magnitude: `{cluster.min_magnitude}` rad/s")
        lines.append(f"- max magnitude: `{cluster.max_magnitude}` rad/s")
        lines.append(f"- center magnitude: `{cluster.center_magnitude}` rad/s")
        lines.append(f"- center frequency: `{cluster.center_frequency_hz}` Hz")
        lines.append(f"- poles: `{cluster.pole_count}`")
        lines.append(f"- zeros: `{cluster.zero_count}`")
        for root in cluster.roots:
            lines.append(f"- `{root.kind}`: `{root.value}`, `{root.frequency_hz}` Hz")
        lines.append("")
    return "\n".join(lines).rstrip()


def error_specification_report(specs: Iterable[ErrorSubSpecification]) -> str:
    """Format first-pass error sub-specifications as Markdown."""
    lines = ["# Error Sub-Specifications", ""]
    for spec in specs:
        raw_upper = "inf" if np.isinf(spec.raw_upper_frequency_hz or 0.0) else spec.raw_upper_frequency_hz
        user_range = "none"
        if spec.user_lower_frequency_hz is not None and spec.user_upper_frequency_hz is not None:
            user_range = f"{spec.user_lower_frequency_hz} Hz to {spec.user_upper_frequency_hz} Hz"
        lines.append(f"## Cluster {spec.cluster_index}")
        lines.append(f"- error budget: `{spec.error_budget}`")
        lines.append(f"- lower frequency: `{spec.lower_frequency_hz}` Hz")
        lines.append(f"- upper frequency: `{spec.upper_frequency_hz}` Hz")
        lines.append(f"- Eq.(13)-(14) raw lower frequency: `{spec.raw_lower_frequency_hz}` Hz")
        lines.append(f"- Eq.(13)-(14) raw upper frequency: `{raw_upper}` Hz")
        lines.append(f"- user frequency range intersection: `{user_range}`")
        lines.append(f"- root count: `{spec.root_count}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def meta_edge_candidate_report(
    candidates: Iterable[MetaEdgeCandidate],
    notation: str = "scientific",
    precision: int = 6,
) -> str:
    """Format meta-edge candidate analysis as Markdown."""
    lines = ["# Meta-Edge Candidates", ""]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(f"## Candidate {index}: `{candidate.source}` -> `{candidate.target}` ({candidate.domain})")
        lines.append(f"- symbolic: `{sp.sstr(sp.simplify(candidate.symbolic_value))}`")
        lines.append(
            f"- numeric: `{format_numeric_expression(candidate.numeric_value, notation=notation, precision=precision)}`"
        )
        lines.append(f"- contribution count: `{candidate.contribution_count}`")
        lines.append(f"- dynamic order span: `{candidate.dynamic_order_span}`")
        lines.append(f"- structural score: `{candidate.structural_score}`")
        if candidate.order_partitions:
            lines.append("- order partitions:")
            for order, expr in sorted(candidate.order_partitions.items(), key=lambda item: (item[0] is None, item[0] or 0)):
                lines.append(f"  - `{order}`: `{sp.sstr(sp.simplify(expr))}`")
        if candidate.roots:
            lines.append("- candidate roots:")
            for root in candidate.roots:
                lines.append(f"  - `{root.kind}`: `{root.value}`, `{root.frequency_hz}` Hz")
        else:
            lines.append("- candidate roots: none")
        lines.append("")
    return "\n".join(lines).rstrip()


def graph_topology_report(topology: GraphTopology) -> str:
    """Format graph reachability analysis as Markdown."""
    lines = ["# Graph Topology", ""]
    lines.append(f"- source: `{topology.source_ref}`")
    lines.append(f"- source vertex: `{topology.source_vertex}`")
    lines.append(f"- detector: `{topology.detector}`")
    lines.append(f"- detector vertex: `{topology.detector_vertex}`")
    lines.append(f"- reachable from source: `{len(topology.reachable_from_source)}` vertices")
    lines.append(f"- can reach detector: `{len(topology.can_reach_detector)}` vertices")
    lines.append(f"- observable meta-edges: `{len(topology.observable_edges)}`")
    if topology.observable_edges:
        lines.extend(["", "## Observable Meta-Edges"])
        for source, target, domain in sorted(topology.observable_edges):
            lines.append(f"- `{source}` -> `{target}` ({domain})")
    return "\n".join(lines).rstrip()


def paper_topology_report(topology: PaperTopologyMetrics, preview_limit: int = 40) -> str:
    """Format Fig.12 topology objects as Markdown."""
    strict_cuts = [cut for cut in topology.graph_cuts if cut.cut_kind == "complementary"]
    fallback_cuts = [cut for cut in topology.graph_cuts if cut.cut_kind != "complementary"]
    lines = ["# Paper Topology Analysis", ""]
    lines.append(f"- source: `{topology.source_ref}`")
    lines.append(f"- source vertex: `{topology.source_vertex}`")
    lines.append(f"- detector: `{topology.detector}`")
    lines.append(f"- detector vertex: `{topology.detector_vertex}`")
    lines.append(f"- forward paths: `{len(topology.forward_paths)}`")
    lines.append(f"- feedback loops: `{len(topology.feedback_loops)}`")
    lines.append(f"- strict complementary graph cuts: `{len(strict_cuts)}`")
    lines.append(f"- fallback/reconvergent candidate cuts: `{len(fallback_cuts)}`")
    lines.append(f"- summing vertices: `{len(topology.summing_vertices)}`")
    lines.append(f"- observable edges: `{len(topology.observable_edges)}`")
    lines.append(f"- candidate-root edges: `{len(topology.candidate_root_edges)}`")
    lines.extend(["", "## Forward Paths"])
    if not topology.forward_paths:
        lines.append("- none")
    for index, path in enumerate(topology.forward_paths[:preview_limit], start=1):
        lines.append(f"- {index}: `{' -> '.join(path.vertices)}`")
    if len(topology.forward_paths) > preview_limit:
        lines.append(f"- omitted: `{len(topology.forward_paths) - preview_limit}`")
    lines.extend(["", "## Feedback Loops"])
    if not topology.feedback_loops:
        lines.append("- none")
    for index, loop in enumerate(topology.feedback_loops[:preview_limit], start=1):
        lines.append(f"- {index}: `{' -> '.join(loop.vertices)}`")
    if len(topology.feedback_loops) > preview_limit:
        lines.append(f"- omitted: `{len(topology.feedback_loops) - preview_limit}`")
    lines.extend(["", "## Strict Complementary Graph Cuts"])
    if not strict_cuts:
        lines.append("- none")
    for index, cut in enumerate(strict_cuts[:preview_limit], start=1):
        lines.append(
            f"- {index}: `{cut.start_vertex}` -> `{cut.end_vertex}`, "
            f"length `{cut.length}`, vertices `{len(cut.vertices)}`, edges `{len(cut.edges)}`"
        )
    if len(strict_cuts) > preview_limit:
        lines.append(f"- omitted: `{len(strict_cuts) - preview_limit}`")
    lines.extend(["", "## Fallback/Reconvergent Candidate Cuts"])
    lines.append("- these cuts are useful for prototype subgraph extraction, but are not reported as strict paper complementary cuts")
    if not fallback_cuts:
        lines.append("- none")
    for index, cut in enumerate(fallback_cuts[:preview_limit], start=1):
        lines.append(
            f"- {index}: `{cut.start_vertex}` -> `{cut.end_vertex}`, "
            f"kind `{cut.cut_kind}`, length `{cut.length}`, vertices `{len(cut.vertices)}`, edges `{len(cut.edges)}`"
        )
    if len(fallback_cuts) > preview_limit:
        lines.append(f"- omitted: `{len(fallback_cuts) - preview_limit}`")
    lines.extend(["", "## Summing Vertices"])
    if not topology.summing_vertices:
        lines.append("- none")
    for item in topology.summing_vertices:
        lines.append(f"- `{item.vertex}`: `{item.input_count}` inputs")
    return "\n".join(lines).rstrip()


def paper_complexity_report(complexity: PaperComplexity) -> str:
    """Format paper Eq.(9)/(10) complexity terms as Markdown."""
    lines = ["# Paper Complexity", ""]
    lines.append(f"- n_fol open-loop roots: `{complexity.open_loop_roots}`")
    lines.append(f"- n_sum summing points: `{complexity.summing_points}`")
    lines.append(f"- n_fwp forward paths: `{complexity.forward_paths}`")
    lines.append(f"- n_fbl feedback loops: `{complexity.feedback_loops}`")
    lines.append(f"- C(N) Eq.(9): `{complexity.full}`")
    lines.append(f"- Cs(N) Eq.(10): `{complexity.simplified}`")
    lines.append(f"- active complexity: `{complexity.value}`")
    return "\n".join(lines).rstrip()


def summing_point_root_report(roots: Iterable[SummingPointRoot], precision: int = 6) -> str:
    """Format summing-point roots detected using paper Section IV-B."""
    lines = ["# Summing-Point Root Candidates", ""]
    roots = list(roots)
    if not roots:
        lines.append("- none")
        return "\n".join(lines)
    dominant = [root for root in roots if root.dominant]
    if dominant:
        lines.extend(["## Dominant Candidates", ""])
        for root in dominant:
            cluster = "none" if root.cluster_index is None else str(root.cluster_index)
            ratio = "none" if root.frequency_error_ratio is None else f"{root.frequency_error_ratio:.{precision}e}"
            lines.append(
                f"- `{root.vertex}`: root `{root.value}`, `{root.frequency_hz:.{precision}e}` Hz, "
                f"cluster `{cluster}`, frequency error ratio `{ratio}`, observable `{root.observable}`"
            )
            if root.expression is not None:
                lines.append(f"  - symbolic root expression: `{sp.sstr(sp.factor(sp.simplify(root.expression)))}`")
            if root.crossing_equation is not None:
                lines.append(f"  - path crossing equation: `{sp.sstr(sp.factor(sp.simplify(root.crossing_equation)))}`")
        lines.extend(["", "## All Candidates", ""])
    for root in roots:
        cluster = "none" if root.cluster_index is None else str(root.cluster_index)
        ratio = "none" if root.frequency_error_ratio is None else f"{root.frequency_error_ratio:.{precision}e}"
        lines.append(
            f"- `{root.vertex}`: root `{root.value}`, `{root.frequency_hz:.{precision}e}` Hz, "
            f"cluster `{cluster}`, frequency error ratio `{ratio}`, "
            f"category `{root.category}`, observable `{root.observable}`, dominant `{root.dominant}`"
        )
        if root.dominant and root.expression is not None:
            lines.append(f"  - symbolic root expression: `{sp.sstr(sp.factor(sp.simplify(root.expression)))}`")
    return "\n".join(lines)


def subrange_symbolic_transfer_report(
    transfers: Iterable[SubrangeSymbolicTransfer],
    precision: int = 6,
) -> str:
    """Format symbolic H_j(s) results obtained from frequency subgraphs."""
    transfers = list(transfers)
    lines = ["# Subrange Symbolic Transfer Functions", ""]
    if not transfers:
        lines.append("- none")
        return "\n".join(lines)
    lines.append(
        "Each H_j(s) is solved from the frequency-reduced SFG G_j after root clustering. "
        "The graph is reduced using numeric dominance tests, but the retained edge values stay symbolic."
    )
    for item in transfers:
        upper = "inf" if np.isinf(item.upper_frequency_hz) else f"{item.upper_frequency_hz:.{precision}e}"
        lines.extend(
            [
                "",
                f"## Cluster {item.cluster_index}",
                f"- frequency range: `{item.lower_frequency_hz:.{precision}e}` Hz to `{upper}` Hz",
                f"- representative frequency: `{item.representative_frequency_hz:.{precision}e}` Hz",
                f"- reduced graph size: `{item.vertex_count}` vertices, `{item.edge_count}` meta-edges",
            ]
        )
        if not item.success:
            lines.append(f"- solve status: failed, `{item.error}`")
            continue
        lines.append(f"- H_j(s): `{sp.sstr(sp.factor(sp.simplify(item.transfer)))}`")
        lines.append(f"- numerator: `{sp.sstr(sp.factor(sp.simplify(item.numerator)))}`")
        lines.append(f"- denominator: `{sp.sstr(sp.factor(sp.simplify(item.denominator)))}`")
        lines.extend(["", "### Poles"])
        _append_symbolic_roots(lines, item.poles, precision)
        lines.extend(["", "### Zeroes"])
        _append_symbolic_roots(lines, item.zeros, precision)
    return "\n".join(lines).rstrip()


def paper_example_c0_report(state: PaperAlgorithmState, precision: int = 6) -> str:
    """Format a paper IV.C single-transistor example validation report."""
    substitutions = state.pipeline.network.numeric_substitutions()
    required = ("Gin", "gx", "gpi", "gm", "Gl", "go", "cmu", "cx", "cpi", "Cl")
    values: dict[str, float] = {}
    missing: list[str] = []
    for name in required:
        symbol = sp.Symbol(name)
        if symbol not in substitutions:
            missing.append(name)
            continue
        values[name] = float(sp.N(substitutions[symbol]))
    if missing:
        return "# Paper IV.C Example Validation\n\n- unavailable: missing parameters `" + ", ".join(missing) + "`"

    Gin = values["Gin"]
    gx = values["gx"]
    gpi = values["gpi"]
    gm = values["gm"]
    Gl = values["Gl"]
    go = values["go"]
    cmu = values["cmu"]
    cx = values["cx"]
    cpi = values["cpi"]
    Cl = values["Cl"]
    paper_values = {
        "Eq.(16) fpol1 open-loop input pole": -(Gin + gx) / (2 * np.pi * cx),
        "Eq.(17) fpol2 open-loop internal pole": -(gx + gpi) / (2 * np.pi * (cmu + cpi)),
        "Eq.(18) fpol3 open-loop output pole": -(go + Gl) / (2 * np.pi * (cmu + cx + Cl)),
        "Eq.(19) fzol open-loop gm/cmu zero": gm / (2 * np.pi * cmu),
        "Eq.(21) fp1 SPR low-frequency pole": -((gx + gpi) * (go + Gl)) / (
            2 * np.pi * 2 * (gm * cmu + (gx + gpi) * (cmu + cx + Cl))
        ),
        "Eq.(22) fp2 SPR second pole": -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
            2 * np.pi * (cmu + cpi) * (cmu + cx + Cl)
        ),
        "Eq.(23) fz1 observable open-loop zero": gm / (2 * np.pi * cmu),
        "Eq.(24) fz2 high-frequency takeover zero": -(gx * cmu) / (2 * np.pi * cx * (cmu + cpi)),
        "Eq.(25) fp3 observable high-frequency pole": -(Gin + gx) / (2 * np.pi * cx),
    }
    closed_roots = tuple(
        RootSample("pole", value) for value in state.pipeline.reference.poles
    ) + tuple(RootSample("zero", value) for value in state.pipeline.reference.zeros)
    closed_by_kind = sorted(closed_roots, key=lambda item: item.frequency_hz)
    olr_rows = [
        (item.cluster_index, item.source, item.target, item.root_kind, item.root_frequency_hz, item.category, item.observable)
        for item in state.pipeline.localizations
        if item.root_frequency_hz > 0
    ]
    spr_rows = [
        (item.cluster_index, item.vertex, item.frequency_hz, item.observable, item.dominant)
        for item in state.pipeline.summing_point_roots
        if item.dominant
    ]

    lines = ["# Paper IV.C Example Validation", ""]
    lines.append("## Paper Formula Values")
    for label, value in paper_values.items():
        lines.append(f"- {label}: `{value:.{precision}e}` Hz")
    lines.extend(["", "## SLiCAP Reference Closed-Loop Roots"])
    for root in closed_by_kind:
        lines.append(f"- `{root.kind}`: `{root.value}`, `{root.frequency_hz:.{precision}e}` Hz")
    lines.extend(["", "## Open-Loop Root Localization"])
    for cluster, source, target, kind, frequency, category, observable in olr_rows:
        lines.append(
            f"- cluster `{cluster}` `{source}` -> `{target}` {kind}: "
            f"`{frequency:.{precision}e}` Hz, `{category}`, observable `{observable}`"
        )
    lines.extend(["", "## Dominant Summing-Point Root Candidates"])
    for cluster, vertex, frequency, observable, dominant in spr_rows:
        lines.append(
            f"- cluster `{cluster}` vertex `{vertex}`: `{frequency:.{precision}e}` Hz, "
            f"observable `{observable}`, dominant `{dominant}`"
        )
    lines.extend(["", "## C.0.a-C.0.d Alignment"])
    alignment_rows = [
        ("C.0.a", "Eq.(21) fp1 SPR low-frequency pole", "pole", 1),
        ("C.0.b", "Eq.(22) fp2 SPR second pole", "pole", 2),
        ("C.0.c", "Eq.(23) fz1 observable open-loop zero", "zero", 3),
        ("C.0.d zero", "Eq.(24) fz2 high-frequency takeover zero", "zero", 4),
        ("C.0.d pole", "Eq.(25) fp3 observable high-frequency pole", "pole", 4),
    ]
    for section, formula_label, root_kind, cluster_index in alignment_rows:
        formula = abs(paper_values[formula_label])
        reference = _nearest_root_frequency(closed_by_kind, root_kind, formula)
        spr = _nearest_spr_frequency(state.pipeline.summing_point_roots, cluster_index, formula)
        olr = _nearest_olr_frequency(state.pipeline.localizations, cluster_index, root_kind, formula)
        lines.append(
            f"- {section}: paper `{formula:.{precision}e}` Hz, "
            f"reference `{_format_optional_float(reference, precision)}` Hz, "
            f"dominant SPR `{_format_optional_float(spr, precision)}` Hz, "
            f"OLR `{_format_optional_float(olr, precision)}` Hz"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "- C.0.a/C.0.b: the non-observable open-loop poles are marked `OLR-NO`, and the dominant `SPR` candidates align with Eq.(21) and Eq.(22).",
            "- C.0.c: the `gm/cmu` zero is identified as an observable open-loop zero (`OLR-O`) near Eq.(23).",
            "- C.0.d: the high-frequency takeover zero appears as a dominant `SPR` candidate near Eq.(24), and the high-frequency input pole is identified as `OLR-O` near Eq.(25).",
            "- If both an `SPR` and an `OLR-O` are reported at the same frequency, the paper interpretation should prefer `OLR-O` for directly propagated open-loop roots.",
        ]
    )
    return "\n".join(lines).rstrip()


def subgraph_report(subgraphs: Iterable[SFGSubgraph]) -> str:
    """Format root-cluster subgraphs G_j as Markdown."""
    lines = ["# Root-Cluster Subgraphs", ""]
    for subgraph in subgraphs:
        upper = "inf" if np.isinf(subgraph.upper_frequency_hz) else f"{subgraph.upper_frequency_hz}"
        lines.append(f"## G_{subgraph.cluster_index}")
        lines.append(f"- frequency band: `{subgraph.lower_frequency_hz}` Hz - `{upper}` Hz")
        lines.append(f"- vertices: `{len(subgraph.vertices)}`")
        lines.append(f"- edges: `{len(subgraph.edges)}`")
        lines.append(f"- candidate edges: `{len(subgraph.candidate_edges)}`")
        if subgraph.candidate_edges:
            lines.append("- candidate edge list:")
            for source, target, domain in sorted(subgraph.candidate_edges):
                lines.append(f"  - `{source}` -> `{target}` ({domain})")
        lines.append("")
    return "\n".join(lines).rstrip()


def root_localization_report(
    localizations: Iterable[RootLocalization],
    notation: str = "scientific",
    precision: int = 6,
) -> str:
    """Format candidate-root localization and observability checks as Markdown."""
    lines = ["# Root Localization", ""]
    lines.append("- observability: downstream summing-point dominance check from paper Section IV-B")
    lines.append("")
    items = list(localizations)
    if not items:
        lines.append("- no candidate roots")
        return "\n".join(lines)
    for item in items:
        cluster = "none" if item.cluster_index is None else str(item.cluster_index)
        ratio = "none" if item.frequency_error_ratio is None else f"{item.frequency_error_ratio:.{precision}e}"
        lines.append(f"## `{item.source}` -> `{item.target}` ({item.domain})")
        lines.append(f"- root source: `{item.root_source}`")
        lines.append(f"- category: `{item.category}`")
        lines.append(f"- root kind: `{item.root_kind}`")
        lines.append(f"- root value: `{item.root_value}`")
        lines.append(f"- frequency: `{item.root_frequency_hz}` Hz")
        lines.append(f"- cluster: `{cluster}`")
        lines.append(f"- frequency error ratio: `{ratio}`")
        lines.append(f"- on source-detector path: `{item.on_forward_path}`")
        lines.append(f"- observable: `{item.observable}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def simplification_ranking_report(
    rankings: Iterable[SimplificationRanking],
    precision: int = 6,
) -> str:
    """Format first-pass candidate ranking as Markdown."""
    lines = ["# Simplification Ranking", ""]
    for index, item in enumerate(rankings, start=1):
        lines.append(f"## Rank {index}: `{item.source}` -> `{item.target}` ({item.domain})")
        lines.append(f"- deletion priority: `{item.deletion_priority:.{precision}e}`")
        lines.append(f"- preservation score: `{item.preservation_score}`")
        lines.append(f"- observable roots: `{item.observable_root_count}`")
        lines.append(f"- localized roots: `{item.localized_root_count}`")
        lines.append(f"- reason: `{item.reason}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def numeric_reference_report(
    result: NumericReferenceResult | DescriptorMatrixResult,
    clusters: Iterable[RootCluster] | None = None,
    samples: Iterable[FrequencySample] | None = None,
    error_specs: Iterable[ErrorSubSpecification] | None = None,
) -> str:
    """Format reference analysis results as Markdown."""
    lines = [f"# Numeric Reference Report: {result.title or ''}", ""]
    lines.append(f"- source: `{result.source}`")
    lines.append(f"- detector: `{result.detector}`")
    lines.append(f"- method: `{result.method}`")
    if isinstance(result, DescriptorMatrixResult):
        lines.append(f"- descriptor shape: `{result.conductance_matrix.shape}`")
        lines.append(f"- infinite modes: `{result.infinite_mode_count}`")
        lines.append(f"- infinite Rosenbrock roots: `{result.infinite_zero_count}`")
        lines.append(f"- cancelled finite root pairs: `{len(result.cancelled_roots)}`")
    else:
        lines.append(f"- laplace: `{sp.sstr(result.laplace)}`")
        lines.append(f"- numerator: `{sp.sstr(result.numerator)}`")
        lines.append(f"- denominator: `{sp.sstr(result.denominator)}`")
        lines.append(f"- dc value: `{result.dc_value}`")
    lines.extend(["", "## Poles"])
    if result.poles:
        lines.extend(f"- `{pole}`" for pole in result.poles)
    else:
        lines.append("- none")
    lines.extend(["", "## Zeros"])
    if result.zeros:
        lines.extend(f"- `{zero}`" for zero in result.zeros)
    else:
        lines.append("- none")
    if samples is not None:
        lines.extend(["", "## Frequency Samples"])
        for sample in samples:
            lines.append(
                f"- `{sample.frequency_hz}` Hz: value=`{sample.value}`, "
                f"|H|=`{sample.magnitude}`, phase=`{sample.phase_deg}` deg"
            )
    if clusters is not None:
        lines.extend(["", root_cluster_report(clusters)])
    if error_specs is not None:
        lines.extend(["", error_specification_report(error_specs)])
    return "\n".join(lines).rstrip()


def target_root_symbolic_report(
    roots: Iterable[TargetRootSymbolicApproximation],
    precision: int = 6,
) -> str:
    """Format per-root local symbolic approximations as Markdown."""
    lines = ["# Target-Root Symbolic Approximations", ""]
    for root in roots:
        lines.append(
            f"## Root {root.root_index}: cluster {root.cluster_index} "
            f"({root.kind}, {root.category})"
        )
        lines.append(f"- status: `{root.status}`")
        lines.append(f"- reference root: `{root.reference_value}`")
        lines.append(f"- reference frequency: `{root.reference_frequency_hz:.{precision}e} Hz`")
        lines.append(f"- method: `{root.method}`")
        lines.append(f"- location: `{root.location or 'none'}`")
        if root.expression is None:
            lines.append("- expression: `unresolved`")
        else:
            lines.append(f"- expression: `{sp.sstr(sp.factor(root.expression))}`")
            lines.append(f"- numeric root: `{root.numeric_value}`")
            lines.append(f"- relative root error: `{root.relative_root_error:.{precision}e}`")
            lines.append(f"- parameters: `{', '.join(root.parameters) or 'none'}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def localized_symbolic_zero_report(
    zeros: Iterable[LocalizedSymbolicZero],
    precision: int = 6,
) -> str:
    """Format local SPR-Z path equations and matrix fallbacks as Markdown."""
    zeros = tuple(zeros)
    lines = ["# Local Summing-Point Zero Localization", ""]
    if not zeros:
        lines.append("- none")
        return "\n".join(lines)
    for index, zero in enumerate(zeros, start=1):
        lines.append(f"## {index}. Cluster {zero.cluster_index}: `{zero.vertex}`")
        lines.append(f"- status: `{zero.status}`")
        lines.append(f"- method: `{zero.method}`")
        lines.append(
            f"- local region: `{zero.cut_start or 'none'}` -> `{zero.cut_end or 'none'}` "
            f"(`{zero.cut_kind}`)"
        )
        residual = (
            "not evaluated"
            if zero.cancellation_residual is None
            else f"{zero.cancellation_residual:.{precision}e}"
        )
        lines.append(f"- cancellation residual: `{residual}`")
        for path_index, path in enumerate(zero.paths, start=1):
            lines.append(f"- P_{path_index}(s): `{sp.sstr(sp.factor(path.gain))}`")
            lines.append(f"- Delta_{path_index}(s): `{sp.sstr(sp.factor(path.mason_cofactor))}`")
            lines.append(f"- P_{path_index}(s)Delta_{path_index}(s): `{sp.sstr(sp.factor(path.weighted_gain))}`")
        if zero.characteristic_equation is not None:
            lines.append(
                f"- N_cut(s)=0: `{sp.sstr(sp.factor(zero.characteristic_equation))}`"
            )
        if zero.expression is not None:
            lines.append(f"- symbolic zero: `{sp.sstr(sp.factor(zero.expression))}`")
            lines.append(f"- numeric zero: `{zero.numeric_value}`")
            lines.append(
                f"- relative root error: `{zero.relative_root_error:.{precision}e}`"
            )
            corner_error = (
                "not evaluated"
                if zero.corner_max_relative_error is None
                else f"{zero.corner_max_relative_error:.{precision}e}"
            )
            lines.append(f"- full-transfer parameter-corner root error: `{corner_error}`")
            lines.append(f"- parameters: `{', '.join(zero.parameters) or 'none'}`")
            if zero.parameter_participation:
                text = ", ".join(
                    f"{name}={value:.{precision}e}"
                    for name, value in zero.parameter_participation
                )
                lines.append(f"- normalized parameter participation: `{text}`")
        if zero.failure_reason:
            lines.append(f"- diagnostic: `{zero.failure_reason}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def reference_pipeline_report(
    result: ReferencePipelineResult,
    notation: str = "scientific",
    precision: int = 6,
) -> str:
    """Format the combined reference/SFG candidate pipeline result."""
    sections = [
        numeric_reference_report(result.reference, clusters=result.clusters, error_specs=result.error_specs),
        graph_topology_report(result.topology),
    ]
    if result.topology_metrics:
        sections.append(paper_topology_report(result.topology_metrics))
        sections.append(paper_complexity_report(paper_complexity(result.graph, result.candidates, result.topology_metrics)))
        sections.append(summing_point_root_report(result.summing_point_roots, precision=precision))
    sections.extend(
        [
            subgraph_report(result.subgraphs),
            subrange_symbolic_transfer_report(result.subrange_transfers, precision=precision),
            meta_edge_candidate_report(result.candidates, notation=notation, precision=precision),
            root_localization_report(result.localizations, notation=notation, precision=precision),
            simplification_ranking_report(result.rankings, precision=precision),
        ]
    )
    return "\n\n".join(section for section in sections if section)


def paper_algorithm_state_report(
    state: PaperAlgorithmState,
    notation: str = "scientific",
    precision: int = 6,
) -> str:
    """Format one coherent report for paper Fig.12 stages 4-9 analysis state."""
    pipeline = state.pipeline
    sections = [
        "# Paper Fig.12 Algorithm State",
        "",
        "## Configuration",
        f"- cluster tolerance: `{state.config.cluster_tolerance}`",
        f"- total error budget: `{state.config.total_error_budget}`",
        f"- max paths: `{state.config.max_paths}`",
        f"- max loops: `{state.config.max_loops}`",
        f"- max cuts: `{state.config.max_cuts}`",
        f"- strict cuts only: `{state.config.strict_cuts_only}`",
        f"- strict complementary cuts: `{len(state.strict_graph_cuts)}`",
        f"- fallback candidate cuts: `{len(state.fallback_graph_cuts)}`",
        "",
        numeric_reference_report(pipeline.reference, clusters=pipeline.clusters, error_specs=pipeline.error_specs),
        graph_topology_report(pipeline.topology),
    ]
    if pipeline.topology_metrics:
        sections.append(paper_topology_report(pipeline.topology_metrics))
    sections.extend(
        [
            paper_complexity_report(state.complexity),
            subgraph_report(pipeline.subgraphs),
            meta_edge_candidate_report(pipeline.candidates, notation=notation, precision=precision),
            root_localization_report(pipeline.localizations, notation=notation, precision=precision),
            summing_point_root_report(pipeline.summing_point_roots, precision=precision),
            subrange_symbolic_transfer_report(pipeline.subrange_transfers, precision=precision),
            simplification_ranking_report(pipeline.rankings, precision=precision),
        ]
    )
    return "\n\n".join(section for section in sections if section).rstrip()


def export_paper_algorithm_reports(
    state: PaperAlgorithmState,
    output_dir: str | os.PathLike[str],
    notation: str = "scientific",
    precision: int = 6,
) -> dict[str, str]:
    """Write paper-stage reports and return their paths by logical report name."""
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    pipeline = state.pipeline
    reports = {
        "reference": numeric_reference_report(
            pipeline.reference,
            clusters=pipeline.clusters,
            error_specs=pipeline.error_specs,
        ),
        "clusters": "\n\n".join(
            [
                root_cluster_report(pipeline.clusters),
                error_specification_report(pipeline.error_specs),
            ]
        ),
        "topology": "\n\n".join(
            section
            for section in [
                graph_topology_report(pipeline.topology),
                paper_topology_report(pipeline.topology_metrics) if pipeline.topology_metrics else "",
                paper_complexity_report(state.complexity),
            ]
            if section
        ),
        "subgraphs": subgraph_report(pipeline.subgraphs),
        "operation_ranking": simplification_ranking_report(pipeline.rankings, precision=precision),
        "root_localization": "\n\n".join(
            [
                root_localization_report(pipeline.localizations, notation=notation, precision=precision),
                summing_point_root_report(pipeline.summing_point_roots, precision=precision),
            ]
        ),
        "subrange_transfers": subrange_symbolic_transfer_report(pipeline.subrange_transfers, precision=precision),
        "paper_example_c0": paper_example_c0_report(state, precision=precision),
        "final_summary": paper_algorithm_state_report(state, notation=notation, precision=precision),
    }
    paths: dict[str, str] = {}
    for name, content in reports.items():
        path = output / f"{name}.md"
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        paths[name] = str(path)
    return paths


def _load_circuit(path: Path):
    """Reuse the SLiCAP front-end while suppressing HTML side effects."""
    _initializeParser()
    previous_cir_path = ini.cir_path
    previous_html_page = _yacc_module.htmlPage
    try:
        ini.cir_path = str(path.parent) + os.sep
        _yacc_module.htmlPage = lambda *args, **kwargs: None
        return _checkCircuit(path.name)
    finally:
        ini.cir_path = previous_cir_path
        _yacc_module.htmlPage = previous_html_page


def _parse_substitutions(substitutions: dict[str | sp.Symbol, Any]) -> dict[sp.Symbol, sp.Expr]:
    """Parse external substitutions with the same metric-prefix support as the SFG layer."""
    return {
        sp.Symbol(str(key)): evaluate_numeric_expression(value, {}).numeric
        for key, value in substitutions.items()
    }


def _base_instruction(circuit, source: str, detector: str) -> instruction:
    """Create a standard numeric gain-analysis instruction."""
    instr = instruction()
    instr.circuit = circuit
    instr.setSimType("numeric")
    instr.setGainType("gain")
    instr.setSource(source)
    instr.setDetector(detector)
    return instr


def _first_defined(values: list[str | None] | None) -> str | None:
    """Return the first non-empty value from a SLiCAP source/detector list."""
    if not values:
        return None
    return next((value for value in values if value is not None), None)


def _select_source_drive(graph: SignalFlowGraph, source: str | None) -> Any:
    """Select one independent source drive from the graph."""
    if not graph.source_drives:
        raise ValueError("The SFG has no independent source drive.")
    if source is None and graph.source_name is not None:
        source = graph.source_name
    if source is None:
        if len(graph.source_drives) != 1:
            names = ", ".join(item.source_ref for item in graph.source_drives)
            raise ValueError(f"Multiple source drives exist; pass source=... explicitly. Available: {names}")
        return graph.source_drives[0]
    for drive in graph.source_drives:
        if source in {drive.source_ref, drive.vertex_name}:
            return drive
    names = ", ".join(item.source_ref for item in graph.source_drives)
    raise ValueError(f"Unknown source '{source}'. Available sources: {names}")


def _first_available_detector_vertex(graph: SignalFlowGraph) -> str:
    """Return a deterministic voltage vertex when no detector was provided."""
    if graph.detector_name is not None:
        return _detector_to_vertex(graph, graph.detector_name)
    voltage_vertices = sorted(name for name, vertex in graph.vertices.items() if vertex.kind == "voltage")
    if not voltage_vertices:
        raise ValueError("The SFG has no voltage vertex that can be used as detector.")
    return voltage_vertices[0]


def _detector_to_vertex(graph: SignalFlowGraph, detector: str) -> str:
    """Map a SLiCAP detector string to an SFG vertex."""
    if detector in graph.vertices:
        return detector
    if detector.startswith("V_"):
        node = graph.voltage_aliases.get(detector[2:], detector[2:])
        vertex = f"{node}.v"
    elif detector.startswith("I_"):
        vertex = detector
    else:
        vertex = detector
    if vertex not in graph.vertices:
        raise ValueError(f"Detector '{detector}' does not map to an existing SFG vertex '{vertex}'.")
    return vertex


def _vertices_relevant_to_detector(graph: SignalFlowGraph, detector_vertex: str) -> set[str]:
    """Return vertices that can influence the selected detector."""
    _adjacency, reverse_adjacency = _graph_adjacency(graph)
    relevant: set[str] = set()
    stack = [detector_vertex]
    while stack:
        vertex = stack.pop()
        if vertex in relevant:
            continue
        relevant.add(vertex)
        stack.extend(reverse_adjacency.get(vertex, set()) - relevant)
    return relevant


def _solve_linear_sfg_equations(equations: list[sp.Expr], variables: list[sp.Symbol]) -> dict[sp.Symbol, sp.Expr]:
    """Solve linear SFG equations and reject underdetermined symbolic results."""
    if not equations:
        raise ValueError("No SFG equations were generated.")
    matrix, rhs = sp.linear_eq_to_matrix(equations, variables)
    solution_set = sp.linsolve((matrix, rhs), variables)
    if solution_set is sp.EmptySet:
        raise ValueError("SFG equations are inconsistent.")
    solution_tuple = next(iter(solution_set))
    solution = {variable: sp.simplify(value) for variable, value in zip(variables, solution_tuple)}
    unresolved = [variable for variable, value in solution.items() if variable in value.free_symbols]
    if unresolved:
        names = ", ".join(sp.sstr(item) for item in unresolved[:8])
        raise ValueError(f"SFG equations are underdetermined; unresolved variables: {names}")
    return solution


def _safe_symbol_name(name: str) -> str:
    """Create a SymPy-safe variable name for one SFG vertex."""
    return "x_" + "".join(ch if ch.isalnum() else "_" for ch in name)


def _graph_adjacency(graph: SignalFlowGraph) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build forward and reverse adjacency maps from meta-edges."""
    adjacency = {name: set() for name in graph.vertices}
    reverse_adjacency = {name: set() for name in graph.vertices}
    for edge in graph.meta_edges.values():
        adjacency.setdefault(edge.source, set()).add(edge.target)
        reverse_adjacency.setdefault(edge.target, set()).add(edge.source)
        adjacency.setdefault(edge.target, set())
        reverse_adjacency.setdefault(edge.source, set())
    return adjacency, reverse_adjacency


def _edge_lookup(graph: SignalFlowGraph) -> dict[tuple[str, str], tuple[tuple[str, str, str], ...]]:
    """Map a vertex pair to all meta-edge keys between them."""
    pairs: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for key, edge in graph.meta_edges.items():
        pairs.setdefault((edge.source, edge.target), []).append(key)
    return {pair: tuple(sorted(keys)) for pair, keys in pairs.items()}


def _enumerate_forward_paths(
    adjacency: dict[str, set[str]],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
    source: str,
    detector: str,
    max_paths: int,
) -> list[SignalPath]:
    """Enumerate simple source-to-detector paths with a safety cap."""
    paths: list[SignalPath] = []
    stack: list[tuple[str, list[str]]] = [(source, [source])]
    while stack and len(paths) < max_paths:
        vertex, path = stack.pop()
        if vertex == detector:
            paths.append(_path_from_vertices(path, edge_lookup))
            continue
        for nxt in sorted(adjacency.get(vertex, set()), reverse=True):
            if nxt in path:
                continue
            stack.append((nxt, path + [nxt]))
    return paths


def _enumerate_feedback_loops(
    adjacency: dict[str, set[str]],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
    max_loops: int,
) -> list[FeedbackLoop]:
    """Enumerate simple directed cycles with duplicate rotation suppression."""
    loops: list[FeedbackLoop] = []
    seen: set[tuple[str, ...]] = set()
    for start in sorted(adjacency):
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack and len(loops) < max_loops:
            vertex, path = stack.pop()
            for nxt in sorted(adjacency.get(vertex, set()), reverse=True):
                if nxt == start and len(path) > 1:
                    canonical = _canonical_cycle(path)
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    loop_vertices = tuple(path + [start])
                    loops.append(
                        FeedbackLoop(
                            vertices=loop_vertices,
                            edges=_edges_for_vertex_path(loop_vertices, edge_lookup),
                        )
                    )
                elif nxt not in path and nxt >= start:
                    stack.append((nxt, path + [nxt]))
            if len(loops) >= max_loops:
                break
    return loops


def _enumerate_graph_cuts(
    adjacency: dict[str, set[str]],
    reverse_adjacency: dict[str, set[str]],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
    max_cuts: int,
    include_fallback: bool = True,
) -> list[GraphCut]:
    """Enumerate conservative complementary-vertex graph cuts from Section IV-B."""
    cuts: list[GraphCut] = []
    seen_pairs: set[tuple[str, str]] = set()
    vertices = sorted(adjacency)
    for start in vertices:
        for end in vertices:
            if start == end:
                continue
            # Complementary vertices have interchanged topological degrees:
            # the entry's in-adjacency matches the exit's out-adjacency and
            # the entry's out-adjacency matches the exit's in-adjacency.
            if len(reverse_adjacency.get(start, set())) != len(adjacency.get(end, set())):
                continue
            if len(adjacency.get(start, set())) != len(reverse_adjacency.get(end, set())):
                continue
            cut_vertices = _single_entry_single_exit_region(
                adjacency,
                reverse_adjacency,
                start,
                end,
            )
            if not cut_vertices:
                continue
            paths = _enumerate_simple_vertex_paths(adjacency, start, end, max_paths=32)
            # Root decomposition is useful only when at least two internal
            # signal paths reconverge; a single edge is not a root-generating cut.
            if len(paths) < 2:
                continue
            cut_edges: set[tuple[str, str, str]] = set()
            min_length = min(len(path) - 1 for path in paths)
            for source in cut_vertices:
                for target in adjacency.get(source, set()):
                    if target in cut_vertices:
                        cut_edges.update(edge_lookup.get((source, target), ()))
            cuts.append(
                GraphCut(
                    start_vertex=start,
                    end_vertex=end,
                    length=min_length,
                    vertices=frozenset(cut_vertices),
                    edges=frozenset(cut_edges),
                    cut_kind="complementary",
                )
            )
            seen_pairs.add((start, end))
            if len(cuts) >= max_cuts:
                return sorted(cuts, key=lambda item: (item.length, item.start_vertex, item.end_vertex))
    if not include_fallback:
        return sorted(cuts, key=lambda item: (item.length, item.start_vertex, item.end_vertex))
    for start in vertices:
        for end in vertices:
            if start == end or (start, end) in seen_pairs:
                continue
            paths = _enumerate_simple_vertex_paths(adjacency, start, end, max_paths=8)
            if len(paths) < 2:
                continue
            min_length = min(len(path) - 1 for path in paths)
            if min_length < 2:
                continue
            cut_vertices = set().union(*(set(path) for path in paths))
            cut_edges: set[tuple[str, str, str]] = set()
            for path in paths:
                cut_edges.update(_edges_for_vertex_path(tuple(path), edge_lookup))
            cuts.append(
                GraphCut(
                    start_vertex=start,
                    end_vertex=end,
                    length=min_length,
                    vertices=frozenset(cut_vertices),
                    edges=frozenset(cut_edges),
                    cut_kind="reconvergent",
                )
            )
            seen_pairs.add((start, end))
            if len(cuts) >= max_cuts:
                return sorted(cuts, key=lambda item: (item.cut_kind != "complementary", item.length, item.start_vertex, item.end_vertex))
    return sorted(cuts, key=lambda item: (item.cut_kind != "complementary", item.length, item.start_vertex, item.end_vertex))


def _enumerate_simple_vertex_paths(
    adjacency: dict[str, set[str]],
    start: str,
    end: str,
    max_paths: int,
) -> list[list[str]]:
    """Enumerate simple vertex paths between two vertices."""
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    while stack and len(paths) < max_paths:
        vertex, path = stack.pop()
        if vertex == end:
            paths.append(path)
            continue
        for nxt in sorted(adjacency.get(vertex, set()), reverse=True):
            if nxt in path:
                continue
            stack.append((nxt, path + [nxt]))
    return paths


def _single_entry_single_exit_region(
    adjacency: dict[str, set[str]],
    reverse_adjacency: dict[str, set[str]],
    start: str,
    end: str,
) -> set[str]:
    """Return a strict single-entry/single-exit region, or an empty set if invalid."""
    forward = _reachable_without_boundary(adjacency, start, end)
    backward = _reachable_without_boundary(reverse_adjacency, end, start)
    region = forward & backward
    if start not in region or end not in region:
        return set()
    for vertex in region:
        if vertex != start and any(predecessor not in region for predecessor in reverse_adjacency.get(vertex, set())):
            return set()
        if vertex != end and any(successor not in region for successor in adjacency.get(vertex, set())):
            return set()
    return region


def _reachable_without_boundary(
    adjacency: dict[str, set[str]],
    start: str,
    stop: str,
) -> set[str]:
    """Return vertices reachable from start without traversing beyond the stop boundary."""
    visited: set[str] = set()
    stack = [start]
    while stack:
        vertex = stack.pop()
        if vertex in visited:
            continue
        visited.add(vertex)
        if vertex == stop:
            continue
        for nxt in adjacency.get(vertex, set()):
            if nxt == start:
                continue
            stack.append(nxt)
    return visited


def _path_from_vertices(
    vertices: list[str],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
) -> SignalPath:
    """Create a SignalPath object from a vertex sequence."""
    return SignalPath(
        vertices=tuple(vertices),
        edges=_edges_for_vertex_path(tuple(vertices), edge_lookup),
    )


def _edges_for_vertex_path(
    vertices: tuple[str, ...],
    edge_lookup: dict[tuple[str, str], tuple[tuple[str, str, str], ...]],
) -> tuple[tuple[str, str, str], ...]:
    """Return all meta-edge keys encountered along a vertex path."""
    keys: list[tuple[str, str, str]] = []
    for left, right in zip(vertices, vertices[1:]):
        keys.extend(edge_lookup.get((left, right), ()))
    return tuple(keys)


def _canonical_cycle(vertices: list[str]) -> tuple[str, ...]:
    """Return a rotation-invariant key for one directed cycle."""
    cycle = vertices[:]
    rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
    return min(rotations)


def _reachable_vertices(adjacency: dict[str, set[str]], starts: set[str]) -> set[str]:
    """Return all vertices reachable from a start set."""
    visited: set[str] = set()
    stack = list(starts)
    while stack:
        vertex = stack.pop()
        if vertex in visited:
            continue
        visited.add(vertex)
        stack.extend(adjacency.get(vertex, set()) - visited)
    return visited


def _candidate_in_frequency_band(candidate: MetaEdgeCandidate, spec: ErrorSubSpecification) -> bool:
    """Return True if any candidate root lies inside an error subrange."""
    if not candidate.roots:
        return False
    upper = spec.upper_frequency_hz
    for root in candidate.roots:
        if root.frequency_hz >= spec.lower_frequency_hz and (np.isinf(upper) or root.frequency_hz <= upper):
            return True
    return False


def _root_in_error_subrange(frequency_hz: float, spec: ErrorSubSpecification) -> bool:
    """Return True if a root belongs to the frequency band of one G_j."""
    if frequency_hz < spec.lower_frequency_hz:
        return False
    if np.isinf(spec.upper_frequency_hz):
        return True
    return frequency_hz <= spec.upper_frequency_hz


def _subrange_center_frequency(spec: ErrorSubSpecification) -> float:
    """Return a representative frequency for order-partition dominance tests."""
    lower = spec.lower_frequency_hz
    upper = spec.upper_frequency_hz
    if lower <= 0 and np.isfinite(upper):
        lower = max(upper / 100.0, 1e-6)
    if not np.isfinite(upper):
        upper = max(lower * 100.0, 1.0)
    if lower <= 0:
        lower = max(upper / 100.0, 1e-6)
    return float(np.sqrt(lower * upper))


def _frequency_reduced_edge_value(
    edge: MetaEdge,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
    relative_threshold: float,
) -> sp.Expr:
    """Reduce one edge expression by keeping numerically dominant terms."""
    if edge.domain == "impedance":
        base = sp.simplify(1 / edge.value)
        kept = _dominant_terms(base, frequency_hz, substitutions, relative_threshold)
        if _is_zero_expr(kept):
            return edge.value
        return sp.simplify(1 / kept)
    kept = _dominant_terms(edge.value, frequency_hz, substitutions, relative_threshold)
    return sp.simplify(kept)


def _prune_weak_subrange_edges(
    graph: SignalFlowGraph,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
    edge_relative_threshold: float,
) -> None:
    """Remove weak non-essential edges from one frequency-reduced graph."""
    incoming: dict[str, list[tuple[tuple[str, str, str], MetaEdge, float]]] = {}
    for key, edge in graph.meta_edges.items():
        if edge.domain == "voltage" or edge.source.endswith(".src") or edge.domain == "impedance":
            continue
        magnitude = _term_magnitude(edge.value, frequency_hz, substitutions)
        incoming.setdefault(edge.target, []).append((key, edge, magnitude))
    remove: set[tuple[str, str, str]] = set()
    for _target, edges in incoming.items():
        maximum = max((magnitude for _key, _edge, magnitude in edges), default=0.0)
        if maximum <= 0:
            continue
        threshold = max(float(edge_relative_threshold), 0.0) * maximum
        for key, _edge, magnitude in edges:
            if magnitude < threshold:
                remove.add(key)
    for key in remove:
        graph.meta_edges.pop(key, None)


def _dominant_terms(
    expr: sp.Expr,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
    relative_threshold: float,
) -> sp.Expr:
    """Keep additive terms whose magnitude is significant at one frequency."""
    terms = list(sp.Add.make_args(sp.expand(sp.sympify(expr))))
    if len(terms) <= 1:
        return sp.simplify(expr)
    magnitudes = [
        _term_magnitude(term, frequency_hz, substitutions)
        for term in terms
    ]
    maximum = max(magnitudes, default=0.0)
    if maximum <= 0:
        return sp.simplify(expr)
    threshold = max(float(relative_threshold), 0.0) * maximum
    kept_terms = [term for term, magnitude in zip(terms, magnitudes) if magnitude >= threshold]
    if not kept_terms:
        kept_terms = [terms[int(np.argmax(magnitudes))]]
    return sp.simplify(sum(kept_terms))


def _term_magnitude(
    expr: sp.Expr,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Evaluate an expression magnitude at one frequency."""
    try:
        value = evaluate_numeric_expression(expr, substitutions, keep_laplace=True).numeric
        value = value.subs(ini.laplace, 2 * sp.pi * sp.I * frequency_hz)
        return abs(complex(sp.N(value)))
    except Exception:
        return 0.0


def _partition_expression_for_domain(expr: sp.Expr, domain: str) -> dict[int | None, sp.Expr]:
    """Create order partitions for a reduced expression."""
    expr = sp.simplify(sp.sympify(expr))
    if _is_zero_expr(expr):
        return {}
    base = sp.simplify(1 / expr) if domain == "impedance" else expr
    terms = list(sp.Add.make_args(sp.expand(base)))
    grouped: dict[int | None, list[sp.Expr]] = {}
    for term in terms:
        grouped.setdefault(_laplace_order_of_term(term), []).append(term)
    partitions = {order: sp.simplify(sum(parts)) for order, parts in grouped.items()}
    if domain == "impedance" and len(partitions) == 1:
        return {None: expr}
    return partitions


def _laplace_order_of_term(expr: sp.Expr) -> int | None:
    """Return the Laplace order of a monomial-like term."""
    expr = sp.cancel(sp.sympify(expr))
    if ini.laplace not in expr.free_symbols:
        return 0
    numerator, denominator = sp.fraction(expr)
    try:
        num_poly = sp.Poly(numerator, ini.laplace)
        den_poly = sp.Poly(denominator, ini.laplace)
    except sp.PolynomialError:
        return None
    if len(num_poly.monoms()) != 1 or len(den_poly.monoms()) != 1:
        return None
    return int(num_poly.monoms()[0][0] - den_poly.monoms()[0][0])


def _is_zero_expr(expr: Any) -> bool:
    """Return True for structurally zero expressions."""
    value = sp.simplify(sp.sympify(expr))
    return value == 0 or value.is_zero is True


def _has_candidate_root(expr: sp.Expr) -> bool:
    """Return True if an expression has a numerator or denominator root in s."""
    expr = sp.cancel(sp.simplify(expr))
    numerator, denominator = expr.as_numer_denom()
    for part in (numerator, denominator):
        try:
            polynomial = sp.Poly(part, ini.laplace)
        except sp.PolynomialError:
            continue
        if polynomial.degree() > 0:
            return True
    return False


def _first_downstream_summing_vertex(
    path: SignalPath,
    edge_key: tuple[str, str, str],
    summing_vertices: set[str],
) -> str | None:
    """Return the first summing vertex downstream of a candidate edge."""
    try:
        edge_index = path.edges.index(edge_key)
    except ValueError:
        return None
    for vertex in path.vertices[edge_index + 1:]:
        if vertex in summing_vertices:
            return vertex
    return None


def _path_gain_to_vertex(
    graph: SignalFlowGraph,
    path: SignalPath,
    vertex: str,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Evaluate the magnitude of a forward-path gain up to one vertex."""
    if vertex not in path.vertices:
        return 0.0
    stop_index = path.vertices.index(vertex)
    gain = sp.Integer(1)
    for edge_key in path.edges[:stop_index]:
        edge = graph.meta_edges.get(edge_key)
        if edge is None:
            return 0.0
        numeric_value = evaluate_numeric_expression(edge.value, substitutions, keep_laplace=True).numeric
        numeric_value = numeric_value.subs(ini.laplace, 2 * sp.pi * sp.I * frequency_hz)
        gain *= numeric_value
    try:
        return abs(complex(sp.N(gain)))
    except Exception:
        return 0.0


def _max_feedback_loop_gain_containing_edge(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    edge_key: tuple[str, str, str],
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Return the strongest local feedback-loop gain containing one edge."""
    gains = [
        _loop_gain_magnitude(graph, loop, frequency_hz, substitutions)
        for loop in topology.feedback_loops
        if edge_key in loop.edges
    ]
    return max(gains, default=0.0)


def _effective_feedback_loop_gain_containing_edge(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    edge_key: tuple[str, str, str],
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Return loop gain with all feedback loops not containing the edge already closed."""
    loops = tuple(topology.feedback_loops)
    if not any(edge_key in loop.edges for loop in loops):
        return 0.0
    gains = tuple(
        _loop_gain_value(graph, loop, frequency_hz, substitutions)
        for loop in loops
    )
    remaining = tuple(
        (loop, gain)
        for loop, gain in zip(loops, gains)
        if edge_key not in loop.edges
    )
    delta_full = complex(sp.N(_mason_characteristic_equation(loops, gains)))
    delta_rest = complex(
        sp.N(
            _mason_characteristic_equation(
                tuple(item[0] for item in remaining),
                tuple(item[1] for item in remaining),
            )
        )
    )
    if abs(delta_rest) <= 1e-300:
        return math.inf
    return float(abs((delta_rest - delta_full) / delta_rest))


def _total_feedback_loop_gain_containing_edge(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    edge_key: tuple[str, str, str],
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Return the aggregate magnitude bound of feedback loops containing one edge."""
    return sum(
        _loop_gain_magnitude(graph, loop, frequency_hz, substitutions)
        for loop in topology.feedback_loops
        if edge_key in loop.edges
    )


def _feedback_loop_gain_envelope_containing_edge(
    graph: SignalFlowGraph,
    topology: PaperTopologyMetrics,
    edge_key: tuple[str, str, str],
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Return a conservative multi-loop gain for paper root-observability decisions."""
    effective = _effective_feedback_loop_gain_containing_edge(
        graph,
        topology,
        edge_key,
        frequency_hz,
        substitutions,
    )
    magnitude_bound = _total_feedback_loop_gain_containing_edge(
        graph,
        topology,
        edge_key,
        frequency_hz,
        substitutions,
    )
    return max(effective, magnitude_bound)


def _loop_gain_magnitude(
    graph: SignalFlowGraph,
    loop: FeedbackLoop,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> float:
    """Evaluate the magnitude of one feedback-loop edge product."""
    return float(abs(_loop_gain_value(graph, loop, frequency_hz, substitutions)))


def _loop_gain_value(
    graph: SignalFlowGraph,
    loop: FeedbackLoop,
    frequency_hz: float,
    substitutions: dict[str | sp.Symbol, Any],
) -> complex:
    """Evaluate one signed complex feedback-loop edge product."""
    gain = sp.Integer(1)
    for edge_key in loop.edges:
        edge = graph.meta_edges.get(edge_key)
        if edge is None:
            return 0.0 + 0.0j
        numeric_value = evaluate_numeric_expression(edge.value, substitutions, keep_laplace=True).numeric
        numeric_value = numeric_value.subs(ini.laplace, 2 * sp.pi * sp.I * frequency_hz)
        gain *= numeric_value
    try:
        return complex(sp.N(gain))
    except Exception:
        return 0.0 + 0.0j


def _path_gain_expr_to_vertex(
    graph: SignalFlowGraph,
    path: SignalPath,
    vertex: str,
    substitutions: dict[str | sp.Symbol, Any],
) -> sp.Expr | None:
    """Return a symbolic/numeric path gain expression up to one vertex."""
    if vertex not in path.vertices:
        return None
    stop_index = path.vertices.index(vertex)
    gain = sp.Integer(1)
    for edge_key in path.edges[:stop_index]:
        edge = graph.meta_edges.get(edge_key)
        if edge is None:
            return None
        gain *= evaluate_numeric_expression(edge.value, substitutions, keep_laplace=True).numeric
    return sp.cancel(sp.simplify(gain))


def _path_gain_raw_expr_to_vertex(
    graph: SignalFlowGraph,
    path: SignalPath,
    vertex: str,
) -> sp.Expr | None:
    """Return a lightweight symbolic path-gain product up to one vertex."""
    if vertex not in path.vertices:
        return None
    stop_index = path.vertices.index(vertex)
    gain = sp.Integer(1)
    for edge_key in path.edges[:stop_index]:
        edge = graph.meta_edges.get(edge_key)
        if edge is None:
            return None
        gain *= sp.sympify(edge.value)
    return sp.factor(gain)


def _refine_summing_point_symbolic_expressions(
    graph: SignalFlowGraph,
    roots: Iterable[SummingPointRoot],
    localizations: Iterable[RootLocalization],
    substitutions: dict[str | sp.Symbol, Any],
) -> list[SummingPointRoot]:
    """Use cluster-aware loop approximations to recover paper-style SPR formulas."""
    edge_root_clusters = _edge_root_cluster_map(localizations)
    refined: list[SummingPointRoot] = []
    for root in roots:
        loop_edges = _loop_edges_from_path_pair(root.path_edges)
        if not loop_edges or root.cluster_index is None:
            refined.append(root)
            continue
        crossing_equation = _cluster_approximated_loop_crossing_equation(
            graph,
            loop_edges,
            root.cluster_index,
            edge_root_clusters,
        )
        if crossing_equation is None:
            refined.append(root)
            continue
        expression = _nearest_symbolic_root_expression(
            crossing_equation,
            root.value,
            substitutions,
        )
        refined.append(
            replace(
                root,
                expression=expression or root.expression,
                crossing_equation=sp.factor(sp.simplify(crossing_equation)),
            )
        )
    return refined


def _edge_root_cluster_map(
    localizations: Iterable[RootLocalization],
) -> dict[tuple[str, str, str], int]:
    """Map each meta-edge to the nearest closed-loop cluster of its own root."""
    mapping: dict[tuple[str, str, str], int] = {}
    for item in localizations:
        if item.cluster_index is None:
            continue
        key = (item.source, item.target, item.domain)
        current = mapping.get(key)
        if current is None or item.cluster_index < current:
            mapping[key] = item.cluster_index
    return mapping


def _loop_edges_from_path_pair(
    path_pair: tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]
) -> tuple[tuple[str, str, str], ...] | None:
    """Return the feedback-loop suffix when one summing path extends the other."""
    left, right = path_pair
    if len(left) <= len(right) and right[: len(left)] == left:
        return right[len(left) :]
    if len(right) <= len(left) and left[: len(right)] == right:
        return left[len(right) :]
    return None


def _cluster_approximated_loop_crossing_equation(
    graph: SignalFlowGraph,
    loop_edges: Iterable[tuple[str, str, str]],
    cluster_index: int,
    edge_root_clusters: dict[tuple[str, str, str], int],
) -> sp.Expr | None:
    """Build numerator of 1-L(s)=0 after paper-style frequency subrange pruning."""
    loop_gain = sp.Integer(1)
    for edge_key in loop_edges:
        edge = graph.meta_edges.get(edge_key)
        if edge is None:
            return None
        value = _cluster_approximated_edge_value(
            sp.sympify(edge.value),
            cluster_index,
            edge_root_clusters.get(edge_key),
        )
        loop_gain *= value
    numerator, _denominator = sp.together(1 - loop_gain).as_numer_denom()
    return sp.factor(sp.simplify(numerator))


def _cluster_approximated_edge_value(
    value: sp.Expr,
    cluster_index: int,
    edge_root_cluster: int | None,
) -> sp.Expr:
    """Approximate an edge below/at/above the cluster of its own root."""
    if edge_root_cluster is None:
        return sp.sympify(value)
    numerator, denominator = sp.together(value).as_numer_denom()
    numerator = _cluster_approximated_polynomial(numerator, cluster_index, edge_root_cluster)
    denominator = _cluster_approximated_polynomial(denominator, cluster_index, edge_root_cluster)
    if denominator == 0:
        return sp.sympify(value)
    return sp.factor(sp.simplify(numerator / denominator))


def _cluster_approximated_polynomial(
    expression: sp.Expr,
    cluster_index: int,
    root_cluster: int,
) -> sp.Expr:
    """Keep low-, full-, or high-frequency terms of a polynomial by root cluster."""
    expression = sp.expand(sp.sympify(expression))
    laplace = _laplace_symbol_for(expression) or ini.laplace
    try:
        polynomial = sp.Poly(expression, laplace)
    except sp.PolynomialError:
        return expression
    if polynomial.degree() <= 0 or root_cluster == cluster_index:
        return expression
    terms: list[sp.Expr] = []
    if root_cluster > cluster_index:
        selected_degree = min(power[0] for power, coeff in polynomial.terms() if coeff != 0)
    else:
        selected_degree = max(power[0] for power, coeff in polynomial.terms() if coeff != 0)
    for power, coeff in polynomial.terms():
        if coeff != 0 and power[0] == selected_degree:
            terms.append(coeff * laplace ** power[0])
    if not terms:
        return expression
    return sp.factor(sp.simplify(sum(terms)))


def _dedupe_summing_point_roots(roots: Iterable[SummingPointRoot]) -> list[SummingPointRoot]:
    """Merge numerically identical summing-point roots at the same vertex."""
    deduped: list[SummingPointRoot] = []
    seen: set[tuple[str, int]] = set()
    for root in sorted(roots, key=lambda item: (item.frequency_hz, item.vertex)):
        bucket = (root.vertex, int(round(np.log10(max(root.frequency_hz, 1e-300)) * 1e6)))
        if bucket in seen:
            continue
        seen.add(bucket)
        deduped.append(root)
    return deduped


def _mark_dominant_summing_point_roots(
    roots: Iterable[SummingPointRoot],
    clusters: Iterable[RootCluster],
) -> list[SummingPointRoot]:
    """Mark the nearest SPR candidate for each closed-loop root in a cluster."""
    roots = list(roots)
    dominant_indices: set[int] = set()
    for cluster in clusters:
        cluster_indices = [
            index for index, root in enumerate(roots)
            if root.cluster_index == cluster.index and root.frequency_hz > 0
        ]
        if not cluster_indices:
            continue
        for closed_root in cluster.roots:
            best_index = min(
                cluster_indices,
                key=lambda index: _frequency_error_ratio(roots[index].frequency_hz, closed_root.frequency_hz),
            )
            dominant_indices.add(best_index)
    return [
        replace(root, dominant=index in dominant_indices)
        for index, root in enumerate(roots)
    ]


def _remark_dominant_summing_point_roots_from_olr(
    roots: Iterable[SummingPointRoot],
    localizations: Iterable[RootLocalization],
) -> list[SummingPointRoot]:
    """Prefer SPR roots that explain non-observable open-loop poles in the same cluster."""
    roots = list(roots)
    preferred_vertices: dict[int, set[str]] = {}
    for item in localizations:
        if item.cluster_index is None or item.category != "OLR-NO" or item.root_kind != "pole":
            continue
        preferred_vertices.setdefault(item.cluster_index, set()).add(item.source)
    dominant_indices: set[int] = set()
    for cluster_index, vertices in preferred_vertices.items():
        for index, root in enumerate(roots):
            if root.cluster_index == cluster_index:
                roots[index] = replace(root, dominant=False)
        candidates = [
            index for index, root in enumerate(roots)
            if root.cluster_index == cluster_index and root.vertex in vertices
        ]
        if not candidates:
            candidates = [
                index for index, root in enumerate(roots)
                if root.cluster_index == cluster_index
            ]
        if not candidates:
            continue
        reference_frequency = _cluster_olr_frequency(localizations, cluster_index)
        if reference_frequency is None:
            dominant_indices.add(candidates[0])
        else:
            below = [index for index in candidates if roots[index].frequency_hz < reference_frequency]
            if below:
                dominant_indices.add(min(below, key=lambda index: roots[index].frequency_hz))
            else:
                dominant_indices.add(
                    min(candidates, key=lambda index: _frequency_error_ratio(roots[index].frequency_hz, reference_frequency))
                )
    for index, root in enumerate(roots):
        if root.cluster_index in preferred_vertices:
            roots[index] = replace(root, dominant=index in dominant_indices)
    return roots


def _cluster_olr_frequency(localizations: Iterable[RootLocalization], cluster_index: int) -> float | None:
    """Return a representative non-observable open-loop pole frequency for one cluster."""
    candidates = [
        item.root_frequency_hz
        for item in localizations
        if item.cluster_index == cluster_index and item.category == "OLR-NO" and item.root_kind == "pole"
    ]
    if not candidates:
        return None
    return min(candidates)


def _nearest_root_frequency(
    roots: Iterable[RootSample],
    kind: str,
    target_hz: float,
) -> float | None:
    """Return nearest closed-loop root frequency with the requested kind."""
    candidates = [root.frequency_hz for root in roots if root.kind == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda value: _frequency_error_ratio(value, target_hz))


def _nearest_spr_frequency(
    roots: Iterable[SummingPointRoot],
    cluster_index: int,
    target_hz: float,
) -> float | None:
    """Return nearest dominant SPR frequency in a cluster."""
    candidates = [
        root.frequency_hz
        for root in roots
        if root.cluster_index == cluster_index and root.dominant
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda value: _frequency_error_ratio(value, target_hz))


def _nearest_olr_frequency(
    roots: Iterable[RootLocalization],
    cluster_index: int,
    kind: str,
    target_hz: float,
) -> float | None:
    """Return nearest OLR frequency in a cluster."""
    candidates = [
        root.root_frequency_hz
        for root in roots
        if root.cluster_index == cluster_index and root.root_kind == kind and root.root_frequency_hz > 0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda value: _frequency_error_ratio(value, target_hz))


def _format_optional_float(value: float | None, precision: int) -> str:
    """Format optional report floats."""
    if value is None:
        return "none"
    return f"{value:.{precision}e}"


def _append_symbolic_roots(
    lines: list[str],
    roots: Iterable[SymbolicRootExpression],
    precision: int,
) -> None:
    """Append symbolic/numeric root rows to a Markdown report."""
    roots = list(roots)
    if not roots:
        lines.append("- none")
        return
    for root in roots:
        expression = "numeric-only" if root.expression is None else sp.sstr(sp.factor(sp.simplify(root.expression)))
        numeric = "none" if root.numeric_value is None else str(root.numeric_value)
        frequency = "none" if root.frequency_hz is None else f"{root.frequency_hz:.{precision}e} Hz"
        exact = "symbolic" if root.exact else "numeric"
        lines.append(
            f"- {root.kind}: `{expression}`, value `{numeric}`, frequency `{frequency}`, "
            f"degree `{root.polynomial_degree}`, `{exact}`"
        )


def _roots_from_polynomial_like(expr: sp.Expr) -> list[complex]:
    """Return roots of a rational expression numerator in the Laplace variable."""
    expr = sp.cancel(sp.simplify(expr))
    numerator, _denominator = expr.as_numer_denom()
    numerator = sp.expand(numerator)
    laplace = _laplace_symbol_for(numerator)
    if laplace is None:
        return []
    try:
        polynomial = sp.Poly(sp.N(numerator), laplace)
    except sp.PolynomialError:
        return []
    if polynomial.degree() <= 0:
        return []
    try:
        return [complex(root) for root in polynomial.nroots(n=30, maxsteps=200)]
    except Exception:
        try:
            coeffs = [complex(sp.N(coeff)) for coeff in polynomial.all_coeffs()]
            return [complex(root) for root in np.roots(coeffs)]
        except Exception:
            return []


def _nearest_symbolic_root_expression(
    expr: sp.Expr,
    numeric_root: complex,
    substitutions: dict[str | sp.Symbol, Any] | None,
    max_degree: int = 2,
) -> sp.Expr | None:
    """Return the symbolic root expression nearest to a numerical candidate."""
    expr = sp.cancel(sp.simplify(expr))
    numerator, _denominator = expr.as_numer_denom()
    laplace = _laplace_symbol_for(numerator)
    if laplace is None:
        return None
    try:
        polynomial = sp.Poly(sp.expand(numerator), laplace)
    except sp.PolynomialError:
        return None
    degree = polynomial.degree()
    if degree <= 0 or degree > max_degree:
        return None
    if degree > 1 and sp.count_ops(polynomial.as_expr()) > 80:
        return None
    try:
        roots = _closed_form_roots(polynomial, laplace)
    except Exception:
        return None
    if not roots:
        return None
    substitutions = substitutions or {}
    parsed_subs = _parse_substitutions(substitutions)

    def distance(root_expr: sp.Expr) -> float:
        try:
            value = complex(sp.N(sp.sympify(root_expr).subs(parsed_subs)))
            return abs(value - numeric_root)
        except Exception:
            return float("inf")

    best = min(roots, key=distance)
    if not np.isfinite(distance(best)):
        return None
    return sp.factor(sp.simplify(best))


def _symbolic_roots_from_polynomial(
    expr: sp.Expr,
    kind: str,
    substitutions: dict[str | sp.Symbol, Any] | None,
    max_symbolic_degree: int,
) -> list[SymbolicRootExpression]:
    """Extract symbolic roots when practical and numerical roots as fallback."""
    expr = sp.factor(sp.simplify(sp.sympify(expr)))
    laplace = _laplace_symbol_for(expr)
    if laplace is None:
        return []
    try:
        polynomial = sp.Poly(sp.expand(expr), laplace)
    except sp.PolynomialError:
        return []
    degree = polynomial.degree()
    if degree <= 0:
        return []

    roots: list[SymbolicRootExpression] = []
    if degree <= max_symbolic_degree:
        symbolic_roots = _closed_form_roots(polynomial, laplace)
        if symbolic_roots:
            for root_expr in symbolic_roots:
                numeric_value = _numeric_root_value(root_expr, substitutions)
                roots.append(
                    SymbolicRootExpression(
                        kind=kind,
                        expression=sp.factor(sp.simplify(root_expr)),
                        numeric_value=numeric_value,
                        frequency_hz=None if numeric_value is None else abs(numeric_value) / (2 * np.pi),
                        polynomial_degree=degree,
                        exact=True,
                    )
                )
            return roots

    factored_roots = _closed_form_roots_from_factors(
        polynomial.as_expr(),
        laplace,
        max_symbolic_degree=max_symbolic_degree,
    )
    if factored_roots:
        for root_expr in factored_roots:
            numeric_value = _numeric_root_value(root_expr, substitutions)
            roots.append(
                SymbolicRootExpression(
                    kind=kind,
                    expression=sp.factor(sp.simplify(root_expr)),
                    numeric_value=numeric_value,
                    frequency_hz=None if numeric_value is None else abs(numeric_value) / (2 * np.pi),
                    polynomial_degree=degree,
                    exact=True,
                )
            )
        return roots

    for numeric_value in _numeric_roots_for_polynomial(polynomial.as_expr(), laplace, substitutions):
        roots.append(
            SymbolicRootExpression(
                kind=kind,
                expression=None,
                numeric_value=numeric_value,
                frequency_hz=abs(numeric_value) / (2 * np.pi),
                polynomial_degree=degree,
                exact=False,
            )
        )
    return roots


def _closed_form_roots(polynomial: sp.Poly, laplace: sp.Symbol) -> list[sp.Expr]:
    """Return simple symbolic roots for low-degree polynomials."""
    degree = polynomial.degree()
    if degree == 1:
        a1, a0 = polynomial.all_coeffs()
        return [sp.simplify(-a0 / a1)]
    roots = sp.roots(polynomial.as_expr(), laplace)
    if roots:
        result: list[sp.Expr] = []
        for root, multiplicity in roots.items():
            result.extend([sp.simplify(root)] * int(multiplicity))
        return result
    try:
        return [sp.simplify(root) for root in sp.solve(polynomial.as_expr(), laplace)]
    except Exception:
        return []


def _closed_form_roots_from_factors(
    expr: sp.Expr,
    laplace: sp.Symbol,
    max_symbolic_degree: int,
) -> list[sp.Expr]:
    """Extract symbolic roots from low-degree factors of a higher-order polynomial."""
    try:
        _coefficient, factors = sp.factor_list(sp.factor(sp.expand(expr)), laplace)
    except Exception:
        return []
    if not factors:
        return []
    roots: list[sp.Expr] = []
    covered_degree = 0
    for factor, multiplicity in factors:
        try:
            polynomial = sp.Poly(factor, laplace)
        except sp.PolynomialError:
            return []
        degree = polynomial.degree()
        if degree <= 0:
            continue
        covered_degree += degree * int(multiplicity)
        if degree > max_symbolic_degree:
            return []
        factor_roots = _closed_form_roots(polynomial, laplace)
        if not factor_roots:
            return []
        for root in factor_roots:
            roots.extend([sp.simplify(root)] * int(multiplicity))
    if covered_degree <= 0:
        return []
    return roots


def _numeric_roots_for_polynomial(
    expr: sp.Expr,
    laplace: sp.Symbol,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> list[complex]:
    """Return numerical roots of a polynomial after applying known substitutions."""
    numeric_expr = sp.expand(sp.sympify(expr).subs(_symbol_substitution_map(substitutions)))
    if laplace not in numeric_expr.free_symbols:
        return []
    try:
        polynomial = sp.Poly(sp.N(numeric_expr), laplace)
    except sp.PolynomialError:
        return []
    if polynomial.degree() <= 0:
        return []
    try:
        return [complex(root) for root in polynomial.nroots(n=30, maxsteps=200)]
    except Exception:
        try:
            coeffs = [complex(sp.N(coeff)) for coeff in polynomial.all_coeffs()]
            return [complex(root) for root in np.roots(coeffs)]
        except Exception:
            return []


def _numeric_root_value(
    expr: sp.Expr,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> complex | None:
    """Evaluate one symbolic root if all required parameter values are known."""
    try:
        value = sp.sympify(expr).subs(_symbol_substitution_map(substitutions))
        if value.free_symbols:
            return None
        return complex(sp.N(value))
    except Exception:
        return None


def _symbol_substitution_map(substitutions: dict[str | sp.Symbol, Any] | None) -> dict[sp.Symbol, sp.Expr]:
    """Normalize substitution keys to SymPy symbols."""
    result: dict[sp.Symbol, sp.Expr] = {}
    for key, value in (substitutions or {}).items():
        result[sp.Symbol(str(key))] = sp.sympify(value)
    return result


def _laplace_symbol_for(expr: sp.Expr) -> sp.Symbol | None:
    """Return the Laplace symbol used in an expression."""
    for symbol in expr.free_symbols:
        if str(symbol) == str(ini.laplace):
            return symbol
    if ini.laplace in expr.free_symbols:
        return ini.laplace
    return None


def _match_root_cluster(
    frequency_hz: float,
    clusters: tuple[RootCluster, ...],
    specs_by_cluster: dict[int, ErrorSubSpecification],
) -> tuple[RootCluster | None, float | None]:
    """Match a candidate root frequency to a reference root cluster."""
    if not clusters:
        return None, None
    for cluster in clusters:
        spec = specs_by_cluster.get(cluster.index)
        if spec is None:
            continue
        if spec.lower_frequency_hz <= frequency_hz <= spec.upper_frequency_hz:
            return cluster, _frequency_error_ratio(frequency_hz, cluster.center_frequency_hz)
    nearest = min(clusters, key=lambda cluster: _frequency_error_ratio(frequency_hz, cluster.center_frequency_hz))
    ratio = _frequency_error_ratio(frequency_hz, nearest.center_frequency_hz)
    return nearest, ratio


def _frequency_error_ratio(left_hz: float, right_hz: float) -> float:
    """Return a scale-normalized frequency distance."""
    scale = max(abs(left_hz), abs(right_hz), 1.0)
    return abs(left_hz - right_hz) / scale


def _analyze_meta_edge_candidate(
    edge: MetaEdge,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> MetaEdgeCandidate:
    """Analyze one meta-edge and return a candidate descriptor."""
    numeric_value = evaluate_numeric_expression(edge.value, substitutions or {}).numeric
    roots = tuple(_candidate_roots_from_expression(numeric_value))
    orders = [order for order in edge.order_partitions if order is not None]
    if orders:
        dynamic_order_span = max(orders) - min(orders)
    else:
        dynamic_order_span = 0
    structural_score = (
        len(edge.contributions)
        + 2 * len(roots)
        + dynamic_order_span
        + (1 if edge.domain == "impedance" else 0)
    )
    return MetaEdgeCandidate(
        source=edge.source,
        target=edge.target,
        domain=edge.domain,
        symbolic_value=edge.value,
        numeric_value=sp.simplify(numeric_value),
        order_partitions=edge.order_partitions,
        roots=roots,
        contribution_count=len(edge.contributions),
        dynamic_order_span=dynamic_order_span,
        structural_score=float(structural_score),
    )


def _candidate_roots_from_expression(expr: sp.Expr) -> list[CandidateRoot]:
    """Extract numerical numerator and denominator roots from one edge expression."""
    expr = sp.cancel(sp.simplify(expr))
    numerator, denominator = expr.as_numer_denom()
    candidates: list[CandidateRoot] = []
    candidates.extend(_roots_from_polynomial(numerator, "zero"))
    candidates.extend(_roots_from_polynomial(denominator, "pole"))
    candidates.sort(key=lambda root: root.frequency_hz)
    return candidates


def _roots_from_polynomial(expr: sp.Expr, kind: str) -> list[CandidateRoot]:
    """Return numerical roots of one polynomial-like expression."""
    expr = sp.expand(sp.sympify(expr))
    if ini.laplace not in expr.free_symbols:
        return []
    try:
        polynomial = sp.Poly(sp.N(expr), ini.laplace)
    except sp.PolynomialError:
        return []
    if polynomial.degree() <= 0:
        return []
    roots = []
    for root in sp.nroots(polynomial.as_expr()):
        value = complex(root)
        if abs(value) <= 1e-30:
            continue
        roots.append(CandidateRoot(kind=kind, value=value, frequency_hz=abs(value) / (2 * np.pi)))
    return roots


def _cluster_frequency_boundaries(clusters: list[RootCluster]) -> list[tuple[float, float]]:
    """Create frequency bands from root-cluster borders using paper eqs. (13)-(14)."""
    boundaries: list[tuple[float, float]] = []
    min_freqs = [_cluster_min_frequency_hz(cluster) for cluster in clusters]
    max_freqs = [_cluster_max_frequency_hz(cluster) for cluster in clusters]
    for index, _cluster in enumerate(clusters):
        if index == 0:
            lower = 0.0
        else:
            lower = float(np.sqrt(max(max_freqs[index - 1], 0.0) * max(min_freqs[index], 0.0)))
        if index == len(clusters) - 1:
            upper = float("inf")
        else:
            upper = float(np.sqrt(max(max_freqs[index], 0.0) * max(min_freqs[index + 1], 0.0)))
        boundaries.append((lower, upper))
    return boundaries


def _cluster_min_frequency_hz(cluster: RootCluster) -> float:
    """Return the smallest root magnitude in a cluster, in Hz."""
    return min(root.frequency_hz for root in cluster.roots)


def _cluster_max_frequency_hz(cluster: RootCluster) -> float:
    """Return the largest root magnitude in a cluster, in Hz."""
    return max(root.frequency_hz for root in cluster.roots)
