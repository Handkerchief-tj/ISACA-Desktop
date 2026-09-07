"""Versioned data contracts shared by the API and schematic editor."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import math

from pydantic import BaseModel, Field, field_validator


class DiagnosticLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Diagnostic(BaseModel):
    level: DiagnosticLevel
    code: str
    message: str
    location: str | None = None


class ParameterSource(StrEnum):
    INLINE = "inline"
    NETLIST = "netlist_param"
    USER = "user_override"
    SLICAP_DEFAULT = "slicap_default"
    UNRESOLVED = "unresolved"


class ParameterSpec(BaseModel):
    name: str
    expression: str
    numeric_value: float | None = None
    source: ParameterSource = ParameterSource.UNRESOLVED
    contexts: list[str] = Field(default_factory=list)
    unit: str | None = None
    required_for_numeric: bool = True
    default_model_value: str | None = None
    slicap_version: str | None = None


class Point(BaseModel):
    x: float
    y: float


class PinRef(BaseModel):
    component_id: str
    pin_id: str


class SchematicComponent(BaseModel):
    id: str
    refdes: str
    device: str
    position: Point
    rotation: int = 0
    model: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    control_ref: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    passthrough: dict[str, Any] = Field(default_factory=dict)


class SchematicWire(BaseModel):
    id: str
    source: PinRef
    target: PinRef
    waypoints: list[Point] = Field(default_factory=list)
    net_name: str | None = None


class AnalysisPorts(BaseModel):
    source: str | None = None
    detector: str | None = None
    lgref: str | None = None


class SchematicDocument(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = "Untitled circuit"
    components: list[SchematicComponent] = Field(default_factory=list)
    wires: list[SchematicWire] = Field(default_factory=list)
    parameters: dict[str, str] = Field(default_factory=dict)
    analysis: AnalysisPorts = Field(default_factory=AnalysisPorts)
    passthrough: dict[str, Any] = Field(default_factory=dict)


class CircuitDocument(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str
    input_kind: Literal["netlist", "internal_schematic", "slicap_schematic", "vision_ir"]
    netlist_text: str
    parameters: list[ParameterSpec] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class NormalizeRequest(BaseModel):
    netlist_text: str
    title: str | None = None
    parameter_overrides: dict[str, str | float] = Field(default_factory=dict)
    use_slicap_defaults: bool = False


class SchematicConvertRequest(BaseModel):
    schematic: SchematicDocument | None = None
    slicap_schematic: dict[str, Any] | None = None
    output_format: Literal["cir", "internal_json", "slicap_sch"] = "cir"


class SchematicConvertResponse(BaseModel):
    output_format: Literal["cir", "internal_json", "slicap_sch"]
    netlist_text: str | None = None
    schematic: SchematicDocument | None = None
    slicap_schematic: dict[str, Any] | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    netlist_text: str
    modes: list[Literal["laplace", "pz", "matrix", "noise", "bode", "symbolic"]] = Field(
        default_factory=lambda: ["laplace", "pz"]
    )
    parameter_overrides: dict[str, str | float] = Field(default_factory=dict)
    use_slicap_defaults: bool = False
    numeric: bool = True
    frequency_range_hz: tuple[float, float] | None = None
    magnitude_error_db: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    phase_error_deg: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    bode_points: int = Field(default=300, ge=32, le=5000)
    max_steps_per_subrange: int = Field(default=10, ge=0, le=100)

    @field_validator("frequency_range_hz")
    @classmethod
    def validate_frequency_range(cls, value):
        """Reject invalid ranges before launching an expensive analysis worker."""
        if value is not None and (not all(math.isfinite(x) for x in value) or not 0 < value[0] < value[1]):
            raise ValueError("frequency_range_hz must be a finite positive increasing pair")
        return value


class AnalysisJob(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    result: dict[str, Any] | None = None
    error: str | None = None
