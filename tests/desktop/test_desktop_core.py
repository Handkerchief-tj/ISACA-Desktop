from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from isaca_api.models import AnalysisRequest
from isaca_api.slicap_adapter import _bode_frequency_range, _root_record, _transfer_presentation
from isaca_api.slicap_schematic import internal_to_slicap_schematic
from isaca_desktop.panels import AnalysisSetupDock
from isaca_desktop.protocol import WorkerRequest, WorkerEvent, write_request
from isaca_desktop.results import _numeric_html, _summary_html
from isaca_desktop.worker import _run_export


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qt_app():
    application = QApplication.instance() or QApplication([])
    yield application


def test_worker_protocol_round_trip(tmp_path) -> None:
    request = WorkerRequest(
        action="analysis",
        job_id="desktop-test",
        run_root=str(tmp_path),
        result_path=str(tmp_path / "result.json"),
        payload={"request": AnalysisRequest(netlist_text="RC\n.end\n").model_dump(mode="json")},
    )
    path = write_request(request, tmp_path / "request.json")
    restored = WorkerRequest.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored == request
    assert WorkerEvent(event="progress", stage="test", message="ok", progress=0.5).progress == 0.5


def test_bode_range_accepts_numpy_root_arrays() -> None:
    class Result:
        poles = np.asarray([-1000.0, -2000.0])
        zeros = np.asarray([-5000.0])

    lower, upper = _bode_frequency_range(AnalysisRequest(netlist_text="RC\n.end\n"), Result())
    assert lower > 0
    assert upper > lower


def test_analysis_panel_uses_paper_defaults_and_explicit_default_opt_in(qt_app) -> None:
    panel = AnalysisSetupDock()
    options = panel.options()
    assert options["magnitude_error_db"] == 2.0
    assert options["phase_error_deg"] == 5.0
    assert options["max_steps_per_subrange"] == 10
    assert options["use_slicap_defaults"] is False
    panel.deleteLater()


def test_component_parameter_candidates_keep_component_property_context() -> None:
    from types import SimpleNamespace
    from isaca_desktop.parameter_ui import component_parameter_candidates

    components = [
        SimpleNamespace(instance_id="Q1", params={"gm": "gm_Q1", "go": "go", "cpi": "2*cpi_Q1", "cbc": "1p"}),
        SimpleNamespace(instance_id="Q2", params={"gm": "gm_Q2", "go": "go", "_internal": "ignored"}),
    ]
    candidates = {item.name: item.contexts for item in component_parameter_candidates(components)}
    assert candidates == {
        "cpi_Q1": ("Q1.cpi",),
        "gm_Q1": ("Q1.gm",),
        "gm_Q2": ("Q2.gm",),
        "go": ("Q1.go", "Q2.go"),
    }


def test_parameter_table_arrow_keys_move_between_editable_cells(qt_app) -> None:
    from isaca_desktop.parameter_ui import configure_spreadsheet_table

    table = QTableWidget(1, 2)
    for column in range(2):
        table.setItem(0, column, QTableWidgetItem(str(column)))
    configure_spreadsheet_table(table)
    table.show()
    table.setCurrentCell(0, 0)
    table.editItem(table.item(0, 0))
    qt_app.processEvents()
    QTest.keyClick(table.focusWidget(), Qt.Key.Key_Right)
    qt_app.processEvents()
    assert table.currentColumn() == 1
    table.close()


def test_official_parameter_dialog_receives_component_symbol_suggestions(qt_app) -> None:
    from types import SimpleNamespace
    from isaca_desktop.parameter_ui import install_slicap_parameter_dialog_enhancement

    component = SimpleNamespace(instance_id="Q1", params={"gm": "gm_Q1", "cpi": "cpi_Q1"})
    install_slicap_parameter_dialog_enhancement(lambda: [component])
    from SLiCAP.schematic.parameter_dialog import ParameterDialog

    dialog = ParameterDialog(params=[], show=False)
    assert [item.name for item in dialog._isaca_delegate.candidates] == ["cpi_Q1", "gm_Q1"]
    add_button = next(button for button in dialog.findChildren(type(dialog._action_btn)) if button.text() == "Add row")
    add_button.click()
    qt_app.processEvents()
    assert dialog._table.rowCount() == 1
    assert dialog._table.currentColumn() == 0
    dialog.close()


