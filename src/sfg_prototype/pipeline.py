#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end SFG simplification pipeline based on the paper algorithm.

This module is the orchestration layer after ``LinearNetwork -> SignalFlowGraph``.
SLiCAP remains the reference solver for the original netlist. Simplified graphs
are evaluated by solving the SFG linear equations directly:

    x_target = sum(edge_value * x_source)

with source vertices fixed by ``SourceDrive.value``. This gives a practical
closed loop for ranking graph manipulations by numerical error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any, Iterable

import numpy as np
import sympy as sp

from .slicap_compat import ini
from .network import evaluate_numeric_expression
from .sfg import MetaEdge, SignalFlowGraph
from .analysis import (
    DescriptorMatrixResult,
    ErrorSubSpecification,
    FrequencySample,
    GraphTopology,
    LocalizedSymbolicRoot,
    LocalizedSymbolicZero,
    NumericReferenceResult,
    PaperComplexity,
    ReferencePipelineResult,
    RootLocalization,
    SFGSubgraph,
    SummingPointRoot,
    SymbolicRootExpression,
    SubrangeSymbolicTransfer,
    TargetRootSymbolicApproximation,
    analyze_meta_edge_candidates,
    analyze_graph_topology,
    analyze_paper_topology,
    derive_local_summing_root_expression,
    derive_local_summing_zero_expression,
    derive_target_root_symbolic_approximations,
    frequency_reduced_graph,
    localize_candidate_roots,
    localize_closed_loop_summing_roots,
    paper_complexity,
    root_observability_check,
    run_reference_pipeline,
    sample_descriptor_frequency_response,
    solve_cluster_approximated_symbolic_transfer,
    solve_subrange_symbolic_transfer,
    symbolic_transfer_roots,
    validate_localized_zero_corners,
)
from .simplify import (
    GraphComplexity,
    GraphManipulation,
    apply_graph_manipulation,
    graph_complexity,
    propose_graph_manipulations,
    propose_open_loop_root_removals,
    propose_local_zero_substitutions,
    propose_signal_path_removals,
    propose_subgraph_substitutions,
    propose_vertex_pair_contractions,
)


@dataclass(frozen=True)
class SimplificationConfig:
    """Configuration for the full paper-style simplification loop."""

    cluster_tolerance: float = 0.9
    total_error_budget: float = 0.05
    error_norm: str = "auto"
    pre_reduction_fraction: float = 1e-4
    bias_node_merges: tuple[tuple[str, str], ...] = ()
    points_per_subrange: int = 16
    include_subrange_boundaries: bool = True
    max_steps: int = 30
    max_candidates_per_round: int = 80
    candidate_generation_mode: str = "auto"
    exhaustive_candidate_edge_limit: int = 16
    ranking_alpha: float = 0.0
    strict_graph: bool = False
    use_simplified_complexity: bool = False
    frequency_range_hz: tuple[float, float] | None = None
    magnitude_error_db: float | None = None
    phase_error_deg: float | None = None
    max_paths: int = 256
    max_loops: int = 512
    max_cuts: int = 256
    strict_cuts_only: bool = False
    root_observability_dominance_ratio: float = 10.0
    root_feedback_loop_gain_threshold: float = 1.0
    max_steps_per_subrange: int | None = None
    manipulation_sequence: tuple[str, ...] = ("VPC", "RSP", "RR", "SS")
    evaluate_subgraph_candidates_only: bool = True
    apply_subrange_to_global: bool = False
    use_dominance_reduced_subrange_seed: bool = False
    enable_symbolic_spr_expressions: bool = False
    subrange_term_relative_threshold: float = 0.1
    subrange_edge_relative_threshold: float = 1e-5
    max_symbolic_root_degree: int = 2
    enable_local_spr_expressions: bool = True
    enable_target_root_expressions: bool = True
    target_root_use_frequency_reduction: bool = False
    enforce_target_root_error: bool = False
    target_root_relative_error: float = 0.05
    max_local_spr_loops: int = 12
    local_spr_relative_frequency_error: float = 0.5
    local_spr_parameter_tolerances: tuple[tuple[str, float], ...] = ()
    local_spr_default_parameter_tolerance: float | None = None
    local_spr_max_corner_parameters: int = 6
    local_spr_corner_relative_error: float | None = None
    local_spr_reported_sensitivities: int = 8
    enable_local_spr_term_pruning: bool = True
    local_spr_max_term_pruning_steps: int = 12
    local_spr_max_term_count: int = 64
    local_spr_term_relative_frequency_error: float = 0.05
    local_spr_term_corner_relative_error: float = 0.05
    enable_local_zero_expressions: bool = True
    local_zero_strategy: str = "auto"
    local_zero_max_paths: int = 64
    local_zero_max_loops: int = 64
    local_zero_max_vertices: int = 12
    local_zero_max_symbolic_operations: int = 5000
    local_zero_parameter_tolerances: tuple[tuple[str, float], ...] = ()
    local_zero_max_corner_parameters: int = 6
    enable_transfer_term_pruning: bool = True
    transfer_term_max_steps: int = 12
    transfer_term_max_count: int = 128
    transfer_term_parameter_tolerances: tuple[tuple[str, float], ...] = ()
    transfer_term_default_parameter_tolerance: float | None = None
    transfer_term_max_corner_parameters: int = 4
    enable_transfer_joint_order_pruning: bool = True
    transfer_term_max_joint_size: int = 8
    transfer_parameter_report_limit: int = 10
    rr_partition_relative_threshold: float = 0.2
    rsp_path_relative_threshold: float = 0.2
    require_baseline_equivalence: bool = True
    baseline_relative_tolerance: float = 1e-8
    reference_mode: str = "slicap_symbolic"

    def performance_specification(self) -> "PerformanceSpecification":
        """Return the user performance-error specification from paper Section III-B."""
        return PerformanceSpecification(
            error_norm=self.error_norm,
            relative_error_limit=self.total_error_budget,
            magnitude_error_db=self.magnitude_error_db,
            phase_error_deg=self.phase_error_deg,
        )


class BaselineEquivalenceError(RuntimeError):
    """Raised when the unsimplified SFG disagrees with the SLiCAP reference."""


@dataclass(frozen=True)
class PerformanceSpecification:
    """User error boundary over a frequency range, corresponding to paper Eq.(6)."""

    error_norm: str = "auto"
    relative_error_limit: float = 0.05
    magnitude_error_db: float | None = None
    phase_error_deg: float | None = None

    def scaled(self, factor: float) -> "PerformanceSpecification":
        """Scale every active error boundary, as required by paper presimplification."""
        factor = float(factor)
        if factor < 0:
            raise ValueError("Performance-boundary scale factor must be non-negative.")
        return PerformanceSpecification(
            error_norm=self.error_norm,
            relative_error_limit=self.relative_error_limit * factor,
            magnitude_error_db=None if self.magnitude_error_db is None else self.magnitude_error_db * factor,
            phase_error_deg=None if self.phase_error_deg is None else self.phase_error_deg * factor,
        )


@dataclass(frozen=True)
class GraphPerformanceSample:
    """One frequency sample of an SFG transfer function."""

    frequency_hz: float
    value: complex

    @property
    def magnitude(self) -> float:
        """Return the sample magnitude."""
        return abs(self.value)

    @property
    def phase_deg(self) -> float:
        """Return the sample phase in degrees."""
        return float(np.angle(self.value, deg=True))


@dataclass(frozen=True)
class GraphPerformance:
    """Transfer function and frequency samples of one SFG."""

    transfer: sp.Expr
    source: str
    detector: str
    detector_vertex: str
    samples: tuple[GraphPerformanceSample, ...]


@dataclass(frozen=True)
class GraphPerformanceError:
    """Numerical error between an SFG and the SLiCAP reference."""

    max_relative_error: float
    rms_relative_error: float
    max_absolute_error: float
    accepted: bool
    norm_error: float = 0.0
    norm_name: str = "relative_inf"
    max_magnitude_error_db: float = 0.0
    max_phase_error_deg: float = 0.0
    magnitude_limit_db: float | None = None
    phase_limit_deg: float | None = None
    relative_error_budget: float | None = None
    error_scope: str = "absolute"
    worst_magnitude_frequency_hz: float | None = None
    worst_phase_frequency_hz: float | None = None


@dataclass(frozen=True)
class DominantParameterInfluence:
    """Frequency-domain importance of one retained symbolic parameter."""

    parameter: str
    max_normalized_sensitivity: float
    representative_normalized_sensitivity: float
    peak_frequency_hz: float
    numerator_term_count: int
    denominator_term_count: int
    pole_expression_count: int
    zero_expression_count: int


@dataclass(frozen=True)
class TransferTermPruningStep:
    """One accepted single-term or same-order joint removal from H_j(s)."""

    index: int
    side: str
    removed_terms: tuple[sp.Expr, ...]
    laplace_order: int | None
    joint: bool
    nominal_norm_error: float
    corner_max_norm_error: float | None


@dataclass(frozen=True)
class DominantTermSymbolicTransfer:
    """A shorter H_j(s) accepted against the original reference performance."""

    transfer: sp.Expr
    numerator: sp.Expr
    denominator: sp.Expr
    poles: tuple[SymbolicRootExpression, ...]
    zeros: tuple[SymbolicRootExpression, ...]
    full_term_count: int
    retained_term_count: int
    nominal_error: GraphPerformanceError
    corner_max_norm_error: float | None = None
    corner_count: int = 0
    parameter_influences: tuple[DominantParameterInfluence, ...] = ()
    discarded_parameters: tuple[str, ...] = ()
    pruning_steps: tuple[TransferTermPruningStep, ...] = ()
    representation: str = "term-pruned"
    readability_operations_before: int | None = None
    readability_operations_after: int | None = None


@dataclass(frozen=True)
class SimplificationStep:
    """One accepted or rejected graph simplification attempt."""

    index: int
    phase: str
    cluster_index: int | None
    manipulation: GraphManipulation
    accepted: bool
    reason: str
    error: GraphPerformanceError | None
    complexity_before: GraphComplexity
    complexity_after: GraphComplexity | None
    paper_complexity_before: PaperComplexity | None = None
    paper_complexity_after: PaperComplexity | None = None
    quality: float | None = None
    ranking_value: float | None = None
    relative_error: GraphPerformanceError | None = None


@dataclass(frozen=True)
class ManipulationEvaluation:
    """Paper Eq.(11)/(12) evaluation of one candidate manipulation."""

    manipulation: GraphManipulation
    graph: SignalFlowGraph
    relative_error: GraphPerformanceError
    absolute_error: GraphPerformanceError
    complexity_before: PaperComplexity
    complexity_after: PaperComplexity
    quality: float
    ranking_value: float

    @property
    def error(self) -> GraphPerformanceError:
        """Return cumulative absolute error for backward-compatible callers."""
        return self.absolute_error


@dataclass(frozen=True)
class RootLocalizationResult:
    """Root localization output grouped with graph topology."""

    topology: GraphTopology
    localizations: tuple[RootLocalization, ...]


@dataclass(frozen=True)
class SubrangeSimplificationResult:
    """One error-controlled simplified frequency subgraph G_j^*."""

    cluster_index: int
    lower_frequency_hz: float
    upper_frequency_hz: float
    initial_graph: SignalFlowGraph
    simplified_graph: SignalFlowGraph
    transfer: SubrangeSymbolicTransfer
    dominant_term_transfer: DominantTermSymbolicTransfer | None
    paper_style_transfer: SubrangeSymbolicTransfer | None
    localized_symbolic_roots: tuple[LocalizedSymbolicRoot, ...]
    localized_symbolic_zeros: tuple[LocalizedSymbolicZero, ...]
    target_root_approximations: tuple[TargetRootSymbolicApproximation, ...]
    performance: GraphPerformance
    error: GraphPerformanceError
    steps: tuple[SimplificationStep, ...]


@dataclass
class _ManipulationAnalysisCache:
    """Per-run cache for immutable graph-analysis and numeric-performance results."""

    graph_keys: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    graph_refs: dict[int, SignalFlowGraph] = field(default_factory=dict)
    complexity_analyses: dict[tuple[Any, ...], tuple[Any, Any, PaperComplexity]] = field(
        default_factory=dict
    )
    graph_topologies: dict[tuple[Any, ...], GraphTopology] = field(default_factory=dict)
    performances: dict[tuple[Any, ...], GraphPerformance] = field(default_factory=dict)
    symbolic_transfers: dict[tuple[Any, ...], SubrangeSymbolicTransfer] = field(
        default_factory=dict
    )
    root_contexts: dict[
        tuple[Any, ...],
        tuple[tuple[RootLocalization, ...], tuple[SummingPointRoot, ...]],
    ] = field(default_factory=dict)


@dataclass(frozen=True)
class SimplificationResult:
    """Complete result of the paper-style simplification pipeline."""

    config: SimplificationConfig
    pipeline: ReferencePipelineResult
    final_graph: SignalFlowGraph
    baseline_performance: GraphPerformance
    baseline_error: GraphPerformanceError
    final_performance: GraphPerformance
    final_error: GraphPerformanceError
    steps: tuple[SimplificationStep, ...]
    root_localization: RootLocalizationResult
    substitutions: dict[str | sp.Symbol, Any]
    subrange_results: tuple[SubrangeSimplificationResult, ...] = ()
    subrange_operations_applied_to_global: bool = False

    @property
    def accepted_steps(self) -> tuple[SimplificationStep, ...]:
        """Return accepted simplification steps."""
        return tuple(step for step in self.steps if step.accepted)

    @property
    def rejected_steps(self) -> tuple[SimplificationStep, ...]:
        """Return rejected simplification steps."""
        return tuple(step for step in self.steps if not step.accepted)


def simplify_netlist(
    file_path: str,
    config: SimplificationConfig | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    source: str | None = None,
    detector: str | None = None,
) -> SimplificationResult:
    """Run netlist parsing, SFG construction, reference analysis and simplification."""
    config = config or SimplificationConfig()
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
        root_observability_dominance_ratio=config.root_observability_dominance_ratio,
        root_feedback_loop_gain_threshold=config.root_feedback_loop_gain_threshold,
        frequency_range_hz=config.frequency_range_hz,
        enable_symbolic_spr_expressions=config.enable_symbolic_spr_expressions,
        subrange_term_relative_threshold=config.subrange_term_relative_threshold,
        subrange_edge_relative_threshold=config.subrange_edge_relative_threshold,
        max_symbolic_root_degree=config.max_symbolic_root_degree,
        reference_mode=config.reference_mode,
    )
    return simplify_graph(
        pipeline.graph,
        pipeline,
        config=config,
        substitutions=pipeline.network.numeric_substitutions(extra=substitutions),
    )


