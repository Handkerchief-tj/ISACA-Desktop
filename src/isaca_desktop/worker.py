"""Headless worker actions used by the desktop shell and packaged executable."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from isaca_api.models import AnalysisRequest
from .protocol import WorkerEvent, WorkerRequest, emit_event


class WorkerCancelled(Exception):
    """Cooperative cancellation detected between analysis stages."""


def _check_cancelled(request: WorkerRequest) -> None:
    if request.cancel_path and Path(request.cancel_path).exists():
        raise WorkerCancelled("任务已取消。")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _graphviz_dot() -> str | None:
    explicit = os.environ.get("ISACA_GRAPHVIZ_DOT")
    if explicit and Path(explicit).is_file():
        return explicit
    private = Path(sys.executable).resolve().parent / "graphviz" / "bin" / "dot.exe"
    if private.is_file():
        return str(private)
    return shutil.which("dot")


def _render_dot_artifacts(result: dict[str, Any]) -> list[dict[str, str]]:
    """Render SFG DOT files without making graph layout a calculation dependency."""

    graphviz = _graphviz_dot()
    graphs = result.get("analyses", {}).get("symbolic", {}).get("graphs", {})
    diagnostics: list[dict[str, str]] = []
    if not graphs:
        return diagnostics
    if graphviz is None:
        diagnostics.append({
            "level": "warning",
            "code": "graphviz_unavailable",
            "message": "SFG calculations completed, but the private Graphviz renderer was not found.",
        })
        return diagnostics
    artifacts = result.setdefault("artifacts", {})
    for name, raw_path in graphs.items():
        dot_path = Path(raw_path)
        svg_path = dot_path.with_suffix(".svg")
        try:
            completed = subprocess.run(
                [graphviz, "-Tsvg", str(dot_path), "-o", str(svg_path)],
                check=False, capture_output=True, text=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            diagnostics.append({"level": "warning", "code": "graphviz_render_failed", "message": str(error)})
            continue
        if completed.returncode == 0 and svg_path.is_file():
            artifacts[svg_path.name] = str(svg_path)
        else:
            diagnostics.append({
                "level": "warning",
                "code": "graphviz_render_failed",
                "message": completed.stderr.strip() or f"Could not render {name}.",
            })
    return diagnostics


def _run_analysis(request: WorkerRequest) -> dict[str, Any]:
    from isaca_api.slicap_adapter import SLiCAP521Adapter

    _check_cancelled(request)
    emit_event(WorkerEvent(
        event="progress", stage="normalize", message="正在检查网表与参数。", progress=0.1
    ))
    analysis_request = AnalysisRequest.model_validate(request.payload.get("request", {}))
    emit_event(WorkerEvent(
        event="progress", stage="analysis", message="正在执行 SLiCAP 与 SFG 分析。", progress=0.2
    ))
    result = SLiCAP521Adapter(request.run_root).analyze(request.job_id, analysis_request)
    _check_cancelled(request)
    emit_event(WorkerEvent(
        event="progress", stage="render", message="正在整理图表与结果文件。", progress=0.9
    ))
    result.setdefault("diagnostics", []).extend(_render_dot_artifacts(result))
    manifest = result.get("artifacts", {}).get("result.json")
    if manifest:
        _write_json(Path(manifest), result)
    return result


def _run_export(request: WorkerRequest) -> dict[str, Any]:
    input_path = Path(str(request.payload["input_path"])).expanduser().resolve()
    output_path = Path(str(request.payload["output_path"])).expanduser().resolve()
    if not input_path.is_file() or input_path.suffix.lower() != ".slicap_sch":
        raise ValueError(f"Expected an existing .slicap_sch file: {input_path}")
    expected_hash = request.payload.get("input_sha256")
    def check_input():
        if expected_hash and hashlib.sha256(input_path.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError("原理图在提交导出后被修改；请保存当前图并重新运行。")
    check_input()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Invoke the same loader and netlist builder as the official CLI. Calling
    # them in-process keeps this worker functional after Nuitka packaging,
    # where ``sys.executable -m SLiCAP...`` would restart ISACA.exe instead of
    # a Python interpreter.
    from SLiCAP.schematic.cli import _load_scene, _qt_app, _write_netlist

    application = _qt_app()
    try:
        scene, data = _load_scene(input_path)
        title = data.properties.title or input_path.stem
        _write_netlist(input_path, scene, data, output_path, title)
    except SystemExit as error:
        message = str(error) if isinstance(error.code, str) else "SLiCAP 网表导出失败，请检查任务日志中的连接和参数诊断。"
        raise RuntimeError(message) from error
    check_input()
    if not output_path.is_file():
        raise RuntimeError("SLiCAP did not create the requested netlist.")
    return {
        "schematic_path": str(input_path),
        "netlist_path": str(output_path),
        "netlist_text": output_path.read_text(encoding="utf-8"),
    }


def execute_worker(request_path: str | Path) -> int:
    """Execute one request, always persisting either a result or an error."""

    path = Path(request_path).expanduser().resolve()
    request = WorkerRequest.model_validate_json(path.read_text(encoding="utf-8"))
    result_path = Path(request.result_path).expanduser().resolve()
    emit_event(WorkerEvent(
        event="started", stage=request.action, message=f"任务 {request.job_id} 已启动。", progress=0.0
    ))
    try:
        _check_cancelled(request)
        with redirect_stdout(sys.stderr):
            result = _run_analysis(request) if request.action == "analysis" else _run_export(request)
        _check_cancelled(request)
        envelope = {"status": "completed", "action": request.action, "result": result}
        _write_json(result_path, envelope)
        emit_event(WorkerEvent(
            event="completed", stage=request.action, message="任务完成。", progress=1.0,
            data={"result_path": str(result_path)},
        ))
        return 0
    except WorkerCancelled as error:
        _write_json(result_path, {"status": "cancelled", "action": request.action})
        emit_event(WorkerEvent(event="cancelled", stage=request.action, message=str(error)))
        return 2
    except Exception as error:
        envelope = {
            "status": "failed",
            "action": request.action,
            "error": str(error),
            "traceback": traceback.format_exc(),
        }
        _write_json(result_path, envelope)
        emit_event(WorkerEvent(
            event="failed", stage=request.action, message=str(error), progress=1.0
        ))
        return 1