def test_transfer_presentation_normalizes_numeric_roots_in_hz() -> None:
    from types import SimpleNamespace
    import sympy as sp

    s = sp.Symbol("s")
    laplace = SimpleNamespace(laplace=1 / (1 + s / 1000), DCvalue=1)
    pz = SimpleNamespace(poles=np.asarray([-1000.0]), zeros=np.asarray([2000.0]), DCvalue=1)
    presentation = _transfer_presentation(laplace, pz)
    assert "normalized" in presentation
    assert "\\frac{s}{1000}" in presentation["_latex"]["normalized"]
    assert _root_record(-1000.0)["frequency_hz"] == pytest.approx(1000 / (2 * np.pi))


def test_parameter_overrides_preserve_provenance_and_dependency_values(qt_app) -> None:
    from isaca_api.netlist import normalize_netlist
    from isaca_api.models import NormalizeRequest

    text = "RC\nV1 in 0 V value=1\nR1 in out R value={R}\nC1 out 0 C value={C}\n.param R=1k C={R*1e-9}\n.source V1\n.detector V_out\n.end\n"
    panel = AnalysisSetupDock()
    document = normalize_netlist(NormalizeRequest(netlist_text=text))
    panel.show_document(document)
    assert panel.parameter_overrides() == {}
    row = next(i for i in range(panel.parameter_table.rowCount()) if panel.parameter_table.item(i, 0).text() == "R")
    panel.parameter_table.item(row, 2).setText("2k")
    overrides = panel.parameter_overrides()
    assert overrides == {"R": "2k"}
    refreshed = normalize_netlist(NormalizeRequest(netlist_text=text, parameter_overrides=overrides))
    assert next(p for p in refreshed.parameters if p.name == "C").numeric_value == pytest.approx(2e-6)
    panel.show_document(refreshed)
    assert panel.parameter_overrides() == {"R": "2k"}
    panel.reset_parameters()
    assert panel.parameter_overrides() == {}
    panel.deleteLater()


def test_missing_or_timed_out_graphviz_does_not_discard_analysis(tmp_path, monkeypatch) -> None:
    import subprocess
    from isaca_desktop import worker

    graph = tmp_path / "graph.dot"
    graph.write_text("digraph G {}", encoding="utf-8")
    result = {"analyses": {"symbolic": {"graphs": {graph.name: str(graph)}}}, "artifacts": {}}
    monkeypatch.setattr(worker, "_graphviz_dot", lambda: None)
    assert worker._render_dot_artifacts(result)[0]["code"] == "graphviz_unavailable"
    monkeypatch.setattr(worker, "_graphviz_dot", lambda: "dot.exe")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("dot.exe", 60)
    monkeypatch.setattr(worker.subprocess, "run", timeout)
    assert worker._render_dot_artifacts(result)[0]["code"] == "graphviz_render_failed"


def test_real_window_export_analysis_and_cancellation(tmp_path, rc_schematic) -> None:
    import subprocess
    import sys

    native, _ = internal_to_slicap_schematic(rc_schematic)
    schematic = tmp_path / "sch" / "rc.slicap_sch"
    schematic.parent.mkdir()
    schematic.write_text(json.dumps(native), encoding="utf-8")
    script = Path(__file__).with_name("desktop_smoke.py")
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "QT_QPA_PLATFORM": "offscreen"}
    completed = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True,
                               encoding="utf-8", errors="replace", env=environment, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    assert summary["pole"] == pytest.approx(-1000)
    assert summary["pole_frequency_hz"] == pytest.approx(1000 / (2 * np.pi))
    assert summary["normalized_transfer"]
    assert summary["cancelled_job"] is True
    assert summary["svg_exists"] is True
    assert summary["katex_formula_count"] >= 1