def simplify_graph(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
) -> SimplificationResult:
    """Run the error-controlled simplification loop on an existing SFG."""
    config = config or SimplificationConfig()
    substitutions = substitutions or pipeline.network.numeric_substitutions()
    performance_spec = config.performance_specification()
    all_frequencies = _frequency_grid(
        pipeline.error_specs,
        pipeline.reference,
        config.points_per_subrange,
        frequency_range_hz=config.frequency_range_hz,
        include_boundaries=config.include_subrange_boundaries,
    )
    baseline, baseline_error = validate_graph_equivalence(
        graph,
        pipeline,
        all_frequencies,
        substitutions,
        relative_tolerance=config.baseline_relative_tolerance,
        raise_on_failure=config.require_baseline_equivalence,
    )
    reference_samples = _pipeline_reference_samples(pipeline.reference, all_frequencies, substitutions)

    current_graph = graph
    analysis_cache = _ManipulationAnalysisCache()
    steps: list[SimplificationStep] = []
    subrange_results: list[SubrangeSimplificationResult] = []
    step_index = 1
    pre_spec = performance_spec.scaled(config.pre_reduction_fraction)
    current_graph, step_index = _run_bias_presimplification(
        current_graph,
        pipeline,
        config,
        substitutions,
        all_frequencies,
        pre_spec,
        steps,
        step_index,
    )

    for spec in pipeline.error_specs:
        subrange_frequencies = _frequency_grid(
            (spec,),
            pipeline.reference,
            config.points_per_subrange,
            frequency_range_hz=config.frequency_range_hz,
            include_boundaries=config.include_subrange_boundaries,
        )
        root_index_start = 1 + sum(
            len(cluster.roots)
            for cluster in pipeline.clusters
            if cluster.index < spec.cluster_index
        )
        target_clusters = tuple(
            cluster
            for cluster in pipeline.clusters
            if cluster.index == spec.cluster_index
        )
        target_localizations = tuple(
            item
            for item in pipeline.localizations
            if item.cluster_index == spec.cluster_index
        )
        target_summing_roots = tuple(
            item
            for item in pipeline.summing_point_roots
            if item.cluster_index == spec.cluster_index
        )
        if config.use_dominance_reduced_subrange_seed:
            subrange_graph = frequency_reduced_graph(
                current_graph,
                spec,
                substitutions=substitutions,
                relative_threshold=config.subrange_term_relative_threshold,
                edge_relative_threshold=config.subrange_edge_relative_threshold,
            )
        else:
            subrange_graph = current_graph
        subrange_step_start = len(steps)
        simplified_subrange_graph, step_index = _run_simplification_round(
            subrange_graph,
            pipeline,
            config,
            substitutions,
            subrange_frequencies,
            performance_spec,
            "subrange",
            spec.cluster_index,
            steps,
            step_index,
            apply_accepted_to_current=True,
            analysis_cache=analysis_cache,
        )
        if config.apply_subrange_to_global:
            current_graph = simplified_subrange_graph
        subrange_performance = evaluate_graph_performance(
            simplified_subrange_graph,
            source=pipeline.reference.source,
            detector=pipeline.reference.detector,
            frequencies=subrange_frequencies,
            substitutions=substitutions,
            symbolic_transfer=False,
        )
        subrange_reference = _pipeline_reference_samples(
            pipeline.reference,
            subrange_frequencies,
            substitutions,
        )
        subrange_error = compare_graph_performance(
            subrange_performance,
            subrange_reference,
            error_budget=performance_spec.relative_error_limit,
            error_norm=performance_spec.error_norm,
            magnitude_error_db=performance_spec.magnitude_error_db,
            phase_error_deg=performance_spec.phase_error_deg,
            error_scope="absolute",
        )
        subrange_transfer = _cached_subrange_symbolic_transfer(
            analysis_cache,
            simplified_subrange_graph,
            spec,
            source=pipeline.reference.source,
            detector=pipeline.reference.detector,
            substitutions=substitutions,
            relative_threshold=0.0,
            edge_relative_threshold=0.0,
            max_symbolic_root_degree=config.max_symbolic_root_degree,
        )
        dominant_term_transfer = None
        if (
            config.enable_transfer_term_pruning
            and subrange_transfer.success
            and isinstance(pipeline.reference, NumericReferenceResult)
        ):
            dominant_term_transfer = simplify_symbolic_transfer_terms(
                subrange_transfer,
                frequencies=subrange_frequencies,
                reference_expression=pipeline.reference.laplace,
                substitutions=substitutions,
                performance_specification=performance_spec,
                max_steps=config.transfer_term_max_steps,
                max_terms=config.transfer_term_max_count,
                parameter_tolerances=dict(config.transfer_term_parameter_tolerances),
                default_parameter_tolerance=config.transfer_term_default_parameter_tolerance,
                max_corner_parameters=config.transfer_term_max_corner_parameters,
                joint_order_pruning=config.enable_transfer_joint_order_pruning,
                max_joint_size=config.transfer_term_max_joint_size,
                max_symbolic_root_degree=config.max_symbolic_root_degree,
                parameter_report_limit=config.transfer_parameter_report_limit,
            )
        paper_style_transfer = solve_cluster_approximated_symbolic_transfer(
            pipeline.graph,
            spec,
            pipeline.localizations,
            source=pipeline.reference.source,
            detector=pipeline.reference.detector,
            substitutions=substitutions,
            edge_relative_threshold=config.subrange_edge_relative_threshold,
            max_symbolic_root_degree=config.max_symbolic_root_degree,
        )
        localized_symbolic_roots: tuple[LocalizedSymbolicRoot, ...] = ()
        if config.enable_local_spr_expressions:
            localized_symbolic_roots = tuple(
                expression
                for root in pipeline.summing_point_roots
                if root.cluster_index == spec.cluster_index
                for expression in (
                    derive_local_summing_root_expression(
                        simplified_subrange_graph,
                        root,
                        substitutions=substitutions,
                        max_loops=config.max_local_spr_loops,
                        max_relative_frequency_error=config.local_spr_relative_frequency_error,
                        parameter_tolerances=dict(config.local_spr_parameter_tolerances),
                        default_parameter_tolerance=config.local_spr_default_parameter_tolerance,
                        max_corner_parameters=config.local_spr_max_corner_parameters,
                        max_corner_relative_error=config.local_spr_corner_relative_error,
                        max_reported_sensitivities=config.local_spr_reported_sensitivities,
                        enable_term_pruning=config.enable_local_spr_term_pruning,
                        max_term_pruning_steps=config.local_spr_max_term_pruning_steps,
                        max_term_count=config.local_spr_max_term_count,
                        term_relative_frequency_error=config.local_spr_term_relative_frequency_error,
                        term_corner_relative_error=config.local_spr_term_corner_relative_error,
                    ),
                )
                if expression is not None
            )
        target_root_approximations: tuple[TargetRootSymbolicApproximation, ...] = ()
        localized_symbolic_zeros: tuple[LocalizedSymbolicZero, ...] = ()
        if config.enable_target_root_expressions:
            root_error_limit = (
                config.target_root_relative_error
                if config.enforce_target_root_error
                else math.inf
            )
            target_expression_graph = (
                frequency_reduced_graph(
                    simplified_subrange_graph,
                    spec,
                    substitutions=substitutions,
                    relative_threshold=config.subrange_term_relative_threshold,
                    edge_relative_threshold=config.subrange_edge_relative_threshold,
                )
                if config.target_root_use_frequency_reduction
                else simplified_subrange_graph
            )
            if config.enable_local_zero_expressions:
                localized_symbolic_zeros = tuple(
                    validate_localized_zero_corners(
                        derive_local_summing_zero_expression(
                            target_expression_graph,
                            item,
                            pipeline.topology_metrics,
                            substitutions=substitutions,
                            max_paths=config.local_zero_max_paths,
                            max_loops=config.local_zero_max_loops,
                            max_vertices=config.local_zero_max_vertices,
                            max_symbolic_operations=config.local_zero_max_symbolic_operations,
                            max_relative_root_error=root_error_limit,
                            strategy=config.local_zero_strategy,
                        ),
                        pipeline.reference.numerator,
                        substitutions,
                        dict(config.local_zero_parameter_tolerances),
                        max_corner_parameters=config.local_zero_max_corner_parameters,
                        max_relative_root_error=root_error_limit,
                    )
                    if isinstance(pipeline.reference, NumericReferenceResult)
                    else derive_local_summing_zero_expression(
                        target_expression_graph,
                        item,
                        pipeline.topology_metrics,
                        substitutions=substitutions,
                        max_paths=config.local_zero_max_paths,
                        max_loops=config.local_zero_max_loops,
                        max_vertices=config.local_zero_max_vertices,
                        max_symbolic_operations=config.local_zero_max_symbolic_operations,
                        max_relative_root_error=root_error_limit,
                        strategy=config.local_zero_strategy,
                    )
                    for item in target_summing_roots
                    if "zero" in item.root_source
                )
            target_root_approximations = derive_target_root_symbolic_approximations(
                target_expression_graph,
                target_clusters,
                target_localizations,
                target_summing_roots,
                substitutions=substitutions,
                max_relative_root_error=config.target_root_relative_error,
                enforce_relative_root_error=config.enforce_target_root_error,
                max_local_loops=config.max_local_spr_loops,
                parameter_tolerances=dict(config.local_spr_parameter_tolerances),
                default_parameter_tolerance=config.local_spr_default_parameter_tolerance,
                max_corner_parameters=config.local_spr_max_corner_parameters,
                localized_spr_expressions=localized_symbolic_roots,
                localized_spr_zero_expressions=localized_symbolic_zeros,
                summing_root_graph=simplified_subrange_graph,
                root_context_localizations=pipeline.localizations,
                root_index_start=root_index_start,
            )
            if config.target_root_use_frequency_reduction and any(
                item.status == "unresolved" for item in target_root_approximations
            ):
                full_graph_candidates = derive_target_root_symbolic_approximations(
                    simplified_subrange_graph,
                    target_clusters,
                    target_localizations,
                    target_summing_roots,
                    substitutions=substitutions,
                    max_relative_root_error=config.target_root_relative_error,
                    enforce_relative_root_error=config.enforce_target_root_error,
                    max_local_loops=config.max_local_spr_loops,
                    parameter_tolerances=dict(config.local_spr_parameter_tolerances),
                    default_parameter_tolerance=config.local_spr_default_parameter_tolerance,
                    max_corner_parameters=config.local_spr_max_corner_parameters,
                    localized_spr_expressions=localized_symbolic_roots,
                    localized_spr_zero_expressions=localized_symbolic_zeros,
                    summing_root_graph=simplified_subrange_graph,
                    root_context_localizations=pipeline.localizations,
                    root_index_start=root_index_start,
                )
                fallback_by_root = {
                    item.root_index: item for item in full_graph_candidates
                }
                target_root_approximations = tuple(
                    fallback_by_root.get(item.root_index, item)
                    if item.status == "unresolved"
                    and fallback_by_root.get(item.root_index, item).status != "unresolved"
                    else item
                    for item in target_root_approximations
                )
            target_root_approximations = tuple(
                replace(
                    item,
                    expression=_best_readable_rational_factorization(item.expression),
                )
                if item.expression is not None
                else item
                for item in target_root_approximations
            )
        subrange_results.append(
            SubrangeSimplificationResult(
                cluster_index=spec.cluster_index,
                lower_frequency_hz=spec.lower_frequency_hz,
                upper_frequency_hz=spec.upper_frequency_hz,
                initial_graph=subrange_graph,
                simplified_graph=simplified_subrange_graph,
                transfer=subrange_transfer,
                dominant_term_transfer=dominant_term_transfer,
                paper_style_transfer=paper_style_transfer,
                localized_symbolic_roots=localized_symbolic_roots,
                localized_symbolic_zeros=localized_symbolic_zeros,
                target_root_approximations=target_root_approximations,
                performance=subrange_performance,
                error=subrange_error,
                steps=tuple(steps[subrange_step_start:]),
            )
        )

    final_performance = evaluate_graph_performance(
        current_graph,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
        frequencies=all_frequencies,
        substitutions=substitutions,
    )
    final_error = compare_graph_performance(
        final_performance,
        reference_samples,
        error_budget=performance_spec.relative_error_limit,
        error_norm=performance_spec.error_norm,
        magnitude_error_db=performance_spec.magnitude_error_db,
        phase_error_deg=performance_spec.phase_error_deg,
        error_scope="absolute",
    )
    return SimplificationResult(
        config=config,
        pipeline=pipeline,
        final_graph=current_graph,
        baseline_performance=baseline,
        baseline_error=baseline_error,
        final_performance=final_performance,
        final_error=final_error,
        steps=tuple(steps),
        root_localization=RootLocalizationResult(
            topology=pipeline.topology,
            localizations=pipeline.localizations,
        ),
        substitutions=dict(substitutions),
        subrange_results=tuple(subrange_results),
        subrange_operations_applied_to_global=config.apply_subrange_to_global,
    )


def graph_transfer_function(
    graph: SignalFlowGraph,
    source: str | None = None,
    detector: str | None = None,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
) -> tuple[sp.Expr, str, str, str]:
    """Solve the SFG equations and return ``H(s)``, source and detector metadata."""
    substitution_map = _numeric_substitutions(substitutions)
    drive = _select_source_drive(graph, source)
    detector_name = detector or _first_detector(graph)
    detector_vertex = _detector_to_vertex(graph, detector_name)
    symbols = {name: sp.Symbol(_safe_symbol_name(name)) for name in graph.vertices}
    equations: list[sp.Expr] = []
    source_vertices = {item.vertex_name for item in graph.source_drives}
    for item in graph.source_drives:
        value = sp.sympify(item.value).subs(substitution_map)
        equations.append(symbols[item.vertex_name] - value)
    incoming: dict[str, list[Any]] = {}
    for edge in graph.meta_edges.values():
        incoming.setdefault(edge.target, []).append(edge)
    for target, edges in incoming.items():
        if target not in symbols or target in source_vertices:
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
                expr -= sp.sympify(edge.value).subs(substitution_map) * symbols[edge.source]
            equations.append(sp.simplify(expr))
    for vertex_name in _kcl_zero_vertices(graph):
        equations.append(symbols[vertex_name])
    variables = [symbols[name] for name in sorted(symbols)]
    solution = _solve_linear_equations(equations, variables)
    detector_expr = sp.simplify(solution[symbols[detector_vertex]])
    drive_value = sp.sympify(drive.value).subs(substitution_map)
    if drive_value == 0:
        transfer = detector_expr
    else:
        transfer = sp.simplify(detector_expr / drive_value)
    return sp.simplify(transfer), drive.source_ref, detector_name, detector_vertex


def validate_graph_equivalence(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    frequencies: Iterable[float],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    relative_tolerance: float = 1e-8,
    raise_on_failure: bool = True,
) -> tuple[GraphPerformance, GraphPerformanceError]:
    """Validate the unsimplified SFG against the SLiCAP reference solution."""
    frequencies = tuple(float(value) for value in frequencies)
    performance = evaluate_graph_performance(
        graph,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
        frequencies=frequencies,
        substitutions=substitutions,
    )
    reference_samples = _pipeline_reference_samples(
        pipeline.reference,
        frequencies,
        substitutions,
    )
    error = compare_graph_performance(
        performance,
        reference_samples,
        error_budget=relative_tolerance,
        error_norm="relative_linf",
    )
    symbolic_match = False
    if isinstance(pipeline.reference, NumericReferenceResult):
        symbolic_reference = sp.sympify(pipeline.reference.laplace).subs(
            _numeric_substitutions(substitutions)
        )
        symbolic_match = _symbolically_equal(performance.transfer, symbolic_reference)
    if raise_on_failure and not (symbolic_match or error.accepted):
        raise BaselineEquivalenceError(
            "Unsimplified SFG does not match the SLiCAP reference: "
            f"max_relative_error={error.max_relative_error:.6e}, "
            f"max_magnitude_error={error.max_magnitude_error_db:.6e} dB, "
            f"max_phase_error={error.max_phase_error_deg:.6e} deg."
        )
    return performance, error


def evaluate_graph_performance(
    graph: SignalFlowGraph,
    source: str | None,
    detector: str | None,
    frequencies: Iterable[float],
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    symbolic_transfer: bool = True,
) -> GraphPerformance:
    """Evaluate an SFG transfer function over a frequency grid."""
    substitution_map = _numeric_substitutions(substitutions)
    if symbolic_transfer:
        transfer, source_ref, detector_name, detector_vertex = graph_transfer_function(
            graph,
            source=source,
            detector=detector,
            substitutions=substitutions,
        )
        samples = tuple(
            GraphPerformanceSample(
                frequency_hz=float(frequency),
                value=_evaluate_laplace(transfer, frequency, substitution_map),
            )
            for frequency in frequencies
        )
    else:
        drive = _select_source_drive(graph, source)
        source_ref = drive.source_ref
        detector_name = detector or _first_detector(graph)
        detector_vertex = _detector_to_vertex(graph, detector_name)
        transfer = sp.nan
        frequencies = tuple(float(frequency) for frequency in frequencies)
        numeric_values = _evaluate_graph_samples_numeric(
            graph,
            drive,
            detector_vertex,
            frequencies,
            substitution_map,
        )
        samples = tuple(
            GraphPerformanceSample(
                frequency_hz=frequency,
                value=value,
            )
            for frequency, value in zip(frequencies, numeric_values)
        )
    return GraphPerformance(
        transfer=transfer,
        source=source_ref,
        detector=detector_name,
        detector_vertex=detector_vertex,
        samples=samples,
    )


def compare_graph_performance(
    graph_performance: GraphPerformance,
    reference_samples: Iterable[FrequencySample],
    error_budget: float,
    error_norm: str = "relative_linf",
    magnitude_error_db: float | None = None,
    phase_error_deg: float | None = None,
    error_scope: str = "absolute",
) -> GraphPerformanceError:
    """Compare graph samples against reference samples using the configured norm."""
    if error_scope not in {"absolute", "relative"}:
        raise ValueError("error_scope must be 'absolute' or 'relative'.")
    graph_samples = list(graph_performance.samples)
    reference_samples = list(reference_samples)
    if len(graph_samples) != len(reference_samples):
        raise ValueError("Graph and reference sample counts differ.")
    relative_errors: list[float] = []
    absolute_errors: list[float] = []
    magnitude_errors: list[float] = []
    phase_errors: list[float] = []
    for graph_sample, reference_sample in zip(graph_samples, reference_samples):
        absolute = abs(graph_sample.value - reference_sample.value)
        scale = max(abs(reference_sample.value), 1e-30)
        relative = absolute / scale
        absolute_errors.append(float(absolute))
        relative_errors.append(float(relative))
        magnitude_errors.append(abs(_magnitude_db(graph_sample.value) - _magnitude_db(reference_sample.value)))
        phase_errors.append(abs(_phase_difference_deg(graph_sample.value, reference_sample.value)))
    max_relative = max(relative_errors, default=0.0)
    rms_relative = math.sqrt(sum(value * value for value in relative_errors) / max(len(relative_errors), 1))
    max_absolute = max(absolute_errors, default=0.0)
    max_magnitude_error = max(magnitude_errors, default=0.0)
    max_phase_error = max(phase_errors, default=0.0)
    worst_magnitude_frequency = None
    worst_phase_frequency = None
    if graph_samples and magnitude_errors:
        worst_magnitude_frequency = graph_samples[magnitude_errors.index(max_magnitude_error)].frequency_hz
    if graph_samples and phase_errors:
        worst_phase_frequency = graph_samples[phase_errors.index(max_phase_error)].frequency_hz
    selected_norm = _normalize_error_norm(error_norm, magnitude_error_db, phase_error_deg)
    if selected_norm == "bode_linf":
        magnitude_ok = magnitude_error_db is None or max_magnitude_error <= magnitude_error_db
        phase_ok = phase_error_deg is None or max_phase_error <= phase_error_deg
        accepted = magnitude_ok and phase_ok
        norm_terms = []
        if magnitude_error_db is not None:
            norm_terms.append(max_magnitude_error / max(magnitude_error_db, 1e-300))
        if phase_error_deg is not None:
            norm_terms.append(max_phase_error / max(phase_error_deg, 1e-300))
        norm_error = max(norm_terms, default=0.0)
        norm_name = "bode_inf_normalized"
    elif selected_norm == "relative_l2":
        accepted = rms_relative <= error_budget
        norm_error = rms_relative
        norm_name = "relative_l2"
    elif selected_norm == "absolute_linf":
        accepted = max_absolute <= error_budget
        norm_error = max_absolute
        norm_name = "absolute_linf"
    elif selected_norm == "hybrid_linf":
        relative_term = max_relative / max(error_budget, 1e-300)
        bode_terms = []
        if magnitude_error_db is not None:
            bode_terms.append(max_magnitude_error / max(magnitude_error_db, 1e-300))
        if phase_error_deg is not None:
            bode_terms.append(max_phase_error / max(phase_error_deg, 1e-300))
        norm_error = max([relative_term, *bode_terms])
        accepted = norm_error <= 1.0
        norm_name = "hybrid_linf_normalized"
    else:
        accepted = max_relative <= error_budget
        norm_error = max_relative
        norm_name = "relative_inf"
    return GraphPerformanceError(
        max_relative_error=float(max_relative),
        rms_relative_error=float(rms_relative),
        max_absolute_error=float(max_absolute),
        max_magnitude_error_db=float(max_magnitude_error),
        max_phase_error_deg=float(max_phase_error),
        norm_error=float(norm_error),
        norm_name=norm_name,
        magnitude_limit_db=magnitude_error_db,
        phase_limit_deg=phase_error_deg,
        relative_error_budget=error_budget,
        accepted=accepted,
        error_scope=error_scope,
        worst_magnitude_frequency_hz=worst_magnitude_frequency,
        worst_phase_frequency_hz=worst_phase_frequency,
    )


