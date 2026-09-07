#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prototype graph manipulations for the paper's network simplification stage.

This module implements operational versions of the four manipulation categories
from section III-A:

- RSP: signal path removal, implemented as deleting an incoming meta-edge
  of a summing vertex.
- RR: open-loop root removal, implemented as deleting one extreme order
  partition from one meta-edge.
- VPC: vertex-pair contraction, implemented conservatively as node-pair
  aliasing for graph nodes that can be shorted without removing the I/O.
- SS: subgraph substitution, implemented as replacing a valid simple chain
  that preserves graph connectedness with one equivalent edge.

The implementation is intentionally conservative. It protects source/voltage
constraint edges by default. These transformations are evaluated by the
error-controlled simplification loop before they are accepted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Iterable

import sympy as sp

from .slicap_compat import ini
from .sfg import GraphAuditEvent, MetaEdge, SignalFlowGraph


@dataclass(frozen=True)
class GraphManipulation:
    """One proposed graph manipulation."""

    kind: str
    source: str
    target: str
    domain: str
    removed_order: int | None
    original_value: sp.Expr
    new_value: sp.Expr | None
    reason: str
    removed_edges: tuple[tuple[str, str, str], ...] = ()
    replacement_edges: tuple[tuple[str, str, str, sp.Expr], ...] = ()
    removed_vertices: tuple[str, ...] = ()
    vertex_aliases: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the target meta-edge key."""
        return (self.source, self.target, self.domain)


@dataclass(frozen=True)
class ManipulationApplication:
    """Result of applying one manipulation to a graph copy."""

    manipulation: GraphManipulation
    graph: SignalFlowGraph
    removed_meta_edges: int
    changed_meta_edges: int


@dataclass(frozen=True)
class GraphComplexity:
    """Small complexity summary used to inspect a manipulation effect."""

    vertices: int
    meta_edges: int
    candidate_roots: int
    summing_vertices: int

    @property
    def simple_score(self) -> int:
        """Return the simplified complexity score used by the current prototype."""
        return self.candidate_roots + self.summing_vertices


def propose_signal_path_removals(
    graph: SignalFlowGraph,
    protect_sources: bool = True,
    protect_voltage_constraints: bool = True,
) -> list[GraphManipulation]:
    """Propose paper τ_RSP by deleting incoming edges of summing vertices."""
    manipulations: list[GraphManipulation] = []
    incoming, _outgoing = _edge_incidence(graph)
    summing_targets = {target for target, edges in incoming.items() if len(edges) > 1}
    for edge in graph.meta_edges.values():
        if edge.target not in summing_targets:
            continue
        if protect_sources and edge.source.endswith(".src"):
            continue
        if protect_voltage_constraints and edge.domain == "voltage":
            continue
        manipulations.append(
            GraphManipulation(
                kind="RSP",
                source=edge.source,
                target=edge.target,
                domain=edge.domain,
                removed_order=None,
                original_value=edge.value,
                new_value=None,
                reason="tau_RSP_delete_incoming_meta_edge_of_summing_vertex",
            )
        )
    return manipulations


def propose_open_loop_root_removals(graph: SignalFlowGraph) -> list[GraphManipulation]:
    """Propose paper τ_RR by removing extreme order partitions."""
    manipulations: list[GraphManipulation] = []
    for edge in graph.meta_edges.values():
        if edge.source.endswith(".src") or edge.domain == "voltage":
            continue
        rr_base, partitions = _rr_partition_base(edge)
        orders = sorted(order for order in partitions if order is not None)
        if len(orders) < 2:
            continue
        for order in {orders[0], orders[-1]}:
            kept_value = sp.simplify(sum(value for part_order, value in partitions.items() if part_order != order))
            if edge.domain == "impedance":
                if _is_zero(kept_value):
                    continue
                new_value = sp.simplify(1 / kept_value)
            else:
                new_value = kept_value
            if _is_zero(new_value):
                continue
            manipulations.append(
                GraphManipulation(
                    kind="RR",
                    source=edge.source,
                    target=edge.target,
                    domain=edge.domain,
                    removed_order=order,
                    original_value=edge.value,
                    new_value=new_value,
                    reason=f"tau_RR_remove_extreme_order_partition_{order}_from_{rr_base}",
                )
            )
    return manipulations


def propose_graph_manipulations(graph: SignalFlowGraph) -> list[GraphManipulation]:
    """Return the current prototype's complete manipulation set."""
    manipulations = []
    manipulations.extend(propose_signal_path_removals(graph))
    manipulations.extend(propose_open_loop_root_removals(graph))
    manipulations.extend(propose_vertex_pair_contractions(graph))
    manipulations.extend(propose_subgraph_substitutions(graph))
    return manipulations


