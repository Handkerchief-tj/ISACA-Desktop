#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal-flow graph construction from a flattened SLiCAP network.

本模块只实现论文算法中的第 3 步：把已经由 SLiCAP 展平的小信号
线性网络 ``N`` 转换成信号流图 ``G``。当前流程为：

1. 读取 ``LinearNetwork``，该对象已经由 SLiCAP 完成词法/语法分析、
   模型展开和层次展平。
2. 识别独立电压源，建立小信号节点别名。非输入的零值电压源用于
   AC-ground/节点合并；输入电压源保留为外部激励。
3. 为非参考节点创建电压顶点 ``*.v`` 和电流顶点 ``*.i``。接地输入
   电压源驱动的节点只创建 ``*.v``。
4. 按元件 stamp 生成图边：R/r/C/L、VCCS、VCVS、CCCS、CCVS、独立源。
5. 删除参考节点冗余，吸收同节点 ``*.v -> *.i`` 自反馈边，合并平行边
   为 meta-edge，并计算 order partitions。
6. 输出 DOT/SVG、构图审计报告和 meta-edge 报告。

边界：本模块暂不实现论文第 4-9 步的数值参考性能、root clustering、
误差排序、图化简和可观测性检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import sympy as sp

from .slicap_compat import ini
from .network import (
    LinearElement,
    LinearNetwork,
    NumericExpression,
    evaluate_numeric_expression,
    format_numeric_expression,
    flatten_netlist,
)


@dataclass(frozen=True)
class GraphVertex:
    """表示信号流图中的一个顶点。"""

    name: str
    node: str
    kind: str


@dataclass(frozen=True)
class GraphContribution:
    """表示 meta-edge 合并前的一条原始图边贡献。"""

    source: str
    target: str
    value: sp.Expr
    element_ref: str
    element_model: str
    role: str
    domain: str
    order: int | None


@dataclass(frozen=True)
class MetaEdge:
    """表示同一对顶点之间合并后的 meta-edge。"""

    source: str
    target: str
    domain: str
    value: sp.Expr
    order_partitions: dict[int | None, sp.Expr]
    contributions: tuple[GraphContribution, ...]


@dataclass(frozen=True)
class SourceDrive:
    """记录外部独立源及其小信号激励值。"""

    source_ref: str
    vertex_name: str
    positive_node: str
    negative_node: str
    value: sp.Expr
    source_kind: str


@dataclass(frozen=True)
class VoltageConstraint:
    """记录尚未完全消除的理想电压源约束。"""

    source_ref: str
    positive_node: str
    negative_node: str
    value: sp.Expr


@dataclass(frozen=True)
class VoltageBranch:
    """记录需要统一定向的理想电压型支路。"""

    element: LinearElement
    positive_node: str
    negative_node: str


@dataclass(frozen=True)
class OrientedVoltageBranch:
    """记录经过一致性修正后的一条电压型支路。"""

    branch: VoltageBranch
    parent_node: str
    child_node: str
    sign: int
    component_root: str
    requires_alignment: bool


@dataclass(frozen=True)
class DeferredElement:
    """记录当前阶段暂未实现 stamp 的元件。"""

    refdes: str
    model: str
    reason: str


@dataclass(frozen=True)
class GraphAuditEvent:
    """记录构图过程中的可追踪事件。"""

    action: str
    detail: str
    element_ref: str | None = None
    source: str | None = None
    target: str | None = None
    value: Any = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """把审计事件转换成普通 dict，便于 JSON 导出。"""
        return {
            "action": self.action,
            "detail": self.detail,
            "element_ref": self.element_ref,
            "source": self.source,
            "target": self.target,
            "value": None if self.value is None else sp.sstr(self.value),
            "reason": self.reason,
        }


