"""Offline result tabs for numeric and SFG analyses."""

from __future__ import annotations

import html
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QTabWidget, QVBoxLayout, QWidget

_STYLE = """
body { font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; font-size: 13px; line-height: 1.48; margin: 14px 18px; color: #202020; background: #fff; }
h1 { color: #174f74; font-size: 19px; font-weight: 600; margin: 0 0 12px; }
h2 { color: #174f74; font-size: 15px; font-weight: 600; border-bottom: 1px solid #b9c7d0; padding-bottom: 4px; margin-top: 20px; }
h3 { color: #263f50; font-size: 13px; margin: 14px 0 6px; }
p { margin: 7px 0; }
table { border-collapse: collapse; width: 100%; margin: 7px 0 14px; }
th, td { border: 1px solid #c9d2d8; padding: 5px 7px; text-align: left; vertical-align: top; }
th { background: #edf2f5; color: #173f59; font-weight: 600; }
code, pre { font-family: Consolas, monospace; }
pre { background: #f5f6f7; border: 1px solid #d8dcdf; padding: 8px; overflow-x: auto; }
.formula { overflow-x: auto; margin: 7px 0 12px; font-size: 1em; }
.warning { color: #8a5a00; }
.error { color: #a12622; font-weight: 600; }
.muted { color: #66727a; }
.note { background: #f2f7fa; border-left: 3px solid #397aa3; padding: 7px 10px; margin: 8px 0 12px; }
.subrange { border: 1px solid #bfcbd2; margin: 16px 0; padding: 0 14px 12px; background: #fff; }
.subrange > h2 { margin: 0 -14px 12px; padding: 9px 14px; border-bottom: 1px solid #bfcbd2; background: #edf3f6; }
.two-column { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
.panel { border: 1px solid #d5dde2; padding: 0 9px 7px; background: #fbfcfc; min-width: 0; }
.result-primary { border-left: 4px solid #397aa3; background: #f7fafb; padding: 3px 12px 9px; margin: 8px 0 12px; }
.result-primary h3 { color: #174f74; }
.root-result { display: grid; grid-template-columns: 100px minmax(240px, 1.4fr) minmax(230px, 1fr); gap: 10px; align-items: center; border-top: 1px solid #d8e0e4; padding: 9px 0; }
.root-result:first-child { border-top: 0; }
.root-label { font-weight: 600; color: #174f74; }
.root-meta { color: #44535c; line-height: 1.6; }
.root-result .formula { font-size: 1.08em; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 10px; }
.chip { border: 1px solid #afc2cd; background: #f3f7f9; padding: 2px 7px; border-radius: 2px; }
.validation { border-top: 1px solid #d5dde2; margin-top: 10px; padding-top: 7px; color: #53616a; }
.matrix-block { overflow-x: auto; border: 1px solid #d5dde2; background: #fbfcfc; padding: 8px 12px; margin: 8px 0 14px; }
.status-resolved { color: #256b36; font-weight: 600; }
.status-unresolved { color: #a12622; font-weight: 600; }
.frequency-map { width: 100%; min-width: 860px; height: auto; background: #fff; }
.frequency-map-frame { overflow-x: auto; border: 1px solid #d5dde2; background: #fff; padding: 5px; }
.root-card { border-top: 1px solid #d8e0e4; padding-top: 6px; margin-top: 8px; }
.root-card:first-child { border-top: 0; margin-top: 0; }
.root-kind { font-weight: 600; color: #174f74; }
details { margin: 6px 0; }
summary { cursor: pointer; color: #174f74; }
.artifact { margin: 12px 0; }
img { max-width: 100%; height: auto; border: 1px solid #d8dcdf; background: white; }
@media (max-width: 900px) { .two-column { grid-template-columns: 1fr; } }
"""


