"""FastAPI application joining netlists, schematics, SLiCAP and SFG."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from .catalog import DEVICE_CATALOG, device_catalog, symbol_bundle, symbol_svg
from .jobs import AnalysisJobManager
from .models import (
    AnalysisJob,
    AnalysisRequest,
    CircuitDocument,
    NormalizeRequest,
    SchematicConvertRequest,
    SchematicConvertResponse,
)
from .netlist import normalize_netlist
from .official_schematic import official_netlist_from_schematic
from .slicap_adapter import assert_slicap_version
from .slicap_schematic import internal_to_slicap_schematic, slicap_schematic_to_internal


def create_app(run_root: str | Path | None = None) -> FastAPI:
    """Create an application with an isolated, configurable analysis directory."""

    root = Path(run_root or os.environ.get("ISACA_RUN_ROOT", "runs"))
    manager = AnalysisJobManager(root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.shutdown()

    api = FastAPI(
        title="Intelligent Symbolic Analog Circuit Analyzer",
        version="0.1.0",
        description="Unified local API for SLiCAP 5.2.1 and SFG symbolic simplification.",
        lifespan=lifespan,
    )
    cors_origins = [
        origin.strip()
        for origin in os.environ.get("ISACA_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    api.state.job_manager = manager

    @api.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok", "slicap": assert_slicap_version()}

    @api.get("/api/v1/catalog/devices")
    def devices() -> dict:
        return {"slicap_version": "5.2.1", "devices": device_catalog()}

    @api.get("/api/v1/catalog/symbols/{symbol_name}.svg")
    def symbol_asset(symbol_name: str) -> Response:
        allowed = {item["symbol"] for item in DEVICE_CATALOG.values() if item.get("symbol")}
        if symbol_name not in allowed:
            raise HTTPException(status_code=404, detail="SLiCAP symbol not found.")
        content = symbol_svg(symbol_name)
        if content is None:
            raise HTTPException(status_code=404, detail="SLiCAP symbol not found.")
        return Response(content=content, media_type="image/svg+xml")

    @api.get("/api/v1/catalog/symbols/{library_name}")
    def symbol_library_asset(library_name: str) -> Response:
        content = symbol_bundle(library_name)
        if content is None:
            raise HTTPException(status_code=404, detail="SLiCAP symbol library not found.")
        return Response(content=content, media_type="image/svg+xml")

    @api.post("/api/v1/circuits/normalize", response_model=CircuitDocument)
    def normalize(request: NormalizeRequest) -> CircuitDocument:
        return normalize_netlist(request)

    @api.post("/api/v1/schematics/convert", response_model=SchematicConvertResponse)
    def convert(request: SchematicConvertRequest) -> SchematicConvertResponse:
        diagnostics = []
        schematic = request.schematic
        native_input = request.slicap_schematic
        if request.slicap_schematic is not None:
            if schematic is not None:
                raise HTTPException(status_code=422, detail="Provide one schematic input format, not two.")
            schematic, import_diagnostics = slicap_schematic_to_internal(request.slicap_schematic)
            diagnostics.extend(import_diagnostics)
        if schematic is None:
            raise HTTPException(status_code=422, detail="A schematic input is required.")

        if request.output_format == "internal_json":
            return SchematicConvertResponse(
                output_format="internal_json",
                schematic=schematic,
                diagnostics=diagnostics,
            )
        if request.output_format == "slicap_sch":
            if native_input is not None:
                native = native_input
            else:
                native, export_diagnostics = internal_to_slicap_schematic(schematic)
                diagnostics.extend(export_diagnostics)
            return SchematicConvertResponse(
                output_format="slicap_sch",
                slicap_schematic=native,
                diagnostics=diagnostics,
            )
        if native_input is not None:
            native = native_input
        else:
            native, export_diagnostics = internal_to_slicap_schematic(schematic)
            diagnostics.extend(export_diagnostics)
        if any(item.level == "error" for item in diagnostics):
            netlist = None
        else:
            netlist, official_diagnostics = official_netlist_from_schematic(
                native,
                root / "_schematic_exports",
            )
            diagnostics.extend(official_diagnostics)
        return SchematicConvertResponse(
            output_format="cir",
            netlist_text=netlist,
            schematic=schematic,
            diagnostics=diagnostics,
        )

    @api.post("/api/v1/analyses", response_model=AnalysisJob, status_code=202)
    def submit_analysis(request: AnalysisRequest) -> AnalysisJob:
        return manager.submit(request)

    @api.get("/api/v1/analyses/{job_id}", response_model=AnalysisJob)
    def analysis_status(job_id: str) -> AnalysisJob:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Analysis job not found.")
        return job

    @api.get("/api/v1/analyses/{job_id}/artifacts/{name:path}")
    def analysis_artifact(job_id: str, name: str) -> FileResponse:
        path = manager.artifact(job_id, name)
        if path is None:
            raise HTTPException(status_code=404, detail="Artifact not found.")
        return FileResponse(path)

    return api


app = create_app()