def propose_vertex_pair_contractions(graph: SignalFlowGraph) -> list[GraphManipulation]:
    """Propose paper τ_VPC by shorting voltage/current vertex pairs."""
    manipulations: list[GraphManipulation] = []
    protected_nodes = _protected_nodes(graph)
    for left, right in _candidate_node_pairs(graph):
        if left in protected_nodes and right in protected_nodes:
            continue
        source_node, target_node = _vpc_alias_direction(left, right, protected_nodes)
        aliases = _node_pair_vertex_aliases(graph, source_node, target_node)
        if not aliases:
            continue
        removed_edges = _edges_between_nodes(graph, source_node, target_node)
        if not removed_edges:
            continue
        if not _vpc_preserves_io(graph, source_node, target_node):
            continue
        manipulations.append(
            GraphManipulation(
                kind="VPC",
                source=f"{source_node}.*",
                target=f"{target_node}.*",
                domain="node_pair",
                removed_order=None,
                original_value=sp.Integer(0),
                new_value=None,
                reason=f"tau_VPC_contract_node_pair_{source_node}_into_{target_node}",
                removed_edges=tuple(sorted(removed_edges)),
                vertex_aliases=tuple(sorted(aliases)),
                removed_vertices=tuple(sorted(_node_vertices(graph, source_node))),
            )
        )
    return manipulations


def propose_subgraph_substitutions(graph: SignalFlowGraph) -> list[GraphManipulation]:
    """Propose paper τ_SS by substituting valid simple chain subgraphs."""
    manipulations: list[GraphManipulation] = []
    incoming, outgoing = _edge_incidence(graph)
    protected = _protected_vertices(graph)
    for vertex in sorted(graph.vertices):
        if vertex in protected:
            continue
        in_edges = incoming.get(vertex, [])
        out_edges = outgoing.get(vertex, [])
        if len(in_edges) != 1 or len(out_edges) != 1:
            continue
        in_edge = in_edges[0]
        out_edge = out_edges[0]
        if in_edge.domain == "voltage" or out_edge.domain == "voltage":
            continue
        if in_edge.source == out_edge.target:
            continue
        if _touches_summing_or_source(graph, vertex):
            continue
        replacement_value = sp.simplify(in_edge.value * out_edge.value)
        if _is_zero(replacement_value):
            continue
        manipulations.append(
            GraphManipulation(
                kind="SS",
                source=in_edge.source,
                target=out_edge.target,
                domain=out_edge.domain,
                removed_order=None,
                original_value=sp.simplify(in_edge.value * out_edge.value),
                new_value=replacement_value,
                reason=f"tau_SS_substitute_connected_chain_through_{vertex}",
                removed_edges=(in_edge_key(in_edge), in_edge_key(out_edge)),
                replacement_edges=((in_edge.source, out_edge.target, out_edge.domain, replacement_value),),
                removed_vertices=(vertex,),
            )
        )
    return manipulations