def _document(title: str, body: str) -> str:
    """Wrap trusted, locally generated result markup in a consistent page."""

    resources = Path(__file__).parent / "resources" / "katex"
    css_url = (resources / "katex.min.css").resolve().as_uri()
    js_url = (resources / "katex.min.js").resolve().as_uri()
    renderer = """<script>
    document.querySelectorAll('[data-tex]').forEach(function(node) {
      if (typeof katex !== 'undefined') {
        katex.render(node.dataset.tex, node, {displayMode:true, throwOnError:false, trust:false});
      }
    });</script>"""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<link rel='stylesheet' href='{css_url}'><script src='{js_url}'></script>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}{renderer}</body></html>"
    )


def _formula(value: Any, latex: str | None = None) -> str:
    """Display worker-generated TeX, with an escaped plain-text fallback."""

    if value is None:
        return "<span class='muted'>not available</span>"
    text = html.escape(str(value))
    if latex is not None:
        return f"<div class='formula' data-tex='{html.escape(latex, quote=True)}'>{text}</div>"
    return f"<pre>{text}</pre>"


def _field_formula(record: dict, field: str) -> str:
    """Read a formula from the structured worker contract without parsing SymPy."""
    return _formula(record.get(field), record.get("_latex", {}).get(field))


def _labeled_formula(label_tex: str, record: dict, field: str) -> str:
    """Render a worker expression with a short mathematical label."""

    value = record.get(field)
    latex = record.get("_latex", {}).get(field)
    return _formula(value, f"{label_tex}={latex}" if latex else None)


def _display(value: Any) -> str:
    """Use scientific notation for measured values, preserving symbolic strings."""
    if value is None or value == "":
        return "-"
    if isinstance(value, dict) and set(value) >= {"real", "imag"}:
        real = float(value["real"])
        imag = float(value["imag"])
        if abs(imag) <= 1e-14 * max(1.0, abs(real)):
            return f"{real:.6e}"
        return f"{real:.6e} {imag:+.6e}j"
    if isinstance(value, float):
        return f"{value:.6e}"
    return str(value)