def test_result_pages_are_offline_and_include_core_values() -> None:
    result = {
        "software": {"slicap": "5.2.1"},
        "circuit": {"title": "RC", "parameters": []},
        "flattened_circuit": {"source": ["V1", None], "detector": ["V_out", None], "nodes": ["0", "in", "out"], "elements": {}},
        "analyses": {"laplace": {
            "laplace": "1/(C*R*s + 1)",
            "_latex": {"laplace": r"\frac{1}{CRs+1}"},
            "presentation": {
                "normalized": "normalized", "dc_gain": "1", "dc_gain_magnitude": 1.0,
                "dc_gain_db": 0.0, "dc_gain_phase_deg": 0.0,
                "_latex": {"normalized": r"H(s)=\frac{1}{1+s/1000}", "dc_gain": "1"},
            },
        }, "pz": {
            "poles": [-1000.0], "zeros": [], "DCvalue": "1",
            "pole_records": [{"root": -1000.0, "angular_frequency_rad_s": 1000.0,
                              "frequency_hz": 159.154943, "half_plane": "LHP"}],
            "zero_records": [],
        }},
    }
    summary = _summary_html(result)
    numeric = _numeric_html(result)
    assert "5.2.1" in summary
    assert "data-tex=" in numeric and "katex.min.js" in numeric
    assert "低频小信号增益 H(0)" in numeric
    assert "159.155 Hz" in numeric
    assert "http://" not in numeric and "https://" not in numeric


def test_invalid_error_and_frequency_settings_are_rejected() -> None:
    from pydantic import ValidationError
    for values in ({"frequency_range_hz": [1, float("inf")]},
                   {"frequency_range_hz": [100, 1]}, {"phase_error_deg": -1},
                   {"magnitude_error_db": float("nan")}):
        with pytest.raises(ValidationError):
            AnalysisRequest(netlist_text="RC\n.end\n", **values)


def test_symbolic_worker_contract_keeps_root_errors_and_tex_separate() -> None:
    from types import SimpleNamespace
    import sympy as sp
    from isaca_api.slicap_adapter import _subrange_record
    from isaca_desktop.results import _symbolic_html
    s, R, C = sp.symbols("s R C")
    item = SimpleNamespace(
        cluster_index=1, lower_frequency_hz=1, upper_frequency_hz=1e3,
        transfer=SimpleNamespace(transfer=1/(1+s*R*C), success=True, poles=(), zeros=()),
        paper_style_transfer=SimpleNamespace(
            transfer=1/(1+s*R*C), numerator=1, denominator=1+s*R*C,
            success=True, poles=(), zeros=(),
        ),
        target_root_approximations=(SimpleNamespace(
            expression=-1/(R*C), parameters=("R", "C"), kind="pole", category="OLR-O",
            relative_root_error=0.36, status="resolved", method="local characteristic",
        ),),
        error=SimpleNamespace(max_magnitude_error_db=0.5, max_phase_error_deg=1.0, max_relative_error=0.03),
    )
    record = _subrange_record(item)
    assert record["transfer"]["_latex"]["transfer"] == sp.latex(1/(1+s*R*C))
    assert record["error"]["max_magnitude_error_db"] == 0.5
    assert record["target_roots"][0]["relative_root_error"] == 0.36
    assert record["paper_style_transfer"]["transfer"] == "1/(C*R*s + 1)"
    page = _symbolic_html({"analyses": {"symbolic": {"frequency_results": [record]}}})
    assert "data-tex=" in page and "OLR-O" in page
    assert "根聚类与频率子区间" in page and "frequency-map" in page
    assert "本频段的简化增益表达式" in page and "本频段的零极点" in page
    assert "paper-style 传递函数" in page
    assert "已定位" in page and "根位置偏差（诊断）" in page
    assert "未通过局部根误差验收" not in page