def simplify_symbolic_transfer_terms(
    transfer_result: SubrangeSymbolicTransfer,
    frequencies: Iterable[float],
    reference_expression: sp.Expr,
    substitutions: dict[str | sp.Symbol, Any] | None,
    performance_specification: PerformanceSpecification,
    max_steps: int = 12,
    max_terms: int = 128,
    parameter_tolerances: dict[str | sp.Symbol, float] | None = None,
    default_parameter_tolerance: float | None = None,
    max_corner_parameters: int = 4,
    joint_order_pruning: bool = True,
    max_joint_size: int = 8,
    max_symbolic_root_degree: int = 2,
    parameter_report_limit: int = 10,
) -> DominantTermSymbolicTransfer | None:
    """Remove transfer-function product terms while preserving the user error boundary.

    Every trial is compared with the original SLiCAP reference over the complete
    subrange grid. Optional parameter corners use the same performance boundary,
    so a shorter expression cannot consume hidden error outside the design point.
    """
    frequencies = tuple(float(value) for value in frequencies)
    numerator_terms, denominator_terms = _transfer_additive_terms(transfer_result.transfer)
    full_term_count = len(numerator_terms) + len(denominator_terms)
    if not frequencies or full_term_count <= 2:
        return None
    corner_maps = _transfer_parameter_corners(
        transfer_result.transfer,
        frequencies,
        substitutions,
        parameter_tolerances,
        default_parameter_tolerance,
        max_corner_parameters,
    )
    scenarios: tuple[dict[str | sp.Symbol, Any] | dict[sp.Symbol, sp.Expr], ...] = (
        substitutions or {},
        *corner_maps,
    )
    try:
        evaluated_numerator = _evaluate_transfer_terms(numerator_terms, frequencies, scenarios)
        evaluated_denominator = _evaluate_transfer_terms(denominator_terms, frequencies, scenarios)
        reference_scenarios = tuple(
            _reference_samples(reference_expression, frequencies, scenario)
            for scenario in scenarios
        )
        current_error, current_corner_error = _evaluated_transfer_term_errors(
            evaluated_numerator,
            evaluated_denominator,
            frequencies,
            reference_scenarios,
            performance_specification,
        )
    except Exception:
        return None
    if not current_error.accepted:
        return None
    if current_corner_error is not None and current_corner_error > 1.0:
        return None

    pruning_steps: list[TransferTermPruningStep] = []
    pruning_step_limit = max_steps if full_term_count <= max_terms else 0
    for step_index in range(1, pruning_step_limit + 1):
        trials: list[
            tuple[
                float,
                int,
                str,
                tuple[int, ...],
                int | None,
                tuple[sp.Expr, ...],
                list[tuple[sp.Expr, tuple[np.ndarray, ...]]],
                list[tuple[sp.Expr, tuple[np.ndarray, ...]]],
                GraphPerformanceError,
                float | None,
            ]
        ] = []
        for side, terms in (
            ("numerator", evaluated_numerator),
            ("denominator", evaluated_denominator),
        ):
            if len(terms) <= 1:
                continue
            for indices, laplace_order in _transfer_term_removal_candidates(
                terms,
                include_joint_orders=joint_order_pruning,
                max_joint_size=max_joint_size,
            ):
                trial_numerator = list(evaluated_numerator)
                trial_denominator = list(evaluated_denominator)
                if side == "numerator":
                    trial_numerator = [
                        item for index, item in enumerate(trial_numerator) if index not in indices
                    ]
                else:
                    trial_denominator = [
                        item for index, item in enumerate(trial_denominator) if index not in indices
                    ]
                trial_error, trial_corner_error = _evaluated_transfer_term_errors(
                    trial_numerator,
                    trial_denominator,
                    frequencies,
                    reference_scenarios,
                    performance_specification,
                )
                if not trial_error.accepted:
                    continue
                if trial_corner_error is not None and trial_corner_error > 1.0:
                    continue
                trials.append(
                    (
                        max(trial_error.norm_error, trial_corner_error or 0.0),
                        len(indices),
                        side,
                        indices,
                        laplace_order,
                        tuple(terms[index][0] for index in indices),
                        trial_numerator,
                        trial_denominator,
                        trial_error,
                        trial_corner_error,
                    )
                )
        if not trials:
            break
        (
            _worst_normalized_error,
            _removed_count,
            removed_side,
            removed_indices,
            removed_order,
            removed_terms,
            evaluated_numerator,
            evaluated_denominator,
            current_error,
            current_corner_error,
        ) = min(
            trials,
            key=lambda item: (
                -item[1],
                item[0],
                item[2],
                item[3],
            ),
        )
        pruning_steps.append(
            TransferTermPruningStep(
                index=step_index,
                side=removed_side,
                removed_terms=removed_terms,
                laplace_order=removed_order,
                joint=len(removed_indices) > 1,
                nominal_norm_error=current_error.norm_error,
                corner_max_norm_error=current_corner_error,
            )
        )

    retained_term_count = len(evaluated_numerator) + len(evaluated_denominator)
    raw_numerator = sp.Add(*(item[0] for item in evaluated_numerator))
    raw_denominator = sp.Add(*(item[0] for item in evaluated_denominator))
    raw_expression = raw_numerator / raw_denominator
    readability_before = _symbolic_operation_count(raw_expression)
    numerator = _best_readable_factorization(raw_numerator)
    denominator = _best_readable_factorization(raw_denominator)
    final_expression = numerator / denominator
    readability_after = _symbolic_operation_count(final_expression)
    representation = "term-pruned"
    if retained_term_count >= full_term_count:
        if readability_after >= readability_before:
            return None
        representation = "factored-equivalent"
    poles, zeros = symbolic_transfer_roots(
        final_expression,
        substitutions=substitutions,
        max_symbolic_degree=max_symbolic_root_degree,
    )
    parameter_influences = _rank_transfer_parameter_influences(
        final_expression,
        numerator_terms=[item[0] for item in evaluated_numerator],
        denominator_terms=[item[0] for item in evaluated_denominator],
        poles=poles,
        zeros=zeros,
        frequencies=frequencies,
        substitutions=substitutions,
        limit=parameter_report_limit,
    )
    discarded_parameters = tuple(
        sorted(
            str(symbol)
            for symbol in (
                sp.sympify(transfer_result.transfer).free_symbols
                - final_expression.free_symbols
                - {ini.laplace}
            )
        )
    )
    return DominantTermSymbolicTransfer(
        transfer=final_expression,
        numerator=numerator,
        denominator=denominator,
        poles=poles,
        zeros=zeros,
        full_term_count=full_term_count,
        retained_term_count=retained_term_count,
        nominal_error=current_error,
        corner_max_norm_error=current_corner_error,
        corner_count=len(corner_maps),
        parameter_influences=parameter_influences,
        discarded_parameters=discarded_parameters,
        pruning_steps=tuple(pruning_steps),
        representation=representation,
        readability_operations_before=readability_before,
        readability_operations_after=readability_after,
    )


def _transfer_additive_terms(expression: sp.Expr) -> tuple[list[sp.Expr], list[sp.Expr]]:
    """Expand H(s) into numerator and denominator product-term lists."""
    numerator, denominator = sp.cancel(sp.sympify(expression)).as_numer_denom()
    return (
        list(sp.Add.make_args(sp.expand(numerator))),
        list(sp.Add.make_args(sp.expand(denominator))),
    )


def _symbolic_operation_count(expression: sp.Expr) -> int:
    """Return a stable integer proxy for symbolic reading effort."""
    try:
        return int(sp.count_ops(sp.sympify(expression), visual=False))
    except (TypeError, ValueError):
        return len(sp.sstr(expression))


def _readable_additive_factorization(
    expression: sp.Expr,
    max_terms: int = 24,
    max_rounds: int = 4,
    beam_width: int = 12,
) -> sp.Expr:
    """Factor repeated additive subsets without an unbounded simplify search.

    SymPy's global ``factor()`` cannot rewrite expressions such as
    ``cmu*gm + gpi*A + gx*A`` into ``cmu*gm + (gpi + gx)*A`` when the
    complete sum has no common factor.  This bounded beam search extracts
    common factors from subsets, which preserves the parameter groupings that
    usually carry circuit meaning in compact pole and zero formulae.
    """
    expanded = sp.expand(sp.sympify(expression))
    terms = sp.Add.make_args(expanded)
    if len(terms) < 2 or len(terms) > max_terms:
        return sp.factor_terms(expression)

    best = expanded
    best_score = (_symbolic_operation_count(best), len(sp.sstr(best)))
    frontier = (expanded,)
    seen = {sp.srepr(expanded)}
    for _round in range(max_rounds):
        candidates: list[sp.Expr] = []
        for current in frontier:
            current_terms = sp.Add.make_args(current)
            factors: dict[str, sp.Expr] = {}
            for left_index, left in enumerate(current_terms):
                for right in current_terms[left_index + 1 :]:
                    try:
                        factor = sp.factor_terms(sp.gcd(left, right))
                    except (TypeError, ValueError, NotImplementedError):
                        continue
                    if factor in (0, 1, -1) or not factor.free_symbols:
                        continue
                    factors.setdefault(sp.srepr(factor), factor)
            for factor in factors.values():
                grouped: list[tuple[sp.Expr, sp.Expr]] = []
                remainder: list[sp.Expr] = []
                for term in current_terms:
                    quotient = sp.cancel(term / factor)
                    if quotient.as_numer_denom()[1] == 1:
                        grouped.append((term, quotient))
                    else:
                        remainder.append(term)
                if len(grouped) < 2:
                    continue
                grouped_expression = sp.Mul(
                    factor,
                    sp.Add(*(item[1] for item in grouped)),
                    evaluate=False,
                )
                candidate = sp.Add(grouped_expression, *remainder, evaluate=False)
                key = sp.srepr(candidate)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
                score = (_symbolic_operation_count(candidate), len(sp.sstr(candidate)))
                if score < best_score:
                    best = candidate
                    best_score = score
        if not candidates:
            break
        frontier = tuple(
            sorted(
                candidates,
                key=lambda item: (_symbolic_operation_count(item), len(sp.sstr(item))),
            )[:beam_width]
        )
    return best


def _readable_polynomial_factorization(expression: sp.Expr) -> sp.Expr:
    """Expose repeated circuit parameter groups independently at each s order.

    Large transfer polynomials often exceed the safe search width of
    ``_readable_additive_factorization`` only because terms from many Laplace
    orders are mixed together. Grouping by powers of ``s`` keeps the search
    bounded and preserves the polynomial order exactly.
    """
    expression = sp.sympify(expression)
    try:
        polynomial = sp.Poly(sp.expand(expression), ini.laplace)
    except sp.PolynomialError:
        return _readable_additive_factorization(expression)
    grouped_terms: list[sp.Expr] = []
    for (order,), coefficient in polynomial.terms():
        readable_coefficient = _readable_additive_factorization(coefficient)
        if order:
            laplace_term = ini.laplace**order
            if readable_coefficient == 1:
                grouped_terms.append(laplace_term)
            elif readable_coefficient == -1:
                grouped_terms.append(-laplace_term)
            else:
                grouped_terms.append(
                    sp.Mul(readable_coefficient, laplace_term, evaluate=False)
                )
        else:
            grouped_terms.append(readable_coefficient)
    return sp.Add(*grouped_terms, evaluate=False)


def _best_readable_factorization(expression: sp.Expr) -> sp.Expr:
    """Choose the shortest exact multiplicative or additive representation."""
    expression = sp.sympify(expression)
    candidates = [
        expression,
        sp.factor_terms(expression),
        _readable_additive_factorization(expression),
        _readable_polynomial_factorization(expression),
    ]
    try:
        candidates.append(sp.factor(expression))
    except (TypeError, ValueError, NotImplementedError, sp.PolynomialError):
        pass
    return min(
        candidates,
        key=lambda item: (_symbolic_operation_count(item), len(sp.sstr(item))),
    )


def _best_readable_rational_factorization(expression: sp.Expr) -> sp.Expr:
    """Compact numerator and denominator groups without changing a rational expression."""
    expression = sp.cancel(sp.sympify(expression))
    numerator, denominator = expression.as_numer_denom()
    readable_numerator = _best_readable_factorization(numerator)
    readable_denominator = _best_readable_factorization(denominator)
    if denominator == 1:
        candidate = readable_numerator
    else:
        candidate = sp.Mul(
            readable_numerator,
            sp.Pow(readable_denominator, -1, evaluate=False),
            evaluate=False,
        )
    return min(
        (expression, candidate),
        key=lambda item: (_symbolic_operation_count(item), len(sp.sstr(item))),
    )


def _transfer_term_removal_candidates(
    terms: list[tuple[sp.Expr, tuple[np.ndarray, ...]]],
    include_joint_orders: bool,
    max_joint_size: int,
) -> tuple[tuple[tuple[int, ...], int | None], ...]:
    """Return single terms plus cumulative weak same-order term groups."""
    candidates: list[tuple[tuple[int, ...], int | None]] = [
        ((index,), _laplace_order(item[0])) for index, item in enumerate(terms)
    ]
    if include_joint_orders:
        grouped: dict[int, list[int]] = {}
        for index, item in enumerate(terms):
            order = _laplace_order(item[0])
            if order is not None:
                grouped.setdefault(order, []).append(index)
        for order, indices in sorted(grouped.items()):
            ranked = sorted(
                indices,
                key=lambda index: (_evaluated_term_strength(terms[index]), index),
            )
            upper = min(len(ranked), max(int(max_joint_size), 1))
            for count in range(2, upper + 1):
                selected = tuple(sorted(ranked[:count]))
                if len(selected) < len(terms):
                    candidates.append((selected, order))
    return tuple(candidates)


def _evaluated_term_strength(
    term: tuple[sp.Expr, tuple[np.ndarray, ...]],
) -> float:
    """Return the largest finite sampled magnitude of one product term."""
    finite_values = [
        float(value)
        for scenario_values in term[1]
        for value in np.abs(scenario_values)
        if np.isfinite(value)
    ]
    return max(finite_values, default=math.inf)


def _rank_transfer_parameter_influences(
    transfer: sp.Expr,
    numerator_terms: list[sp.Expr],
    denominator_terms: list[sp.Expr],
    poles: tuple[SymbolicRootExpression, ...],
    zeros: tuple[SymbolicRootExpression, ...],
    frequencies: tuple[float, ...],
    substitutions: dict[str | sp.Symbol, Any] | None,
    limit: int,
) -> tuple[DominantParameterInfluence, ...]:
    """Rank retained parameters by maximum normalized transfer sensitivity."""
    if not frequencies or limit <= 0:
        return ()
    substitution_map = _numeric_substitutions(substitutions)
    transfer = sp.sympify(transfer)
    parameters = [
        symbol
        for symbol in sorted(transfer.free_symbols - {ini.laplace}, key=str)
        if symbol in substitution_map and substitution_map[symbol] != 0
    ]
    if not parameters:
        return ()
    sensitivity_expressions = tuple(
        sp.diff(transfer, symbol) * symbol / transfer
        for symbol in parameters
    )
    prepared = tuple(expression.subs(substitution_map) for expression in sensitivity_expressions)
    remaining = set().union(*(expression.free_symbols for expression in prepared)) - {ini.laplace}
    values_by_parameter: list[list[complex]] = [[] for _symbol in parameters]
    try:
        if remaining:
            for index, expression in enumerate(prepared):
                values_by_parameter[index] = [
                    _evaluate_expr_numeric(expression, frequency, {})
                    for frequency in frequencies
                ]
        else:
            function = sp.lambdify(ini.laplace, prepared, modules="numpy")
            for frequency in frequencies:
                row = function(2j * np.pi * frequency)
                if len(prepared) == 1 and not isinstance(row, (tuple, list)):
                    row = (row,)
                for index, value in enumerate(row):
                    values_by_parameter[index].append(complex(value))
    except Exception:
        return ()
    representative_frequency = math.sqrt(min(frequencies) * max(frequencies))
    representative_index = min(
        range(len(frequencies)),
        key=lambda index: abs(math.log(frequencies[index] / representative_frequency)),
    )
    influences: list[DominantParameterInfluence] = []
    for symbol, values in zip(parameters, values_by_parameter):
        magnitudes = [abs(value) for value in values]
        finite_indices = [index for index, value in enumerate(magnitudes) if np.isfinite(value)]
        if not finite_indices:
            continue
        finite_representative_index = min(
            finite_indices,
            key=lambda index: abs(index - representative_index),
        )
        peak_index = max(finite_indices, key=lambda index: magnitudes[index])
        influences.append(
            DominantParameterInfluence(
                parameter=str(symbol),
                max_normalized_sensitivity=float(magnitudes[peak_index]),
                representative_normalized_sensitivity=float(
                    magnitudes[finite_representative_index]
                ),
                peak_frequency_hz=frequencies[peak_index],
                numerator_term_count=sum(symbol in term.free_symbols for term in numerator_terms),
                denominator_term_count=sum(symbol in term.free_symbols for term in denominator_terms),
                pole_expression_count=sum(
                    root.expression is not None and symbol in root.expression.free_symbols
                    for root in poles
                ),
                zero_expression_count=sum(
                    root.expression is not None and symbol in root.expression.free_symbols
                    for root in zeros
                ),
            )
        )
    return tuple(
        sorted(
            influences,
            key=lambda item: (-item.max_normalized_sensitivity, item.parameter),
        )[:limit]
    )