def _frequency_label(value: Any) -> str:
    """Format a frequency with a readable SI prefix while retaining precision."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    for scale, suffix in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(number) >= scale:
            return f"{number / scale:.6g} {suffix}"
    return f"{number:.6g} Hz"


def _rows(headers: list[str], rows: list[list[Any]]) -> str:
    """Build a compact HTML table from already structured values."""

    header = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_display(value))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _matrix_table(value: Any) -> str:
    """Render a serialized symbolic matrix without evaluating it."""

    if not isinstance(value, list) or not value:
        return "<p class='muted'>not available</p>"
    if not isinstance(value[0], list):
        value = [[item] for item in value]
    width = max(len(row) for row in value)
    return _rows([str(index + 1) for index in range(width)], value)


def _summary_html(result: dict[str, Any]) -> str:
    circuit = result.get("circuit", {})
    flattened = result.get("flattened_circuit", {})
    diagnostics = result.get("diagnostics", [])
    body = "<h2>输入与软件</h2>"
    body += _rows(
        ["项目", "值"],
        [
            ["SLiCAP", result.get("software", {}).get("slicap", "-")],
            ["标题", circuit.get("title", "-")],
            ["Source", flattened.get("source", "-")],
            ["Detector", flattened.get("detector", "-")],
            ["节点数", len(flattened.get("nodes", []))],
            ["展平元件数", len(flattened.get("elements", {}))],
        ],
    )
    body += "<h2>参数来源</h2>"
    body += _rows(
        ["参数", "表达式", "数值", "来源"],
        [
            [item.get("name", ""), item.get("expression", ""), item.get("numeric_value", ""), item.get("source", "")]
            for item in circuit.get("parameters", [])
        ],
    )
    body += "<h2>诊断</h2>"
    if diagnostics:
        body += "<ul>" + "".join(
            f"<li class='{html.escape(str(item.get('level', '')))}'>"
            f"{html.escape(str(item.get('code', 'diagnostic')))}: "
            f"{html.escape(str(item.get('message', '')))}</li>"
            for item in diagnostics
        ) + "</ul>"
    else:
        body += "<p>未发现阻断性诊断。</p>"
    return _document("分析摘要", body)


def _numeric_html(result: dict[str, Any]) -> str:
    analyses = result.get("analyses", {})
    laplace = analyses.get("laplace", {})
    pz = analyses.get("pz", {})
    transfer_record = laplace or pz
    presentation = laplace.get("presentation") or pz.get("presentation", {})
    source = result.get("flattened_circuit", {}).get("source", ["source"])
    detector = result.get("flattened_circuit", {}).get("detector", ["detector"])
    body = "<div class='note'><b>小信号电压/电流增益：</b>"
    body += "SLiCAP 根据 <code>.source</code> 与 <code>.detector</code> 计算 "
    body += f"<code>H(s) = {html.escape(str(detector[0] if isinstance(detector, list) else detector))} / "
    body += f"{html.escape(str(source[0] if isinstance(source, list) else source))}</code>。"
    body += "这里的 H(s) 就是完整交流小信号传递函数。</div>"
    body += "<h2>可读的传递函数</h2>"
    if presentation.get("normalized"):
        body += _field_formula(presentation, "normalized")
        body += "<p class='muted'>该式由同一组数值极点、零点和 H(0) 重写而成；原始精确结果没有被替换。</p>"
    else:
        body += _field_formula(presentation or transfer_record, "compact" if presentation else "laplace")
    body += "<details><summary>查看 SLiCAP 原始精确表达式</summary>"
    body += _field_formula(transfer_record, "laplace") + "</details>"
    body += "<h2>低频小信号增益 H(0)</h2>"
    body += "<p>H(0) 是上述小信号传递函数在频率趋近 0 时的值，不是大信号直流偏置或工作点。</p>"
    body += _field_formula(presentation, "dc_gain") if presentation.get("dc_gain") is not None else _field_formula(
        pz if "DCvalue" in pz else laplace, "DCvalue"
    )
    if presentation:
        body += _rows(["|H(0)|", "增益 (dB)", "相位 (deg)"], [[
            presentation.get("dc_gain_magnitude"), presentation.get("dc_gain_db"),
            presentation.get("dc_gain_phase_deg"),
        ]])

    def root_rows(kind: str) -> list[list[Any]]:
        records = pz.get(f"{kind}_records")
        if records is None:
            records = [{"root": value} for value in pz.get(f"{kind}s", [])]
        return [
            [index, _frequency_label(item.get("frequency_hz")), item.get("angular_frequency_rad_s"),
             item.get("root"), item.get("half_plane", "-")]
            for index, item in enumerate(records, start=1)
        ]

    body += "<h2>极点频率</h2>" + _rows(
        ["序号", "频率", "|s| (rad/s)", "s 平面根 (rad/s)", "位置"], root_rows("pole")
    )
    body += "<h2>零点频率</h2>" + _rows(
        ["序号", "频率", "|s| (rad/s)", "s 平面根 (rad/s)", "位置"], root_rows("zero")
    )
    bode = analyses.get("bode", {})
    artifact = bode.get("artifact")
    if artifact and Path(artifact).is_file():
        body += "<h2>波特图</h2>"
        body += f"<div class='artifact'><img src='{Path(artifact).resolve().as_uri()}'></div>"
    return _document("数值分析", body)


def _finite_positive(value: Any) -> float | None:
    """Return a finite positive float, or None for zero/infinite display bounds."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and math.isfinite(number) else None