def propose_local_zero_substitutions(
    graph: SignalFlowGraph,
    localized_zeros: Iterable[Any],
) -> list[GraphManipulation]:
    """Propose exact SS replacements for strict local-zero graph cuts.

    Fallback/reconvergent regions remain report-only. A replacement is emitted
    only when every edge incident on an internal cut vertex stays inside the
    cut, which preserves the topological integrity required by paper III-A.
    """
    manipulations: list[GraphManipulation] = []
    for zero in localized_zeros:
        if (
            getattr(zero, "status", None) != "resolved"
            or getattr(zero, "cut_kind", None) != "complementary"
            or getattr(zero, "equivalent_transfer", None) is None
        ):
            continue
        start = getattr(zero, "cut_start", None)
        end = getattr(zero, "cut_end", None)
        if not start or not end or start == end:
            continue
        region_vertices = {start, end}
        for path in getattr(zero, "paths", ()):
            region_vertices.update(path.vertices)
        internal_vertices = region_vertices - {start, end}
        if not internal_vertices:
            continue
        if any(
            (edge.source in internal_vertices or edge.target in internal_vertices)
            and not ({edge.source, edge.target} <= region_vertices)
            for edge in graph.meta_edges.values()
        ):
            continue
        removed_edges = tuple(
            sorted(
                key
                for key, edge in graph.meta_edges.items()
                if edge.source in region_vertices and edge.target in region_vertices
            )
        )
        if not removed_edges:
            continue
        value = sp.factor(sp.cancel(zero.equivalent_transfer))
        manipulations.append(
            GraphManipulation(
                kind="SS",
                source=start,
                target=end,
                domain="subgraph",
                removed_order=None,
                original_value=value,
                new_value=value,
                reason=f"tau_SS_replace_complementary_local_zero_region_at_{zero.vertex}",
                removed_edges=removed_edges,
                replacement_edges=((start, end, "subgraph", value),),
                removed_vertices=tuple(sorted(internal_vertices)),
            )
        )
    return manipulations


def apply_graph_manipulation(
    graph: SignalFlowGraph,
    manipulation: GraphManipulation,
) -> ManipulationApplication:
    """Apply one manipulation to a deep copy of the graph."""
    new_graph = deepcopy(graph)
    removed = 0
    changed = 0
    key = manipulation.key
    if manipulation.kind in {"RSP", "RR"} and key not in new_graph.meta_edges:
        raise KeyError(f"Meta-edge not found: {key}")
    if manipulation.kind == "RSP":
        del new_graph.meta_edges[key]
        new_graph.contributions = [
            item for item in new_graph.contributions
            if (item.source, item.target, item.domain) != key
        ]
        removed = 1
    elif manipulation.kind == "RR":
        if manipulation.new_value is None:
            raise ValueError("RR manipulation requires a new_value.")
        edge = new_graph.meta_edges[key]
        new_graph.meta_edges[key] = replace(
            edge,
            value=sp.simplify(manipulation.new_value),
            order_partitions=_order_partitions(manipulation.new_value, edge.domain),
        )
        changed = 1
    elif manipulation.kind == "VPC":
        for removed_key in manipulation.removed_edges:
            if removed_key in new_graph.meta_edges:
                del new_graph.meta_edges[removed_key]
                removed += 1
        _alias_vertices(new_graph, dict(manipulation.vertex_aliases))
        changed = len(manipulation.vertex_aliases)
    elif manipulation.kind == "SS":
        if key not in new_graph.meta_edges and not manipulation.removed_edges:
            raise KeyError(f"Meta-edge not found: {key}")
        for removed_key in manipulation.removed_edges:
            if removed_key in new_graph.meta_edges:
                del new_graph.meta_edges[removed_key]
                removed += 1
        for source, target, domain, value in manipulation.replacement_edges:
            _add_or_lump_meta_edge(new_graph, source, target, domain, value)
            changed += 1
        _remove_unused_vertices(new_graph, manipulation.removed_vertices)
    else:
        raise ValueError(f"Unsupported manipulation kind: {manipulation.kind}")

    new_graph.audit_events.append(
        GraphAuditEvent(
            action="apply_manipulation",
            detail=f"applied {manipulation.kind} to {manipulation.source} -> {manipulation.target}",
            source=manipulation.source,
            target=manipulation.target,
            value=manipulation.new_value,
            reason=manipulation.reason,
        )
    )
    return ManipulationApplication(
        manipulation=manipulation,
        graph=new_graph,
        removed_meta_edges=removed,
        changed_meta_edges=changed,
    )


def graph_complexity(graph: SignalFlowGraph) -> GraphComplexity:
    """Return a small complexity estimate for the current graph."""
    candidate_roots = 0
    for edge in graph.meta_edges.values():
        candidate_roots += len(_candidate_roots(edge.value))
    in_counts: dict[str, int] = {}
    for edge in graph.meta_edges.values():
        in_counts[edge.target] = in_counts.get(edge.target, 0) + 1
    summing_vertices = sum(1 for count in in_counts.values() if count > 1)
    return GraphComplexity(
        vertices=len(graph.vertices),
        meta_edges=len(graph.meta_edges),
        candidate_roots=candidate_roots,
        summing_vertices=summing_vertices,
    )


