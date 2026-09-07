"""Run an actual Qt window and QProcess chain in one disposable process."""

import json
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication


def main(root: Path) -> int:
    application = QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(root / "settings"))
    from isaca_desktop.main_window import IsacaMainWindow

    window = IsacaMainWindow(project_root=root, file=root / "sch" / "rc.slicap_sch")
    window.show()
    failures = []
    state = {"cancelled_job": False}

    def fail(message):
        failures.append(str(message))
        print(message, file=sys.stderr, flush=True)
        application.exit(1)

    window._show_error = fail
    window._show_warning = fail
    window.worker.failed.connect(fail)
    options = {"modes": ["laplace", "pz", "matrix", "bode"], "numeric": True,
               "bode_points": 64, "input_mode": "schematic"}

    def start_export():
        try:
            panel = window.desktop_adapter.active_panel(window)
            assert panel is not None and hasattr(panel, "_scene")
            assert window.desktop_adapter.save_panel(panel).is_file()
            window.run_analysis(options)
        except Exception:
            fail(traceback.format_exc())

    def finished(action, result):
        if action != "analysis":
            return
        state["pole"] = float(result["analyses"]["pz"]["poles"][0])
        state["pole_frequency_hz"] = result["analyses"]["pz"]["pole_records"][0]["frequency_hz"]
        state["normalized_transfer"] = result["analyses"]["laplace"]["presentation"].get("normalized")
        state["svg_exists"] = Path(result["analyses"]["bode"]["artifact"]).is_file()
        window.results.setCurrentIndex(2)
        QTimer.singleShot(500, check_formula)

    def check_formula():
        view = window.results.widget(2)
        view.page().runJavaScript("document.querySelectorAll('.katex').length", formula_checked)

    def formula_checked(count):
        if not count:
            state["formula_attempts"] = state.get("formula_attempts", 0) + 1
            if state["formula_attempts"] > 20:
                fail("Offline KaTeX did not render the numerical transfer function.")
            else:
                QTimer.singleShot(500, check_formula)
            return
        state["katex_formula_count"] = count
        (root / "smoke.json").write_text(json.dumps(state), encoding="utf-8")
        QTimer.singleShot(250, finish)

    def finish():
        window.grab().save(str(root / "desktop.png"))
        window.netlist_editor.editor.document().setModified(False)
        window.close()
        application.quit()

    def cancelled():
        state["cancelled_job"] = True
        assert window.project_dock.isEnabled()
        assert window.netlist_editor.isEnabled()
        QTimer.singleShot(0, start_export)

    window.worker.cancelled.connect(cancelled)
    window.worker.completed.connect(finished)
    window.worker.start("analysis", {"request": {"netlist_text": "cancelled\n.end\n"}}, root / "runs")
    assert not window.project_dock.isEnabled()
    assert not window.netlist_editor.isEnabled()
    QTimer.singleShot(50, window.worker.cancel)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: fail("Desktop smoke test exceeded 100 seconds."))
    deadline.start(100_000)
    code = application.exec()
    return 1 if failures else code


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
