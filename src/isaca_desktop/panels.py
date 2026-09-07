"""Native Qt input, configuration and log panels for ISACA Desktop."""

from __future__ import annotations

import re
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileSystemModel,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from isaca_api.models import CircuitDocument
from .parameter_ui import configure_spreadsheet_table


class _LineNumberArea(QWidget):
    def __init__(self, editor: "NetlistCodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_numbers(event)


class _NetlistHighlighter(QSyntaxHighlighter):
    """Small, deterministic SLiCAP netlist highlighter."""

    def __init__(self, document):
        super().__init__(document)
        self.rules: list[tuple[re.Pattern[str], QTextCharFormat]] = []
        self._add(r"^\s*\*.*$|;.*$", "#6a737d")
        self._add(r"(?i)^\s*\.(source|detector|param|model|subckt|ends?|include|lib)\b", "#005cc5", True)
        self._add(r"\{[^}]+\}", "#7a3e9d")
        self._add(r"(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?(?:meg|[kmunpf])?\b", "#986801")
        self._add(r"^\s*[A-Za-z][A-Za-z0-9_.$-]*", "#24292e", True)

    def _add(self, pattern: str, color: str, bold: bool = False) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        self.rules.append((re.compile(pattern), fmt))

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class NetlistCodeEditor(QPlainTextEdit):
    """QPlainTextEdit with a compact line-number gutter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_numbers = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_line_numbers)
        self._update_margin()
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = _NetlistHighlighter(self.document())

    def line_number_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_args) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_line_numbers(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._line_numbers.setGeometry(QRect(contents.left(), contents.top(), self.line_number_width(), contents.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._line_numbers)
        painter.fillRect(event.rect(), QColor("#f3f3f3"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        painter.setPen(QColor("#707070"))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, top, self._line_numbers.width() - 5, self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight, str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1


class NetlistEditor(QWidget):
    """Editable canonical .cir input with explicit source path state."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Path | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self.path_label = QLabel("未加载网表")
        self.editor = NetlistCodeEditor()
        self.editor.textChanged.connect(self.changed)
        layout.addWidget(self.path_label)
        layout.addWidget(self.editor)

    def set_netlist(self, text: str, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path else None
        self.path_label.setText(str(self.path) if self.path else "未保存的网表")
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)

    def text(self) -> str:
        return self.editor.toPlainText()


class ProjectInputDock(QDockWidget):
    """Project tree and the three supported circuit input paths."""

    new_project_requested = Signal()
    open_project_requested = Signal()
    new_schematic_requested = Signal()
    open_schematic_requested = Signal()
    export_schematic_requested = Signal()
    open_netlist_requested = Signal()
    save_netlist_requested = Signal()
    import_image_requested = Signal()

    def __init__(self, parent=None):
        super().__init__("项目与输入", parent)
        self.setObjectName("isaca_project_input")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        for text, signal in (
            ("新建项目...", self.new_project_requested),
            ("打开项目...", self.open_project_requested),
            ("新建 SLiCAP 原理图", self.new_schematic_requested),
            ("打开 SLiCAP 原理图...", self.open_schematic_requested),
            ("导出当前原理图网表", self.export_schematic_requested),
            ("打开网表...", self.open_netlist_requested),
            ("保存网表", self.save_netlist_requested),
            ("从电路图片识别...", self.import_image_requested),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, target=signal: target.emit())
            layout.addWidget(button)
        self.root_label = QLabel("未打开项目")
        self.root_label.setWordWrap(True)
        layout.addWidget(self.root_label)
        self.model = QFileSystemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        for column in range(1, 4):
            self.tree.hideColumn(column)
        layout.addWidget(self.tree, 1)
        self.setWidget(body)

    def set_project_root(self, root: str | Path) -> None:
        path = str(Path(root).resolve())
        self.root_label.setText(path)
        self.model.setRootPath(path)
        self.tree.setRootIndex(self.model.index(path))


class AnalysisSetupDock(QDockWidget):
    """Analysis modes, tolerances and resolved parameter provenance."""

    validate_requested = Signal()
    run_requested = Signal(object)
    cancel_requested = Signal()
    option_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__("分析设置", parent)
        self._overrides: dict[str, str] = {}
        self.setObjectName("isaca_analysis_setup")
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        self.input_mode = QComboBox()
        self.input_mode.addItem("分析当前网表", "netlist")
        self.input_mode.addItem("保存当前原理图并分析", "schematic")
        outer.addWidget(self.input_mode)
        ports = QGroupBox("分析端口")
        ports_form = QFormLayout(ports)
        self.source_value = QLabel("-")
        self.detector_value = QLabel("-")
        ports_form.addRow("Source", self.source_value)
        ports_form.addRow("Detector", self.detector_value)
        outer.addWidget(ports)

        modes = QGroupBox("分析类型")
        modes_layout = QVBoxLayout(modes)
        self.mode_checks: dict[str, QCheckBox] = {}
        for key, title, checked in (
            ("laplace", "小信号增益 H(s) / 拉普拉斯传递函数", True),
            ("pz", "极点与零点", True),
            ("matrix", "MNA 矩阵", False),
            ("noise", "噪声分析", False),
            ("bode", "波特图", True),
            ("symbolic", "SFG 分频段符号化简", True),
        ):
            check = QCheckBox(title)
            check.setChecked(checked)
            self.mode_checks[key] = check
            modes_layout.addWidget(check)
        self.numeric = QCheckBox("执行数值分析")
        self.numeric.setChecked(True)
        modes_layout.addWidget(self.numeric)
        self.use_defaults = QCheckBox("用器件默认值补齐未赋值的符号参数")
        self.use_defaults.setChecked(False)
        self.use_defaults.setToolTip(
            "器件属性留空时，官方导出器会省略该字段，SLiCAP 模型仍可能采用非零默认值。\n"
            "本开关只用于补齐已经写入器件表达式、但尚未由 .param 赋值的符号。"
        )
        modes_layout.addWidget(self.use_defaults)
        outer.addWidget(modes)

        options = QGroupBox("频率与误差")
        options_form = QFormLayout(options)
        self.f_min = QLineEdit()
        self.f_max = QLineEdit()
        self.f_min.setPlaceholderText("自动（按根外推）")
        self.f_max.setPlaceholderText("自动（按根外推）")
        frequency_tip = (
            "两项留空时，程序根据计算出的零极点自动确定分析范围。\n"
            "若要复现论文单管示例，请分别填写 10 和 1e11 Hz。"
        )
        self.f_min.setToolTip(frequency_tip)
        self.f_max.setToolTip(frequency_tip)
        self.mag_error = QDoubleSpinBox()
        self.mag_error.setRange(0.0, 100.0)
        self.mag_error.setValue(2.0)
        self.mag_error.setSuffix(" dB")
        self.phase_error = QDoubleSpinBox()
        self.phase_error.setRange(0.0, 180.0)
        self.phase_error.setValue(5.0)
        self.phase_error.setSuffix(" deg")
        self.max_steps = QSpinBox()
        self.max_steps.setRange(0, 100)
        self.max_steps.setValue(10)
        self.bode_points = QSpinBox()
        self.bode_points.setRange(32, 5000)
        self.bode_points.setValue(300)
        options_form.addRow("最低频率 (Hz)", self.f_min)
        options_form.addRow("最高频率 (Hz)", self.f_max)
        options_form.addRow("幅值误差", self.mag_error)
        options_form.addRow("相位误差", self.phase_error)
        options_form.addRow("每频段最大步骤", self.max_steps)
        options_form.addRow("波特图采样点", self.bode_points)
        outer.addWidget(options)

        self.parameter_table = QTableWidget(0, 4)
        self.parameter_table.setHorizontalHeaderLabels(["参数", "表达式", "数值", "来源"])
        self.parameter_table.setMinimumHeight(150)
        self._parameter_delegate = configure_spreadsheet_table(self.parameter_table)
        outer.addWidget(QLabel("参数与来源"))
        parameter_hint = QLabel(
            "建议先在器件 Properties 中用符号引用小信号参数，再在 Place > Parameters 中赋值。"
            "留空字段不一定等于 0，而是采用对应 SLiCAP 模型的默认值。"
        )
        parameter_hint.setWordWrap(True)
        parameter_hint.setStyleSheet("color: #666;")
        outer.addWidget(parameter_hint)
        outer.addWidget(self.parameter_table, 1)

        buttons = QHBoxLayout()
        self.validate_button = QPushButton("验证网表")
        self.run_button = QPushButton("运行分析")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.validate_button.clicked.connect(lambda _checked=False: self.validate_requested.emit())
        self.run_button.clicked.connect(self._emit_run_request)
        self.cancel_button.clicked.connect(lambda _checked=False: self.cancel_requested.emit())
        buttons.addWidget(self.validate_button)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        outer.addLayout(buttons)
        self.status = QLabel("请加载或导出网表。")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        self.setWidget(scroll)

    def options(self) -> dict[str, Any]:
        lower = self.f_min.text().strip()
        upper = self.f_max.text().strip()
        frequency_range = None
        if lower or upper:
            if not lower or not upper:
                raise ValueError("最低频率和最高频率必须同时填写。")
            frequency_range = (float(lower), float(upper))
            if not all(math.isfinite(value) for value in frequency_range) or frequency_range[0] <= 0 or frequency_range[1] <= frequency_range[0]:
                raise ValueError("频率范围必须为正数且最高频率大于最低频率。")
        return {
            "input_mode": self.input_mode.currentData(),
            "modes": [key for key, check in self.mode_checks.items() if check.isChecked()],
            "numeric": self.numeric.isChecked(),
            "use_slicap_defaults": self.use_defaults.isChecked(),
            "parameter_overrides": self.parameter_overrides(),
            "frequency_range_hz": frequency_range,
            "magnitude_error_db": self.mag_error.value(),
            "phase_error_deg": self.phase_error.value(),
            "bode_points": self.bode_points.value(),
            "max_steps_per_subrange": self.max_steps.value(),
        }

    def parameter_overrides(self) -> dict[str, str]:
        """Return user-entered values for symbolic parameter rows only."""

        overrides = dict(self._overrides)
        for row in range(self.parameter_table.rowCount()):
            name_item = self.parameter_table.item(row, 0)
            value_item = self.parameter_table.item(row, 2)
            if name_item is None or value_item is None:
                continue
            name = name_item.text().strip()
            value = value_item.text().strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name) and value != value_item.data(Qt.ItemDataRole.UserRole):
                if value:
                    overrides[name] = value
                else:
                    overrides.pop(name, None)
        return overrides

    def reset_parameters(self) -> None:
        """Clear overrides when a different circuit is loaded."""

        self._overrides.clear()
        self.parameter_table.setRowCount(0)

    def _emit_run_request(self, _checked: bool = False) -> None:
        """Validate editable options before handing them to the main window."""

        try:
            options = self.options()
        except ValueError as error:
            self.option_error.emit(str(error))
            return
        if not options["modes"]:
            self.option_error.emit("请至少选择一种分析类型。")
            return
        self.run_requested.emit(options)

    def show_document(self, document: CircuitDocument) -> None:
        self._overrides = {
            item.name: item.expression for item in document.parameters
            if item.source.value == "user_override"
        }
        source = next((line.split(maxsplit=1)[1] for line in document.netlist_text.splitlines() if line.lower().startswith(".source ")), "-")
        detector = next((line.split(maxsplit=1)[1] for line in document.netlist_text.splitlines() if line.lower().startswith(".detector ")), "-")
        self.source_value.setText(source)
        self.detector_value.setText(detector)
        self.parameter_table.setRowCount(len(document.parameters))
        for row, parameter in enumerate(document.parameters):
            values = (
                parameter.name,
                parameter.expression,
                "" if parameter.numeric_value is None else f"{parameter.numeric_value:.8g}",
                parameter.source.value,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setData(Qt.ItemDataRole.UserRole, value)
                if column != 2 or not re.fullmatch(r"[A-Za-z_]\w*", parameter.name):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.parameter_table.setItem(row, column, item)
        errors = [item.message for item in document.diagnostics if item.level.value == "error"]
        warnings = [item.message for item in document.diagnostics if item.level.value == "warning"]
        if errors:
            self.status.setText("错误：" + "；".join(errors))
        elif warnings:
            self.status.setText("警告：" + "；".join(warnings))
        else:
            self.status.setText("网表结构与参数解析完成。")

    def set_busy(self, busy: bool) -> None:
        self.run_button.setEnabled(not busy)
        self.validate_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)


class TaskLogDock(QDockWidget):
    """Shared progress and diagnostic output with official-style controls."""

    def __init__(self, parent=None):
        super().__init__("任务与日志", parent)
        self.setObjectName("isaca_task_log")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        clear = QPushButton("清空日志")
        clear.clicked.connect(lambda: self.output.clear())
        bar.addWidget(clear)
        self.stage = QLabel("空闲")
        bar.addWidget(self.stage)
        bar.addStretch()
        layout.addLayout(bar)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.output.setFont(font)
        layout.addWidget(self.output)
        self.setWidget(body)

    def append(self, message: str) -> None:
        self.output.appendPlainText(message.rstrip())

    def show_event(self, event) -> None:
        self.stage.setText(event.stage)
        if event.progress is not None:
            self.progress.setValue(round(event.progress * 100))