def manipulation_report(manipulations: Iterable[GraphManipulation]) -> str:
    """Format proposed graph manipulations as Markdown."""
    lines = ["# Graph Manipulation Candidates", ""]
    for index, manipulation in enumerate(manipulations, start=1):
        lines.append(f"## {index}. {manipulation.kind}: `{manipulation.source}` -> `{manipulation.target}`")
        lines.append(f"- domain: `{manipulation.domain}`")
        lines.append(f"- reason: `{manipulation.reason}`")
        lines.append(f"- original: `{sp.sstr(sp.simplify(manipulation.original_value))}`")
        if manipulation.removed_order is not None:
            lines.append(f"- removed order: `{manipulation.removed_order}`")
        if manipulation.new_value is not None:
            lines.append(f"- new value: `{sp.sstr(sp.simplify(manipulation.new_value))}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def manipulation_effect_report(
    graph: SignalFlowGraph,
    manipulations: Iterable[GraphManipulation],
    limit: int = 20,
) -> str:
    """Preview the complexity impact of proposed manipulations."""
    base = graph_complexity(graph)
    lines = ["# Manipulation Effect Preview", ""]
    lines.append(f"- base vertices: `{base.vertices}`")
    lines.append(f"- base meta-edges: `{base.meta_edges}`")
    lines.append(f"- base candidate roots: `{base.candidate_roots}`")
    lines.append(f"- base summing vertices: `{base.summing_vertices}`")
    lines.append(f"- base simple score: `{base.simple_score}`")
    for index, manipulation in enumerate(list(manipulations)[:limit], start=1):
        applied = apply_graph_manipulation(graph, manipulation)
        complexity = graph_complexity(applied.graph)
        lines.extend(["", f"## {index}. {manipulation.kind}: `{manipulation.source}` -> `{manipulation.target}`"])
        lines.append(f"- reason: `{manipulation.reason}`")
        lines.append(f"- meta-edges: `{base.meta_edges}` -> `{complexity.meta_edges}`")
        lines.append(f"- candidate roots: `{base.candidate_roots}` -> `{complexity.candidate_roots}`")
        lines.append(f"- summing vertices: `{base.summing_vertices}` -> `{complexity.summing_vertices}`")
        lines.append(f"- simple score: `{base.simple_score}` -> `{complexity.simple_score}`")
    return "\n".join(lines).rstrip()


def in_edge_key(edge: MetaEdge) -> tuple[str, str, str]:
    """Return a meta-edge key for one edge object."""
    return (edge.source, edge.target, edge.domain)


def _edge_incidence(graph: SignalFlowGraph) -> tuple[dict[str, list[MetaEdge]], dict[str, list[MetaEdge]]]:
    """Return incoming and outgoing meta-edge lists keyed by vertex."""
    incoming: dict[str, list[MetaEdge]] = {}
    outgoing: dict[str, list[MetaEdge]] = {}
    for edge in graph.meta_edges.values():
        incoming.setdefault(edge.target, []).append(edge)
        outgoing.setdefault(edge.source, []).append(edge)
    return incoming, outgoing


def _protected_vertices(graph: SignalFlowGraph) -> set[str]:
    """Return vertices that should not be removed by local graph rewrites."""
    protected = {drive.vertex_name for drive in graph.source_drives}
    protected.update(vertex for vertex, data in graph.vertices.items() if data.kind == "source")
    return protected


def _add_or_lump_meta_edge(
    graph: SignalFlowGraph,
    source: str,
    target: str,
    domain: str,
    value: sp.Expr,
) -> None:
    """Add a replacement meta-edge or lump it with an existing parallel edge."""
    key = (source, target, domain)
    value = sp.simplify(value)
    if key in graph.meta_edges:
        existing = graph.meta_edges[key]
        if domain == "impedance":
            combined = sp.simplify(1 / (1 / existing.value + 1 / value))
        else:
            combined = sp.simplify(existing.value + value)
        graph.meta_edges[key] = replace(
            existing,
            value=combined,
            order_partitions=_order_partitions(combined, domain),
        )
        return
    graph.meta_edges[key] = MetaEdge(
        source=source,
        target=target,
        domain=domain,
        value=value,
        order_partitions=_order_partitions(value, domain),
        contributions=(),
    )