def _evaluate_transfer_terms(
    terms: Iterable[sp.Expr],
    frequencies: tuple[float, ...],
    scenarios: tuple[dict[str | sp.Symbol, Any] | dict[sp.Symbol, sp.Expr], ...],
) -> list[tuple[sp.Expr, tuple[np.ndarray, ...]]]:
    """Sample every product term once for all frequencies and parameter scenarios."""
    terms = tuple(sp.sympify(term) for term in terms)
    values_by_term: list[list[np.ndarray]] = [[] for _term in terms]
    for substitutions in scenarios:
        substitution_map = _numeric_substitutions(substitutions)
        prepared = tuple(term.subs(substitution_map) for term in terms)
        remaining = set().union(*(term.free_symbols for term in prepared)) - {ini.laplace}
        if remaining:
            sampled = [
                [_evaluate_expr_numeric(term, frequency, {}) for frequency in frequencies]
                for term in prepared
            ]
        else:
            function = sp.lambdify(ini.laplace, prepared, modules="numpy")
            sampled = [[] for _term in prepared]
            for frequency in frequencies:
                row = function(2j * np.pi * frequency)
                if len(prepared) == 1 and not isinstance(row, (tuple, list)):
                    row = (row,)
                for index, value in enumerate(row):
                    sampled[index].append(complex(value))
        for index, term_values in enumerate(sampled):
            values_by_term[index].append(np.asarray(term_values, dtype=complex))
    return [
        (term, tuple(scenario_values))
        for term, scenario_values in zip(terms, values_by_term)
    ]


def _evaluated_transfer_term_errors(
    numerator_terms: list[tuple[sp.Expr, tuple[np.ndarray, ...]]],
    denominator_terms: list[tuple[sp.Expr, tuple[np.ndarray, ...]]],
    frequencies: tuple[float, ...],
    reference_scenarios: tuple[tuple[FrequencySample, ...], ...],
    specification: PerformanceSpecification,
) -> tuple[GraphPerformanceError, float | None]:
    """Evaluate one retained-term set using cached term samples."""
    errors: list[GraphPerformanceError] = []
    for scenario_index, reference_samples in enumerate(reference_scenarios):
        numerator = sum((item[1][scenario_index] for item in numerator_terms), np.zeros(len(frequencies), dtype=complex))
        denominator = sum((item[1][scenario_index] for item in denominator_terms), np.zeros(len(frequencies), dtype=complex))
        with np.errstate(divide="ignore", invalid="ignore"):
            values = numerator / denominator
        if not np.all(np.isfinite(values)):
            return _invalid_graph_performance_error(specification), math.inf
        performance = GraphPerformance(
            transfer=sp.nan,
            source="symbolic",
            detector="symbolic",
            detector_vertex="symbolic",
            samples=tuple(
                GraphPerformanceSample(frequency_hz=frequency, value=complex(value))
                for frequency, value in zip(frequencies, values)
            ),
        )
        errors.append(
            compare_graph_performance(
                performance,
                reference_samples,
                error_budget=specification.relative_error_limit,
                error_norm=specification.error_norm,
                magnitude_error_db=specification.magnitude_error_db,
                phase_error_deg=specification.phase_error_deg,
                error_scope="absolute",
            )
        )
    nominal_error = errors[0]
    corner_error = None if len(errors) == 1 else max(item.norm_error for item in errors[1:])
    if any(not item.accepted for item in errors[1:]):
        corner_error = math.inf
    return nominal_error, corner_error


def _invalid_graph_performance_error(
    specification: PerformanceSpecification,
) -> GraphPerformanceError:
    """Return a rejected sentinel for a singular symbolic-transfer candidate."""
    return GraphPerformanceError(
        max_relative_error=math.inf,
        rms_relative_error=math.inf,
        max_absolute_error=math.inf,
        accepted=False,
        norm_error=math.inf,
        norm_name=_normalize_error_norm(
            specification.error_norm,
            specification.magnitude_error_db,
            specification.phase_error_deg,
        ),
        max_magnitude_error_db=math.inf,
        max_phase_error_deg=math.inf,
    )


def _symbolic_transfer_performance_error(
    candidate: sp.Expr,
    reference: sp.Expr,
    frequencies: tuple[float, ...],
    substitutions: dict[str | sp.Symbol, Any] | None,
    specification: PerformanceSpecification,
) -> GraphPerformanceError:
    """Evaluate one symbolic H(s) candidate against the original reference."""
    substitution_map = _numeric_substitutions(substitutions)
    evaluator = _numeric_expression_evaluator(candidate, substitution_map)
    performance = GraphPerformance(
        transfer=sp.sympify(candidate),
        source="symbolic",
        detector="symbolic",
        detector_vertex="symbolic",
        samples=tuple(
            GraphPerformanceSample(frequency_hz=frequency, value=evaluator(frequency))
            for frequency in frequencies
        ),
    )
    reference_samples = _reference_samples(reference, frequencies, substitutions)
    return compare_graph_performance(
        performance,
        reference_samples,
        error_budget=specification.relative_error_limit,
        error_norm=specification.error_norm,
        magnitude_error_db=specification.magnitude_error_db,
        phase_error_deg=specification.phase_error_deg,
        error_scope="absolute",
    )