@dataclass
class SignalFlowGraph:
    """保存第一阶段构建得到的信号流图 G。"""

    title: str | None
    file_path: str | None
    source_name: str | None = None
    detector_name: str | None = None
    vertices: dict[str, GraphVertex] = field(default_factory=dict)
    contributions: list[GraphContribution] = field(default_factory=list)
    meta_edges: dict[tuple[str, str, str], MetaEdge] = field(default_factory=dict)
    source_drives: list[SourceDrive] = field(default_factory=list)
    voltage_constraints: list[VoltageConstraint] = field(default_factory=list)
    deferred_elements: list[DeferredElement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_events: list[GraphAuditEvent] = field(default_factory=list)
    dropped_contributions: list[GraphAuditEvent] = field(default_factory=list)
    voltage_aliases: dict[str, str] = field(default_factory=dict)
    current_references: dict[str, str] = field(default_factory=dict)
    current_reference_directions: dict[str, tuple[str, str]] = field(default_factory=dict)

    def record_event(
        self,
        action: str,
        detail: str,
        element_ref: str | None = None,
        source: str | None = None,
        target: str | None = None,
        value: Any = None,
        reason: str | None = None,
    ) -> GraphAuditEvent:
        """追加一条构图审计事件并返回该事件。"""
        event = GraphAuditEvent(
            action=action,
            detail=detail,
            element_ref=element_ref,
            source=source,
            target=target,
            value=value,
            reason=reason,
        )
        self.audit_events.append(event)
        return event

    def add_vertex_pair(self, node: str, is_voltage_driven: bool = False) -> None:
        """为节点创建 ``*.v``，并按需要创建 ``*.i``。"""
        voltage_name = f"{node}.v"
        self.vertices[voltage_name] = GraphVertex(name=voltage_name, node=node, kind="voltage")
        self.record_event("add_vertex", f"created voltage vertex {voltage_name}", target=voltage_name)
        if not is_voltage_driven:
            current_name = f"{node}.i"
            self.vertices[current_name] = GraphVertex(name=current_name, node=node, kind="current")
            self.record_event("add_vertex", f"created current vertex {current_name}", target=current_name)
        else:
            self.record_event(
                "skip_vertex",
                f"skipped current vertex for voltage-driven node {node}",
                target=f"{node}.i",
                reason="voltage_driven_node",
            )

    def add_source_vertex(self, source_ref: str) -> str:
        """创建独立源顶点并返回顶点名。"""
        vertex_name = f"{source_ref}.src"
        self.vertices[vertex_name] = GraphVertex(name=vertex_name, node=source_ref, kind="source")
        self.record_event("add_vertex", f"created source vertex {vertex_name}", target=vertex_name)
        return vertex_name

    def add_aux_current_vertex(
        self,
        refdes: str,
        positive_node: str | None = None,
        negative_node: str | None = None,
    ) -> str:
        """创建或复用理想电压支路/电流控制源使用的辅助电流顶点。"""
        vertex_name = f"I_{refdes}"
        if vertex_name in self.vertices:
            if positive_node is not None and negative_node is not None:
                self.current_reference_directions.setdefault(refdes, (positive_node, negative_node))
            self.record_event("reuse_current_reference", f"reused auxiliary current vertex {vertex_name}", target=vertex_name)
            return vertex_name
        self.vertices[vertex_name] = GraphVertex(name=vertex_name, node=refdes, kind="current")
        self.current_references[refdes] = vertex_name
        if positive_node is not None and negative_node is not None:
            self.current_reference_directions[refdes] = (positive_node, negative_node)
        self.record_event("current_reference", f"created auxiliary current vertex {vertex_name}", target=vertex_name)
        return vertex_name

    def add_contribution(
        self,
        source: str,
        target: str,
        value: Any,
        element: LinearElement,
        role: str,
        domain: str,
    ) -> None:
        """添加一条原始图边；若被丢弃则写入审计记录。"""
        expr = sp.sympify(value)
        if _is_zero(expr):
            self._drop_contribution(source, target, expr, element, role, domain, "zero_value")
            return
        if source not in self.vertices:
            self._drop_contribution(source, target, expr, element, role, domain, "missing_source_vertex")
            return
        if target not in self.vertices:
            self._drop_contribution(source, target, expr, element, role, domain, "missing_target_vertex")
            return
        contribution = GraphContribution(
            source=source,
            target=target,
            value=expr,
            element_ref=element.refdes,
            element_model=element.model,
            role=role,
            domain=domain,
            order=_laplace_order(expr),
        )
        self.contributions.append(contribution)
        self.record_event(
            "add_contribution",
            f"added {domain} edge {source} -> {target}",
            element_ref=element.refdes,
            source=source,
            target=target,
            value=expr,
        )

    def _drop_contribution(
        self,
        source: str,
        target: str,
        value: sp.Expr,
        element: LinearElement,
        role: str,
        domain: str,
        reason: str,
    ) -> None:
        """记录一条未进入图的候选边。"""
        event = self.record_event(
            "drop_contribution",
            f"dropped {domain} edge {source} -> {target}",
            element_ref=element.refdes,
            source=source,
            target=target,
            value=value,
            reason=reason,
        )
        self.dropped_contributions.append(event)

    def lump_meta_edges(self) -> None:
        """把平行原始边合并为 meta-edge 并生成 order partitions。"""
        grouped: dict[tuple[str, str, str], list[GraphContribution]] = {}
        for contribution in self.contributions:
            key = (contribution.source, contribution.target, contribution.domain)
            grouped.setdefault(key, []).append(contribution)
        self.meta_edges = {}
        for key, parts in grouped.items():
            value = _combine_parallel([part.value for part in parts], key[2])
            self.meta_edges[key] = MetaEdge(
                source=key[0],
                target=key[1],
                domain=key[2],
                value=value,
                order_partitions=_order_partitions(value, key[2]),
                contributions=tuple(parts),
            )
            self.record_event(
                "lump_meta_edge",
                f"lumped {len(parts)} contribution(s) into {key[0]} -> {key[1]}",
                source=key[0],
                target=key[1],
                value=value,
            )

    def eliminate_self_loops(self) -> None:
        """把同节点 ``*.v -> *.i`` 自反馈导纳吸收到 ``*.i -> *.v``。"""
        loops_to_remove: list[tuple[str, str, str]] = []
        updates: dict[tuple[str, str, str], sp.Expr] = {}
        for key, edge in list(self.meta_edges.items()):
            source, target, domain = key
            if domain != "admittance" or not source.endswith(".v") or not target.endswith(".i"):
                continue
            if source[:-2] != target[:-2]:
                continue
            imp_key = (target, source, "impedance")
            loops_to_remove.append(key)
            if imp_key in self.meta_edges:
                z_old = self.meta_edges[imp_key].value
                z_new = sp.simplify(1 / (sp.simplify(1 / z_old) - edge.value))
            else:
                z_new = sp.simplify(1 / (-edge.value))
            updates[imp_key] = z_new
            self.record_event(
                "remove_self_loop",
                f"absorbed self-loop {source} -> {target}",
                source=source,
                target=target,
                value=edge.value,
                reason="self_loop_absorbed",
            )
        for key in loops_to_remove:
            del self.meta_edges[key]
        for key, value in updates.items():
            partitions = _order_partitions(value, key[2])
            if key in self.meta_edges:
                self.meta_edges[key] = replace(self.meta_edges[key], value=value, order_partitions=partitions)
            else:
                self.meta_edges[key] = MetaEdge(
                    source=key[0],
                    target=key[1],
                    domain=key[2],
                    value=value,
                    order_partitions=partitions,
                    contributions=(),
                )
            self.record_event(
                "update_meta_edge",
                f"updated self impedance {key[0]} -> {key[1]}",
                source=key[0],
                target=key[1],
                value=value,
            )

    def summary(self) -> dict[str, int]:
        """返回图规模统计。"""
        return {
            "vertices": len(self.vertices),
            "contributions": len(self.contributions),
            "meta_edges": len(self.meta_edges),
            "source_drives": len(self.source_drives),
            "voltage_constraints": len(self.voltage_constraints),
            "deferred_elements": len(self.deferred_elements),
            "dropped_contributions": len(self.dropped_contributions),
            "audit_events": len(self.audit_events),
        }

    def to_dot(self, style: str = "default") -> str:
        """导出 Graphviz DOT 文本。"""
        lines = ["digraph slicap_sfg {", '  rankdir="LR";', '  fontname="Helvetica";']
        if style == "linear_points":
            lines.extend(["  splines=curved;", "  nodesep=0.8;", "  ranksep=1.2;"])
            names = sorted(self.vertices.keys(), key=lambda x: (x.split(".")[0], 0 if x.endswith(".i") else 1))
            for name in names:
                lines.append(
                    f'  "{name}" [shape=circle, width=0.1, height=0.1, label="", xlabel="{name}", fontcolor="blue"];'
                )
            for index in range(len(names) - 1):
                lines.append(f'  "{names[index]}" -> "{names[index + 1]}" [style=invis, minlen=1];')
        else:
            lines.extend(["  nodesep=0.5;", "  ranksep=1.0;"])
            for vertex in self.vertices.values():
                shape = "ellipse" if vertex.kind == "voltage" else "box" if vertex.kind == "current" else "diamond"
                lines.append(f'  "{vertex.name}" [shape={shape}, label="{vertex.name}"];')
        for edge in self.meta_edges.values():
            raw_value = sp.sstr(sp.simplify(edge.value))
            if style == "boxed":
                label = f'<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0"><TR><TD>{raw_value}</TD></TR></TABLE>>'
            else:
                label = f'"{raw_value}"'
            lines.append(f'  "{edge.source}" -> "{edge.target}" [label={label}];')
        lines.append("}")
        return "\n".join(lines)

    def render_dot(
        self,
        output_path: str,
        format: str = "svg",
        graph_name: str = "slicap_sfg",
        cleanup: bool = True,
        style: str = "default",
    ) -> str:
        """把 DOT 渲染成 SVG 或 PNG 文件并返回绝对路径。"""
        output_format = format.lower().lstrip(".")
        if output_format not in {"svg", "png"}:
            raise ValueError("render_dot() only supports format='svg' or format='png'.")
        path = Path(output_path).expanduser()
        if path.suffix.lower() in {".svg", ".png"}:
            suffix_format = path.suffix.lower().lstrip(".")
            if suffix_format != output_format:
                raise ValueError(f"Output suffix '.{suffix_format}' does not match format='{output_format}'.")
            path = path.with_suffix("")
        output_dir = path.parent
        if not output_dir.exists():
            raise FileNotFoundError(f"Output directory does not exist: {output_dir}. Create it before calling render_dot().")
        try:
            import graphviz
            from graphviz.backend.execute import ExecutableNotFound
        except ImportError as exc:
            raise ImportError(
                "render_dot() requires the Python package 'graphviz'. Install it in slicap_env with: "
                "conda install -n slicap_env -c conda-forge python-graphviz"
            ) from exc
        dot_text = self.to_dot(style=style)
        if graph_name:
            safe_graph_name = graph_name.replace("\\", "\\\\").replace('"', '\\"')
            dot_text = dot_text.replace("digraph slicap_sfg {", f'digraph "{safe_graph_name}" {{', 1)
        source = graphviz.Source(dot_text, filename=path.name, directory=str(output_dir), format=output_format)
        try:
            rendered_path = source.render(cleanup=cleanup)
        except ExecutableNotFound as exc:
            raise RuntimeError(
                "render_dot() requires the Graphviz executable 'dot'. Install Graphviz in slicap_env with: "
                "conda install -n slicap_env -c conda-forge graphviz"
            ) from exc
        return str(Path(rendered_path).resolve())

    def audit_report(self) -> str:
        """生成构图审计报告。"""
        lines = [f"# SFG Audit Report: {self.title or ''}", ""]
        lines.append(f"- vertices: {len(self.vertices)}")
        lines.append(f"- contributions: {len(self.contributions)}")
        lines.append(f"- meta_edges: {len(self.meta_edges)}")
        lines.append(f"- dropped_contributions: {len(self.dropped_contributions)}")
        lines.append(f"- deferred_elements: {len(self.deferred_elements)}")
        if self.voltage_aliases:
            lines.extend(["", "## Voltage Aliases"])
            for node, alias in sorted(self.voltage_aliases.items()):
                if node != alias:
                    lines.append(f"- `{node}` -> `{alias}`")
        if self.voltage_constraints:
            lines.extend(["", "## Voltage Constraints"])
            for item in self.voltage_constraints:
                lines.append(f"- `{item.source_ref}`: `{item.positive_node}` - `{item.negative_node}` = `{sp.sstr(item.value)}`")
        if self.warnings:
            lines.extend(["", "## Warnings"])
            for warning in self.warnings:
                lines.append(f"- {warning}")
        if self.dropped_contributions:
            lines.extend(["", "## Dropped Contributions"])
            for event in self.dropped_contributions:
                value = "" if event.value is None else f", value=`{sp.sstr(event.value)}`"
                lines.append(
                    f"- `{event.element_ref}`: `{event.source}` -> `{event.target}`"
                    f"{value}, reason=`{event.reason}`"
                )
        lines.extend(["", "## Events"])
        for event in self.audit_events:
            value = "" if event.value is None else f", value=`{sp.sstr(event.value)}`"
            reason = "" if event.reason is None else f", reason=`{event.reason}`"
            ref = "" if event.element_ref is None else f" `{event.element_ref}`"
            lines.append(f"- `{event.action}`{ref}: {event.detail}{value}{reason}")
        return "\n".join(lines)

    def meta_edge_report(self) -> str:
        """生成 meta-edge 与 order partition 报告。"""
        lines = [f"# Meta-Edge Report: {self.title or ''}", ""]
        for key, edge in sorted(self.meta_edges.items()):
            lines.append(f"## `{edge.source}` -> `{edge.target}` ({edge.domain})")
            lines.append(f"- value: `{sp.sstr(sp.simplify(edge.value))}`")
            if edge.order_partitions:
                for order, expr in sorted(edge.order_partitions.items(), key=lambda item: (item[0] is None, item[0] or 0)):
                    lines.append(f"- order `{order}`: `{sp.sstr(sp.simplify(expr))}`")
            candidate = _candidate_open_loop_root(edge)
            if candidate is not None:
                lines.append(f"- candidate open-loop root: `{candidate}`")
            lines.append("")
        return "\n".join(lines).rstrip()

    def numeric_contributions(
        self,
        substitutions: dict[str | sp.Symbol, Any] | None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> list[dict[str, Any]]:
        """把原始图边转换为带数值替换结果的列表。"""
        return [
            {
                "source": item.source,
                "target": item.target,
                "domain": item.domain,
                "role": item.role,
                "element_ref": item.element_ref,
                "element_model": item.element_model,
                "value": evaluate_numeric_expression(
                    item.value,
                    substitutions,
                    keep_laplace=keep_laplace,
                    numeric=numeric,
                ),
            }
            for item in self.contributions
        ]

    def numeric_meta_edges(
        self,
        substitutions: dict[str | sp.Symbol, Any] | None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> dict[tuple[str, str, str], NumericExpression]:
        """把 meta-edge 表达式转换为数值替换后的表达式视图。"""
        return {
            key: evaluate_numeric_expression(
                edge.value,
                substitutions,
                keep_laplace=keep_laplace,
                numeric=numeric,
            )
            for key, edge in self.meta_edges.items()
        }

    def numeric_source_drives(
        self,
        substitutions: dict[str | sp.Symbol, Any] | None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> list[dict[str, Any]]:
        """把独立源激励值转换为带数值替换结果的列表。"""
        return [
            {
                "source_ref": item.source_ref,
                "vertex_name": item.vertex_name,
                "positive_node": item.positive_node,
                "negative_node": item.negative_node,
                "source_kind": item.source_kind,
                "value": evaluate_numeric_expression(
                    item.value,
                    substitutions,
                    keep_laplace=keep_laplace,
                    numeric=numeric,
                ),
            }
            for item in self.source_drives
        ]

    def numeric_meta_edge_report(
        self,
        substitutions: dict[str | sp.Symbol, Any] | None,
        keep_laplace: bool = True,
        numeric: bool = False,
        notation: str = "scientific",
        precision: int = 6,
    ) -> str:
        """生成 meta-edge 的符号值、数值替换值和剩余符号报告。"""
        lines = [f"# Numeric Meta-Edge Report: {self.title or ''}", ""]
        numeric_edges = self.numeric_meta_edges(
            substitutions,
            keep_laplace=keep_laplace,
            numeric=numeric,
        )
        for key, result in sorted(numeric_edges.items()):
            source, target, domain = key
            remaining = ", ".join(sp.sstr(symbol) for symbol in result.remaining_symbols) or "none"
            lines.append(f"## `{source}` -> `{target}` ({domain})")
            lines.append(f"- symbolic: `{sp.sstr(sp.simplify(result.symbolic))}`")
            lines.append(
                f"- numeric: `{format_numeric_expression(sp.simplify(result.numeric), notation=notation, precision=precision)}`"
            )
            lines.append(f"- status: `{result.status}`")
            lines.append(f"- remaining symbols: `{remaining}`")
            lines.append("")
        return "\n".join(lines).rstrip()

    def to_dict(self) -> dict[str, Any]:
        """导出便于 JSON 序列化的图数据。"""
        return {
            "title": self.title,
            "file_path": self.file_path,
            "source_name": self.source_name,
            "detector_name": self.detector_name,
            "vertices": {key: vars(value) for key, value in self.vertices.items()},
            "contributions": [
                {**vars(item), "value": sp.sstr(item.value)}
                for item in self.contributions
            ],
            "meta_edges": [
                {
                    "source": item.source,
                    "target": item.target,
                    "domain": item.domain,
                    "value": sp.sstr(item.value),
                    "order_partitions": {str(order): sp.sstr(expr) for order, expr in item.order_partitions.items()},
                }
                for item in self.meta_edges.values()
            ],
            "source_drives": [{**vars(item), "value": sp.sstr(item.value)} for item in self.source_drives],
            "voltage_constraints": [{**vars(item), "value": sp.sstr(item.value)} for item in self.voltage_constraints],
            "deferred_elements": [vars(item) for item in self.deferred_elements],
            "warnings": list(self.warnings),
            "audit_events": [item.to_dict() for item in self.audit_events],
        }


def build_signal_flow_graph(network: LinearNetwork, strict: bool = False, audit: bool = True) -> SignalFlowGraph:
    """把展平网络 ``N`` 转换成第一阶段信号流图 ``G``。"""
    source_refs = _source_refdes(network)
    current_control_refs = _current_control_references(network)
    aliases, alias_events, alias_conflicts = _voltage_source_aliases(
        network,
        source_refs,
        current_control_refs,
    )
    graph = SignalFlowGraph(
        title=network.title,
        file_path=network.file_path,
        source_name=None if not network.source else network.source[0],
        detector_name=None if not network.detector else network.detector[0],
    )
    graph.voltage_aliases = aliases
    for event in alias_events:
        graph.audit_events.append(event)
    for conflict in alias_conflicts:
        graph.warnings.append(conflict)
        graph.record_event("voltage_constraint_conflict", conflict, reason="voltage_source_loop_conflict")
    voltage_driven_nodes = _voltage_driven_nodes(network, aliases, source_refs)
    active_nodes = sorted({_alias_node(node, aliases) for node in network.active_nodes if _alias_node(node, aliases) != "0"})
    for node in active_nodes:
        graph.add_vertex_pair(node, is_voltage_driven=node in voltage_driven_nodes)
    voltage_branches = _collect_voltage_branches(
        graph,
        network,
        aliases,
        source_refs,
        current_control_refs,
    )
    for branch in voltage_branches:
        graph.current_reference_directions[branch.element.refdes] = (branch.positive_node, branch.negative_node)
    for element in network.elements.values():
        if element.model in {"V", "E", "H"}:
            continue
        _stamp_element(graph, element, aliases, source_refs)
    oriented_branches, alignment_warnings = _orient_voltage_branches(voltage_branches)
    for warning in alignment_warnings:
        graph.warnings.append(warning)
        graph.record_event("voltage_alignment_warning", warning, reason="voltage_alignment")
    for oriented in oriented_branches:
        _stamp_oriented_voltage_branch(
            graph,
            oriented,
            aliases,
            source_refs,
            current_control_refs,
        )
    graph.lump_meta_edges()
    graph.eliminate_self_loops()
    if strict and (graph.deferred_elements or graph.voltage_constraints or alias_conflicts or alignment_warnings):
        details = []
        if graph.deferred_elements:
            details.append("deferred elements: " + ", ".join(item.refdes for item in graph.deferred_elements))
        if graph.voltage_constraints:
            details.append("voltage constraints: " + ", ".join(item.source_ref for item in graph.voltage_constraints))
        if alias_conflicts:
            details.append("voltage source conflicts")
        if alignment_warnings:
            details.append("voltage alignment warnings")
        raise ValueError("; ".join(details))
    if not audit:
        graph.audit_events.clear()
        graph.dropped_contributions.clear()
    return graph


def netlist_to_signal_flow_graph(
    file_path: str,
    strict: bool = False,
    audit: bool = True,
) -> tuple[LinearNetwork, SignalFlowGraph]:
    """完成 ``.cir -> LinearNetwork -> SignalFlowGraph`` 的便捷流程。"""
    network = flatten_netlist(file_path)
    return network, build_signal_flow_graph(network, strict=strict, audit=audit)


def _stamp_element(
    graph: SignalFlowGraph,
    element: LinearElement,
    aliases: dict[str, str],
    source_refs: set[str],
) -> None:
    """按元件模型选择对应的 SFG stamp。"""
    model = element.model
    if model in {"R", "r", "C", "L"}:
        _stamp_bilateral_branch(graph, element, aliases, _branch_admittance(element))
    elif model in {"g", "G"}:
        _stamp_vccs(graph, element, aliases)
    elif model == "F":
        _stamp_cccs(graph, element, aliases)
    elif model == "I":
        _store_current_source(graph, element, aliases)
    else:
        graph.deferred_elements.append(
            DeferredElement(element.refdes, model, "No first-stage signal-flow stamp implemented yet.")
        )
        graph.record_event("defer_element", f"deferred unsupported element model {model}", element_ref=element.refdes)


def _stamp_bilateral_branch(
    graph: SignalFlowGraph,
    element: LinearElement,
    aliases: dict[str, str],
    admittance: sp.Expr,
) -> None:
    """为双端导纳/阻抗元件生成 impedance 和 transfer 边。"""
    p_node, n_node = (_alias_node(node, aliases) for node in element.nodes)
    if p_node == n_node or _is_zero(admittance):
        graph.record_event("skip_element", f"skipped shorted or zero branch {element.refdes}", element_ref=element.refdes)
        return
    impedance = sp.simplify(1 / admittance)
    if p_node != "0":
        graph.add_contribution(f"{p_node}.i", f"{p_node}.v", impedance, element, "self", "impedance")
    if n_node != "0":
        graph.add_contribution(f"{n_node}.i", f"{n_node}.v", impedance, element, "self", "impedance")
    if p_node != "0" and n_node != "0":
        graph.add_contribution(f"{p_node}.v", f"{n_node}.i", admittance, element, "transfer", "admittance")
        graph.add_contribution(f"{n_node}.v", f"{p_node}.i", admittance, element, "transfer", "admittance")


def _stamp_vccs(graph: SignalFlowGraph, element: LinearElement, aliases: dict[str, str]) -> None:
    """为 VCCS 生成控制电压顶点到输出电流顶点的边。"""
    out_p, out_n, ctrl_p, ctrl_n = (_alias_node(node, aliases) for node in element.nodes)
    gm = sp.sympify(element.params["value"])
    if out_p == out_n or ctrl_p == ctrl_n or _is_zero(gm):
        graph.record_event("skip_element", f"skipped degenerate VCCS {element.refdes}", element_ref=element.refdes)
        return
    edges = [
        (ctrl_p, out_p, -gm),
        (ctrl_n, out_p, gm),
        (ctrl_p, out_n, gm),
        (ctrl_n, out_n, -gm),
    ]
    for ctrl_node, out_node, value in edges:
        if ctrl_node != "0" and out_node != "0":
            graph.add_contribution(f"{ctrl_node}.v", f"{out_node}.i", value, element, "controlled", "admittance")


def _stamp_vcvs(graph: SignalFlowGraph, element: LinearElement, aliases: dict[str, str]) -> None:
    """为 VCVS 生成电压约束型边。"""
    out_p, out_n, ctrl_p, ctrl_n = (_alias_node(node, aliases) for node in element.nodes)
    gain = sp.sympify(element.params["value"])
    if out_p == "0":
        graph.voltage_constraints.append(VoltageConstraint(element.refdes, out_p, out_n, gain))
        graph.warnings.append(f"VCVS '{element.refdes}' has its positive output terminal at ground; constraint kept explicit.")
        graph.record_event("voltage_constraint", f"kept VCVS {element.refdes} explicit", element_ref=element.refdes)
        return
    if out_n != "0":
        graph.add_contribution(f"{out_n}.v", f"{out_p}.v", sp.Integer(1), element, "controlled", "voltage")
    if ctrl_p != "0":
        graph.add_contribution(f"{ctrl_p}.v", f"{out_p}.v", gain, element, "controlled", "voltage")
    if ctrl_n != "0":
        graph.add_contribution(f"{ctrl_n}.v", f"{out_p}.v", -gain, element, "controlled", "voltage")


def _stamp_cccs(graph: SignalFlowGraph, element: LinearElement, aliases: dict[str, str]) -> None:
    """为 CCCS 生成辅助电流变量到输出电流顶点的边。"""
    out_p, out_n = (_alias_node(node, aliases) for node in element.nodes)
    gain = sp.sympify(element.params["value"])
    if not element.refs:
        _defer_missing_control_ref(graph, element, "CCCS")
        return
    control = _control_current_vertex(graph, element, "CCCS")
    if control is None:
        return
    graph.record_event("current_reference", f"CCCS {element.refdes} controlled by {control}", element_ref=element.refdes)
    if out_p != "0":
        graph.add_contribution(control, f"{out_p}.i", -gain, element, "controlled", "current")
    if out_n != "0":
        graph.add_contribution(control, f"{out_n}.i", gain, element, "controlled", "current")


def _stamp_ccvs(graph: SignalFlowGraph, element: LinearElement, aliases: dict[str, str]) -> None:
    """为 CCVS 生成辅助电流变量到输出电压顶点的边。"""
    out_p, out_n = (_alias_node(node, aliases) for node in element.nodes)
    transresistance = sp.sympify(element.params["value"])
    if not element.refs:
        _defer_missing_control_ref(graph, element, "CCVS")
        return
    if out_p == "0":
        graph.voltage_constraints.append(VoltageConstraint(element.refdes, out_p, out_n, transresistance))
        graph.warnings.append(f"CCVS '{element.refdes}' has its positive output terminal at ground; constraint kept explicit.")
        graph.record_event("voltage_constraint", f"kept CCVS {element.refdes} explicit", element_ref=element.refdes)
        return
    control = graph.add_aux_current_vertex(element.refs[0])
    graph.record_event("current_reference", f"CCVS {element.refdes} controlled by {control}", element_ref=element.refdes)
    if out_n != "0":
        graph.add_contribution(f"{out_n}.v", f"{out_p}.v", sp.Integer(1), element, "controlled", "voltage")
    graph.add_contribution(control, f"{out_p}.v", transresistance, element, "controlled", "voltage")


def _store_current_source(graph: SignalFlowGraph, element: LinearElement, aliases: dict[str, str]) -> None:
    """把独立电流源转换为源顶点到两个端点 current vertex 的边。"""
    p_node, n_node = (_alias_node(node, aliases) for node in element.nodes)
    if p_node == n_node:
        graph.record_event("skip_element", f"skipped shorted current source {element.refdes}", element_ref=element.refdes)
        return
    vertex_name = graph.add_source_vertex(element.refdes)
    value = sp.sympify(element.params["value"])
    graph.source_drives.append(SourceDrive(element.refdes, vertex_name, p_node, n_node, value, "current"))
    if p_node != "0":
        graph.add_contribution(vertex_name, f"{p_node}.i", sp.Integer(-1), element, "drive", "current")
    if n_node != "0":
        graph.add_contribution(vertex_name, f"{n_node}.i", sp.Integer(1), element, "drive", "current")


def _collect_voltage_branches(
    graph: SignalFlowGraph,
    network: LinearNetwork,
    aliases: dict[str, str],
    source_refs: set[str],
    current_control_refs: set[str],
) -> list[VoltageBranch]:
    """收集论文范围内可安全处理的 V/E/H 电压型支路。"""
    branches: list[VoltageBranch] = []
    for element in network.elements.values():
        if element.model not in {"V", "E", "H"}:
            continue
        positive_node, negative_node = (_alias_node(node, aliases) for node in element.nodes[:2])
        if positive_node == negative_node:
            continue
        if element.model == "V":
            if element.refdes not in source_refs and element.refdes not in current_control_refs:
                continue
        branches.append(VoltageBranch(element, positive_node, negative_node))
    return branches


def _orient_voltage_branches(
    branches: list[VoltageBranch],
) -> tuple[list[OrientedVoltageBranch], list[str]]:
    """对每个电压型支路连通分量建立统一传播方向。"""
    by_node: dict[str, list[int]] = {}
    for index, branch in enumerate(branches):
        by_node.setdefault(branch.positive_node, []).append(index)
        by_node.setdefault(branch.negative_node, []).append(index)

    oriented: list[OrientedVoltageBranch] = []
    warnings: list[str] = []
    visited_nodes: set[str] = set()
    visited_edges: set[int] = set()

    for start in sorted(by_node):
        if start in visited_nodes:
            continue
        component_nodes: set[str] = set()
        component_edges: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component_nodes:
                continue
            component_nodes.add(node)
            for edge_index in by_node.get(node, []):
                component_edges.add(edge_index)
                branch = branches[edge_index]
                other = branch.negative_node if branch.positive_node == node else branch.positive_node
                if other not in component_nodes:
                    stack.append(other)
        visited_nodes.update(component_nodes)

        root = "0" if "0" in component_nodes else min(component_nodes)
        requires_alignment = len(component_edges) > 1
        if not requires_alignment:
            branch = branches[next(iter(component_edges))]
            if "0" in component_nodes:
                parent = "0"
                child = branch.negative_node if branch.positive_node == "0" else branch.positive_node
            else:
                parent = branch.negative_node
                child = branch.positive_node
            sign = 1 if (parent == branch.negative_node and child == branch.positive_node) else -1
            oriented.append(OrientedVoltageBranch(branch, parent, child, sign, root, False))
            visited_edges.update(component_edges)
            continue

        queue = [root]
        tree_nodes = {root}
        while queue:
            node = queue.pop(0)
            for edge_index in sorted(by_node.get(node, [])):
                if edge_index in visited_edges:
                    continue
                branch = branches[edge_index]
                other = branch.negative_node if branch.positive_node == node else branch.positive_node
                if other in tree_nodes:
                    warnings.append(
                        f"Voltage-defined branch '{branch.element.refdes}' closes a loop in component rooted at '{root}'."
                    )
                    visited_edges.add(edge_index)
                    continue
                sign = 1 if (node == branch.negative_node and other == branch.positive_node) else -1
                oriented.append(OrientedVoltageBranch(branch, node, other, sign, root, True))
                visited_edges.add(edge_index)
                tree_nodes.add(other)
                queue.append(other)
    return oriented, warnings


def _stamp_oriented_voltage_branch(
    graph: SignalFlowGraph,
    oriented: OrientedVoltageBranch,
    aliases: dict[str, str],
    source_refs: set[str],
    current_control_refs: set[str],
) -> None:
    """按一致方向 stamp 一条 V/E/H 支路，并补入其支路电流对 KCL 的贡献。"""
    branch = oriented.branch
    element = branch.element
    if oriented.requires_alignment:
        graph.record_event(
            "voltage_alignment",
            f"aligned {element.refdes}: {oriented.parent_node} -> {oriented.child_node}",
            element_ref=element.refdes,
            source=oriented.parent_node,
            target=oriented.child_node,
            value=oriented.sign,
        )

    floating_independent_voltage = (
        element.model == "V"
        and branch.positive_node != "0"
        and branch.negative_node != "0"
    )
    if (
        element.model != "V"
        or floating_independent_voltage
        or element.refdes in current_control_refs
    ):
        current = graph.add_aux_current_vertex(element.refdes, branch.positive_node, branch.negative_node)
        if branch.positive_node != "0":
            graph.add_contribution(current, f"{branch.positive_node}.i", sp.Integer(-1), element, "branch_current", "current")
        if branch.negative_node != "0":
            graph.add_contribution(current, f"{branch.negative_node}.i", sp.Integer(1), element, "branch_current", "current")
        if floating_independent_voltage:
            graph.record_event(
                "floating_voltage_source_current",
                f"added MNA-style branch current for floating source {element.refdes}",
                element_ref=element.refdes,
                target=current,
                reason="floating_independent_voltage_source",
            )

    if oriented.parent_node != "0":
        graph.add_contribution(
            f"{oriented.parent_node}.v",
            f"{oriented.child_node}.v",
            sp.Integer(1),
            element,
            "constraint",
            "voltage",
        )

    if element.model == "V":
        value = sp.sympify(element.params["value"])
        vertex_name = graph.add_source_vertex(element.refdes)
        graph.source_drives.append(
            SourceDrive(element.refdes, vertex_name, branch.positive_node, branch.negative_node, value, "voltage")
        )
        graph.add_contribution(
            vertex_name,
            f"{oriented.child_node}.v",
            sp.Integer(oriented.sign),
            element,
            "drive",
            "voltage",
        )
    elif element.model == "E":
        gain = sp.sympify(element.params["value"])
        ctrl_p, ctrl_n = (_alias_node(node, aliases) for node in element.nodes[2:4])
        if ctrl_p != "0":
            graph.add_contribution(
                f"{ctrl_p}.v",
                f"{oriented.child_node}.v",
                sp.Integer(oriented.sign) * gain,
                element,
                "controlled",
                "voltage",
            )
        if ctrl_n != "0":
            graph.add_contribution(
                f"{ctrl_n}.v",
                f"{oriented.child_node}.v",
                -sp.Integer(oriented.sign) * gain,
                element,
                "controlled",
                "voltage",
            )
    elif element.model == "H":
        control = _control_current_vertex(graph, element, "CCVS")
        if control is None:
            return
        transresistance = sp.sympify(element.params["value"])
        graph.add_contribution(
            control,
            f"{oriented.child_node}.v",
            sp.Integer(oriented.sign) * transresistance,
            element,
            "controlled",
            "voltage",
        )


def _source_refdes(network: LinearNetwork) -> set[str]:
    """返回 `.source` 指定的独立源名称集合。"""
    if not network.source:
        return set()
    return {item for item in network.source if item is not None}


def _current_control_references(network: LinearNetwork) -> set[str]:
    """返回 F/H 元件引用的控制支路名称集合。"""
    return {
        refdes
        for element in network.elements.values()
        if element.model in {"F", "H"}
        for refdes in element.refs
    }


def _voltage_source_aliases(
    network: LinearNetwork,
    source_refs: set[str],
    current_control_refs: set[str],
) -> tuple[dict[str, str], list[GraphAuditEvent], list[str]]:
    """对非输入的零值理想电压源执行小信号节点合并。"""
    parent: dict[str, str] = {}
    events: list[GraphAuditEvent] = []
    conflicts: list[str] = []

    def find(node: str) -> str:
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(left: str, right: str, refdes: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root == "0":
            parent[right_root] = "0"
            alias = "0"
            original = right_root
        elif right_root == "0":
            parent[left_root] = "0"
            alias = "0"
            original = left_root
        else:
            parent[right_root] = left_root
            alias = left_root
            original = right_root
        events.append(
            GraphAuditEvent(
                action="voltage_alias",
                detail=f"merged {original} into {alias} due to zero voltage source {refdes}",
                element_ref=refdes,
                source=original,
                target=alias,
                value=0,
                reason="zero_voltage_source",
            )
        )

    for node in network.nodes:
        find(node)
    for element in network.elements.values():
        for node in element.nodes:
            find(node)
    for element in network.elements.values():
        if (
            element.model != "V"
            or element.refdes in source_refs
            or element.refdes in current_control_refs
        ):
            continue
        value = sp.sympify(element.params.get("value", 0))
        if _is_zero(value):
            union(element.nodes[0], element.nodes[1], element.refdes)
        else:
            conflicts.append(f"Non-source voltage source '{element.refdes}' has nonzero small-signal value '{value}'.")
    return {node: find(node) for node in parent}, events, conflicts


def _voltage_driven_nodes(network: LinearNetwork, aliases: dict[str, str], source_refs: set[str]) -> set[str]:
    """找出由一端接地独立输入电压源直接驱动的节点。"""
    nodes: set[str] = set()
    for element in network.elements.values():
        if element.model != "V" or element.refdes not in source_refs:
            continue
        p_node, n_node = (_alias_node(node, aliases) for node in element.nodes)
        if p_node != "0" and n_node == "0":
            nodes.add(p_node)
        elif p_node == "0" and n_node != "0":
            nodes.add(n_node)
    return nodes


def _alias_node(node: str, aliases: dict[str, str]) -> str:
    """返回节点经过零值电压源合并后的代表节点。"""
    return aliases.get(node, node)


def _branch_admittance(element: LinearElement) -> sp.Expr:
    """返回 R/r/C/L 元件对应的小信号导纳。"""
    value = sp.sympify(element.params["value"])
    if element.model in {"R", "r"}:
        return sp.simplify(1 / value)
    if element.model == "C":
        return sp.simplify(value * ini.laplace)
    if element.model == "L":
        return sp.simplify(1 / (value * ini.laplace))
    raise ValueError(f"Unsupported bilateral branch model: {element.model}")


def _combine_parallel(values: list[sp.Expr], domain: str) -> sp.Expr:
    """按边的物理域合并平行贡献。"""
    if not values:
        return sp.Integer(0)
    if domain in {"admittance", "voltage", "current"}:
        return sp.simplify(sum(values))
    if domain == "impedance":
        reciprocal_sum = sum(sp.simplify(1 / value) for value in values)
        return sp.simplify(1 / reciprocal_sum)
    raise ValueError(f"Unknown meta-edge domain: {domain}")


def _is_zero(value: Any) -> bool:
    """判断一个符号表达式是否为零。"""
    expr = sp.simplify(sp.sympify(value))
    return expr == 0 or expr.is_zero is True


def _order_partitions(expr: sp.Expr, domain: str) -> dict[int | None, sp.Expr]:
    """按 Laplace 变量幂次划分 meta-edge 表达式。"""
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


def _candidate_open_loop_root(edge: MetaEdge) -> str | None:
    """从简单一阶 meta-edge 中提取候选 open-loop root 文本。"""
    partitions = edge.order_partitions
    if 0 in partitions and 1 in partitions and edge.domain in {"admittance", "voltage", "current"}:
        root = sp.simplify(-partitions[0] / (partitions[1] / ini.laplace))
        return sp.sstr(root)
    return None


def _defer_missing_control_ref(graph: SignalFlowGraph, element: LinearElement, source_type: str) -> None:
    """记录缺失控制引用的电流控制源。"""
    graph.deferred_elements.append(
        DeferredElement(element.refdes, element.model, f"{source_type} has no controlling reference.")
    )
    graph.record_event(
        "defer_element",
        f"deferred {source_type} with missing controlling reference",
        element_ref=element.refdes,
        reason="missing current-control reference",
    )


def _control_current_vertex(graph: SignalFlowGraph, element: LinearElement, source_type: str) -> str | None:
    """返回受控源引用支路的电流顶点，并统一采用正端到负端的参考方向。"""
    if not element.refs:
        _defer_missing_control_ref(graph, element, source_type)
        return None
    refdes = element.refs[0]
    direction = graph.current_reference_directions.get(refdes)
    if direction is None:
        graph.deferred_elements.append(
            DeferredElement(element.refdes, element.model, f"{source_type} references unknown current branch '{refdes}'.")
        )
        graph.record_event(
            "defer_element",
            f"deferred {source_type} with unknown controlling branch {refdes}",
            element_ref=element.refdes,
            reason="unknown current-control reference",
        )
        return None
    positive_node, negative_node = direction
    vertex = graph.add_aux_current_vertex(refdes, positive_node, negative_node)
    graph.record_event(
        "current_reference_direction",
        f"{source_type} {element.refdes} uses {vertex} defined from {positive_node} to {negative_node}",
        element_ref=element.refdes,
        target=vertex,
    )
    return vertex


def _laplace_order(expr: sp.Expr) -> int | None:
    """返回表达式相对 Laplace 变量 ``s`` 的单项幂次。"""
    expr = sp.cancel(sp.sympify(expr))
    if expr == 0:
        return None
    laplace = ini.laplace
    if laplace not in expr.free_symbols:
        return 0
    num, den = sp.fraction(expr)
    num_power = _monomial_power(num, laplace)
    den_power = _monomial_power(den, laplace)
    if num_power is None or den_power is None:
        return None
    return num_power - den_power


def _monomial_power(expr: sp.Expr, symbol: sp.Symbol) -> int | None:
    """若表达式是关于指定符号的单项式，返回其幂次。"""
    expr = sp.expand(sp.sympify(expr))
    if expr == 0:
        return None
    poly = sp.Poly(expr, symbol)
    if len(poly.monoms()) != 1:
        return None
    return int(poly.monoms()[0][0])
