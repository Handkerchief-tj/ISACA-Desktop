"""ISACA desktop shell built around the official SLiCAP schematic window."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDockWidget, QFileDialog, QMessageBox
from SLiCAP.schematic.window import MainWindow as SLiCAPMainWindow

from isaca_api.models import AnalysisRequest, DiagnosticLevel, NormalizeRequest
from isaca_api.netlist import normalize_netlist

from .panels import AnalysisSetupDock, NetlistEditor, ProjectInputDock, TaskLogDock
from .parameter_ui import install_slicap_parameter_dialog_enhancement
from .paths import create_project_layout, ensure_user_directories
from .process import WorkerController
from .results import ResultTabs
from .slicap_ui import SLiCAPDesktopAdapter


class IsacaMainWindow(SLiCAPMainWindow):
    """Combine the official editor with ISACA input, analysis and result docks."""

    def __init__(self, project_root: str | Path | None = None, file: str | Path | None = None):
        self.desktop_adapter = SLiCAPDesktopAdapter()
        super().__init__(config="slicap", schematic_only=True)
        install_slicap_parameter_dialog_enhancement(self._active_components)
        self.setWindowTitle("ISACA - Intelligent Symbolic Analog Circuit Analyzer")
        self.resize(1560, 940)
        self.settings = QSettings("ISACA", "ISACA Desktop")
        self.user_paths = ensure_user_directories()
        self.project_root: Path | None = None
        self._pending_analysis_options: dict[str, Any] | None = None
        self._document = None
        self._close_after_cancel = False

        self.project_dock = ProjectInputDock(self)
        self.analysis_dock = AnalysisSetupDock(self)
        self.log_dock = TaskLogDock(self)
        self.netlist_editor = NetlistEditor(self)
        self.netlist_dock = QDockWidget("SLiCAP 网表", self)
        self.netlist_dock.setObjectName("isaca_netlist_editor")
        self.netlist_dock.setWidget(self.netlist_editor)
        self.results = ResultTabs(self)
        self.results_dock = QDockWidget("分析结果", self)
        self.results_dock.setObjectName("isaca_results")
        self.results_dock.setWidget(self.results)

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.project_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.analysis_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.netlist_dock)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.results_dock)
        self.netlist_dock.hide()
        self.results_dock.hide()

        self.worker = WorkerController(self)
        self._connect_signals()
        self._extend_menus()

        initial_root = Path(project_root).resolve() if project_root else self._stored_project()
        if initial_root is not None and initial_root.exists():
            self.set_project(initial_root)
        if file is not None:
            self.open_input(Path(file))
        self.statusBar().showMessage("请创建或打开项目。" if self.project_root is None else "就绪")

    def _active_components(self) -> list[Any]:
        """Return components from the active official scene for parameter suggestions."""

        panel = self.desktop_adapter.active_panel(self)
        scene = getattr(panel, "_scene", None)
        if scene is None:
            return []
        return [
            item for item in scene.items()
            if hasattr(item, "instance_id") and hasattr(item, "params")
        ]

    def _connect_signals(self) -> None:
        """Connect custom panels without modifying official SLiCAP classes."""

        self.project_dock.new_project_requested.connect(self.select_project)
        self.project_dock.open_project_requested.connect(self.select_project)
        self.project_dock.new_schematic_requested.connect(self.new_schematic)
        self.project_dock.open_schematic_requested.connect(self.open_schematic)
        self.project_dock.export_schematic_requested.connect(self.export_active_schematic)
        self.project_dock.open_netlist_requested.connect(self.open_netlist)
        self.project_dock.save_netlist_requested.connect(self.save_netlist)
        self.project_dock.import_image_requested.connect(self.import_image)
        self.project_dock.tree.doubleClicked.connect(self._project_item_activated)
        self.analysis_dock.validate_requested.connect(self.validate_netlist)
        self.analysis_dock.run_requested.connect(self.run_analysis)
        self.analysis_dock.cancel_requested.connect(self.worker.cancel)
        self.analysis_dock.option_error.connect(self._show_warning)
        self.worker.event_received.connect(self.log_dock.show_event)
        self.worker.log_received.connect(self.log_dock.append)
        self.worker.busy_changed.connect(self.analysis_dock.set_busy)
        self.worker.busy_changed.connect(self._set_input_busy)
        self.worker.completed.connect(self._worker_completed)
        self.worker.failed.connect(self._worker_failed)
        self.worker.cancelled.connect(self._worker_cancelled)
        self.results.artifact_open_failed.connect(self._show_warning)

    def _extend_menus(self) -> None:
        """Add ISACA actions while preserving the official English menus."""

        analysis_menu = self.menuBar().addMenu("分析")
        for label, handler in (
            ("验证当前网表", self.validate_netlist),
            ("运行所选分析", self._run_from_menu),
            ("导出当前原理图并分析", self._export_and_run),
            ("取消当前任务", self.worker.cancel),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, target=handler: target())
            analysis_menu.addAction(action)
        view_menu = self.menuBar().addMenu("ISACA 视图")
        for dock in (self.project_dock, self.analysis_dock, self.netlist_dock, self.results_dock, self.log_dock):
            view_menu.addAction(dock.toggleViewAction())

    def _stored_project(self) -> Path | None:
        raw = self.settings.value("project/root", "", str)
        return Path(raw).resolve() if raw else None

    def set_project(self, root: str | Path) -> None:
        """Select the project used by the editor, workers and artifacts."""

        project = create_project_layout(root)
        from SLiCAP.schematic import project as slicap_project

        slicap_project.set_app_root(project)
        self.project_root = project
        self.project_dock.set_project_root(project)
        self.settings.setValue("project/root", str(project))
        self.statusBar().showMessage(f"项目：{project}")
        self.log_dock.append(f"已打开项目：{project}")

    def select_project(self) -> None:
        """Ask for a project folder and initialize the standard layout."""

        if self.worker.busy:
            self._show_warning("请等待当前任务结束或取消任务后再切换项目。")
            return
        start = str(self.project_root or Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择 ISACA/SLiCAP 项目目录", start)
        if selected:
            self.set_project(selected)

    def _require_project(self) -> bool:
        if self.project_root is not None:
            return True
        self._show_warning("请先在左侧创建或打开项目目录。")
        return False

    def _set_input_busy(self, busy: bool) -> None:
        """Freeze input replacement during export/analysis, leaving cancellation active."""
        self.project_dock.setEnabled(not busy)
        self.netlist_editor.setEnabled(not busy)

    def new_schematic(self) -> None:
        """Open an unsaved official SLiCAP 5.2.1 schematic canvas."""

        if self._require_project():
            self.add_canvas_panel("slicap", config="slicap")
            self.analysis_dock.input_mode.setCurrentIndex(1)

    def open_schematic(self) -> None:
        """Open one official schematic in the central dock area."""

        if not self._require_project():
            return
        selected, _ = QFileDialog.getOpenFileName(
            self, "打开 SLiCAP 原理图", str(self.project_root / "sch"),
            "SLiCAP schematic (*.slicap_sch)",
        )
        if selected:
            self.load_file(Path(selected))
            self.analysis_dock.input_mode.setCurrentIndex(1)

    def open_netlist(self) -> None:
        """Load a hand-written .cir file into the native text editor."""

        start = str((self.project_root / "cir") if self.project_root else Path.home())
        selected, _ = QFileDialog.getOpenFileName(self, "打开 SLiCAP 网表", start, "SLiCAP netlist (*.cir);;All files (*)")
        if selected:
            self._load_netlist_path(Path(selected))

    def _load_netlist_path(self, path: Path) -> None:
        if not self._confirm_netlist_change():
            return
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            self._show_error(f"无法读取网表：{error}")
            return
        self.analysis_dock.reset_parameters()
        self.netlist_editor.set_netlist(text, path)
        self.analysis_dock.input_mode.setCurrentIndex(0)
        self._show_top_dock(self.netlist_dock)
        self.validate_netlist()

    def save_netlist(self) -> bool:
        """Save the canonical analysis input without touching the schematic."""

        if not self._require_project():
            return False
        path = self.netlist_editor.path
        if path is None:
            selected, _ = QFileDialog.getSaveFileName(
                self, "保存 SLiCAP 网表", str(self.project_root / "cir" / "circuit.cir"),
                "SLiCAP netlist (*.cir)",
            )
            if not selected:
                return False
            path = Path(selected)
            if path.suffix.lower() != ".cir":
                path = path.with_suffix(".cir")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.netlist_editor.text(), encoding="utf-8")
        except OSError as error:
            self._show_error(f"保存网表失败：{error}")
            return False
        self.netlist_editor.path = path.resolve()
        self.netlist_editor.path_label.setText(str(self.netlist_editor.path))
        self.netlist_editor.editor.document().setModified(False)
        self.log_dock.append(f"已保存网表：{path}")
        return True

    def validate_netlist(self) -> Any | None:
        """Normalize the editor text and refresh strict parameter diagnostics."""

        text = self.netlist_editor.text().strip()
        if not text:
            self._show_warning("请先打开网表，或从当前原理图导出网表。")
            return None
        try:
            document = normalize_netlist(NormalizeRequest(
                netlist_text=text,
                parameter_overrides=self.analysis_dock.parameter_overrides(),
                use_slicap_defaults=self.analysis_dock.use_defaults.isChecked(),
            ))
        except Exception as error:
            self._show_error(f"网表规范化失败：{error}")
            return None
        if document.netlist_text != self.netlist_editor.text():
            current_path = self.netlist_editor.path
            self.netlist_editor.editor.setPlainText(document.netlist_text)
            self.netlist_editor.editor.document().setModified(True)
        self._document = document
        self.analysis_dock.show_document(document)
        for diagnostic in document.diagnostics:
            self.log_dock.append(f"[{diagnostic.level.value}] {diagnostic.code}: {diagnostic.message}")
        return document

    def _analysis_blocker(self, document: Any, options: dict[str, Any]) -> str | None:
        errors = [item.message for item in document.diagnostics if item.level == DiagnosticLevel.ERROR]
        if errors:
            return "；".join(errors)
        lower = document.netlist_text.lower()
        if ".source " not in lower or ".detector " not in lower:
            return "分析前必须在网表或原理图中设置 source 和 detector。"
        needs_values = options["numeric"] or "bode" in options["modes"] or "symbolic" in options["modes"]
        if needs_values:
            missing = [item.name for item in document.parameters if item.required_for_numeric and item.numeric_value is None]
            if missing:
                return "数值分析和分频段化简需要先填写参数：" + ", ".join(missing)
        return None

    def run_analysis(self, options: dict[str, Any]) -> None:
        """Validate and submit the current .cir text to an isolated worker."""

        if not self._require_project() or self.worker.busy:
            return
        options = dict(options)
        mode = options.pop("input_mode", "netlist")
        if mode == "schematic":
            self.export_active_schematic(options)
            return
        if not self.netlist_editor.text().strip():
            if self.desktop_adapter.active_panel(self) is not None:
                self.export_active_schematic(options)
            else:
                self._show_warning("当前既没有网表，也没有可导出的原理图。")
            return
        document = self.validate_netlist()
        if document is None:
            return
        blocker = self._analysis_blocker(document, options)
        if blocker:
            self._show_warning(blocker)
            return
        request_data = {
            "netlist_text": document.netlist_text,
            **options,
        }
        try:
            request = AnalysisRequest.model_validate(request_data)
            job_id = self.worker.start(
                "analysis", {"request": request.model_dump(mode="json")}, self.project_root / "runs"
            )
        except Exception as error:
            self._show_error(f"无法启动分析任务：{error}")
            return
        self.log_dock.append(f"分析任务：{job_id}")
        self.statusBar().showMessage("分析正在后台运行...")

    def export_active_schematic(self, analysis_options: dict[str, Any] | None = None) -> None:
        """Save the active official canvas and export its authoritative .cir."""

        if not self._require_project() or self.worker.busy:
            return
        self._pending_analysis_options = None
        if not self._confirm_netlist_change():
            return
        panel = self.desktop_adapter.active_panel(self)
        if panel is None:
            self._show_warning("没有活动的 SLiCAP 原理图。")
            return
        try:
            schematic_path = self.desktop_adapter.save_panel(panel)
        except Exception as error:
            self._show_error(f"原理图保存失败：{error}")
            return
        if schematic_path is None:
            self._show_warning("原理图尚未保存，导出已取消。")
            return
        output = self.project_root / "cir" / f"{schematic_path.stem}.cir"
        try:
            job_id = self.worker.start(
                "export_schematic",
                {"input_path": str(schematic_path), "output_path": str(output)},
                self.project_root / "runs",
            )
            self._pending_analysis_options = analysis_options
        except Exception as error:
            self._show_error(f"无法启动官方网表导出器：{error}")
            return
        self.log_dock.append(f"网表导出任务：{job_id}")

    def _run_from_menu(self) -> None:
        self.analysis_dock._emit_run_request()

    def _export_and_run(self) -> None:
        try:
            options = self.analysis_dock.options()
            options.pop("input_mode", None)
        except ValueError as error:
            self._show_warning(str(error))
            return
        self.export_active_schematic(options)

    def import_image(self) -> None:
        """Open the optional netLens review workflow without importing its runtime."""

        if not self._require_project():
            return
        try:
            from .vision import VisionReviewDialog
        except ImportError as error:
            self._show_error(f"视觉校对模块不可用：{error}")
            return
        if not self._confirm_netlist_change():
            return
        dialog = VisionReviewDialog(self.project_root, self)
        if dialog.exec() and dialog.netlist_text:
            self.analysis_dock.reset_parameters()
            self.netlist_editor.set_netlist(dialog.netlist_text)
            self.netlist_editor.editor.document().setModified(True)
            self.analysis_dock.input_mode.setCurrentIndex(0)
            self._show_top_dock(self.netlist_dock)
            self.validate_netlist()

    def open_input(self, path: Path) -> None:
        """Dispatch a startup or project-tree file by extension."""

        suffix = path.suffix.lower()
        if suffix == ".slicap_sch":
            self.load_file(path)
            self.analysis_dock.input_mode.setCurrentIndex(1)
        elif suffix == ".cir":
            self._load_netlist_path(path)

    def _project_item_activated(self, index) -> None:
        path = Path(self.project_dock.model.filePath(index))
        if path.is_file():
            self.open_input(path)

    def _worker_completed(self, action: str, result: dict[str, Any]) -> None:
        if action == "export_schematic":
            self.analysis_dock.reset_parameters()
            self.netlist_editor.set_netlist(result["netlist_text"], result["netlist_path"])
            self.analysis_dock.input_mode.setCurrentIndex(0)
            self._show_top_dock(self.netlist_dock)
            self.validate_netlist()
            pending = self._pending_analysis_options
            self._pending_analysis_options = None
            if pending is not None:
                # An override from a previous netlist must not leak into the
                # newly exported schematic.
                pending = {**pending, "parameter_overrides": {}}
                self.run_analysis(pending)
            return
        self.results.show_result(result)
        self._show_top_dock(self.results_dock)
        self.statusBar().showMessage("分析完成。")

    def _worker_failed(self, message: str) -> None:
        self._pending_analysis_options = None
        self.statusBar().showMessage("任务失败。")
        self._show_error(message)

    def _worker_cancelled(self) -> None:
        self._pending_analysis_options = None
        self.log_dock.append("任务已取消。")
        self.statusBar().showMessage("任务已取消。")
        if self._close_after_cancel:
            self._close_after_cancel = False
            QTimer.singleShot(0, self.close)

    def _show_top_dock(self, dock: QDockWidget) -> None:
        if not self._canvas_docks and self.centralWidget() is not None:
            self.centralWidget().setFixedHeight(0)
        dock.show()
        anchor = next((item for item in reversed(self._canvas_docks) if item.isVisible() and not item.isFloating()), None)
        if anchor is not None and dock is not anchor:
            self.tabifyDockWidget(anchor, dock)
        dock.raise_()

    def _show_warning(self, message: str) -> None:
        self.log_dock.append(f"警告：{message}")
        QMessageBox.warning(self, "ISACA", message)

    def _show_error(self, message: str) -> None:
        self.log_dock.append(f"错误：{message}")
        QMessageBox.critical(self, "ISACA", message)

    def closeEvent(self, event) -> None:
        if not self._confirm_netlist_change():
            event.ignore()
            return
        if self.worker.busy:
            answer = QMessageBox.question(
                self, "退出 ISACA", "分析任务仍在运行。是否取消任务并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_cancel = True
            self.worker.cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def _confirm_netlist_change(self) -> bool:
        """Give editable netlists the same save/discard protection as schematics."""
        if not self.netlist_editor.editor.document().isModified():
            return True
        answer = QMessageBox.question(
            self, "未保存的网表", "是否先保存当前网表修改？",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_netlist()
        if answer == QMessageBox.StandardButton.Discard:
            self.netlist_editor.editor.document().setModified(False)
            return True
        return False


def compatibility_snapshot() -> str:
    """Expose the desktop compatibility boundary for diagnostics and tests."""

    adapter = SLiCAPDesktopAdapter()
    return json.dumps(adapter.capabilities.__dict__, ensure_ascii=False, sort_keys=True)