def _transfer_parameter_corners(
    transfer: sp.Expr,
    frequencies: tuple[float, ...],
    substitutions: dict[str | sp.Symbol, Any] | None,
    tolerances: dict[str | sp.Symbol, float] | None,
    default_tolerance: float | None,
    max_corner_parameters: int,
) -> tuple[dict[sp.Symbol, sp.Expr], ...]:
    """Create min/max transfer-function corners ordered by normalized sensitivity."""
    if max_corner_parameters <= 0:
        return ()
    nominal = _numeric_substitutions(substitutions)
    expression = sp.sympify(transfer)
    default_value = 0.0 if default_tolerance is None else abs(float(default_tolerance))
    tolerance_map = {
        symbol: default_value
        for symbol in expression.free_symbols - {ini.laplace}
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
    sensitivities: list[tuple[float, sp.Symbol]] = []
    for symbol in sorted(expression.free_symbols & tolerance_map.keys(), key=str):
        if symbol not in nominal or nominal[symbol] == 0:
            continue
        normalized = sp.diff(expression, symbol) * symbol / expression
        evaluator = _numeric_expression_evaluator(normalized, nominal)
        try:
            sensitivity = max(abs(evaluator(frequency)) for frequency in frequencies)
        except Exception:
            continue
        if np.isfinite(sensitivity):
            sensitivities.append((float(sensitivity), symbol))
    selected = [
        symbol for _value, symbol in sorted(sensitivities, key=lambda item: (-item[0], str(item[1])))
    ][:max_corner_parameters]
    corners: list[dict[sp.Symbol, sp.Expr]] = []
    for signs in product((-1.0, 1.0), repeat=len(selected)):
        corner = dict(nominal)
        for symbol, sign in zip(selected, signs):
            corner[symbol] = nominal[symbol] * (1.0 + sign * tolerance_map[symbol])
        corners.append(corner)
    return tuple(corners)


def _symbolic_transfer_corner_error(
    candidate: sp.Expr,
    reference: sp.Expr,
    frequencies: tuple[float, ...],
    corners: tuple[dict[sp.Symbol, sp.Expr], ...],
    specification: PerformanceSpecification,
) -> float | None:
    """Return the worst normalized performance error over parameter corners."""
    errors: list[float] = []
    for corner in corners:
        try:
            error = _symbolic_transfer_performance_error(
                candidate,
                reference,
                frequencies,
                corner,
                specification,
            )
        except Exception:
            return math.inf
        if not error.accepted:
            return math.inf
        errors.append(error.norm_error)
    return None if not errors else max(errors)


def simplification_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format the complete simplification result as Markdown."""
    base_complexity = graph_complexity(result.pipeline.graph)
    final_complexity = graph_complexity(result.final_graph)
    base_paper_complexity = None
    if result.pipeline.topology_metrics is not None:
        base_paper_complexity = paper_complexity(
            result.pipeline.graph,
            result.pipeline.candidates,
            result.pipeline.topology_metrics,
        )
    final_candidates = analyze_meta_edge_candidates(result.final_graph, substitutions=result.substitutions)
    final_topology = analyze_paper_topology(
        result.final_graph,
        final_candidates,
        source=result.pipeline.reference.source,
        detector=result.pipeline.reference.detector,
    )
    final_paper_complexity = paper_complexity(result.final_graph, final_candidates, final_topology)
    lines = [f"# SFG Simplification Report: {result.pipeline.network.title or ''}", ""]
    lines.append(f"- source: `{result.final_performance.source}`")
    lines.append(f"- detector: `{result.final_performance.detector}`")
    lines.append(f"- error norm: `{_normalize_error_norm(result.config.error_norm, result.config.magnitude_error_db, result.config.phase_error_deg)}`")
    lines.append(f"- total error budget: `{result.config.total_error_budget:.{precision}e}`")
    lines.append("- subrange policy: `full user boundary restricted to each frequency interval`")
    lines.append(f"- ranking alpha Eq.(11): `{result.config.ranking_alpha:.{precision}e}`")
    lines.append(f"- baseline transfer: `{sp.sstr(sp.simplify(result.baseline_performance.transfer))}`")
    lines.append(f"- final transfer: `{sp.sstr(sp.simplify(result.final_performance.transfer))}`")
    lines.append(f"- baseline max relative error: `{result.baseline_error.max_relative_error:.{precision}e}`")
    lines.append(f"- final max relative error: `{result.final_error.max_relative_error:.{precision}e}`")
    lines.append(f"- final max magnitude error: `{result.final_error.max_magnitude_error_db:.{precision}e}` dB")
    lines.append(f"- final max phase error: `{result.final_error.max_phase_error_deg:.{precision}e}` deg")
    lines.append(f"- final norm error ({result.final_error.norm_name}): `{result.final_error.norm_error:.{precision}e}`")
    lines.append(f"- accepted steps: `{len(result.accepted_steps)}`")
    lines.append(f"- rejected steps: `{len(result.rejected_steps)}`")
    lines.append(f"- simplified subrange graphs: `{len(result.subrange_results)}`")
    lines.append(f"- subrange operations applied to global graph: `{result.subrange_operations_applied_to_global}`")
    if base_paper_complexity is not None:
        lines.append(f"- C(N) Eq.(9): `{base_paper_complexity.full}` -> `{final_paper_complexity.full}`")
        lines.append(f"- Cs(N) Eq.(10): `{base_paper_complexity.simplified}` -> `{final_paper_complexity.simplified}`")
    lines.append(f"- prototype simple score: `{base_complexity.simple_score}` -> `{final_complexity.simple_score}`")
    lines.append(f"- vertices: `{base_complexity.vertices}` -> `{final_complexity.vertices}`")
    lines.append(f"- meta-edges: `{base_complexity.meta_edges}` -> `{final_complexity.meta_edges}`")
    lines.extend(["", "## Accepted Steps"])
    accepted = result.accepted_steps
    if not accepted:
        lines.append("- none")
    for step in accepted:
        lines.extend(_format_step(step, precision))
    lines.extend(["", "## Rejected Steps"])
    rejected = result.rejected_steps
    if not rejected:
        lines.append("- none")
    for step in rejected[:30]:
        lines.extend(_format_step(step, precision))
    if len(rejected) > 30:
        lines.append(f"- omitted rejected steps: `{len(rejected) - 30}`")
    return "\n".join(lines).rstrip()


def subrange_simplification_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format final G_j^* transfer functions after error-controlled simplification."""
    lines = ["# Subrange Simplified Symbolic Results", ""]
    if not result.subrange_results:
        lines.append("- none")
        return "\n".join(lines)
    lines.append(
        "Each section reports the locally simplified frequency graph G_j^*, "
        "its accepted/rejected operations, the final symbolic H_j^*(s), and its extracted roots."
    )
    lines.append(
        "By default G_j^* starts from the unreduced SFG and only changes through accepted Eq.(6)/(7)/(11)/(12) operations. "
        "If dominance-reduced seeding is enabled, the initial graph may already contain numeric dominance pruning."
    )
    for item in result.subrange_results:
        upper = "inf" if math.isinf(item.upper_frequency_hz) else f"{item.upper_frequency_hz:.{precision}e}"
        accepted = tuple(step for step in item.steps if step.accepted)
        rejected = tuple(step for step in item.steps if not step.accepted)
        lines.extend(
            [
                "",
                f"## Cluster {item.cluster_index}",
                f"- frequency range: `{item.lower_frequency_hz:.{precision}e}` Hz to `{upper}` Hz",
                f"- initial graph: `{len(item.initial_graph.vertices)}` vertices, `{len(item.initial_graph.meta_edges)}` meta-edges",
                f"- simplified graph: `{len(item.simplified_graph.vertices)}` vertices, `{len(item.simplified_graph.meta_edges)}` meta-edges",
                f"- accepted operations: `{len(accepted)}`",
                f"- rejected operations: `{len(rejected)}`",
                f"- max relative error: `{item.error.max_relative_error:.{precision}e}`",
                f"- max magnitude error: `{item.error.max_magnitude_error_db:.{precision}e}` dB",
                f"- max phase error: `{item.error.max_phase_error_deg:.{precision}e}` deg",
                f"- norm error ({item.error.norm_name}): `{item.error.norm_error:.{precision}e}`",
            ]
        )
        if not item.transfer.success:
            lines.append(f"- symbolic solve: failed, `{item.transfer.error}`")
            continue
        dominant_spr = [
            root for root in result.pipeline.summing_point_roots
            if root.cluster_index == item.cluster_index and root.dominant
        ]
        if dominant_spr:
            lines.extend(["", "### Dominant Summing-Point Roots"])
            for root in dominant_spr:
                expr = "none" if root.expression is None else _format_expr_limited(root.expression)
                lines.append(
                    f"- `{root.vertex}`: value `{root.value}`, frequency `{root.frequency_hz:.{precision}e}` Hz, "
                    f"symbolic `{expr}`"
                )
        if item.localized_symbolic_roots:
            lines.extend(["", "### Local Physical SPR Expressions"])
            lines.append(
                "These are local Mason-characteristic approximations. They are reported separately from the exact roots of H_j^*(s)."
            )
            for root in item.localized_symbolic_roots:
                frequency = "none" if root.frequency_hz is None else f"{root.frequency_hz:.{precision}e} Hz"
                error = (
                    "none"
                    if root.relative_frequency_error is None
                    else f"{root.relative_frequency_error:.{precision}e}"
                )
                lines.append(f"- vertex: `{root.vertex}`")
                lines.append(f"- method: `{root.method}`")
                lines.append(f"- retained loops: `{len(root.loop_gains)}/{root.full_loop_count}`")
                lines.append(f"- local SCC: `{', '.join(root.scc_vertices)}`")
                lines.append(f"- Delta_local(s)=0: `{_format_expr_limited(root.characteristic_equation)}`")
                for index, loop_gain in enumerate(root.loop_gains, start=1):
                    lines.append(f"- L_{index}(s): `{_format_expr_limited(loop_gain)}`")
                lines.append(f"- localized {root.kind}: `{_format_expr_limited(root.expression)}`")
                lines.append(f"- evaluated frequency: `{frequency}`")
                lines.append(f"- relative frequency error to reference root: `{error}`")
                corner_error = (
                    "not evaluated"
                    if root.corner_max_relative_error is None
                    else f"{root.corner_max_relative_error:.{precision}e}"
                )
                lines.append(f"- worst parameter-corner root error: `{corner_error}`")
                if root.parameter_sensitivities:
                    sensitivity_text = ", ".join(
                        f"{name}={value:.{precision}e}"
                        for name, value in root.parameter_sensitivities
                    )
                    lines.append(f"- normalized parameter sensitivities: `{sensitivity_text}`")
                lines.append(
                    f"- dominant-product terms retained: `{root.retained_term_count}/{root.full_term_count}`"
                )
                if root.simplified_expression is not None:
                    simplified_frequency = (
                        "none"
                        if root.simplified_frequency_hz is None
                        else f"{root.simplified_frequency_hz:.{precision}e} Hz"
                    )
                    simplified_error = (
                        "none"
                        if root.simplified_relative_frequency_error is None
                        else f"{root.simplified_relative_frequency_error:.{precision}e}"
                    )
                    simplified_corner_error = (
                        "not evaluated"
                        if root.simplified_corner_max_relative_error is None
                        else f"{root.simplified_corner_max_relative_error:.{precision}e}"
                    )
                    lines.append(
                        f"- dominant-term simplified {root.kind}: "
                        f"`{_format_expr_limited(root.simplified_expression)}`"
                    )
                    lines.append(f"- simplified evaluated frequency: `{simplified_frequency}`")
                    lines.append(
                        f"- simplified relative frequency error to reference root: `{simplified_error}`"
                    )
                    lines.append(
                        f"- simplified worst parameter-corner root error: `{simplified_corner_error}`"
                    )
        if item.localized_symbolic_zeros:
            lines.extend(["", "### Local Physical SPR-Z Expressions"])
            lines.append(
                "These roots come from a signed local Mason numerator; matrix fallback is used only when bounded path analysis fails."
            )
            for zero in item.localized_symbolic_zeros:
                lines.append(f"- vertex: `{zero.vertex}`")
                lines.append(
                    f"- region: `{zero.cut_start or 'none'}` -> `{zero.cut_end or 'none'}` "
                    f"(`{zero.cut_kind}`)"
                )
                lines.append(f"- method/status: `{zero.method}` / `{zero.status}`")
                if zero.cancellation_residual is not None:
                    lines.append(
                        f"- cancellation residual: `{zero.cancellation_residual:.{precision}e}`"
                    )
                for path_index, path in enumerate(zero.paths, start=1):
                    lines.append(
                        f"- P_{path_index}(s)Delta_{path_index}(s): "
                        f"`{_format_expr_limited(path.weighted_gain)}`"
                    )
                if zero.characteristic_equation is not None:
                    lines.append(
                        f"- N_cut(s)=0: `{_format_expr_limited(zero.characteristic_equation)}`"
                    )
                if zero.expression is not None:
                    lines.append(f"- localized zero: `{_format_expr_limited(zero.expression)}`")
                    lines.append(
                        f"- relative complex-root error: `{zero.relative_root_error:.{precision}e}`"
                    )
                    corner_error = (
                        "not evaluated"
                        if zero.corner_max_relative_error is None
                        else f"{zero.corner_max_relative_error:.{precision}e}"
                    )
                    lines.append(f"- full-transfer parameter-corner root error: `{corner_error}`")
                    if zero.parameter_participation:
                        participation = ", ".join(
                            f"{name}={value:.{precision}e}"
                            for name, value in zero.parameter_participation
                        )
                        lines.append(f"- normalized parameter participation: `{participation}`")
                if zero.failure_reason:
                    lines.append(f"- diagnostic: `{zero.failure_reason}`")
        if item.target_root_approximations:
            lines.extend(["", "### Per-Target-Root Symbolic Results"])
            lines.append(
                "Root status records whether a symbolic explanation was found. "
                "Root-location deviation is diagnostic in the default paper mode; "
                "the accepted graph is controlled by the subrange transfer-function error."
            )
            for root in item.target_root_approximations:
                expression = (
                    "unresolved"
                    if root.expression is None
                    else _format_expr_limited(root.expression)
                )
                error = (
                    "none"
                    if root.relative_root_error is None
                    else f"{root.relative_root_error:.{precision}e}"
                )
                lines.append(
                    f"- root {root.root_index} ({root.kind}, {root.category}): "
                    f"status `{root.status}`, method `{root.method}`, expression `{expression}`, "
                    f"root-location deviation `{error}`, "
                    f"location `{root.location or 'none'}`"
                )
                if root.parameters:
                    lines.append(f"- dominant parameters: `{', '.join(root.parameters)}`")
        if item.paper_style_transfer is not None:
            lines.extend(["", "### Paper-Style Cluster Approximation"])
            if not item.paper_style_transfer.success:
                lines.append(f"- symbolic solve: failed, `{item.paper_style_transfer.error}`")
            else:
                lines.append(f"- H_j,paper(s): `{_format_expr_limited(item.paper_style_transfer.transfer)}`")
                lines.append(f"- numerator: `{_format_expr_limited(item.paper_style_transfer.numerator)}`")
                lines.append(f"- denominator: `{_format_expr_limited(item.paper_style_transfer.denominator)}`")
                lines.extend(["", "#### Paper-Style Poles"])
                _append_transfer_roots(lines, item.paper_style_transfer.poles, precision)
                lines.extend(["", "#### Paper-Style Zeroes"])
                _append_transfer_roots(lines, item.paper_style_transfer.zeros, precision)
        lines.append(f"- H_j^*(s): `{_format_expr_limited(item.transfer.transfer)}`")
        lines.append(f"- numerator: `{_format_expr_limited(item.transfer.numerator)}`")
        lines.append(f"- denominator: `{_format_expr_limited(item.transfer.denominator)}`")
        lines.extend(["", "### Poles"])
        _append_transfer_roots(lines, item.transfer.poles, precision)
        lines.extend(["", "### Zeroes"])
        _append_transfer_roots(lines, item.transfer.zeros, precision)
        if item.dominant_term_transfer is not None:
            dominant = item.dominant_term_transfer
            corner_error = (
                "not evaluated"
                if dominant.corner_max_norm_error is None
                else f"{dominant.corner_max_norm_error:.{precision}e}"
            )
            lines.extend(
                [
                    "",
                    "### Error-Controlled Dominant-Term Transfer",
                    f"- H_j,DT(s): `{_format_expr_limited(dominant.transfer)}`",
                    f"- numerator: `{_format_expr_limited(dominant.numerator)}`",
                    f"- denominator: `{_format_expr_limited(dominant.denominator)}`",
                    f"- retained product terms: `{dominant.retained_term_count}/{dominant.full_term_count}`",
                    f"- representation: `{dominant.representation}`",
                    f"- readability operations: `{dominant.readability_operations_before}` -> `{dominant.readability_operations_after}`",
                    f"- max relative error to original reference: `{dominant.nominal_error.max_relative_error:.{precision}e}`",
                    f"- max magnitude error: `{dominant.nominal_error.max_magnitude_error_db:.{precision}e}` dB",
                    f"- max phase error: `{dominant.nominal_error.max_phase_error_deg:.{precision}e}` deg",
                    f"- normalized error ({dominant.nominal_error.norm_name}): `{dominant.nominal_error.norm_error:.{precision}e}`",
                    f"- parameter corners: `{dominant.corner_count}`",
                    f"- worst normalized corner error: `{corner_error}`",
                ]
            )
            if dominant.discarded_parameters:
                lines.append(
                    "- parameters removed from the short expression: "
                    f"`{', '.join(dominant.discarded_parameters)}`"
                )
            if dominant.pruning_steps:
                lines.extend(["", "#### Accepted Product-Term Removals"])
                for pruning_step in dominant.pruning_steps:
                    removal_kind = "same-order joint" if pruning_step.joint else "single-term"
                    removed = ", ".join(
                        _format_expr_limited(term) for term in pruning_step.removed_terms
                    )
                    corner_error = (
                        "not evaluated"
                        if pruning_step.corner_max_norm_error is None
                        else f"{pruning_step.corner_max_norm_error:.{precision}e}"
                    )
                    lines.append(
                        f"- step `{pruning_step.index}`: {removal_kind} removal from "
                        f"`{pruning_step.side}`, s-order `{pruning_step.laplace_order}`, "
                        f"terms `[{removed}]`, nominal normalized error "
                        f"`{pruning_step.nominal_norm_error:.{precision}e}`, "
                        f"corner error `{corner_error}`"
                    )
            if dominant.parameter_influences:
                lines.extend(["", "#### Dominant Parameters in This Subrange"])
                for influence in dominant.parameter_influences:
                    lines.append(
                        f"- `{influence.parameter}`: max |S_p^H| "
                        f"`{influence.max_normalized_sensitivity:.{precision}e}`, "
                        f"representative |S_p^H| `{influence.representative_normalized_sensitivity:.{precision}e}`, "
                        f"peak `{influence.peak_frequency_hz:.{precision}e}` Hz, "
                        f"terms N/D `{influence.numerator_term_count}/{influence.denominator_term_count}`, "
                        f"roots P/Z `{influence.pole_expression_count}/{influence.zero_expression_count}`"
                    )
            lines.extend(["", "#### Dominant-Term Poles"])
            _append_transfer_roots(lines, dominant.poles, precision)
            lines.extend(["", "#### Dominant-Term Zeroes"])
            _append_transfer_roots(lines, dominant.zeros, precision)
        lines.extend(["", "### Accepted Operations"])
        if not accepted:
            lines.append("- none")
        for step in accepted:
            lines.append(
                f"- step `{step.index}` {step.manipulation.kind} `{step.manipulation.source}` -> "
                f"`{step.manipulation.target}`, Q `{step.quality}`, R_P `{step.ranking_value}`, "
                f"operation `{step.manipulation.reason}`"
            )
    return "\n".join(lines).rstrip()


def operation_ranking_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format accepted/rejected manipulation attempts as Markdown."""
    lines = ["# Operation Ranking and Decisions", ""]
    for step in result.steps:
        status = "accepted" if step.accepted else "rejected"
        err = "none" if step.error is None else f"{step.error.max_relative_error:.{precision}e}"
        norm_err = "none" if step.error is None else f"{step.error.norm_error:.{precision}e}"
        rel_norm_err = "none" if step.relative_error is None else f"{step.relative_error.norm_error:.{precision}e}"
        mag_err = "none" if step.error is None else f"{step.error.max_magnitude_error_db:.{precision}e}"
        phase_err = "none" if step.error is None else f"{step.error.max_phase_error_deg:.{precision}e}"
        quality = "none" if step.quality is None else f"{step.quality:.{precision}e}"
        ranking = "none" if step.ranking_value is None else f"{step.ranking_value:.{precision}e}"
        c_before = "none" if step.paper_complexity_before is None else str(step.paper_complexity_before.value)
        c_after = "none" if step.paper_complexity_after is None else str(step.paper_complexity_after.value)
        lines.append(f"## {step.index}. {step.manipulation.kind}: `{step.manipulation.source}` -> `{step.manipulation.target}`")
        lines.append(f"- status: `{status}`")
        lines.append(f"- phase: `{step.phase}`")
        lines.append(f"- cluster: `{step.cluster_index}`")
        lines.append(f"- C_before/C_after: `{c_before}` -> `{c_after}`")
        lines.append(f"- max relative error: `{err}`")
        lines.append(f"- relative norm error for Eq.(11): `{rel_norm_err}`")
        lines.append(f"- cumulative absolute norm error for acceptance: `{norm_err}`")
        lines.append(f"- max magnitude error: `{mag_err}` dB")
        lines.append(f"- max phase error: `{phase_err}` deg")
        lines.append(f"- Q = C(N)-C(T(N)) Eq.(12): `{quality}`")
        lines.append(f"- R_P Eq.(11): `{ranking}`")
        lines.append(f"- reason: `{step.reason}`")
        lines.append(f"- operation reason: `{step.manipulation.reason}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def error_trace_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format the accepted/rejected error budget trace as Markdown."""
    lines = ["# Error Trace", ""]
    lines.append(f"- baseline max relative error: `{result.baseline_error.max_relative_error:.{precision}e}`")
    lines.append(f"- final max relative error: `{result.final_error.max_relative_error:.{precision}e}`")
    lines.append(f"- final RMS relative error: `{result.final_error.rms_relative_error:.{precision}e}`")
    lines.append(f"- final max magnitude error: `{result.final_error.max_magnitude_error_db:.{precision}e}` dB")
    lines.append(f"- final max phase error: `{result.final_error.max_phase_error_deg:.{precision}e}` deg")
    lines.append(f"- final norm error ({result.final_error.norm_name}): `{result.final_error.norm_error:.{precision}e}`")
    lines.append("")
    lines.append("| step | phase | cluster | kind | edge | accepted | relative norm | absolute norm | complex rel error | mag error dB | phase error deg | Q | R_P | reason |")
    lines.append("|---:|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for step in result.steps:
        cluster = "" if step.cluster_index is None else str(step.cluster_index)
        edge = f"{step.manipulation.source}->{step.manipulation.target} ({step.manipulation.domain})"
        error = "" if step.error is None else f"{step.error.max_relative_error:.{precision}e}"
        norm_error = "" if step.error is None else f"{step.error.norm_error:.{precision}e}"
        relative_norm = "" if step.relative_error is None else f"{step.relative_error.norm_error:.{precision}e}"
        mag_error = "" if step.error is None else f"{step.error.max_magnitude_error_db:.{precision}e}"
        phase_error = "" if step.error is None else f"{step.error.max_phase_error_deg:.{precision}e}"
        quality = "" if step.quality is None else f"{step.quality:.{precision}e}"
        ranking = "" if step.ranking_value is None else f"{step.ranking_value:.{precision}e}"
        lines.append(
            f"| {step.index} | {step.phase} | {cluster} | {step.manipulation.kind} | "
            f"`{edge}` | {step.accepted} | {relative_norm} | {norm_error} | {error} | {mag_error} | {phase_error} | {quality} | {ranking} | {step.reason} |"
        )
    return "\n".join(lines).rstrip()


def error_policy_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format the norm and error-budget settings used for Eq.(6)/(7)/(11)/(12)."""
    config = result.config
    norm = _normalize_error_norm(config.error_norm, config.magnitude_error_db, config.phase_error_deg)
    lines = ["# Error Norm and Budget Policy", ""]
    lines.append("Paper Eq.(6)-(7) uses a weighted performance-vector norm; the prototype uses magnitude/phase samples and worst-case control.")
    lines.append("Relative error ranks each candidate against the current graph; cumulative absolute error accepts it against the original reference.")
    lines.extend(
        [
            "",
            "## Global Policy",
            f"- error norm: `{norm}`",
            f"- total error budget: `{config.total_error_budget:.{precision}e}`",
            f"- magnitude error limit: `{config.magnitude_error_db}` dB",
            f"- phase error limit: `{config.phase_error_deg}` deg",
            "- subrange policy: `retain the full user boundary in every restricted frequency interval`",
            f"- pre-reduction fraction: `{config.pre_reduction_fraction:.{precision}e}`",
            f"- pre-reduction relative boundary: `{config.total_error_budget * config.pre_reduction_fraction:.{precision}e}`",
            f"- pre-reduction magnitude boundary: `{None if config.magnitude_error_db is None else config.magnitude_error_db * config.pre_reduction_fraction}` dB",
            f"- pre-reduction phase boundary: `{None if config.phase_error_deg is None else config.phase_error_deg * config.pre_reduction_fraction}` deg",
            f"- user-declared bias-node merges: `{config.bias_node_merges}`",
            f"- include subrange boundary samples: `{config.include_subrange_boundaries}`",
            f"- ranking alpha Eq.(11): `{config.ranking_alpha:.{precision}e}`",
            f"- candidate generation mode: `{config.candidate_generation_mode}`",
            f"- exhaustive candidate edge limit: `{config.exhaustive_candidate_edge_limit}`",
            f"- bounded-mode RR partition threshold: `{config.rr_partition_relative_threshold:.{precision}e}`",
            f"- bounded-mode RSP path threshold: `{config.rsp_path_relative_threshold:.{precision}e}`",
            f"- downstream path dominance ratio: `{config.root_observability_dominance_ratio:.{precision}e}`",
            f"- feedback loop-gain threshold: `{config.root_feedback_loop_gain_threshold:.{precision}e}`",
            f"- enforce per-root location error: `{config.enforce_target_root_error}`",
            f"- optional per-root location limit: `{config.target_root_relative_error:.{precision}e}`",
            "",
            "## Subrange Boundaries",
        ]
    )
    for spec in result.pipeline.error_specs:
        budget = _allocated_error_budget(spec, result.pipeline, config)
        upper = "inf" if math.isinf(spec.upper_frequency_hz) else f"{spec.upper_frequency_hz:.{precision}e}"
        lines.append(
            f"- cluster `{spec.cluster_index}`: `{budget:.{precision}e}` over "
            f"`{spec.lower_frequency_hz:.{precision}e}` Hz to `{upper}` Hz, roots `{spec.root_count}`"
        )
    lines.extend(
        [
            "",
            "## Norm Definitions",
            "- `relative_linf`: max over sampled frequencies of `|H_ref-H_test|/max(|H_ref|, floor)`.",
            "- `relative_l2`: RMS value of the same relative complex error.",
            "- `absolute_linf`: max absolute complex error `|H_ref-H_test|`.",
            "- `bode_linf`: max normalized Bode error using configured magnitude/phase limits.",
            "- `hybrid_linf`: max of normalized relative, magnitude, and phase errors.",
            "- Small-graph paper mode enumerates all legal RSP/RR candidates before Eq.(11) ranking; fixed dominance thresholds are only a bounded large-graph prefilter.",
            "- Default paper mode accepts graph operations with the selected Eq.(6)-(7) transfer-performance norm; root-location deviation is reported only as an interpretation diagnostic.",
            "- Enabling `enforce_target_root_error` adds the paper Section III-E root-location variant as an optional second acceptance constraint.",
        ]
    )
    return "\n".join(lines).rstrip()


def root_localization_summary_report(result: SimplificationResult, precision: int = 6) -> str:
    """Format root localization output from the pipeline."""
    lines = ["# Root Localization Summary", ""]
    if not result.root_localization.localizations:
        lines.append("- no localized candidate roots")
        return "\n".join(lines)
    for item in result.root_localization.localizations:
        cluster = "none" if item.cluster_index is None else str(item.cluster_index)
        observable = "yes" if item.observable else "no"
        lines.append(f"- `{item.source}` -> `{item.target}` ({item.domain}): "
                     f"{item.root_kind}, `{item.root_frequency_hz:.{precision}e}` Hz, "
                     f"cluster `{cluster}`, observable `{observable}`")
    return "\n".join(lines)


def subgraph_to_dot(
    graph: SignalFlowGraph,
    subgraph: SFGSubgraph,
    graph_name: str | None = None,
) -> str:
    """Export one root-cluster subgraph G_j as Graphviz DOT."""
    name = graph_name or f"G_{subgraph.cluster_index}"
    safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
    lines = [f'digraph "{safe_name}" {{', '  rankdir="LR";', '  fontname="Helvetica";']
    for vertex_name in sorted(subgraph.vertices):
        vertex = graph.vertices.get(vertex_name)
        if vertex is None:
            continue
        shape = "ellipse" if vertex.kind == "voltage" else "box" if vertex.kind == "current" else "diamond"
        lines.append(f'  "{vertex.name}" [shape={shape}, label="{vertex.name}"];')
    for key in sorted(subgraph.edges):
        edge = graph.meta_edges.get(key)
        if edge is None:
            continue
        value = sp.sstr(sp.simplify(edge.value))
        color = "red" if key in subgraph.candidate_edges else "black"
        lines.append(f'  "{edge.source}" -> "{edge.target}" [label="{value}", color="{color}"];')
    lines.append("}")
    return "\n".join(lines)


def _graph_analysis_key(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
) -> tuple[Any, ...]:
    """Return a stable per-run fingerprint for an immutable SFG state."""
    object_id = id(graph)
    if cache.graph_refs.get(object_id) is graph:
        return cache.graph_keys[object_id]
    key = (
        tuple(sorted((name, vertex.node, vertex.kind) for name, vertex in graph.vertices.items())),
        tuple(
            sorted(
                (
                    edge.source,
                    edge.target,
                    edge.domain,
                    sp.srepr(sp.sympify(edge.value)),
                )
                for edge in graph.meta_edges.values()
            )
        ),
        tuple(
            sorted(
                (
                    drive.source_ref,
                    drive.vertex_name,
                    drive.source_kind,
                    sp.srepr(sp.sympify(drive.value)),
                )
                for drive in graph.source_drives
            )
        ),
        tuple(
            sorted(
                (
                    constraint.source_ref,
                    constraint.positive_node,
                    constraint.negative_node,
                    sp.srepr(sp.sympify(constraint.value)),
                )
                for constraint in graph.voltage_constraints
            )
        ),
    )
    cache.graph_refs[object_id] = graph
    cache.graph_keys[object_id] = key
    return key


def _analysis_substitutions_key(
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[tuple[str, str], ...]:
    """Return a deterministic key for the numeric parameter design point."""
    return tuple(
        sorted(
            (str(symbol), sp.srepr(sp.sympify(value)))
            for symbol, value in _numeric_substitutions(substitutions).items()
        )
    )


def _complexity_analysis_key(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[Any, ...]:
    """Build the cache key for paper topology and complexity analysis."""
    return (
        _graph_analysis_key(cache, graph),
        _analysis_substitutions_key(substitutions),
        pipeline.reference.source,
        pipeline.reference.detector,
        config.max_paths,
        config.max_loops,
        config.max_cuts,
        config.strict_cuts_only,
        config.use_simplified_complexity,
    )


def _cached_complexity_analysis(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[tuple[Any, ...], Any, PaperComplexity]:
    """Return cached meta-edge candidates, paper topology, and complexity."""
    key = _complexity_analysis_key(cache, graph, pipeline, config, substitutions)
    cached = cache.complexity_analyses.get(key)
    if cached is not None:
        return cached
    candidates = tuple(analyze_meta_edge_candidates(graph, substitutions=substitutions))
    topology = analyze_paper_topology(
        graph,
        candidates,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
        max_paths=config.max_paths,
        max_loops=config.max_loops,
        max_cuts=config.max_cuts,
        strict_cuts_only=config.strict_cuts_only,
    )
    complexity = paper_complexity(
        graph,
        candidates,
        topology,
        use_simplified=config.use_simplified_complexity,
    )
    result = (candidates, topology, complexity)
    cache.complexity_analyses[key] = result
    return result


def _cached_graph_topology_analysis(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
) -> GraphTopology:
    """Return cached source-to-detector reachability for one graph state."""
    key = (
        _graph_analysis_key(cache, graph),
        pipeline.reference.source,
        pipeline.reference.detector,
    )
    cached = cache.graph_topologies.get(key)
    if cached is not None:
        return cached
    topology = analyze_graph_topology(
        graph,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
    )
    cache.graph_topologies[key] = topology
    return topology


def _cached_numeric_graph_performance(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    frequencies: Iterable[float],
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> GraphPerformance:
    """Return cached direct numeric SFG samples for one frequency grid."""
    frequency_key = tuple(float(value) for value in frequencies)
    key = (
        _graph_analysis_key(cache, graph),
        pipeline.reference.source,
        pipeline.reference.detector,
        frequency_key,
        _analysis_substitutions_key(substitutions),
    )
    cached = cache.performances.get(key)
    if cached is not None:
        return cached
    performance = evaluate_graph_performance(
        graph,
        source=pipeline.reference.source,
        detector=pipeline.reference.detector,
        frequencies=frequency_key,
        substitutions=substitutions,
        symbolic_transfer=False,
    )
    cache.performances[key] = performance
    return performance


def _cached_subrange_symbolic_transfer(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    spec: ErrorSubSpecification,
    source: str | None,
    detector: str | None,
    substitutions: dict[str | sp.Symbol, Any] | None,
    relative_threshold: float,
    edge_relative_threshold: float,
    max_symbolic_root_degree: int,
) -> SubrangeSymbolicTransfer:
    """Reuse an exact symbolic solve when the reduced graph is unchanged.

    With both dominance thresholds equal to zero, ``spec`` only labels the
    result and does not alter the graph equations. Nonzero thresholds depend
    on the representative frequency, so their frequency bounds remain part of
    the cache key.
    """
    frequency_key: tuple[Any, ...] = ()
    if relative_threshold != 0.0 or edge_relative_threshold != 0.0:
        frequency_key = (
            spec.cluster_index,
            spec.lower_frequency_hz,
            spec.upper_frequency_hz,
        )
    key = (
        _graph_analysis_key(cache, graph),
        source,
        detector,
        _analysis_substitutions_key(substitutions),
        float(relative_threshold),
        float(edge_relative_threshold),
        int(max_symbolic_root_degree),
        frequency_key,
    )
    cached = cache.symbolic_transfers.get(key)
    if cached is None:
        cached = solve_subrange_symbolic_transfer(
            graph,
            spec,
            source=source,
            detector=detector,
            substitutions=substitutions,
            relative_threshold=relative_threshold,
            edge_relative_threshold=edge_relative_threshold,
            max_symbolic_root_degree=max_symbolic_root_degree,
        )
        cache.symbolic_transfers[key] = cached
    lower = spec.lower_frequency_hz
    upper = spec.upper_frequency_hz
    if lower <= 0.0 and np.isfinite(upper):
        lower = max(upper / 100.0, 1e-6)
    if not np.isfinite(upper):
        upper = max(lower * 100.0, 1.0)
    if lower <= 0.0:
        lower = max(upper / 100.0, 1e-6)
    return replace(
        cached,
        cluster_index=spec.cluster_index,
        lower_frequency_hz=spec.lower_frequency_hz,
        upper_frequency_hz=spec.upper_frequency_hz,
        representative_frequency_hz=float(np.sqrt(lower * upper)),
    )


def _cached_root_context(
    cache: _ManipulationAnalysisCache,
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any] | None,
    candidates: tuple[Any, ...],
    paper_topology: Any,
    graph_topology: GraphTopology,
    include_summing: bool,
) -> tuple[tuple[RootLocalization, ...], tuple[SummingPointRoot, ...]]:
    """Return cached OLR localization and optional SPR observability context."""
    key = (
        _complexity_analysis_key(cache, graph, pipeline, config, substitutions),
        include_summing,
    )
    cached = cache.root_contexts.get(key)
    if cached is not None:
        return cached
    localizations = tuple(
        localize_candidate_roots(
            candidates,
            pipeline.clusters,
            graph_topology,
            pipeline.error_specs,
        )
    )
    if include_summing:
        summing_roots = tuple(
            localize_closed_loop_summing_roots(
                graph,
                pipeline.reference,
                pipeline.clusters,
                localizations,
                paper_topology,
                substitutions,
            )
        )
    else:
        summing_roots = ()
    localizations = tuple(
        root_observability_check(
            graph,
            localizations,
            paper_topology,
            substitutions,
            summing_point_roots=summing_roots,
            dominance_ratio=config.root_observability_dominance_ratio,
            feedback_interaction_threshold=config.root_feedback_loop_gain_threshold,
        )
    )
    result = (localizations, summing_roots)
    cache.root_contexts[key] = result
    return result


def _run_simplification_round(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any],
    frequencies: Iterable[float],
    performance_spec: PerformanceSpecification,
    phase: str,
    cluster_index: int | None,
    steps: list[SimplificationStep],
    step_index: int,
    apply_accepted_to_current: bool | None = None,
    analysis_cache: _ManipulationAnalysisCache | None = None,
) -> tuple[SignalFlowGraph, int]:
    """Try ranked graph manipulations and accept those within the error budget."""
    current_graph = graph
    analysis_cache = analysis_cache or _ManipulationAnalysisCache()
    if apply_accepted_to_current is None:
        apply_accepted_to_current = phase != "subrange" or config.apply_subrange_to_global
    frequencies = tuple(frequencies)
    reference_samples = _pipeline_reference_samples(pipeline.reference, frequencies, substitutions)
    attempted = 0
    accepted = 0
    max_accepted = config.max_steps_per_subrange if phase == "subrange" and config.max_steps_per_subrange is not None else config.max_steps
    for manipulation_kind in config.manipulation_sequence:
        while accepted < max_accepted and attempted < config.max_candidates_per_round:
            remaining = config.max_candidates_per_round - attempted
            evaluations = _rank_manipulation_evaluations(
                current_graph,
                pipeline,
                config,
                substitutions,
                frequencies,
                reference_samples,
                performance_spec,
                cluster_index=cluster_index,
                manipulation_kind=manipulation_kind,
                evaluation_limit=remaining,
                analysis_cache=analysis_cache,
            )
            if not evaluations:
                break
            progress = False
            for evaluation in evaluations:
                manipulation = evaluation.manipulation
                attempted += 1
                before = graph_complexity(current_graph)
                after = graph_complexity(evaluation.graph)
                if evaluation.absolute_error.accepted:
                    if apply_accepted_to_current:
                        current_graph = evaluation.graph
                    steps.append(
                        SimplificationStep(
                            index=step_index,
                            phase=phase,
                            cluster_index=cluster_index,
                            manipulation=manipulation,
                            accepted=True,
                            reason="within_error_budget",
                            error=evaluation.absolute_error,
                            complexity_before=before,
                            complexity_after=after,
                            paper_complexity_before=evaluation.complexity_before,
                            paper_complexity_after=evaluation.complexity_after,
                            quality=evaluation.quality,
                            ranking_value=evaluation.ranking_value,
                            relative_error=evaluation.relative_error,
                        )
                    )
                    step_index += 1
                    accepted += 1
                    if not apply_accepted_to_current:
                        return current_graph, step_index
                    progress = True
                    break
                steps.append(
                    SimplificationStep(
                        index=step_index,
                        phase=phase,
                        cluster_index=cluster_index,
                        manipulation=manipulation,
                        accepted=False,
                        reason="error_budget_exceeded",
                        error=evaluation.absolute_error,
                        complexity_before=before,
                        complexity_after=after,
                        paper_complexity_before=evaluation.complexity_before,
                        paper_complexity_after=evaluation.complexity_after,
                        quality=evaluation.quality,
                        ranking_value=evaluation.ranking_value,
                        relative_error=evaluation.relative_error,
                    )
                )
                step_index += 1
                if attempted >= config.max_candidates_per_round:
                    break
            if not progress:
                break
        if accepted >= max_accepted or attempted >= config.max_candidates_per_round:
            break
    return current_graph, step_index


def _run_bias_presimplification(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any],
    frequencies: Iterable[float],
    performance_spec: PerformanceSpecification,
    steps: list[SimplificationStep],
    step_index: int,
) -> tuple[SignalFlowGraph, int]:
    """Apply only user-declared bias-node VPC candidates under the paper's small margin."""
    requested_pairs = {
        frozenset((_node_name(left), _node_name(right)))
        for left, right in config.bias_node_merges
        if _node_name(left) != _node_name(right)
    }
    if not requested_pairs:
        return graph, step_index

    frequencies = tuple(frequencies)
    reference_samples = _pipeline_reference_samples(pipeline.reference, frequencies, substitutions)
    current_graph = graph
    attempted = 0
    while attempted < config.max_candidates_per_round:
        current_performance = evaluate_graph_performance(
            current_graph,
            source=pipeline.reference.source,
            detector=pipeline.reference.detector,
            frequencies=frequencies,
            substitutions=substitutions,
            symbolic_transfer=False,
        )
        evaluations: list[tuple[float, GraphManipulation, SignalFlowGraph, GraphPerformanceError, GraphPerformanceError]] = []
        for manipulation in propose_vertex_pair_contractions(current_graph):
            pair = frozenset((_node_name(manipulation.source), _node_name(manipulation.target)))
            if pair not in requested_pairs:
                continue
            try:
                candidate_graph = apply_graph_manipulation(current_graph, manipulation).graph
                candidate_performance = evaluate_graph_performance(
                    candidate_graph,
                    source=pipeline.reference.source,
                    detector=pipeline.reference.detector,
                    frequencies=frequencies,
                    substitutions=substitutions,
                    symbolic_transfer=False,
                )
            except Exception:
                continue
            absolute_error = compare_graph_performance(
                candidate_performance,
                reference_samples,
                error_budget=performance_spec.relative_error_limit,
                error_norm=performance_spec.error_norm,
                magnitude_error_db=performance_spec.magnitude_error_db,
                phase_error_deg=performance_spec.phase_error_deg,
                error_scope="absolute",
            )
            relative_error = compare_graph_performance(
                candidate_performance,
                current_performance.samples,
                error_budget=performance_spec.relative_error_limit,
                error_norm=performance_spec.error_norm,
                magnitude_error_db=performance_spec.magnitude_error_db,
                phase_error_deg=performance_spec.phase_error_deg,
                error_scope="relative",
            )
            evaluations.append(
                (relative_error.norm_error, manipulation, candidate_graph, relative_error, absolute_error)
            )

        if not evaluations:
            break
        progress = False
        for ranking_value, manipulation, candidate_graph, relative_error, absolute_error in sorted(
            evaluations,
            key=lambda item: item[0],
        ):
            attempted += 1
            before = graph_complexity(current_graph)
            after = graph_complexity(candidate_graph)
            accepted = absolute_error.accepted
            steps.append(
                SimplificationStep(
                    index=step_index,
                    phase="pre_reduction",
                    cluster_index=None,
                    manipulation=manipulation,
                    accepted=accepted,
                    reason="within_presimplification_margin" if accepted else "presimplification_margin_exceeded",
                    error=absolute_error,
                    complexity_before=before,
                    complexity_after=after,
                    quality=float(before.simple_score - after.simple_score),
                    ranking_value=ranking_value,
                    relative_error=relative_error,
                )
            )
            step_index += 1
            if accepted:
                current_graph = candidate_graph
                progress = True
                break
            if attempted >= config.max_candidates_per_round:
                break
        if not progress:
            break
    return current_graph, step_index


def _rank_manipulation_evaluations(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any],
    frequencies: Iterable[float],
    reference_samples: Iterable[FrequencySample],
    performance_spec: PerformanceSpecification,
    cluster_index: int | None = None,
    manipulation_kind: str | None = None,
    evaluation_limit: int | None = None,
    analysis_cache: _ManipulationAnalysisCache | None = None,
) -> list[ManipulationEvaluation]:
    """Rank graph manipulations with paper Eq.(11)/(12)."""
    analysis_cache = analysis_cache or _ManipulationAnalysisCache()
    base_candidates, base_topology, base_complexity = _cached_complexity_analysis(
        analysis_cache,
        graph,
        pipeline,
        config,
        substitutions,
    )
    base_graph_topology = _cached_graph_topology_analysis(
        analysis_cache,
        graph,
        pipeline,
    )
    current_performance = _cached_numeric_graph_performance(
        analysis_cache,
        graph,
        pipeline,
        frequencies,
        substitutions,
    )
    base_localizations, base_summing_roots = _cached_root_context(
        analysis_cache,
        graph,
        pipeline,
        config,
        substitutions,
        base_candidates,
        base_topology,
        base_graph_topology,
        include_summing=manipulation_kind in {None, "RSP", "RR"},
    )
    evaluations: list[ManipulationEvaluation] = []
    manipulations = _ordered_manipulation_candidates(
        graph,
        pipeline,
        config,
        substitutions,
        frequencies,
        base_candidates,
        base_localizations,
        base_summing_roots,
        manipulation_kind=manipulation_kind,
        cluster_index=cluster_index,
        subgraph_only=config.evaluate_subgraph_candidates_only,
    )
    if evaluation_limit is not None:
        manipulations = manipulations[: max(int(evaluation_limit), 0)]
    for manipulation in manipulations:
        try:
            applied = apply_graph_manipulation(graph, manipulation)
            if config.use_simplified_complexity:
                applied_candidates = tuple(
                    analyze_meta_edge_candidates(applied.graph, substitutions=substitutions)
                )
                applied_complexity = _paper_simplified_complexity(
                    applied.graph,
                    applied_candidates,
                )
            else:
                _applied_candidates, _applied_topology, applied_complexity = (
                    _cached_complexity_analysis(
                        analysis_cache,
                        applied.graph,
                        pipeline,
                        config,
                        substitutions,
                    )
                )
            quality = float(base_complexity.value - applied_complexity.value)
            if quality <= 0:
                continue
            performance = _cached_numeric_graph_performance(
                analysis_cache,
                applied.graph,
                pipeline,
                frequencies,
                substitutions,
            )
            absolute_error = compare_graph_performance(
                performance,
                reference_samples,
                error_budget=performance_spec.relative_error_limit,
                error_norm=performance_spec.error_norm,
                magnitude_error_db=performance_spec.magnitude_error_db,
                phase_error_deg=performance_spec.phase_error_deg,
                error_scope="absolute",
            )
            relative_error = compare_graph_performance(
                performance,
                current_performance.samples,
                error_budget=performance_spec.relative_error_limit,
                error_norm=performance_spec.error_norm,
                magnitude_error_db=performance_spec.magnitude_error_db,
                phase_error_deg=performance_spec.phase_error_deg,
                error_scope="relative",
            )
            ranking_value = _paper_ranking_value(
                relative_error.norm_error,
                quality,
                config.ranking_alpha,
            )
            evaluations.append(
                ManipulationEvaluation(
                    manipulation=manipulation,
                    graph=applied.graph,
                    relative_error=relative_error,
                    absolute_error=absolute_error,
                    complexity_before=base_complexity,
                    complexity_after=applied_complexity,
                    quality=quality,
                    ranking_value=ranking_value,
                )
            )
        except Exception:
            continue
    evaluations.sort(key=lambda item: item.ranking_value)
    return evaluations


def _paper_simplified_complexity(
    graph: SignalFlowGraph,
    candidates: Iterable[Any],
) -> PaperComplexity:
    """Compute paper Eq.(10) without enumerating paths, loops, or graph cuts."""
    incoming_counts: dict[str, int] = {}
    for edge in graph.meta_edges.values():
        incoming_counts[edge.target] = incoming_counts.get(edge.target, 0) + 1
    return PaperComplexity(
        open_loop_roots=sum(len(candidate.roots) for candidate in candidates),
        summing_points=sum(count > 1 for count in incoming_counts.values()),
        forward_paths=0,
        feedback_loops=0,
        use_simplified=True,
    )


def _use_exhaustive_paper_candidates(
    graph: SignalFlowGraph,
    config: SimplificationConfig,
) -> bool:
    """Use complete paper candidates for small graphs and bounded screening for large graphs."""
    mode = config.candidate_generation_mode.strip().lower()
    if mode not in {"auto", "exhaustive", "bounded"}:
        raise ValueError(
            "candidate_generation_mode must be 'auto', 'exhaustive', or 'bounded'"
        )
    if config.exhaustive_candidate_edge_limit < 0:
        raise ValueError("exhaustive_candidate_edge_limit must be non-negative")
    if mode == "exhaustive":
        return True
    if mode == "bounded":
        return False
    return len(graph.meta_edges) <= config.exhaustive_candidate_edge_limit


def _ordered_manipulation_candidates(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
    substitutions: dict[str | sp.Symbol, Any],
    frequencies: Iterable[float],
    current_candidates: Iterable[Any],
    current_localizations: Iterable[RootLocalization],
    current_summing_roots: Iterable[SummingPointRoot],
    manipulation_kind: str | None = None,
    cluster_index: int | None = None,
    subgraph_only: bool = True,
) -> list[GraphManipulation]:
    """Return candidates in the paper's manipulation-class order and optional G_j scope."""
    frequency = _representative_frequency(frequencies)
    exhaustive = _use_exhaustive_paper_candidates(graph, config)
    if manipulation_kind == "VPC":
        candidates = propose_vertex_pair_contractions(graph)
    elif manipulation_kind == "RSP":
        candidates = (
            propose_signal_path_removals(graph)
            if exhaustive
            else _propose_subrange_signal_path_removals(
                graph,
                pipeline,
                substitutions,
                frequency,
                current_localizations,
                current_summing_roots,
                threshold=config.rsp_path_relative_threshold,
                cluster_index=cluster_index,
            )
        )
    elif manipulation_kind == "RR":
        candidates = (
            propose_open_loop_root_removals(graph)
            if exhaustive
            else _propose_subrange_root_removals(
                graph,
                substitutions,
                frequency,
                current_localizations,
                threshold=config.rr_partition_relative_threshold,
                cluster_index=cluster_index,
            )
        )
    elif manipulation_kind == "SS":
        local_zeros: tuple[LocalizedSymbolicZero, ...] = ()
        if config.enable_local_zero_expressions and pipeline.topology_metrics is not None:
            local_zeros = tuple(
                derive_local_summing_zero_expression(
                    graph,
                    root,
                    pipeline.topology_metrics,
                    substitutions=substitutions,
                    max_paths=config.local_zero_max_paths,
                    max_loops=config.local_zero_max_loops,
                    max_vertices=config.local_zero_max_vertices,
                    max_symbolic_operations=config.local_zero_max_symbolic_operations,
                    max_relative_root_error=(
                        config.target_root_relative_error
                        if config.enforce_target_root_error
                        else math.inf
                    ),
                    strategy=config.local_zero_strategy,
                )
                for root in current_summing_roots
                if "zero" in root.root_source
                and (cluster_index is None or root.cluster_index == cluster_index)
            )
        candidates = propose_subgraph_substitutions(graph)
        candidates.extend(propose_local_zero_substitutions(graph, local_zeros))
    else:
        candidates = propose_graph_manipulations(graph)
    if cluster_index is None or not subgraph_only:
        return _prioritize_protected_candidates(candidates)
    subgraph = next((item for item in pipeline.subgraphs if item.cluster_index == cluster_index), None)
    if subgraph is None:
        return _prioritize_protected_candidates(candidates)
    scoped: list[GraphManipulation] = []
    for candidate in candidates:
        touched_edges = set(candidate.removed_edges or ())
        if candidate.key[0].endswith(".*") or candidate.key[1].endswith(".*"):
            source_node = candidate.source[:-2] if candidate.source.endswith(".*") else candidate.source
            target_node = candidate.target[:-2] if candidate.target.endswith(".*") else candidate.target
            touched_vertices = {
                f"{source_node}.v", f"{source_node}.i",
                f"{target_node}.v", f"{target_node}.i",
            }
            if touched_vertices & set(subgraph.vertices):
                scoped.append(candidate)
            continue
        touched_edges.add(candidate.key)
        if touched_edges & set(subgraph.edges):
            scoped.append(candidate)
    return _prioritize_protected_candidates(scoped)


def _propose_subrange_root_removals(
    graph: SignalFlowGraph,
    substitutions: dict[sp.Symbol, sp.Expr],
    frequency_hz: float,
    localizations: Iterable[RootLocalization],
    threshold: float,
    cluster_index: int | None,
) -> list[GraphManipulation]:
    """Propose RR operations by deleting subrange-nondominant order partitions."""
    localizations = tuple(localizations)
    manipulations: list[GraphManipulation] = []
    seen: set[tuple[str, str, str, int | None, str]] = set()
    for edge in graph.meta_edges.values():
        key = (edge.source, edge.target, edge.domain)
        if edge.source.endswith(".src") or edge.domain == "voltage":
            continue
        base, partitions = _rr_base_partitions(edge)
        if len(partitions) < 2:
            continue
        protected_orders = _protected_rr_orders(edge, partitions, localizations, cluster_index, substitutions)
        for order, reason in _root_directed_rr_orders(edge, partitions, localizations, cluster_index):
            if order in protected_orders:
                continue
            manipulation = _make_rr_manipulation(edge, base, partitions, order, f"{reason}_at_{frequency_hz:.6e}Hz")
            if manipulation is None:
                continue
            marker = (*manipulation.key, manipulation.removed_order, manipulation.reason)
            if marker not in seen:
                seen.add(marker)
                manipulations.append(manipulation)
        for manipulation in _propose_term_level_rr_removals(
            edge,
            base,
            protected_orders,
            frequency_hz,
            substitutions,
            threshold,
        ):
            marker = (*manipulation.key, manipulation.removed_order, manipulation.reason)
            if marker not in seen:
                seen.add(marker)
                manipulations.append(manipulation)
        magnitudes = {
            order: _evaluate_partition_magnitude(value, frequency_hz, substitutions)
            for order, value in partitions.items()
        }
        maximum = max(magnitudes.values(), default=0.0)
        if maximum <= 0:
            continue
        for order, magnitude in sorted(magnitudes.items(), key=lambda item: item[1]):
            if order is None or order in protected_orders or magnitude > max(threshold, 0.0) * maximum:
                continue
            manipulation = _make_rr_manipulation(
                edge,
                base,
                partitions,
                order,
                (
                    f"tau_RR_remove_nondominant_order_partition_{order}_from_{base}"
                    f"_ratio_{magnitude / maximum:.6e}_at_{frequency_hz:.6e}Hz"
                ),
            )
            if manipulation is None:
                continue
            marker = (*manipulation.key, manipulation.removed_order, manipulation.reason)
            if marker not in seen:
                seen.add(marker)
                manipulations.append(manipulation)
    if manipulations or cluster_index is not None:
        return manipulations
    return propose_open_loop_root_removals(graph)


def _propose_subrange_signal_path_removals(
    graph: SignalFlowGraph,
    pipeline: ReferencePipelineResult,
    substitutions: dict[sp.Symbol, sp.Expr],
    frequency_hz: float,
    localizations: Iterable[RootLocalization],
    summing_roots: Iterable[SummingPointRoot],
    threshold: float,
    cluster_index: int | None,
) -> list[GraphManipulation]:
    """Propose RSP only for weak incoming signals at current summing vertices."""
    protected_edges = {
        (item.source, item.target, item.domain)
        for item in localizations
        if item.observable and item.cluster_index == cluster_index
    }
    dominant_spr_edges = _dominant_spr_edges_by_vertex(summing_roots, cluster_index)
    incoming: dict[str, list[MetaEdge]] = {}
    adjacency: dict[str, set[str]] = {}
    for edge in graph.meta_edges.values():
        if edge.source.endswith(".src") or edge.domain == "voltage":
            continue
        incoming.setdefault(edge.target, []).append(edge)
        adjacency.setdefault(edge.source, set()).add(edge.target)
    source_vertex = _select_source_drive(graph, pipeline.reference.source).vertex_name
    scored: list[tuple[float, GraphManipulation]] = []
    for target, edges in incoming.items():
        if len(edges) < 2:
            continue
        magnitudes = [
            _incoming_path_signal_magnitude(graph, source_vertex, edge, frequency_hz, substitutions)
            for edge in edges
        ]
        maximum = max(magnitudes, default=0.0)
        if maximum <= 0:
            continue
        protected_spr_edges = dominant_spr_edges.get(target, set())
        for edge, magnitude in sorted(zip(edges, magnitudes), key=lambda item: item[1]):
            key = (edge.source, edge.target, edge.domain)
            if key in protected_edges:
                continue
            if protected_spr_edges and key in protected_spr_edges:
                continue
            is_weak = magnitude <= max(threshold, 0.0) * maximum
            closes_feedback_loop = _edge_closes_feedback_loop(edge, adjacency)
            if not is_weak and not closes_feedback_loop:
                continue
            scored.append(
                (
                    (0.0 if is_weak else 1.0) + magnitude / maximum,
                    GraphManipulation(
                        kind="RSP",
                        source=edge.source,
                        target=edge.target,
                        domain=edge.domain,
                        removed_order=None,
                        original_value=edge.value,
                        new_value=None,
                        reason=(
                            f"tau_RSP_remove_nondominant_summing_input_at_{target}"
                            f"_ratio_{magnitude / maximum:.6e}"
                            + ("_feedback_candidate" if closes_feedback_loop else "")
                            + ("_dominant_SPR_protected" if protected_spr_edges else "")
                        ),
                    ),
                )
            )
    if scored or cluster_index is not None:
        return [item for _score, item in sorted(scored, key=lambda row: row[0])]
    return propose_signal_path_removals(graph)


def _edge_closes_feedback_loop(edge: MetaEdge, adjacency: dict[str, set[str]]) -> bool:
    """Return whether an incoming summing edge participates in a directed cycle."""
    stack = [edge.target]
    visited: set[str] = set()
    while stack:
        vertex = stack.pop()
        if vertex == edge.source:
            return True
        if vertex in visited:
            continue
        visited.add(vertex)
        stack.extend(adjacency.get(vertex, set()) - visited)
    return False


def _prioritize_protected_candidates(candidates: Iterable[GraphManipulation]) -> list[GraphManipulation]:
    """Sort cheap/local candidates before expensive topology-changing candidates."""
    kind_order = {"VPC": 0, "RSP": 1, "RR": 2, "SS": 3}
    return sorted(
        candidates,
        key=lambda item: (
            kind_order.get(item.kind, 99),
            item.source.endswith(".src"),
            item.domain == "voltage",
            item.source,
            item.target,
            item.domain,
            -999 if item.removed_order is None else item.removed_order,
        ),
    )


def _paper_ranking_value(error: float, quality: float, alpha: float) -> float:
    """Return paper Eq.(11) ranking value."""
    alpha = min(max(float(alpha), 0.0), 1.0)
    error_term = max(float(error), 1e-300) ** alpha
    quality_term = max(float(quality), 1e-300) ** (1.0 - alpha)
    return float(error_term / quality_term)


def _normalize_error_norm(
    error_norm: str,
    magnitude_error_db: float | None = None,
    phase_error_deg: float | None = None,
) -> str:
    """Return a supported error norm name, preserving old Bode-limit behavior."""
    norm = (error_norm or "").strip().lower().replace("-", "_")
    aliases = {
        "relative_inf": "relative_linf",
        "relative_linf": "relative_linf",
        "relative_max": "relative_linf",
        "relative_l2": "relative_l2",
        "relative_rms": "relative_l2",
        "absolute_inf": "absolute_linf",
        "absolute_linf": "absolute_linf",
        "absolute_max": "absolute_linf",
        "bode": "bode_linf",
        "bode_inf": "bode_linf",
        "bode_linf": "bode_linf",
        "hybrid": "hybrid_linf",
        "hybrid_linf": "hybrid_linf",
    }
    if norm in {"", "auto"}:
        return "bode_linf" if magnitude_error_db is not None or phase_error_deg is not None else "relative_linf"
    if norm not in aliases:
        raise ValueError(
            "Unsupported error_norm. Use one of: relative_linf, relative_l2, "
            "absolute_linf, bode_linf, hybrid_linf."
        )
    normalized = aliases[norm]
    if normalized == "bode_linf" and magnitude_error_db is None and phase_error_deg is None:
        raise ValueError("bode_linf requires --mag-error-db and/or --phase-error-deg.")
    return normalized


def _allocated_error_budget(
    spec: ErrorSubSpecification,
    pipeline: ReferencePipelineResult,
    config: SimplificationConfig,
) -> float:
    """Return the unchanged user bound for a frequency-restricted subrange."""
    del pipeline, config
    return spec.error_budget


def _representative_frequency(frequencies: Iterable[float]) -> float:
    """Return a geometric-center frequency for subrange dominance checks."""
    values = [float(value) for value in frequencies if float(value) > 0 and math.isfinite(float(value))]
    if not values:
        return 1.0
    return float(math.sqrt(min(values) * max(values)))


def _node_name(vertex_or_node: str) -> str:
    """Return a circuit-node name from a node-pair or voltage/current vertex label."""
    name = str(vertex_or_node)
    for suffix in (".*", ".v", ".i"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _rr_base_partitions(edge: MetaEdge) -> tuple[str, dict[int | None, sp.Expr]]:
    """Return the expression whose order partitions should be tested for RR."""
    if edge.domain == "impedance":
        base = sp.simplify(1 / edge.value)
        label = "admittance_inverse_of_impedance_edge"
    else:
        base = sp.simplify(edge.value)
        label = edge.domain
    terms = list(sp.Add.make_args(sp.expand(base)))
    grouped: dict[int | None, list[sp.Expr]] = {}
    for term in terms:
        grouped.setdefault(_laplace_order(term), []).append(term)
    return label, {order: sp.simplify(sum(parts)) for order, parts in grouped.items()}


def _rr_base_expression(edge: MetaEdge) -> tuple[str, sp.Expr]:
    """Return the admittance/expression base used by RR manipulations."""
    if edge.domain == "impedance":
        return "admittance_inverse_of_impedance_edge", sp.simplify(1 / edge.value)
    return edge.domain, sp.simplify(edge.value)


def _propose_term_level_rr_removals(
    edge: MetaEdge,
    base_label: str,
    protected_orders: set[int | None],
    frequency_hz: float,
    substitutions: dict[sp.Symbol, sp.Expr],
    threshold: float,
) -> list[GraphManipulation]:
    """Propose RR operations that remove weak additive terms inside one order partition."""
    _label, base_expr = _rr_base_expression(edge)
    terms = [sp.simplify(term) for term in sp.Add.make_args(sp.expand(base_expr))]
    if len(terms) < 2:
        return []
    magnitudes = [_evaluate_partition_magnitude(term, frequency_hz, substitutions) for term in terms]
    maximum = max(magnitudes, default=0.0)
    if maximum <= 0:
        return []
    manipulations: list[GraphManipulation] = []
    term_threshold = min(max(threshold, 0.0), 1e-2)
    for term, magnitude in sorted(zip(terms, magnitudes), key=lambda item: item[1]):
        if len(manipulations) >= 4:
            break
        order = _laplace_order(term)
        if order is None or order in protected_orders:
            continue
        ratio = magnitude / maximum
        if ratio > term_threshold:
            continue
        manipulation = _make_rr_term_manipulation(
            edge,
            base_label,
            base_expr,
            term,
            order,
            (
                f"tau_RR_remove_nondominant_term_order_{order}"
                f"_ratio_{ratio:.6e}_at_{frequency_hz:.6e}Hz"
            ),
        )
        if manipulation is not None:
            manipulations.append(manipulation)
    return manipulations


def _make_rr_term_manipulation(
    edge: MetaEdge,
    base: str,
    base_expr: sp.Expr,
    removed_term: sp.Expr,
    order: int,
    reason: str,
) -> GraphManipulation | None:
    """Create one RR manipulation by removing a single additive term."""
    kept = sp.simplify(base_expr - removed_term)
    if kept == 0:
        return None
    new_value = sp.simplify(1 / kept) if edge.domain == "impedance" else kept
    if new_value == 0:
        return None
    term_label = sp.sstr(sp.simplify(removed_term)).replace("|", " ")
    return GraphManipulation(
        kind="RR",
        source=edge.source,
        target=edge.target,
        domain=edge.domain,
        removed_order=order,
        original_value=edge.value,
        new_value=new_value,
        reason=f"{reason}_remove_{term_label}_from_{base}",
    )


def _make_rr_manipulation(
    edge: MetaEdge,
    base: str,
    partitions: dict[int | None, sp.Expr],
    order: int | None,
    reason: str,
) -> GraphManipulation | None:
    """Create one RR manipulation by removing a selected order partition."""
    if order is None or order not in partitions:
        return None
    kept = sp.simplify(sum(value for part_order, value in partitions.items() if part_order != order))
    if kept == 0:
        return None
    new_value = sp.simplify(1 / kept) if edge.domain == "impedance" else kept
    if new_value == 0:
        return None
    return GraphManipulation(
        kind="RR",
        source=edge.source,
        target=edge.target,
        domain=edge.domain,
        removed_order=order,
        original_value=edge.value,
        new_value=new_value,
        reason=f"{reason}_from_{base}",
    )


def _protected_rr_orders(
    edge: MetaEdge,
    partitions: dict[int | None, sp.Expr],
    localizations: Iterable[RootLocalization],
    cluster_index: int | None,
    substitutions: dict[sp.Symbol, sp.Expr],
) -> set[int | None]:
    """Protect dominant order partitions that generate an observable root in the active cluster."""
    if cluster_index is None:
        return set()
    key = (edge.source, edge.target, edge.domain)
    protected: set[int | None] = set()
    for item in localizations:
        if not item.observable or item.cluster_index != cluster_index:
            continue
        if (item.source, item.target, item.domain) != key:
            continue
        magnitudes = {
            order: _evaluate_partition_magnitude(value, item.root_frequency_hz, substitutions)
            for order, value in partitions.items()
        }
        maximum = max(magnitudes.values(), default=0.0)
        if maximum <= 0:
            continue
        ranked = sorted(
            (order for order in magnitudes if order is not None),
            key=lambda order: magnitudes[order],
            reverse=True,
        )
        protected.update(ranked[:2])
        protected.update(order for order, magnitude in magnitudes.items() if magnitude >= 0.2 * maximum)
    return protected


def _root_directed_rr_orders(
    edge: MetaEdge,
    partitions: dict[int | None, sp.Expr],
    localizations: Iterable[RootLocalization],
    cluster_index: int | None,
) -> list[tuple[int | None, str]]:
    """Return RR orders implied by roots below/above the active frequency subrange."""
    if cluster_index is None:
        return []
    finite_orders = sorted(order for order in partitions if order is not None)
    if len(finite_orders) < 2:
        return []
    key = (edge.source, edge.target, edge.domain)
    orders: list[tuple[int | None, str]] = []
    for item in localizations:
        if item.cluster_index is None or (item.source, item.target, item.domain) != key:
            continue
        if item.root_kind != "pole":
            continue
        if item.cluster_index < cluster_index:
            orders.append(
                (
                    finite_orders[0],
                    f"tau_RR_remove_lower_order_partition_after_lower_frequency_root_cluster_{item.cluster_index}",
                )
            )
        elif item.cluster_index > cluster_index:
            orders.append(
                (
                    finite_orders[-1],
                    f"tau_RR_remove_higher_order_partition_before_higher_frequency_root_cluster_{item.cluster_index}",
                )
            )
    return orders


def _dominant_spr_edges_by_vertex(
    roots: Iterable[SummingPointRoot],
    cluster_index: int | None,
) -> dict[str, set[tuple[str, str, str]]]:
    """Return incoming edges that must be kept to preserve dominant local SPRs."""
    if cluster_index is None:
        return {}
    protected: dict[str, set[tuple[str, str, str]]] = {}
    for root in roots:
        if root.cluster_index != cluster_index or not root.dominant:
            continue
        for path in root.path_edges:
            if not path:
                continue
            last_edge = path[-1]
            protected.setdefault(root.vertex, set()).add(last_edge)
    return protected


def _laplace_order(expr: sp.Expr) -> int | None:
    """Return the Laplace order of a monomial-like expression."""
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


def _evaluate_partition_magnitude(
    expr: sp.Expr,
    frequency_hz: float,
    substitutions: dict[sp.Symbol, sp.Expr],
) -> float:
    """Evaluate the magnitude of one order partition at a frequency."""
    try:
        return abs(_evaluate_expr_numeric(expr, frequency_hz, substitutions))
    except Exception:
        return 0.0


def _incoming_path_signal_magnitude(
    graph: SignalFlowGraph,
    source_vertex: str,
    incoming_edge: MetaEdge,
    frequency_hz: float,
    substitutions: dict[sp.Symbol, sp.Expr],
    max_paths: int = 64,
) -> float:
    """Estimate signal level carried by one edge into a summing vertex."""
    prefix = _path_gain_sum(
        graph,
        source_vertex,
        incoming_edge.source,
        frequency_hz,
        substitutions,
        max_paths=max_paths,
    )
    try:
        edge_gain = _evaluate_expr_numeric(incoming_edge.value, frequency_hz, substitutions)
    except Exception:
        edge_gain = 0.0
    return abs(prefix * edge_gain)


def _path_gain_sum(
    graph: SignalFlowGraph,
    source: str,
    target: str,
    frequency_hz: float,
    substitutions: dict[sp.Symbol, sp.Expr],
    max_paths: int = 64,
) -> complex:
    """Return the sum of simple path gains from source to target."""
    if source == target:
        return 1.0 + 0.0j
    adjacency: dict[str, list[MetaEdge]] = {}
    for edge in graph.meta_edges.values():
        if edge.source in graph.vertices and edge.target in graph.vertices:
            adjacency.setdefault(edge.source, []).append(edge)
    total = 0.0 + 0.0j
    stack: list[tuple[str, complex, frozenset[str]]] = [(source, 1.0 + 0.0j, frozenset({source}))]
    path_count = 0
    while stack and path_count < max_paths:
        vertex, gain, visited = stack.pop()
        for edge in adjacency.get(vertex, []):
            if edge.target in visited:
                continue
            try:
                edge_gain = _evaluate_expr_numeric(edge.value, frequency_hz, substitutions)
            except Exception:
                continue
            next_gain = gain * edge_gain
            if edge.target == target:
                total += next_gain
                path_count += 1
                if path_count >= max_paths:
                    break
            else:
                stack.append((edge.target, next_gain, visited | {edge.target}))
    return total


def _solve_linear_equations(equations: list[sp.Expr], variables: list[sp.Symbol]) -> dict[sp.Symbol, sp.Expr]:
    """Solve linear SFG equations and return a symbol-expression map."""
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


def _reference_samples(
    laplace: sp.Expr,
    frequencies: Iterable[float],
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[FrequencySample, ...]:
    """Sample the SLiCAP reference expression with numeric substitutions."""
    substitution_map = _numeric_substitutions(substitutions)
    return tuple(
        FrequencySample(
            frequency_hz=float(frequency),
            value=_evaluate_laplace(laplace, frequency, substitution_map),
        )
        for frequency in frequencies
    )


def _pipeline_reference_samples(
    reference: NumericReferenceResult | DescriptorMatrixResult,
    frequencies: Iterable[float],
    substitutions: dict[str | sp.Symbol, Any] | None,
) -> tuple[FrequencySample, ...]:
    """Sample either a symbolic reference or a numerical descriptor pencil."""
    if isinstance(reference, DescriptorMatrixResult):
        return tuple(sample_descriptor_frequency_response(reference, frequencies))
    return _reference_samples(reference.laplace, frequencies, substitutions)


def _evaluate_graph_sample_numeric(
    graph: SignalFlowGraph,
    drive: Any,
    detector_vertex: str,
    frequency_hz: float,
    substitutions: dict[sp.Symbol, sp.Expr],
) -> complex:
    """Solve SFG equations numerically at one frequency point."""
    return _evaluate_graph_samples_numeric(
        graph,
        drive,
        detector_vertex,
        (frequency_hz,),
        substitutions,
    )[0]


def _evaluate_graph_samples_numeric(
    graph: SignalFlowGraph,
    drive: Any,
    detector_vertex: str,
    frequencies: Iterable[float],
    substitutions: dict[sp.Symbol, sp.Expr],
) -> tuple[complex, ...]:
    """Solve one SFG over a frequency grid while reusing topology and edge evaluators."""
    vertex_names = sorted(graph.vertices)
    index_by_vertex = {name: index for index, name in enumerate(vertex_names)}
    source_vertices = {item.vertex_name for item in graph.source_drives}
    source_rows: list[tuple[int, Any]] = []
    for item in graph.source_drives:
        if item.vertex_name not in index_by_vertex:
            continue
        source_rows.append(
            (
                index_by_vertex[item.vertex_name],
                _numeric_expression_evaluator(item.value, substitutions),
            )
        )
    incoming: dict[str, list[Any]] = {}
    for edge in graph.meta_edges.values():
        incoming.setdefault(edge.target, []).append(edge)
    equation_rows: list[tuple[int, tuple[tuple[int, Any], ...]]] = []
    for target, edges in incoming.items():
        if target not in index_by_vertex or target in source_vertices:
            continue
        voltage_edges = [edge for edge in edges if edge.domain == "voltage"]
        signal_edges = [edge for edge in edges if edge.domain != "voltage"]
        for edge_group in (signal_edges, voltage_edges):
            if not edge_group:
                continue
            terms: list[tuple[int, Any]] = []
            for edge in edge_group:
                if edge.source not in index_by_vertex:
                    continue
                terms.append(
                    (
                        index_by_vertex[edge.source],
                        _numeric_expression_evaluator(edge.value, substitutions),
                    )
                )
            equation_rows.append((index_by_vertex[target], tuple(terms)))
    zero_rows = tuple(index_by_vertex[name] for name in _kcl_zero_vertices(graph))
    if detector_vertex not in index_by_vertex:
        raise ValueError(f"Detector vertex '{detector_vertex}' is not in the graph.")
    row_count = len(source_rows) + len(equation_rows) + len(zero_rows)
    if row_count == 0:
        raise ValueError("No SFG equations were generated.")
    drive_evaluator = _numeric_expression_evaluator(drive.value, substitutions)
    results: list[complex] = []
    for frequency_hz in frequencies:
        matrix = np.zeros((row_count, len(vertex_names)), dtype=complex)
        vector = np.zeros(row_count, dtype=complex)
        row_index = 0
        for vertex_index, evaluator in source_rows:
            matrix[row_index, vertex_index] = 1.0
            vector[row_index] = evaluator(frequency_hz)
            row_index += 1
        for target_index, terms in equation_rows:
            matrix[row_index, target_index] = 1.0
            for source_index, evaluator in terms:
                matrix[row_index, source_index] -= evaluator(frequency_hz)
            row_index += 1
        for vertex_index in zero_rows:
            matrix[row_index, vertex_index] = 1.0
            row_index += 1
        if matrix.shape[0] == matrix.shape[1]:
            solution = np.linalg.solve(matrix, vector)
        else:
            solution, *_ = np.linalg.lstsq(matrix, vector, rcond=None)
        drive_value = drive_evaluator(frequency_hz)
        detector_value = solution[index_by_vertex[detector_vertex]]
        results.append(
            complex(detector_value)
            if abs(drive_value) <= 1e-300
            else complex(detector_value / drive_value)
        )
    return tuple(results)


def _numeric_expression_evaluator(
    expr: Any,
    substitutions: dict[sp.Symbol, sp.Expr],
):
    """Compile a scalar edge expression once for repeated frequency evaluation."""
    prepared = sp.sympify(expr).subs(substitutions)
    remaining = prepared.free_symbols - {ini.laplace}
    if remaining:
        return lambda frequency_hz: _evaluate_expr_numeric(prepared, frequency_hz, {})
    function = sp.lambdify(ini.laplace, prepared, modules="numpy")
    return lambda frequency_hz: complex(function(2j * np.pi * float(frequency_hz)))


def _evaluate_expr_numeric(expr: Any, frequency_hz: float, substitutions: dict[sp.Symbol, sp.Expr]) -> complex:
    """Evaluate a scalar SFG expression at one frequency point."""
    value = sp.sympify(expr).subs(substitutions)
    value = value.subs(ini.laplace, 2 * sp.pi * sp.I * float(frequency_hz))
    return complex(sp.N(value))


def _evaluate_laplace(expr: sp.Expr, frequency_hz: float, substitutions: dict[sp.Symbol, sp.Expr]) -> complex:
    """Evaluate one Laplace expression at ``s = j 2*pi*f``."""
    return _evaluate_expr_numeric(expr, frequency_hz, substitutions)


def _magnitude_db(value: complex) -> float:
    """Return magnitude in dB with a floor that avoids log(0)."""
    return 20.0 * math.log10(max(abs(value), 1e-300))


def _phase_difference_deg(left: complex, right: complex) -> float:
    """Return wrapped phase difference in degrees."""
    diff = float(np.angle(left, deg=True) - np.angle(right, deg=True))
    return (diff + 180.0) % 360.0 - 180.0


def _frequency_grid(
    specs: Iterable[ErrorSubSpecification],
    reference: NumericReferenceResult | DescriptorMatrixResult,
    points_per_subrange: int,
    frequency_range_hz: tuple[float, float] | None = None,
    include_boundaries: bool = True,
) -> tuple[float, ...]:
    """Create a positive logarithmic frequency grid for error checks."""
    root_freqs = sorted(
        abs(root) / (2 * math.pi)
        for root in tuple(reference.poles) + tuple(reference.zeros)
        if abs(root) > 0
    )
    if root_freqs:
        global_min = max(min(root_freqs) / 100, 1e-6)
        global_max = max(max(root_freqs) * 100, global_min * 10)
    else:
        global_min = 1.0
        global_max = 1e9
    if frequency_range_hz is not None:
        configured_min, configured_max = frequency_range_hz
        if configured_min <= 0 or configured_max <= configured_min:
            raise ValueError("frequency_range_hz must be a positive (min, max) tuple with max > min.")
        global_min = float(configured_min)
        global_max = float(configured_max)
    frequencies: set[float] = set()
    for spec in specs:
        lower = spec.lower_frequency_hz
        upper = spec.upper_frequency_hz
        if lower <= 0 or not math.isfinite(lower):
            lower = global_min
        if upper <= lower or not math.isfinite(upper):
            upper = global_max
        lower = max(lower, global_min)
        upper = min(upper, global_max)
        if upper <= lower:
            continue
        point_count = max(points_per_subrange, 2)
        if include_boundaries:
            values = np.geomspace(lower, upper, point_count)
        else:
            values = np.geomspace(lower, upper, point_count + 2)[1:-1]
        for value in values:
            frequencies.add(float(value))
    if not frequencies:
        for value in np.geomspace(global_min, global_max, max(points_per_subrange, 2)):
            frequencies.add(float(value))
    return tuple(sorted(frequencies))


def _numeric_substitutions(substitutions: dict[str | sp.Symbol, Any] | None) -> dict[sp.Symbol, sp.Expr]:
    """Convert substitutions to SymPy expressions with SLiCAP metric-prefix parsing."""
    if not substitutions:
        return {}
    converted: dict[sp.Symbol, sp.Expr] = {}
    for key, value in substitutions.items():
        if key == ini.laplace or str(key) == str(ini.laplace):
            continue
        converted[sp.Symbol(str(key))] = evaluate_numeric_expression(value, {}).numeric
    return converted


def _symbolically_equal(left: Any, right: Any) -> bool:
    """Return whether two transfer expressions are exactly algebraically equal."""
    try:
        return sp.simplify(sp.cancel(sp.sympify(left) - sp.sympify(right))) == 0
    except (sp.PolynomialError, TypeError, ValueError):
        return False


def _kcl_zero_vertices(graph: SignalFlowGraph) -> tuple[str, ...]:
    """Return node-current vertices whose voltage is fixed by a voltage constraint."""
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


def _select_source_drive(graph: SignalFlowGraph, source: str | None) -> Any:
    """Select the source drive used as the input variable."""
    if not graph.source_drives:
        raise ValueError("The SFG has no source drive.")
    if source is None and graph.source_name is not None:
        source = graph.source_name
    if source is None:
        if len(graph.source_drives) != 1:
            names = ", ".join(item.source_ref for item in graph.source_drives)
            raise ValueError(f"Multiple source drives exist; pass source=... Available: {names}")
        return graph.source_drives[0]
    for item in graph.source_drives:
        if source in {item.source_ref, item.vertex_name}:
            return item
    names = ", ".join(item.source_ref for item in graph.source_drives)
    raise ValueError(f"Unknown source '{source}'. Available: {names}")


def _first_detector(graph: SignalFlowGraph) -> str:
    """Return a deterministic voltage detector when none is provided."""
    if graph.detector_name is not None:
        return graph.detector_name
    voltage_vertices = sorted(name for name, vertex in graph.vertices.items() if vertex.kind == "voltage")
    if not voltage_vertices:
        raise ValueError("The SFG has no voltage detector candidate.")
    return voltage_vertices[0]


def _detector_to_vertex(graph: SignalFlowGraph, detector: str) -> str:
    """Map a SLiCAP detector name to an SFG vertex name."""
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


def _safe_symbol_name(name: str) -> str:
    """Create a SymPy-safe variable name from an SFG vertex name."""
    return "x_" + "".join(ch if ch.isalnum() else "_" for ch in name)


def _format_step(step: SimplificationStep, precision: int) -> list[str]:
    """Format one simplification step as Markdown lines."""
    error = "none" if step.error is None else f"{step.error.max_relative_error:.{precision}e}"
    norm_error = "none" if step.error is None else f"{step.error.norm_error:.{precision}e}"
    relative_norm = "none" if step.relative_error is None else f"{step.relative_error.norm_error:.{precision}e}"
    mag_error = "none" if step.error is None else f"{step.error.max_magnitude_error_db:.{precision}e}"
    phase_error = "none" if step.error is None else f"{step.error.max_phase_error_deg:.{precision}e}"
    after = "none" if step.complexity_after is None else str(step.complexity_after.simple_score)
    paper_before = "none" if step.paper_complexity_before is None else str(step.paper_complexity_before.value)
    paper_after = "none" if step.paper_complexity_after is None else str(step.paper_complexity_after.value)
    return [
        f"- `{step.index}` {step.manipulation.kind} `{step.manipulation.source}` -> `{step.manipulation.target}`",
        f"  relative_norm=`{relative_norm}`, absolute_norm=`{norm_error}`, error=`{error}`, "
        f"mag=`{mag_error}` dB, phase=`{phase_error}` deg, "
        f"score=`{step.complexity_before.simple_score}` -> `{after}`, "
        f"C=`{paper_before}` -> `{paper_after}`, reason=`{step.reason}`, "
        f"operation=`{step.manipulation.reason}`",
    ]


def _append_transfer_roots(lines: list[str], roots: Iterable[Any], precision: int) -> None:
    """Append roots from a SubrangeSymbolicTransfer to Markdown lines."""
    roots = list(roots)
    if not roots:
        lines.append("- none")
        return
    for root in roots:
        expression = "numeric-only" if root.expression is None else _format_expr_limited(root.expression)
        numeric = "none" if root.numeric_value is None else str(root.numeric_value)
        frequency = "none" if root.frequency_hz is None else f"{root.frequency_hz:.{precision}e} Hz"
        exact = "symbolic" if root.exact else "numeric"
        lines.append(
            f"- {root.kind}: `{expression}`, value `{numeric}`, frequency `{frequency}`, "
            f"degree `{root.polynomial_degree}`, `{exact}`"
        )


def _format_expr_limited(expr: Any, max_chars: int = 3000) -> str:
    """Format a symbolic expression without expensive refactoring and cap length."""
    try:
        text = sp.sstr(expr)
    except Exception:
        text = str(expr)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... <truncated {len(text) - max_chars} chars>"
