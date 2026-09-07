"""Optional netLens worker configuration and human-in-the-loop review UI."""

from __future__ import annotations

import os
import json
from pathlib import Path
from uuid import uuid4

import yaml
from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class VisionRuntimeDialog(QDialog):
    """Persist paths for the optional CPU and NVIDIA netLens packs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("netLens 运行环境")
        self.settings = QSettings("ISACA", "ISACA Desktop")
        form = QFormLayout(self)
        self.root = QLineEdit(self._value("vision/root", "ISACA_NETLENS_ROOT"))
        self.cpu_python = QLineEdit(self._value("vision/cpu_python", "ISACA_VISION_CPU_PYTHON"))
        self.gpu_python = QLineEdit(self._value("vision/gpu_python", "ISACA_VISION_GPU_PYTHON"))
        form.addRow("netLens 仓库/运行包", self._path_row(self.root, directory=True))
        form.addRow("CPU Pack Python", self._path_row(self.cpu_python))
        form.addRow("NVIDIA Pack Python", self._path_row(self.gpu_python))
        note = QLabel("CPU 与 NVIDIA 运行时可只配置一个；模型路径由 netLens 的 YAML 配置统一管理。")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _value(self, key: str, environment: str) -> str:
        return self.settings.value(key, os.environ.get(environment, ""), str)

    def _path_row(self, edit: QLineEdit, directory: bool = False) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit)
        button = QPushButton("浏览...")
        if directory:
            button.clicked.connect(lambda: self._choose_directory(edit))
        else:
            button.clicked.connect(lambda: self._choose_file(edit))
        layout.addWidget(button)
        return row

    def _choose_directory(self, edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 netLens 目录", edit.text() or str(Path.home()))
        if selected:
            edit.setText(selected)

    def _choose_file(self, edit: QLineEdit) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "选择 Python 解释器", edit.text() or str(Path.home()), "Python (python.exe);;All files (*)")
        if selected:
            edit.setText(selected)

    def _save(self) -> None:
        self.settings.setValue("vision/root", self.root.text().strip())
        self.settings.setValue("vision/cpu_python", self.cpu_python.text().strip())
        self.settings.setValue("vision/gpu_python", self.gpu_python.text().strip())
        self.accept()


class VisionReviewDialog(QDialog):
    """Run netLens externally and require human review before netlist use."""

    def __init__(self, project_root: str | Path, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root).resolve()
        self.settings = QSettings("ISACA", "ISACA Desktop")
        self.netlist_text = ""
        self._ir = None
        self._diagnostics = ()
        self._netlens_path: Path | None = None
        self._stdout = ""
        self._stderr = ""
        self._output: Path | None = None
        self._bridge_ready = False
        self._closing = False
        self._stopping = False
        self._fallback_attempted = False
        self.process = QProcess(self)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self.process.kill)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._recognition_finished)
        self.process.errorOccurred.connect(self._process_error)
        self.setWindowTitle("netLens 电路图识别与网表校对")
        self.resize(1180, 780)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        controls = QGroupBox("识别输入与运行时")
        form = QFormLayout(controls)
        self.image_path = QLineEdit()
        image_row = QWidget()
        image_layout = QHBoxLayout(image_row)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.addWidget(self.image_path)
        browse_image = QPushButton("选择图片...")
        browse_image.clicked.connect(self._choose_image)
        image_layout.addWidget(browse_image)
        form.addRow("原始电路图片", image_row)
        self.runtime = QComboBox()
        self.runtime.addItems(["自动", "CPU", "NVIDIA GPU"])
        runtime_row = QWidget()
        runtime_layout = QHBoxLayout(runtime_row)
        runtime_layout.setContentsMargins(0, 0, 0, 0)
        runtime_layout.addWidget(self.runtime)
        configure = QPushButton("配置运行时...")
        configure.clicked.connect(self._configure_runtime)
        runtime_layout.addWidget(configure)
        form.addRow("视觉运行模式", runtime_row)
        actions = QWidget()
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.run_button = QPushButton("运行 netLens")
        self.run_button.clicked.connect(self._run_recognition)
        load_sp = QPushButton("载入已有 .sp...")
        load_sp.clicked.connect(self._load_existing_sp)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_recognition)
        action_layout.addWidget(self.run_button)
        action_layout.addWidget(load_sp)
        action_layout.addWidget(self.stop_button)
        form.addRow(actions)
        outer.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        image_box = QWidget()
        image_layout = QVBoxLayout(image_box)
        image_layout.addWidget(QLabel("原始图片"))
        self.image_preview = QLabel("尚未选择图片")
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setMinimumSize(420, 320)
        self.image_preview.setStyleSheet("QLabel { background: white; border: 1px solid #a8a8a8; }")
        image_layout.addWidget(self.image_preview, 1)
        splitter.addWidget(image_box)

        review_box = QWidget()
        review_layout = QVBoxLayout(review_box)
        ports = QGroupBox("人工确认的分析端口")
        ports_form = QFormLayout(ports)
        self.source_node = QComboBox()
        self.source_node.setEditable(True)
        self.detector_node = QComboBox()
        self.detector_node.setEditable(True)
        ports_form.addRow("输入节点", self.source_node)
        ports_form.addRow("输出节点", self.detector_node)
        self.supply_nodes = QLineEdit()
        self.supply_nodes.setPlaceholderText("小信号接地的供电节点，逗号分隔；无供电则留空")
        ports_form.addRow("供电节点", self.supply_nodes)
        self.compile_button = QPushButton("生成/更新 SLiCAP 网表")
        self.compile_button.setEnabled(False)
        self.compile_button.clicked.connect(self._compile_netlist)
        ports_form.addRow(self.compile_button)
        review_layout.addWidget(ports)
        review_layout.addWidget(QLabel("识别与兼容诊断"))
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        review_layout.addWidget(self.diagnostics, 1)
        self.component_table = QTableWidget(0, 3)
        self.component_table.setHorizontalHeaderLabels(["器件", "类型", "引脚/节点顺序"])
        self.component_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        review_layout.addWidget(self.component_table, 1)
        review_layout.addWidget(QLabel("可编辑的 SLiCAP 网表"))
        self.netlist = QPlainTextEdit()
        review_layout.addWidget(self.netlist, 2)
        splitter.addWidget(review_box)
        splitter.setSizes([500, 650])
        outer.addWidget(splitter, 1)
        self.reviewed = QCheckBox("我已校对器件/连线、输入输出、供电节点和网表参数；识别结果不作为自动正确答案。")
        self.reviewed.toggled.connect(self._update_accept)
        outer.addWidget(self.reviewed)
        self.netlist.textChanged.connect(lambda: self.reviewed.setChecked(False))
        for combo in (self.source_node, self.detector_node):
            combo.currentTextChanged.connect(self._ports_changed)
        self.supply_nodes.textChanged.connect(self._ports_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText("确认网表并返回")
        self.accept_button.setEnabled(False)
        buttons.accepted.connect(self._accept_netlist)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _configure_runtime(self) -> None:
        VisionRuntimeDialog(self).exec()

    def _choose_image(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择电路图片", str(self.project_root),
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)",
        )
        if selected:
            self.image_path.setText(selected)
            self._show_image(Path(selected))

    def _show_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.image_preview.setText("无法读取图片")
            return
        self.image_preview.setPixmap(pixmap.scaled(
            self.image_preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _runtime_paths(self, force_cpu=False) -> tuple[Path, Path, str]:
        root = Path(self.settings.value("vision/root", os.environ.get("ISACA_NETLENS_ROOT", ""), str))
        cpu = Path(self.settings.value("vision/cpu_python", os.environ.get("ISACA_VISION_CPU_PYTHON", ""), str))
        gpu = Path(self.settings.value("vision/gpu_python", os.environ.get("ISACA_VISION_GPU_PYTHON", ""), str))
        mode = self.runtime.currentText()
        if mode == "CPU" or force_cpu:
            python = cpu
            device = "cpu"
        elif mode == "NVIDIA GPU":
            python = gpu
            device = "cuda"
        elif gpu.is_file():
            python = gpu
            device = "cuda"
        else:
            python = cpu
            device = "cpu"
        if not root.is_dir() or not (root / "configs" / "default.yaml").is_file():
            raise FileNotFoundError("请先配置有效的 netLens 仓库或运行包目录。")
        if not python.is_file():
            raise FileNotFoundError(f"所选 {device.upper()} 视觉包的 Python 解释器不存在。")
        return root.resolve(), python.resolve(), device

    def _runtime_config(self, root: Path, device: str, output: Path) -> Path:
        source = root / "configs" / "default.yaml"
        config = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        for section, key in (
            ("yolo", "model_path"),
            ("orientation", "model_path"),
            ("orientation", "bubble_model_path"),
            ("current_classifier", "model_path"),
        ):
            raw = config.get(section, {}).get(key)
            if raw:
                path = Path(str(raw))
                config[section][key] = str(path if path.is_absolute() else (source.parent / path).resolve())
        config.setdefault("ocr", {})["use_gpu"] = device == "cuda"
        config.setdefault("current_classifier", {})["device"] = device
        target = output / f"netlens-{device}.yaml"
        target.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return target

    def _run_recognition(self, _checked=False, force_cpu=False) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        if not force_cpu:
            self._fallback_attempted = False
        image = Path(self.image_path.text().strip())
        if not image.is_file():
            QMessageBox.warning(self, "netLens", "请先选择有效的电路图片。")
            return
        try:
            root, python, device = self._runtime_paths(force_cpu=force_cpu)
            output = self.project_root / "runs" / "vision" / uuid4().hex
            output.mkdir(parents=True, exist_ok=False)
            config = self._runtime_config(root, device, output)
        except Exception as error:
            QMessageBox.warning(self, "netLens", str(error))
            return
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        if device == "cpu":
            environment.insert("CUDA_VISIBLE_DEVICES", "-1")
        existing = environment.value("PYTHONPATH")
        environment.insert("PYTHONPATH", str(root) + (os.pathsep + existing if existing else ""))
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(root))
        self._stdout = ""
        self._stderr = ""
        self._output = output
        self._stopping = False
        self._bridge_ready = False
        self.reviewed.setChecked(False)
        self._update_accept()
        self.diagnostics.setPlainText(f"使用 {device.upper()} 运行 netLens...\n")
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.process.start(str(python), [
            str(Path(__file__).parent / "resources" / "vision_worker.py"), "--image", str(image.resolve()),
            "--config", str(config), "--output", str(output),
            "--device", device,
        ])

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.run_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            QMessageBox.critical(self, "netLens", self.process.errorString())

    def _stop_recognition(self) -> None:
        """Terminate only this dialog's worker, with a bounded forced fallback."""
        self._stopping = True
        self.process.terminate()
        self._kill_timer.start(3000)

    def reject(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._closing = True
            self._stop_recognition()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)

    def _read_stdout(self) -> None:
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout += text
        self.diagnostics.moveCursor(QTextCursor.MoveOperation.End)
        self.diagnostics.insertPlainText(text)

    def _read_stderr(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self._stderr += text
        self.diagnostics.moveCursor(QTextCursor.MoveOperation.End)
        self.diagnostics.insertPlainText(text)

    def _recognition_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._kill_timer.stop()
        self._read_stdout()
        self._read_stderr()
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if self._closing:
            self.reject()
            return
        if self._stopping:
            self.diagnostics.appendPlainText("识别已取消。")
            return
        result = {}
        if self._output and (self._output / "vision-result.json").is_file():
            result = json.loads((self._output / "vision-result.json").read_text(encoding="utf-8"))
        if result.get("code") == "gpu_unavailable" and self.runtime.currentText() == "自动" and not self._fallback_attempted:
            self._fallback_attempted = True
            self._run_recognition(force_cpu=True)
            return
        final = Path(result.get("artifacts", {}).get("netlist_path", ""))
        if exit_code != 0 or not final.is_file():
            QMessageBox.critical(self, "netLens", self._stderr.strip() or "识别任务未生成最终 .sp 网表。")
            return
        self._consume_sp(final)

    def _load_existing_sp(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "打开 netLens HSPICE 拓扑", str(self.project_root), "SPICE topology (*.sp)")
        if selected:
            self._consume_sp(Path(selected))

    def _consume_sp(self, path: Path) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        try:
            from sfg_prototype import parse_netlens_sp

            ir, diagnostics = parse_netlens_sp(path)
        except Exception as error:
            QMessageBox.critical(self, "视觉网表桥接", str(error))
            return
        self._netlens_path = path
        self._ir = ir
        self._diagnostics = diagnostics
        nodes = [node for node in ir.nodes if node != "0"]
        self.source_node.clear()
        self.detector_node.clear()
        self.source_node.addItems([""] + nodes)
        self.detector_node.addItems([""] + nodes)
        self.supply_nodes.clear()
        self.netlist.clear()
        self.component_table.setRowCount(len(ir.components))
        for row, component in enumerate(ir.components):
            for col, value in enumerate((component.refdes, component.kind, ", ".join(component.pins))):
                self.component_table.setItem(row, col, QTableWidgetItem(str(value)))
        lines = [f"已载入：{path}", f"元件：{len(ir.components)}，节点：{', '.join(ir.nodes)}"]
        lines.extend(f"[{item.severity}] {item.code}: {item.message}" for item in diagnostics)
        self.diagnostics.setPlainText("\n".join(lines))
        self.compile_button.setEnabled(True)
        self._ports_changed()

    def _ports_changed(self, *_args) -> None:
        """Require a new conversion and human confirmation after port changes."""
        self._bridge_ready = False
        self.reviewed.setChecked(False)
        self._update_accept()

    def _update_accept(self) -> None:
        self.accept_button.setEnabled(self._bridge_ready and self.reviewed.isChecked())

    def _compile_netlist(self) -> None:
        if self._ir is None:
            return
        try:
            from sfg_prototype import VisionBridgeConfig, compile_slicap_netlist

            result = compile_slicap_netlist(
                self._ir,
                VisionBridgeConfig(
                    source_node=self.source_node.currentText().strip() or None,
                    detector_node=self.detector_node.currentText().strip() or None,
                    supply_nodes=tuple(part.strip() for part in self.supply_nodes.text().split(",") if part.strip()),
                ),
                diagnostics=self._diagnostics,
            )
        except Exception as error:
            QMessageBox.critical(self, "视觉网表桥接", str(error))
            return
        report = [
            f"symbolically_ready={result.symbolically_ready}",
            f"numerically_ready={result.numerically_ready}",
            f"unresolved={', '.join(result.unresolved_parameters) or '-'}",
        ]
        report.extend(f"[{item.severity}] {item.code}: {item.message}" for item in result.diagnostics)
        self.diagnostics.setPlainText("\n".join(report))
        self.netlist.setPlainText(result.netlist_text)
        self._bridge_ready = result.symbolically_ready
        self._update_accept()

    def _accept_netlist(self) -> None:
        if not self._bridge_ready or not self.reviewed.isChecked():
            return
        text = self.netlist.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "视觉网表桥接", "没有可返回的 SLiCAP 网表。")
            return
        from isaca_api.models import NormalizeRequest
        from isaca_api.netlist import normalize_netlist
        document = normalize_netlist(NormalizeRequest(netlist_text=text))
        errors = [item.message for item in document.diagnostics if item.level == "error"]
        if errors:
            QMessageBox.warning(self, "网表校对", "\n".join(errors))
            return
        self.netlist_text = text + "\n"
        self.accept()