def _remove_unused_vertices(graph: SignalFlowGraph, candidates: Iterable[str]) -> None:
    """Remove vertices that are no longer incident to any meta-edge."""
    used: set[str] = set()
    for edge in graph.meta_edges.values():
        used.add(edge.source)
        used.add(edge.target)
    used.update(drive.vertex_name for drive in graph.source_drives)
    for vertex in candidates:
        if vertex in graph.vertices and vertex not in used:
            del graph.vertices[vertex]


def _rr_partition_base(edge: MetaEdge) -> tuple[str, dict[int | None, sp.Expr]]:
    """Return the expression base whose extreme partition is removed by τ_RR."""
    if edge.domain == "impedance":
        base = sp.simplify(1 / edge.value)
        return "inverse_impedance", _order_partitions(base, "admittance")
    return "edge_value", _order_partitions(edge.value, edge.domain)


def _candidate_node_pairs(graph: SignalFlowGraph) -> list[tuple[str, str]]:
    """Return node pairs connected by at least one non-voltage meta-edge."""
    pairs: set[tuple[str, str]] = set()
    for edge in graph.meta_edges.values():
        if edge.domain == "voltage":
            continue
        source_node = _vertex_node(edge.source)
        target_node = _vertex_node(edge.target)
        if source_node is None or target_node is None or source_node == target_node:
            continue
        pairs.add(tuple(sorted((source_node, target_node))))
    return sorted(pairs)


def _protected_nodes(graph: SignalFlowGraph) -> set[str]:
    """Return graph nodes associated with independent sources."""
    protected = {drive.source_ref for drive in graph.source_drives}
    for drive in graph.source_drives:
        if drive.positive_node != "0":
            protected.add(drive.positive_node)
        if drive.negative_node != "0":
            protected.add(drive.negative_node)
    return protected


def _vpc_alias_direction(left: str, right: str, protected_nodes: set[str]) -> tuple[str, str]:
    """Choose source and target nodes for a VPC alias operation."""
    if left in protected_nodes and right not in protected_nodes:
        return right, left
    if right in protected_nodes and left not in protected_nodes:
        return left, right
    return max(left, right), min(left, right)


def _node_pair_vertex_aliases(
    graph: SignalFlowGraph,
    source_node: str,
    target_node: str,
) -> list[tuple[str, str]]:
    """Return voltage/current vertex aliases for shorting two graph nodes."""
    aliases: list[tuple[str, str]] = []
    for suffix in (".v", ".i"):
        source_vertex = f"{source_node}{suffix}"
        target_vertex = f"{target_node}{suffix}"
        if source_vertex in graph.vertices and target_vertex in graph.vertices:
            aliases.append((source_vertex, target_vertex))
    return aliases


def _node_vertices(graph: SignalFlowGraph, node: str) -> set[str]:
    """Return existing SFG vertices of one graph node."""
    return {name for name in (f"{node}.v", f"{node}.i") if name in graph.vertices}


def _edges_between_nodes(graph: SignalFlowGraph, left: str, right: str) -> set[tuple[str, str, str]]:
    """Return edges internal to a node-pair contraction."""
    keys: set[tuple[str, str, str]] = set()
    nodes = {left, right}
    for key, edge in graph.meta_edges.items():
        source_node = _vertex_node(edge.source)
        target_node = _vertex_node(edge.target)
        if source_node in nodes and target_node in nodes and source_node != target_node:
            keys.add(key)
    return keys


def _vpc_preserves_io(graph: SignalFlowGraph, source_node: str, target_node: str) -> bool:
    """Reject contractions that would remove source vertices or all detector paths."""
    removed = _node_vertices(graph, source_node)
    if any(vertex.endswith(".src") for vertex in removed):
        return False
    return bool(_node_pair_vertex_aliases(graph, source_node, target_node))


def _touches_summing_or_source(graph: SignalFlowGraph, vertex: str) -> bool:
    """Return True if SS would absorb a protected or summing vertex."""
    if vertex in _protected_vertices(graph):
        return True
    incoming, _outgoing = _edge_incidence(graph)
    return len(incoming.get(vertex, [])) > 1