def _frequency_bound_label(value: Any) -> str:
    """Format finite frequency bounds while preserving zero and infinity."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number == 0:
        return "0 Hz"
    if math.isinf(number):
        return "∞"
    return _frequency_label(number)


def _frequency_partition_map(
    items: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
) -> str:
    """Draw paper-style root clusters and subranges on a decade frequency axis."""

    valid = sorted(
        (item for item in items if item.get("cluster_index") is not None),
        key=lambda item: int(item.get("cluster_index", 0)),
    )
    if not valid:
        return "<p class='muted'>没有可显示的频率子区间。</p>"

    clusters = clusters or []
    root_frequencies = [
        frequency
        for cluster in clusters
        for root in cluster.get("roots", [])
        if (frequency := _finite_positive(root.get("frequency_hz"))) is not None
    ]
    if not root_frequencies:
        root_frequencies = [
            frequency
            for item in valid
            for root in item.get("target_roots", [])
            if (frequency := _finite_positive(root.get("reference_frequency_hz"))) is not None
        ]
    finite_bounds = [
        frequency
        for item in valid
        for key in ("lower_frequency_hz", "upper_frequency_hz")
        if (frequency := _finite_positive(item.get(key))) is not None
    ]
    if not root_frequencies and not finite_bounds:
        return "<p class='muted'>根和频段边界均不可用于对数坐标。</p>"

    first_lower = _finite_positive(valid[0].get("lower_frequency_hz"))
    last_upper = _finite_positive(valid[-1].get("upper_frequency_hz"))
    reference_min = min(root_frequencies or finite_bounds)
    reference_max = max(root_frequencies or finite_bounds)
    axis_lower = first_lower or 10.0 ** math.floor(math.log10(reference_min / 10.0))
    axis_upper = last_upper or 10.0 ** math.ceil(math.log10(reference_max * 1.01))
    if axis_upper <= axis_lower:
        axis_upper = axis_lower * 10.0
    log_lower, log_upper = math.log10(axis_lower), math.log10(axis_upper)
    span = log_upper - log_lower

    def x_at(value: float) -> float:
        clipped = min(max(value, axis_lower), axis_upper)
        return 82.0 + 956.0 * (math.log10(clipped) - log_lower) / span

    parts = [
        "<div class='frequency-map-frame'><svg class='frequency-map' viewBox='0 0 1120 270' role='img' "
        "aria-label='Logarithmic frequency partition map'>",
        "<text x='82' y='18' font-size='12' fill='#3f4f58'>闭环根与 root clusters</text>",
        "<line x1='82' y1='132' x2='1038' y2='132' stroke='#263f50' stroke-width='2'/>",
    ]

    first_decade = math.floor(log_lower)
    last_decade = math.ceil(log_upper)
    for exponent in range(first_decade, last_decade + 1):
        major = 10.0**exponent
        if axis_lower <= major <= axis_upper:
            x = x_at(major)
            parts.append(f"<line x1='{x:.2f}' y1='124' x2='{x:.2f}' y2='141' stroke='#263f50'/>")
            parts.append(
                f"<text x='{x:.2f}' y='158' text-anchor='middle' font-size='11' fill='#263f50'>10^{exponent}</text>"
            )
        for multiplier in range(2, 10):
            minor = multiplier * major
            if axis_lower <= minor <= axis_upper:
                x = x_at(minor)
                parts.append(f"<line x1='{x:.2f}' y1='128' x2='{x:.2f}' y2='136' stroke='#82939d'/>")
    parts.append("<text x='1052' y='136' font-size='11' fill='#263f50'>f (Hz)</text>")

    cluster_lookup = {int(item.get("index", 0)): item for item in clusters}
    for position, item in enumerate(valid):
        cluster_index = int(item.get("cluster_index", position + 1))
        cluster = cluster_lookup.get(cluster_index, {})
        roots = cluster.get("roots", [])
        if not roots:
            roots = [
                {"kind": root.get("kind"), "frequency_hz": root.get("reference_frequency_hz")}
                for root in item.get("target_roots", [])
            ]
        marker_positions = []
        for root_index, root in enumerate(roots):
            frequency = _finite_positive(root.get("frequency_hz"))
            if frequency is None:
                continue
            x = x_at(frequency) + (root_index - (len(roots) - 1) / 2.0) * 10.0
            marker_positions.append(x)
            if root.get("kind") == "zero":
                parts.append(f"<circle cx='{x:.2f}' cy='92' r='6' fill='white' stroke='#1e2f39' stroke-width='2'/>")
            else:
                parts.append(f"<path d='M{x-5:.2f},87 L{x+5:.2f},97 M{x+5:.2f},87 L{x-5:.2f},97' stroke='#1e2f39' stroke-width='2'/>")
            parts.append(
                f"<text x='{x:.2f}' y='115' text-anchor='middle' font-size='9.5' fill='#44535c'>"
                f"{html.escape(_frequency_label(frequency))}</text>"
            )
        if marker_positions:
            left = min(marker_positions) - 15.0
            right = max(marker_positions) + 15.0
            middle = (left + right) / 2.0
            parts.append(
                f"<path d='M{left:.2f},67 V62 H{middle-6:.2f} L{middle:.2f},56 L{middle+6:.2f},62 H{right:.2f} V67' "
                "fill='none' stroke='#35596e' stroke-width='1.5'/>"
            )
            parts.append(
                f"<text x='{middle:.2f}' y='47' text-anchor='middle' font-size='12' fill='#174f74'>Cluster {cluster_index}</text>"
            )

        lower = _finite_positive(item.get("lower_frequency_hz")) or axis_lower
        upper = _finite_positive(item.get("upper_frequency_hz")) or axis_upper
        left, right = x_at(lower), x_at(upper)
        y = 202 + (position % 2) * 24
        parts.append(
            f"<path d='M{left:.2f},{y-10} V{y} H{right:.2f} V{y-10}' fill='none' stroke='#174f74' stroke-width='2'/>"
        )
        parts.append(
            f"<text x='{(left+right)/2:.2f}' y='{y+17}' text-anchor='middle' font-size='12' fill='#174f74'>G{cluster_index}</text>"
        )
    parts.append(
        "<text x='82' y='252' font-size='11' fill='#53616a'>× pole</text>"
        "<circle cx='153' cy='248' r='5' fill='white' stroke='#1e2f39' stroke-width='1.5'/>"
        "<text x='164' y='252' font-size='11' fill='#53616a'>zero</text>"
        "<text x='1038' y='252' text-anchor='end' font-size='11' fill='#53616a'>横轴按十倍频程（decade）递进</text>"
        "</svg></div>"
    )
    return "".join(parts)


def _local_root_block(transfer: dict[str, Any]) -> str:
    """Render the poles and zeros obtained directly from one simplified graph."""

    rows: list[list[Any]] = []
    formulas = ""
    for kind, title in (("poles", "极点"), ("zeros", "零点")):
        for index, root in enumerate(transfer.get(kind, []), start=1):
            rows.append([
                title, index, _frequency_label(root.get("frequency_hz")), root.get("numeric_value"),
                "精确" if root.get("exact") else "近似",
            ])
            formulas += f"<div class='root-card'><span class='root-kind'>{title} {index}</span>"
            formulas += _field_formula(root, "expression") + "</div>"
    if not rows:
        return "<p class='muted'>该子图未提取出独立的局部根。</p>"
    return _rows(["类型", "序号", "数值频率", "数值 s (rad/s)", "性质"], rows) + formulas


def _elements_html(result: dict[str, Any]) -> str:
    """Show the flattened small-signal elements and their parameter expressions."""
    elements = result.get("flattened_circuit", {}).get("elements", {})
    body = _rows(["元件", "模型", "节点（保持引脚顺序）", "参数"], [
        [name, item.get("model"), ", ".join(item.get("nodes", [])),
         "; ".join(f"{key}={value}" for key, value in item.get("params", {}).items())]
        for name, item in elements.items()
    ])
    return _document("展平后的小信号元件", body)


def _noise_html(result: dict[str, Any]) -> str:
    """Display noise spectra only when that analysis was explicitly requested."""
    noise = result.get("analyses", {}).get("noise", {})
    if not noise:
        return _document("噪声分析", "<p>本次任务未执行噪声分析。</p>")
    body = "<h2>输出噪声谱</h2>" + _field_formula(noise, "onoise")
    body += "<h2>输入等效噪声谱</h2>" + _field_formula(noise, "inoise")
    for name in ("onoiseTerms", "inoiseTerms"):
        body += f"<h2>{name}</h2>" + _rows(["来源", "表达式"], list(noise.get(name, {}).items()))
    return _document("噪声分析", body)


def _matrix_html(result: dict[str, Any]) -> str:
    """Render the MNA equation and vectors as mathematical matrices."""

    matrix = result.get("analyses", {}).get("matrix") or result.get("analyses", {}).get("laplace", {})
    body = "<div class='note'>MNA 方程按 <b>M(s)D_v(s)=I_v(s)</b> 排列；向量 Dv 同时给出矩阵各列对应的未知量顺序。</div>"
    body += "<h2>完整 MNA 方程</h2><div class='matrix-block'>"
    matrix_tex = matrix.get("_latex", {}).get("M")
    dv_tex = matrix.get("_latex", {}).get("Dv")
    iv_tex = matrix.get("_latex", {}).get("Iv")
    if matrix_tex and dv_tex and iv_tex:
        body += _formula("M(s) Dv(s) = Iv(s)", f"{matrix_tex}{dv_tex}={iv_tex}")
    else:
        body += "<p class='muted'>矩阵的 TeX 表达不可用。</p>"
    body += "</div><h2>系统矩阵 M(s)</h2><div class='matrix-block'>" + _field_formula(matrix, "M") + "</div>"
    body += "<div class='two-column'><div class='panel'><h3>未知量向量 Dv</h3>" + _field_formula(matrix, "Dv")
    body += "</div><div class='panel'><h3>独立激励向量 Iv</h3>" + _field_formula(matrix, "Iv") + "</div></div>"
    return _document("MNA 矩阵", body)


def _symbolic_html(result: dict[str, Any]) -> str:
    symbolic = result.get("analyses", {}).get("symbolic", {})
    frequency_results = symbolic.get("frequency_results", [])
    body = "<div class='note'><b>阅读顺序：</b>先看根簇如何划分频段，再从上到下查看每个频段的增益、该频段的零极点及主导参数。"
    body += "化简操作清单和完整子图属于审计信息，默认折叠在页面末尾。</div>"
    body += "<h2>根聚类与频率子区间</h2>"
    body += _frequency_partition_map(frequency_results, symbolic.get("root_clusters", []))
    body += "<p class='muted'>根簇由闭环极点（×）和零点（○）按频率接近程度形成；相邻频段边界按论文式 (13)、(14) 取相邻根的几何平均。"
    body += "横轴的 10 倍递进只是对数坐标刻度，不表示每个子区间固定相差 10 倍。</p>"
    body += _rows(["根簇", "下边界", "上边界"], [[
        item.get("cluster_index"), _frequency_bound_label(item.get("lower_frequency_hz")),
        _frequency_bound_label(item.get("upper_frequency_hz")),
    ] for item in frequency_results])
    body += "<h2>各频段的核心结果</h2>"
    for item in frequency_results:
        body += "<section class='subrange'>"
        cluster_index = item["cluster_index"]
        body += f"<h2>频段 {cluster_index}（G{cluster_index}）：{_frequency_bound_label(item['lower_frequency_hz'])} 至 {_frequency_bound_label(item['upper_frequency_hz'])}</h2>"
        transfer = item.get("transfer", {})
        dominant = item.get("dominant_term_transfer")
        paper_style = item.get("paper_style_transfer")
        if not transfer.get("success", True):
            body += "<p class='error'>" + html.escape(str(transfer.get("error"))) + "</p>"
        else:
            primary = dominant or transfer
            body += "<div class='result-primary'><h3>1. 本频段的简化增益表达式</h3>"
            body += _labeled_formula(f"H_{{{cluster_index}}}(s)", primary, "transfer")
            if dominant:
                body += f"<p class='muted'>从 {html.escape(str(dominant.get('full_term_count', '-')))} 个乘积项中保留 "
                body += f"{html.escape(str(dominant.get('retained_term_count', '-')))} 项，并已在整个频段内独立检查误差。</p>"
            evaluation = item.get("evaluation") or {}
            if evaluation:
                body += _rows(["代表频率", "|H|", "增益", "相位"], [[
                    _frequency_label(evaluation.get("frequency_hz")), evaluation.get("magnitude"),
                    f"{_display(evaluation.get('magnitude_db'))} dB", f"{_display(evaluation.get('phase_deg'))} deg",
                ]])
            body += "</div>"

        body += "<h3>2. 本频段的零极点</h3>"
        body += (
            "<p class='muted'>符号根的状态表示是否已找到物理可解释的局部表达式；"
            "根位置偏差仅用于说明近似程度。图操作是否接受由本频段整体传递函数的"
            "幅值与相位误差决定。</p>"
        )
        target_roots = item.get("target_roots", [])
        if not target_roots:
            body += "<p class='muted'>本频段没有闭环目标根；该频段只描述相邻根之间的响应。</p>"
        for root_index, root in enumerate(target_roots, start=1):
            status = str(root.get("status", "unresolved"))
            status_class = "status-resolved" if status == "resolved" else "status-unresolved"
            status_text = "已定位" if status == "resolved" else "未解析"
            kind = "极点" if root.get("kind") == "pole" else "零点"
            body += "<div class='root-result'>"
            body += f"<div class='root-label'>{kind} {root_index}<br><span class='{status_class}'>{status_text}</span></div>"
            body += "<div>" + _labeled_formula(
                f"{'p' if kind == '极点' else 'z'}_{{{cluster_index}}}", root, "expression"
            ) + "</div>"
            body += "<div class='root-meta'>"
            body += f"精确电路数值频率：<b>{html.escape(_frequency_label(root.get('reference_frequency_hz')))}</b><br>"
            body += f"符号式求值频率：{html.escape(_frequency_label(root.get('evaluated_frequency_hz') or root.get('frequency_hz')))}<br>"
            body += f"来源：{html.escape(str(root.get('category', '-')))}；方法：{html.escape(str(root.get('method', '-')))}<br>"
            body += f"根位置偏差（诊断）：{html.escape(_display(root.get('relative_root_error')))}</div>"
            if status != "resolved":
                body += "<div></div><div class='error'>尚未找到可解释的符号根候选；本频段图是否满足要求仍以下方整体频响验收为准。</div><div></div>"
            body += "</div>"

        body += "<h3>3. 主导参数</h3>"
        if dominant and dominant.get("parameter_influences"):
            body += "<div class='chips'>" + "".join(
                f"<span class='chip'>{html.escape(str(value.get('parameter')))}</span>"
                for value in dominant.get("parameter_influences", [])
            ) + "</div>"
        else:
            body += "<p class='muted'>未生成独立的主导参数排序。</p>"

        error = item.get("error", {})
        body += "<div class='validation'><b>4. 整段频响验收：</b>"
        body += _rows(["最大幅值误差", "最大相位误差", "最大相对误差"], [[
            error.get("max_magnitude_error_db"), error.get("max_phase_error_deg"), error.get("max_relative_error"),
        ]])
        body += "</div><details><summary>查看该频段的完整技术细节</summary>"
        body += "<h3>化简 SFG 的完整传递函数</h3>" + _field_formula(transfer, "transfer")
        if paper_style and paper_style.get("success", True):
            body += "<h3>按 root cluster 截断得到的 paper-style 传递函数</h3>"
            body += _field_formula(paper_style, "transfer")
            body += _local_root_block(paper_style)
        body += "<h3>由该局部子图直接提取的全部根</h3>" + _local_root_block(transfer)
        if dominant:
            body += _rows(["参数", "最大归一化灵敏度", "峰值频率"], [[
                value.get("parameter"), value.get("max_normalized_sensitivity"),
                _frequency_label(value.get("peak_frequency_hz")),
            ] for value in dominant.get("parameter_influences", [])])
        body += "</details>"
        body += "</section>"
    svg_paths = [
        Path(path) for name, path in result.get("artifacts", {}).items()
        if str(name).lower().endswith(".svg") and str(name).lower().startswith("sfg_")
    ]
    if svg_paths:
        body += "<h2>信号流图与审计文件</h2>"
        for path in svg_paths:
            if path.is_file():
                body += f"<details><summary>{html.escape(path.name)}</summary><img src='{path.resolve().as_uri()}'></details>"
    if not symbolic:
        body += "<p class='muted'>本次任务未执行 SFG 符号化简。</p>"
    return _document("SFG 分频段符号结果", body)


class _LocalPage(QWebEnginePage):
    """Do not navigate from a result document to remote or executable URLs."""

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.isLocalFile() or url.scheme() == "about"


class _HtmlView(QWebEngineView):
    """QWebEngine page restricted to local generated content and files."""

    def __init__(self):
        super().__init__()
        self._temporary = tempfile.TemporaryDirectory(prefix="isaca-result-")
        self.setPage(_LocalPage(self))
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)

    def set_document(self, markup: str, base_path: Path | None = None) -> None:
        # File-backed pages avoid Qt's 2 MB setHtml/data-URL limit for large circuits.
        document = Path(self._temporary.name) / "result.html"
        document.write_text(markup, encoding="utf-8")
        self.setUrl(QUrl.fromLocalFile(str(document)))


class ResultTabs(QTabWidget):
    """Display one completed worker result without parsing SLiCAP HTML."""

    artifact_open_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self._views = [_HtmlView() for _ in range(6)]
        for view, title in zip(self._views, ("摘要", "小信号元件", "数值结果", "MNA", "噪声", "SFG 符号结果")):
            self.addTab(view, title)
        artifacts_page = QWidget()
        layout = QVBoxLayout(artifacts_page)
        self.artifact_list = QListWidget()
        self.artifact_list.itemDoubleClicked.connect(self._open_artifact)
        layout.addWidget(self.artifact_list)
        self.addTab(artifacts_page, "生成文件")

    def show_result(self, result: dict[str, Any]) -> None:
        """Replace every tab from one structured worker result."""

        artifacts = result.get("artifacts", {})
        base = Path(next(iter(artifacts.values()))).parent if artifacts else Path.cwd()
        pages = (
            _summary_html(result),
            _elements_html(result),
            _numeric_html(result),
            _matrix_html(result),
            _noise_html(result),
            _symbolic_html(result),
        )
        for view, page in zip(self._views, pages):
            view.set_document(page, base)
        self.artifact_list.clear()
        for name, raw_path in sorted(artifacts.items()):
            item = QListWidgetItem(f"{name}\n{raw_path}")
            item.setData(256, str(raw_path))
            self.artifact_list.addItem(item)
        self.setCurrentIndex(0)

    def _open_artifact(self, item: QListWidgetItem) -> None:
        path = Path(str(item.data(256)))
        if not path.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.artifact_open_failed.emit(f"无法打开生成文件：{path}")


def result_snapshot(result: dict[str, Any]) -> str:
    """Return deterministic JSON for tests and support diagnostics."""

    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