def test_frequency_map_handles_zero_and_infinite_outer_bounds() -> None:
    from isaca_desktop.results import _frequency_partition_map

    intervals = [
        {"cluster_index": 1, "lower_frequency_hz": 0.0, "upper_frequency_hz": 1e4},
        {"cluster_index": 2, "lower_frequency_hz": 1e4, "upper_frequency_hz": float("inf")},
    ]
    clusters = [
        {"index": 1, "roots": [{"kind": "pole", "frequency_hz": 1e3}]},
        {"index": 2, "roots": [{"kind": "zero", "frequency_hz": 1e6}]},
    ]
    page = _frequency_partition_map(intervals, clusters)
    assert "10^" in page and "Cluster 1" in page and "Cluster 2" in page
    assert "nan" not in page.lower() and "inf" not in page.lower()


def test_mna_page_uses_math_matrices_instead_of_spreadsheet_tables() -> None:
    from isaca_desktop.results import _matrix_html

    result = {"analyses": {"matrix": {
        "M": [["s", "1"], ["0", "R"]], "Dv": [["V_in"], ["V_out"]], "Iv": [["1"], ["0"]],
        "_latex": {
            "M": r"\left[\begin{matrix}s&1\\0&R\end{matrix}\right]",
            "Dv": r"\left[\begin{matrix}V_{in}\\V_{out}\end{matrix}\right]",
            "Iv": r"\left[\begin{matrix}1\\0\end{matrix}\right]",
        },
    }}}
    page = _matrix_html(result)
    assert "M(s)D_v(s)=I_v(s)" in page
    assert page.count("data-tex=") >= 4
    assert "<table>" not in page


@pytest.mark.skipif(os.environ.get("ISACA_TEST_VISION") != "1", reason="Vision acceptance is deferred; enable explicitly with ISACA_TEST_VISION=1")
def test_vision_requires_explicit_ports_and_human_confirmation(qt_app, tmp_path) -> None:
    from isaca_desktop.vision import VisionReviewDialog
    path = tmp_path / "topology.sp"
    path.write_text(".subckt rc\nr1 IN OUT r\nc1 OUT 0 c\n.ends\n", encoding="utf-8")
    dialog = VisionReviewDialog(tmp_path)
    dialog._consume_sp(path)
    assert dialog.source_node.currentText() == ""
    assert dialog.detector_node.currentText() == ""
    assert not dialog.accept_button.isEnabled()
    assert dialog.component_table.rowCount() == 2
    dialog.source_node.setCurrentText("IN")
    dialog.detector_node.setCurrentText("OUT")
    dialog._compile_netlist()
    assert not dialog.accept_button.isEnabled()
    dialog.reviewed.setChecked(True)
    assert dialog.accept_button.isEnabled()
    dialog.source_node.setCurrentText("OUT")
    assert not dialog.accept_button.isEnabled()
    dialog.reject()


def test_packaged_safe_worker_uses_official_exporter(tmp_path, rc_schematic, qt_app) -> None:
    native, diagnostics = internal_to_slicap_schematic(rc_schematic)
    assert not [item for item in diagnostics if item.level == "error"]
    schematic = tmp_path / "rc.slicap_sch"
    output = tmp_path / "rc.cir"
    schematic.write_text(json.dumps(native), encoding="utf-8")
    request = WorkerRequest(
        action="export_schematic",
        job_id="export-test",
        run_root=str(tmp_path),
        result_path=str(tmp_path / "result.json"),
        payload={"input_path": str(schematic), "output_path": str(output)},
    )
    result = _run_export(request)
    assert output.is_file()
    assert "R1 in out R value={R}" in result["netlist_text"]
    assert ".source V1" in result["netlist_text"]


def test_export_rejects_input_changed_after_submission(tmp_path) -> None:
    import hashlib
    path = tmp_path / "edited.slicap_sch"
    path.write_text("{}", encoding="utf-8")
    request = WorkerRequest(action="export_schematic", job_id="changed", run_root=str(tmp_path),
                            result_path=str(tmp_path / "result.json"), payload={
                                "input_path": str(path), "output_path": str(tmp_path / "out.cir"),
                                "input_sha256": hashlib.sha256(b"before").hexdigest(),
                            })
    with pytest.raises(RuntimeError, match="重新运行"):
        _run_export(request)