def _alias_vertices(graph: SignalFlowGraph, aliases: dict[str, str]) -> None:
    """Apply vertex aliasing to meta-edges and vertices after τ_VPC."""
    if not aliases:
        return
    for source, target in aliases.items():
        if source in graph.vertices:
            del graph.vertices[source]
        graph.vertices.setdefault(target, graph.vertices[target])
    new_edges: dict[tuple[str, str, str], MetaEdge] = {}
    for edge in graph.meta_edges.values():
        source = aliases.get(edge.source, edge.source)
        target = aliases.get(edge.target, edge.target)
        if source == target:
            continue
        _add_or_lump_edge_to_dict(new_edges, source, target, edge.domain, edge.value)
    graph.meta_edges = new_edges


def _add_or_lump_edge_to_dict(
    edges: dict[tuple[str, str, str], MetaEdge],
    source: str,
    target: str,
    domain: str,
    value: sp.Expr,
) -> None:
    """Add or lump one edge in a detached edge dictionary."""
    key = (source, target, domain)
    if key in edges:
        existing = edges[key]
        if domain == "impedance":
            value = sp.simplify(1 / (1 / existing.value + 1 / value))
        else:
            value = sp.simplify(existing.value + value)
        edges[key] = replace(existing, value=value, order_partitions=_order_partitions(value, domain))
        return
    edges[key] = MetaEdge(
        source=source,
        target=target,
        domain=domain,
        value=sp.simplify(value),
        order_partitions=_order_partitions(value, domain),
        contributions=(),
    )


def _vertex_node(vertex: str) -> str | None:
    """Return the graph node name for a .v/.i vertex."""
    if vertex.endswith(".v") or vertex.endswith(".i"):
        return vertex[:-2]
    return None


def _order_partitions(expr: sp.Expr, domain: str) -> dict[int | None, sp.Expr]:
    """Partition an expression by Laplace order."""
    expr = sp.simplify(sp.sympify(expr))
    if _is_zero(expr):
        return {}
    terms = [expr]
    if domain in {"admittance", "voltage", "current"}:
        terms = list(sp.Add.make_args(sp.expand(expr)))
    grouped: dict[int | None, list[sp.Expr]] = {}
    for term in terms:
        grouped.setdefault(_laplace_order(term), []).append(term)
    return {order: sp.simplify(sum(parts)) for order, parts in grouped.items()}


def _candidate_roots(expr: sp.Expr) -> list[complex]:
    """Return numerical roots implied by numerator and denominator factors."""
    expr = sp.cancel(sp.simplify(expr))
    numerator, denominator = expr.as_numer_denom()
    return _roots_from_expr(numerator) + _roots_from_expr(denominator)


def _roots_from_expr(expr: sp.Expr) -> list[complex]:
    """Return numerical roots for a polynomial expression in ``s``."""
    expr = sp.expand(sp.sympify(expr))
    if ini.laplace not in expr.free_symbols:
        return []
    try:
        polynomial = sp.Poly(sp.N(expr), ini.laplace)
    except sp.PolynomialError:
        return []
    if polynomial.degree() <= 0:
        return []
    try:
        return [complex(root) for root in sp.nroots(polynomial.as_expr())]
    except Exception:
        # Symbolic coefficients are sufficient for complexity counting; the
        # actual numeric root values are handled in SLiCAPsfgAnalysis after
        # parameter substitution.
        return [complex(float("nan"), 0.0) for _ in range(int(polynomial.degree()))]


def _laplace_order(expr: sp.Expr) -> int | None:
    """Return the monomial order of an expression relative to ``s``."""
    expr = sp.cancel(sp.sympify(expr))
    if expr == 0:
        return None
    laplace = ini.laplace
    if laplace not in expr.free_symbols:
        return 0
    numerator, denominator = sp.fraction(expr)
    numerator_order = _monomial_power(numerator, laplace)
    denominator_order = _monomial_power(denominator, laplace)
    if numerator_order is None or denominator_order is None:
        return None
    return numerator_order - denominator_order


def _monomial_power(expr: sp.Expr, symbol: sp.Symbol) -> int | None:
    """Return the power if the expression is a monomial in ``symbol``."""
    expr = sp.expand(sp.sympify(expr))
    if expr == 0:
        return None
    try:
        poly = sp.Poly(expr, symbol)
    except sp.PolynomialError:
        return None
    if len(poly.monoms()) != 1:
        return None
    return int(poly.monoms()[0][0])


def _is_zero(value: Any) -> bool:
    """Return True if a symbolic expression is structurally zero."""
    expr = sp.simplify(sp.sympify(value))
    return expr == 0 or expr.is_zero is True
