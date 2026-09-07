#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run the prototype SLiCAP N -> G converter and export inspection artifacts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sfg_prototype import netlist_to_signal_flow_graph


def main() -> None:
    """Parse CLI arguments, build the graph, and write reports."""
    parser = argparse.ArgumentParser(description="Inspect a SLiCAP netlist with the prototype SFG converter.")
    parser.add_argument("netlist", type=Path, help="Path to a .cir netlist file.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Directory for DOT/SVG/report outputs.")
    parser.add_argument("--style", default="default", choices=["default", "boxed", "linear_points"], help="DOT style.")
    parser.add_argument("--strict", action="store_true", help="Fail on deferred elements or unresolved constraints.")
    args = parser.parse_args()

    output_dir = args.out_dir or args.netlist.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.netlist.stem

    network, graph = netlist_to_signal_flow_graph(str(args.netlist), strict=args.strict)

    dot_path = output_dir / f"{stem}.dot"
    audit_path = output_dir / f"{stem}_audit.md"
    meta_path = output_dir / f"{stem}_meta_edges.md"
    svg_base = output_dir / f"{stem}_sfg"

    dot_path.write_text(graph.to_dot(style=args.style), encoding="utf-8")
    audit_path.write_text(graph.audit_report(), encoding="utf-8")
    meta_path.write_text(graph.meta_edge_report(), encoding="utf-8")

    svg_path = None
    try:
        svg_path = graph.render_dot(str(svg_base), format="svg", style=args.style)
    except RuntimeError as exc:
        print("svg render skipped:", exc)

    print("title =", network.title)
    print("network summary =", network.summary())
    print("source =", network.source)
    print("detector =", network.detector)
    print("graph summary =", graph.summary())
    print("dot =", dot_path)
    print("audit =", audit_path)
    print("meta_edges =", meta_path)
    if svg_path is not None:
        print("svg =", svg_path)


if __name__ == "__main__":
    main()
