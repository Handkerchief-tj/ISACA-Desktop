#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the paper-oriented SFG pipeline and export auditable reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sfg_prototype import (  # noqa: E402
    SimplificationConfig,
    error_policy_report,
    error_specification_report,
    localized_symbolic_zero_report,
    numeric_reference_report,
    root_cluster_report,
    root_localization_report,
    simplification_report,
    simplify_netlist,
    subrange_simplification_report,
)


def _write(path: Path, text: str) -> None:
    """Write one UTF-8 report with a trailing newline."""
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    print(f"Saved: {path}")


def main() -> None:
    """Parse arguments, run the full pipeline, and export its main artifacts."""
    parser = argparse.ArgumentParser(description="Run paper-style symbolic SFG simplification.")
    parser.add_argument("netlist", type=Path, help="SLiCAP .cir netlist.")
    parser.add_argument("--out-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("--f-min", type=float, default=None, help="Lower analysis frequency in Hz.")
    parser.add_argument("--f-max", type=float, default=None, help="Upper analysis frequency in Hz.")
    parser.add_argument("--magnitude-error-db", type=float, default=None)
    parser.add_argument("--phase-error-deg", type=float, default=None)
    parser.add_argument(
        "--root-error",
        type=float,
        default=None,
        help=(
            "Optional per-root relative-error limit. Omit it for the paper's "
            "imaginary-axis transfer-error mode, which permits root shifting."
        ),
    )
    parser.add_argument("--max-steps-per-subrange", type=int, default=10)
    parser.add_argument(
        "--candidate-generation",
        choices=("auto", "exhaustive", "bounded"),
        default="auto",
        help="Enumerate paper candidates or use bounded dominance screening.",
    )
    parser.add_argument(
        "--exhaustive-edge-limit",
        type=int,
        default=16,
        help="Maximum meta-edge count for exhaustive candidates in auto mode.",
    )
    parser.add_argument(
        "--observability-dominance-ratio",
        type=float,
        default=10.0,
        help="Competing-path dominance ratio used by downstream observability checks.",
    )
    parser.add_argument(
        "--feedback-loop-threshold",
        type=float,
        default=1.0,
        help="Loop-gain boundary for suppressing an open-loop pole.",
    )
    parser.add_argument("--local-zero-strategy", choices=("auto", "paths", "matrix"), default="auto")
    args = parser.parse_args()

    frequency_range = None
    if args.f_min is not None or args.f_max is not None:
        if args.f_min is None or args.f_max is None or args.f_min <= 0 or args.f_max <= args.f_min:
            parser.error("--f-min and --f-max must define one positive increasing range.")
        frequency_range = (args.f_min, args.f_max)
    if args.exhaustive_edge_limit < 0:
        parser.error("--exhaustive-edge-limit must be non-negative.")
    if args.observability_dominance_ratio < 0 or args.feedback_loop_threshold < 0:
        parser.error("observability and feedback thresholds must be non-negative.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = simplify_netlist(
        str(args.netlist.resolve()),
        config=SimplificationConfig(
            frequency_range_hz=frequency_range,
            magnitude_error_db=args.magnitude_error_db,
            phase_error_deg=args.phase_error_deg,
            enforce_target_root_error=args.root_error is not None,
            target_root_relative_error=0.05 if args.root_error is None else args.root_error,
            max_steps_per_subrange=args.max_steps_per_subrange,
            candidate_generation_mode=args.candidate_generation,
            exhaustive_candidate_edge_limit=args.exhaustive_edge_limit,
            root_observability_dominance_ratio=args.observability_dominance_ratio,
            root_feedback_loop_gain_threshold=args.feedback_loop_threshold,
            local_zero_strategy=args.local_zero_strategy,
        ),
    )
    local_zeros = tuple(
        zero
        for subrange in result.subrange_results
        for zero in subrange.localized_symbolic_zeros
    )
    _write(args.out_dir / "reference.md", numeric_reference_report(result.pipeline.reference))
    _write(args.out_dir / "clusters.md", root_cluster_report(result.pipeline.clusters))
    _write(args.out_dir / "error_policy.md", error_policy_report(result))
    _write(args.out_dir / "error_subranges.md", error_specification_report(result.pipeline.error_specs))
    _write(args.out_dir / "root_localization.md", root_localization_report(result.pipeline.localizations))
    _write(args.out_dir / "local_zero_localization.md", localized_symbolic_zero_report(local_zeros))
    _write(args.out_dir / "subrange_simplification.md", subrange_simplification_report(result))
    _write(args.out_dir / "simplification.md", simplification_report(result))
    _write(args.out_dir / "initial_sfg.dot", result.pipeline.graph.to_dot())
    _write(args.out_dir / "final_sfg.dot", result.final_graph.to_dot())


if __name__ == "__main__":
    main()
