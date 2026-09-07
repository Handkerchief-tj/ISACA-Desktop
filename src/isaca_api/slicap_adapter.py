"""Thread-safe, version-pinned boundary around SLiCAP 5.2.1 public APIs."""

from __future__ import annotations

import json
import locale
import math
import os
import threading
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable

import sympy as sp

from .models import AnalysisRequest, CircuitDocument
from .netlist import normalize_netlist
from .parameters import numeric_substitutions


SUPPORTED_SLICAP_VERSION = "5.2.1"
_SLICAP_LOCK = threading.RLock()


class SLiCAPAdapterError(RuntimeError):
    """Base error raised at the SLiCAP integration boundary."""


class SLiCAPVersionError(SLiCAPAdapterError):
    """Raised when the active SLiCAP package is not the pinned release."""


class MissingNumericParameters(SLiCAPAdapterError):
    """Raised before numeric analysis when required parameters are unresolved."""

    def __init__(self, names: Iterable[str]):
        self.names = tuple(sorted(set(names)))
        super().__init__("Numeric analysis requires values for: " + ", ".join(self.names))


def installed_slicap_version() -> str:
    """Return the installed SLiCAP distribution version without importing it."""

    try:
        return version("SLiCAP")
    except PackageNotFoundError as error:
        raise SLiCAPVersionError("SLiCAP is not installed in the active environment.") from error


def assert_slicap_version() -> str:
    """Require the exact SLiCAP release used by this adapter."""

    installed = installed_slicap_version()
    if installed != SUPPORTED_SLICAP_VERSION:
        raise SLiCAPVersionError(
            f"This application requires SLiCAP=={SUPPORTED_SLICAP_VERSION}; found {installed}."
        )
    return installed


@contextmanager
def _project_directory(path: Path):
    """Serialize SLiCAP global state and execute from one isolated project."""

    with _SLICAP_LOCK:
        previous = Path.cwd()
        path.mkdir(parents=True, exist_ok=True)
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, sp.MatrixBase):
        return [[_json_value(item) for item in row] for row in value.tolist()]
    if isinstance(value, sp.Basic):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_value(value.tolist())
    return str(value)


def _result_fields(result: Any, fields: Iterable[str]) -> dict[str, Any]:
    """Serialize expressions and their TeX in the worker, not the GUI thread."""

    values = {field: getattr(result, field) for field in fields if hasattr(result, field)}
    serialized = {field: _json_value(value) for field, value in values.items()}
    serialized["_latex"] = {
        field: sp.latex(value) for field, value in values.items()
        if isinstance(value, (sp.Basic, sp.MatrixBase))
    }
    return serialized


def _evaluate_symbolic(value: Any, substitutions: dict[str, float] | None = None) -> complex | None:
    """Evaluate a symbolic presentation value without changing its exact form."""

    try:
        expression = sp.sympify(value)
        values = {sp.Symbol(name): number for name, number in (substitutions or {}).items()}
        evaluated = complex(sp.N(expression.subs(values)))
    except (TypeError, ValueError, OverflowError, sp.SympifyError):
        return None
    if not math.isfinite(evaluated.real) or not math.isfinite(evaluated.imag):
        return None
    return evaluated


def _root_record(value: Any) -> dict[str, Any]:
    """Describe one numeric s-plane root in both rad/s and hertz."""

    root = _evaluate_symbolic(value)
    if root is None:
        return {"root": _json_value(value), "frequency_hz": None}
    scale = max(1.0, abs(root))
    if root.real < -1e-10 * scale:
        half_plane = "LHP"
    elif root.real > 1e-10 * scale:
        half_plane = "RHP"
    else:
        half_plane = "imaginary axis"
    return {
        "root": _json_value(value),
        "real_rad_s": root.real,
        "imag_rad_s": root.imag,
        "angular_frequency_rad_s": abs(root),
        "frequency_hz": abs(root) / (2.0 * math.pi),
        "half_plane": half_plane,
    }


def _root_cluster_record(cluster: Any) -> dict[str, Any]:
    """Serialize one closed-loop root cluster for the desktop frequency map."""

    roots = []
    for sample in getattr(cluster, "roots", ()):
        record = _root_record(getattr(sample, "value", None))
        record["kind"] = getattr(sample, "kind", "root")
        roots.append(record)
    return {
        "index": getattr(cluster, "index", None),
        "center_frequency_hz": getattr(cluster, "center_frequency_hz", None),
        "min_frequency_hz": getattr(cluster, "min_magnitude", 0.0) / (2.0 * math.pi),
        "max_frequency_hz": getattr(cluster, "max_magnitude", 0.0) / (2.0 * math.pi),
        "pole_count": getattr(cluster, "pole_count", 0),
        "zero_count": getattr(cluster, "zero_count", 0),
        "roots": roots,
    }


def _root_values(result: Any, field: str) -> list[Any]:
    """Convert SLiCAP list/tuple/NumPy root containers without truth testing."""

    values = getattr(result, field, None) if result is not None else None
    return list(values) if values is not None else []


def _tex_number(value: float) -> str:
    """Format one real number as compact engineering-friendly TeX."""

    if value == 0:
        return "0"
    exponent = int(math.floor(math.log10(abs(value))))
    if -2 <= exponent <= 3:
        return f"{value:.5g}"
    mantissa = value / 10.0**exponent
    return f"{mantissa:.5g}\\times 10^{{{exponent}}}"


def _normalized_factors(roots: Iterable[Any]) -> list[str] | None:
    """Build real first/second-order factors from numeric roots."""

    values: list[complex] = []
    for value in roots:
        root = _evaluate_symbolic(value)
        if root is None or abs(root) == 0:
            return None
        values.append(root)
    factors: list[str] = []
    used: set[int] = set()
    for index, root in enumerate(values):
        if index in used:
            continue
        tolerance = 1e-7 * max(1.0, abs(root))
        if abs(root.imag) <= tolerance:
            sign = "+" if root.real < 0 else "-"
            factors.append(rf"\left(1 {sign} \frac{{s}}{{{_tex_number(abs(root.real))}}}\right)")
            used.add(index)
            continue
        mate = next(
            (
                candidate for candidate in range(index + 1, len(values))
                if candidate not in used
                and abs(values[candidate] - root.conjugate()) <= tolerance
            ),
            None,
        )
        if mate is None:
            return None
        omega = abs(root)
        zeta = -root.real / omega
        middle_sign = "+" if zeta >= 0 else "-"
        factors.append(
            rf"\left[1 {middle_sign} {_tex_number(abs(2.0 * zeta))}"
            rf"\frac{{s}}{{{_tex_number(omega)}}}"
            rf" + \left(\frac{{s}}{{{_tex_number(omega)}}}\right)^2\right]"
        )
        used.update((index, mate))
    return factors


def _transfer_presentation(laplace: Any, pz: Any | None) -> dict[str, Any]:
    """Create readable views of an exact SLiCAP transfer result."""

    expression = getattr(laplace, "laplace", None)
    record: dict[str, Any] = {}
    if expression is not None:
        compact = sp.factor_terms(sp.cancel(sp.sympify(expression)))
        record["compact"] = _json_value(compact)
        record.setdefault("_latex", {})["compact"] = sp.latex(compact)
    dc_value = getattr(pz or laplace, "DCvalue", None)
    dc_numeric = _evaluate_symbolic(dc_value)
    if dc_value is not None:
        record["dc_gain"] = _json_value(dc_value)
        if isinstance(dc_value, sp.Basic):
            record.setdefault("_latex", {})["dc_gain"] = sp.latex(dc_value)
    if dc_numeric is not None:
        record["dc_gain_magnitude"] = abs(dc_numeric)
        record["dc_gain_db"] = 20.0 * math.log10(abs(dc_numeric)) if abs(dc_numeric) else None
        record["dc_gain_phase_deg"] = math.degrees(math.atan2(dc_numeric.imag, dc_numeric.real))
    poles = _root_values(pz, "poles")
    zeros = _root_values(pz, "zeros")
    numerator_factors = _normalized_factors(zeros)
    denominator_factors = _normalized_factors(poles)
    if dc_numeric is not None and numerator_factors is not None and denominator_factors is not None:
        if abs(dc_numeric.imag) <= 1e-12 * max(1.0, abs(dc_numeric)):
            gain_tex = _tex_number(dc_numeric.real)
        else:
            gain_tex = rf"\left({_tex_number(dc_numeric.real)}+j{_tex_number(dc_numeric.imag)}\right)"
        numerator = " ".join(numerator_factors) or "1"
        denominator = " ".join(denominator_factors) or "1"
        record["normalized"] = "H(s) = H(0) times normalized pole-zero factors"
        record.setdefault("_latex", {})["normalized"] = (
            rf"H(s) \approx {gain_tex}\,\frac{{{numerator}}}{{{denominator}}}"
        )
    return record


def _frequency_response_record(
    expression: Any,
    lower_frequency_hz: float,
    upper_frequency_hz: float,
    substitutions: dict[str, float] | None,
) -> dict[str, Any] | None:
    """Evaluate one frequency-local transfer at its geometric center."""

    if lower_frequency_hz <= 0 or upper_frequency_hz <= 0:
        return None
    frequency = math.sqrt(lower_frequency_hz * upper_frequency_hz)
    values = {sp.Symbol(name): number for name, number in (substitutions or {}).items()}
    values[sp.Symbol("s")] = 2j * math.pi * frequency
    try:
        response = complex(sp.N(sp.sympify(expression).subs(values)))
    except (TypeError, ValueError, OverflowError, sp.SympifyError):
        return None
    if not math.isfinite(response.real) or not math.isfinite(response.imag):
        return None
    magnitude = abs(response)
    return {
        "frequency_hz": frequency,
        "value": _json_value(response),
        "magnitude": magnitude,
        "magnitude_db": 20.0 * math.log10(magnitude) if magnitude else None,
        "phase_deg": math.degrees(math.atan2(response.imag, response.real)),
    }


def _symbolic_transfer_record(
    transfer_result: Any,
    substitutions: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Serialize one local symbolic transfer and its evaluated roots."""

    if transfer_result is None:
        return None
    transfer = _result_fields(transfer_result, (
        "transfer", "numerator", "denominator", "success", "error", "vertex_count", "edge_count",
    ))
    for kind in ("poles", "zeros"):
        transfer[kind] = []
        for root in getattr(transfer_result, kind, ()):
            root_record = _result_fields(root, ("expression", "multiplicity", "exact", "polynomial_degree"))
            evaluated = _evaluate_symbolic(getattr(root, "expression", None), substitutions)
            root_record["numeric_value"] = _json_value(evaluated) if evaluated is not None else None
            root_record["frequency_hz"] = abs(evaluated) / (2.0 * math.pi) if evaluated is not None else None
            transfer[kind].append(root_record)
    return transfer


def _subrange_record(item: Any, substitutions: dict[str, float] | None = None) -> dict[str, Any]:
    """Expose frequency-local results without serializing the whole graph object."""

    transfer = _symbolic_transfer_record(item.transfer, substitutions) or {}
    paper_style_transfer = _symbolic_transfer_record(
        getattr(item, "paper_style_transfer", None), substitutions
    )
    roots = []
    for root in item.target_root_approximations:
        root_record = _result_fields(root, (
            "kind", "category", "expression", "reference_frequency_hz", "frequency_hz", "relative_root_error",
            "status", "method", "location", "parameters",
        ))
        evaluated = _evaluate_symbolic(getattr(root, "expression", None), substitutions)
        root_record["numeric_value"] = _json_value(evaluated) if evaluated is not None else None
        root_record["evaluated_frequency_hz"] = abs(evaluated) / (2.0 * math.pi) if evaluated is not None else None
        roots.append(root_record)
    dominant = getattr(item, "dominant_term_transfer", None)
    dominant_record = None
    if dominant is not None:
        dominant_record = _result_fields(dominant, (
            "transfer", "numerator", "denominator", "full_term_count", "retained_term_count",
            "discarded_parameters", "representation", "corner_count", "corner_max_norm_error",
        ))
        dominant_record["error"] = _result_fields(dominant.nominal_error, (
            "max_relative_error", "max_magnitude_error_db", "max_phase_error_deg",
        ))
        dominant_record["parameter_influences"] = [_result_fields(parameter, (
            "parameter", "max_normalized_sensitivity", "peak_frequency_hz",
        )) for parameter in dominant.parameter_influences]
    return {
        "cluster_index": item.cluster_index,
        "lower_frequency_hz": item.lower_frequency_hz,
        "upper_frequency_hz": item.upper_frequency_hz,
        "transfer": transfer,
        "evaluation": _frequency_response_record(
            getattr(item.transfer, "transfer", None),
            item.lower_frequency_hz,
            item.upper_frequency_hz,
            substitutions,
        ),
        "dominant_term_transfer": dominant_record,
        "paper_style_transfer": paper_style_transfer,
        "target_roots": roots,
        "error": _result_fields(item.error, (
            "max_relative_error", "max_magnitude_error_db", "max_phase_error_deg",
        )),
    }


def _root_frequency_hz(value: Any) -> float | None:
    """Return the absolute root frequency in hertz when it is finite."""

    try:
        frequency = abs(complex(value)) / (2.0 * 3.141592653589793)
    except (TypeError, ValueError, OverflowError):
        return None
    return frequency if frequency > 0 and frequency < float("inf") else None


def _bode_frequency_range(request: AnalysisRequest, pz: Any) -> tuple[float, float]:
    """Choose a deterministic plot range from user input or computed roots."""

    if request.frequency_range_hz is not None:
        lower, upper = map(float, request.frequency_range_hz)
        if lower <= 0 or upper <= lower:
            raise SLiCAPAdapterError("frequency_range_hz must be a positive increasing pair.")
        return lower, upper
    poles = getattr(pz, "poles", None)
    zeros = getattr(pz, "zeros", None)
    roots = list(poles) if poles is not None else []
    roots.extend(list(zeros) if zeros is not None else [])
    frequencies = [item for item in (_root_frequency_hz(root) for root in roots) if item is not None]
    if not frequencies:
        return 1.0, 1.0e9
    return max(min(frequencies) / 100.0, 1.0e-6), max(frequencies) * 100.0


def _write_bode_artifact(
    expression: Any,
    output_path: Path,
    frequency_range_hz: tuple[float, float],
    points: int,
) -> dict[str, Any]:
    """Sample one numeric Laplace expression and save an offline SVG plot."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    frequencies = np.geomspace(frequency_range_hz[0], frequency_range_hz[1], points)
    s = sp.Symbol("s")
    try:
        evaluator = sp.lambdify(s, sp.sympify(expression), modules="numpy")
        values = np.asarray(evaluator(2j * np.pi * frequencies), dtype=complex)
        if values.ndim == 0:
            values = np.full(frequencies.shape, values, dtype=complex)
        values = np.broadcast_to(values, frequencies.shape)
    except Exception as error:
        raise SLiCAPAdapterError(f"Cannot evaluate the transfer function for the Bode plot: {error}") from error
    magnitude = 20.0 * np.log10(np.maximum(np.abs(values), np.finfo(float).tiny))
    phase = np.unwrap(np.angle(values)) * 180.0 / np.pi
    figure, axes = plt.subplots(2, 1, figsize=(8.0, 6.0), sharex=True, constrained_layout=True)
    axes[0].semilogx(frequencies, magnitude, color="#185b86", linewidth=1.6)
    axes[0].set_ylabel("Magnitude (dB)")
    axes[0].grid(True, which="both", color="#d8d8d8", linewidth=0.5)
    axes[1].semilogx(frequencies, phase, color="#185b86", linewidth=1.6)
    axes[1].set_xlabel("Frequency (Hz)")
    axes[1].set_ylabel("Phase (deg)")
    axes[1].grid(True, which="both", color="#d8d8d8", linewidth=0.5)
    figure.savefig(output_path, format="svg")
    plt.close(figure)
    sample_indices = np.unique(np.linspace(0, points - 1, min(points, 80), dtype=int))
    return {
        "frequency_range_hz": list(map(float, frequency_range_hz)),
        "points": int(points),
        "samples": [
            {
                "frequency_hz": float(frequencies[index]),
                "magnitude_db": float(magnitude[index]),
                "phase_deg": float(phase[index]),
            }
            for index in sample_indices
        ],
        "artifact": str(output_path),
    }


def _write_slicap_working_netlist(path: Path, text: str) -> tuple[str, int, bool]:
    """Write the parser copy using the platform encoding used by SLiCAP's open()."""

    lines = text.splitlines()
    title_sanitized = bool(lines and any(ord(char) > 127 for char in lines[0]))
    if title_sanitized:
        lines[0] = '"ISACA imported circuit"'
        text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    encoding = locale.getpreferredencoding(False) or "utf-8"
    encoded = text.encode(encoding, errors="replace")
    recovered = encoded.decode(encoding)
    replacements = sum(left != right for left, right in zip(text, recovered))
    replacements += abs(len(text) - len(recovered))
    path.write_bytes(encoded)
    return encoding, replacements, title_sanitized


def _circuit_summary(circuit: Any) -> dict[str, Any]:
    elements = {}
    for refdes, element in sorted(getattr(circuit, "elements", {}).items()):
        elements[str(refdes)] = {
            "model": str(getattr(element, "model", "")),
            "nodes": list(getattr(element, "nodes", [])),
            "params": _json_value(getattr(element, "params", {})),
        }
    return {
        "title": str(getattr(circuit, "title", "")),
        "source": _json_value(getattr(circuit, "source", None)),
        "detector": _json_value(getattr(circuit, "detector", None)),
        "nodes": sorted(str(node) for node in getattr(circuit, "nodes", [])),
        "parameter_definitions": _json_value(getattr(circuit, "parDefs", {})),
        "elements": elements,
    }


def _make_circuit(sl: Any) -> Any:
    """Call SLiCAP and translate its opaque unknown-model exception."""

    try:
        return sl.makeCircuit("input.cir", imgWidth=None, expansion=False)
    except KeyError as error:
        # SLiCAP 5.2.1 raises KeyError(False) after reporting an unknown
        # device model. Translate that implementation detail at our boundary.
        if error.args == (False,):
            raise SLiCAPAdapterError(
                "SLiCAP rejected an undefined or invalid element model; "
                "check device model names and required .model definitions."
            ) from error
        raise


class SLiCAP521Adapter:
    """Run numeric and symbolic analyses in isolated per-job directories."""

    def __init__(self, run_root: str | Path):
        self.run_root = Path(run_root).resolve()
        self.version = assert_slicap_version()

    def prepare_document(self, request: AnalysisRequest) -> CircuitDocument:
        """Normalize input and resolve parameter provenance before execution."""

        from .models import NormalizeRequest

        return normalize_netlist(
            NormalizeRequest(
                netlist_text=request.netlist_text,
                parameter_overrides=request.parameter_overrides,
                use_slicap_defaults=request.use_slicap_defaults,
            )
        )

    def analyze(self, job_id: str, request: AnalysisRequest) -> dict[str, Any]:
        """Execute requested SLiCAP and SFG analyses and return structured data."""

        document = self.prepare_document(request)
        substitutions, missing = numeric_substitutions(document.parameters)
        numeric_modes = {"laplace", "pz", "matrix", "noise", "bode"}
        if missing and ((request.numeric and numeric_modes.intersection(request.modes)) or "symbolic" in request.modes):
            raise MissingNumericParameters(missing)
        errors = [item.message for item in document.diagnostics if item.level == "error"]
        if errors:
            raise SLiCAPAdapterError("; ".join(errors))

        project_dir = self.run_root / job_id
        cir_dir = project_dir / "cir"
        artifacts_dir = project_dir / "artifacts"
        cir_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        netlist_path = cir_dir / "input.cir"
        working_encoding, replacements, title_sanitized = _write_slicap_working_netlist(
            netlist_path, document.netlist_text
        )

        result: dict[str, Any] = {
            "software": {
                "slicap": self.version,
                "working_netlist_encoding": working_encoding,
                "transcoding_replacements": replacements,
                "title_sanitized": title_sanitized,
            },
            "circuit": document.model_dump(mode="json"),
            "analyses": {},
            "artifacts": {},
            "diagnostics": [item.model_dump(mode="json") for item in document.diagnostics],
        }
        with _project_directory(project_dir):
            import SLiCAP as sl

            sl.initProject(f"ISACA analysis {job_id}", report_dirs=True)
            circuit = _make_circuit(sl)
            if circuit is None:
                raise SLiCAPAdapterError("SLiCAP did not return a circuit object.")
            circuit_errors = int(getattr(circuit, "errors", 0) or 0)
            if circuit_errors:
                raise SLiCAPAdapterError(f"SLiCAP reported {circuit_errors} circuit error(s).")
            result["flattened_circuit"] = _circuit_summary(circuit)
            pardefs = {sp.Symbol(name): sp.Float(value) for name, value in substitutions.items()}
            numeric = bool(request.numeric)

            laplace = None
            pz = None
            if "laplace" in request.modes or "bode" in request.modes:
                laplace = sl.doLaplace(circuit, pardefs=pardefs or None, numeric=numeric)
                result["analyses"]["laplace"] = _result_fields(
                    laplace,
                    ("laplace", "numer", "denom", "DCvalue", "M", "Iv", "Dv"),
                )
            if "pz" in request.modes or "bode" in request.modes:
                pz = sl.doPZ(circuit, pardefs=pardefs or None, numeric=numeric)
                pz_record = _result_fields(
                    pz,
                    ("poles", "zeros", "DCvalue", "laplace", "numer", "denom"),
                )
                pz_record["pole_records"] = [_root_record(root) for root in _root_values(pz, "poles")]
                pz_record["zero_records"] = [_root_record(root) for root in _root_values(pz, "zeros")]
                result["analyses"]["pz"] = pz_record
            if laplace is not None:
                result["analyses"]["laplace"]["presentation"] = _transfer_presentation(laplace, pz)
            elif pz is not None:
                result["analyses"]["pz"]["presentation"] = _transfer_presentation(pz, pz)
            if "matrix" in request.modes:
                matrix = sl.doMatrix(circuit, pardefs=pardefs or None, numeric=numeric)
                result["analyses"]["matrix"] = _result_fields(matrix, ("M", "Iv", "Dv"))
            if "noise" in request.modes:
                noise = sl.doNoise(circuit, pardefs=pardefs or None, numeric=numeric)
                result["analyses"]["noise"] = _result_fields(
                    noise,
                    ("onoise", "inoise", "onoiseTerms", "inoiseTerms"),
                )

            if "bode" in request.modes:
                if not numeric:
                    raise SLiCAPAdapterError("Bode analysis requires numeric=True.")
                bode_path = artifacts_dir / "bode.svg"
                result["analyses"]["bode"] = _write_bode_artifact(
                    getattr(laplace, "laplace"),
                    bode_path,
                    _bode_frequency_range(request, pz),
                    request.bode_points,
                )
                result["artifacts"]["bode.svg"] = str(bode_path)

            if "symbolic" in request.modes:
                result["analyses"]["symbolic"] = self._run_symbolic(
                    netlist_path,
                    artifacts_dir,
                    request,
                    substitutions,
                )
                symbolic = result["analyses"]["symbolic"]
                result["artifacts"].update(symbolic["reports"])
                result["artifacts"].update(symbolic["graphs"])
                for interval in symbolic["frequency_results"]:
                    for root in interval["target_roots"]:
                        if root.get("status") != "resolved":
                            result["diagnostics"].append({
                                "level": "warning", "code": "symbolic_root_unresolved",
                                "message": f"Cluster {interval['cluster_index']} {root.get('kind')}: "
                                           f"{root.get('status')}; root-location deviation={root.get('relative_root_error')}. "
                                           "No local symbolic explanation was found; subrange transfer acceptance is reported separately.",
                            })

        input_copy = artifacts_dir / "normalized.cir"
        input_copy.write_text(document.netlist_text, encoding="utf-8")
        result["artifacts"]["normalized.cir"] = str(input_copy)
        manifest = artifacts_dir / "result.json"
        result["artifacts"]["result.json"] = str(manifest)
        manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return result

    def _run_symbolic(
        self,
        netlist_path: Path,
        artifacts_dir: Path,
        request: AnalysisRequest,
        substitutions: dict[str, float],
    ) -> dict[str, Any]:
        """Run the bundled SFG engine and persist its reports."""

        try:
            from sfg_prototype import (
                SimplificationConfig,
                error_trace_report,
                operation_ranking_report,
                root_localization_summary_report,
                simplification_report,
                simplify_netlist,
                subrange_simplification_report,
            )
        except ImportError as error:
            raise SLiCAPAdapterError(
                "The bundled sfg_prototype package is unavailable. Reinstall ISACA Desktop."
            ) from error

        config = SimplificationConfig(
            frequency_range_hz=request.frequency_range_hz,
            magnitude_error_db=request.magnitude_error_db,
            phase_error_deg=request.phase_error_deg,
            max_steps_per_subrange=request.max_steps_per_subrange,
        )
        simplified = simplify_netlist(
            str(netlist_path),
            config=config,
            substitutions=substitutions,
        )
        reports = {
            "simplification.md": simplification_report(simplified),
            "subrange_simplification.md": subrange_simplification_report(simplified),
            "operation_ranking.md": operation_ranking_report(simplified),
            "error_trace.md": error_trace_report(simplified),
            "root_localization.md": root_localization_summary_report(simplified),
        }
        graph_files: dict[str, str] = {}
        original_dot = artifacts_dir / "sfg_original.dot"
        original_dot.write_text(simplified.pipeline.graph.to_dot(), encoding="utf-8")
        graph_files[original_dot.name] = str(original_dot)
        final_dot = artifacts_dir / "sfg_final.dot"
        final_dot.write_text(simplified.final_graph.to_dot(), encoding="utf-8")
        graph_files[final_dot.name] = str(final_dot)
        for item in simplified.subrange_results:
            graph_path = artifacts_dir / f"sfg_cluster_{item.cluster_index}.dot"
            graph_path.write_text(item.simplified_graph.to_dot(), encoding="utf-8")
            graph_files[graph_path.name] = str(graph_path)
        paths: dict[str, str] = {}
        for name, content in reports.items():
            path = artifacts_dir / name
            path.write_text(content, encoding="utf-8")
            paths[name] = str(path)
        return {
            "accepted_steps": len(simplified.accepted_steps),
            "rejected_steps": len(simplified.rejected_steps),
            "subranges": len(simplified.subrange_results),
            "root_clusters": [
                _root_cluster_record(cluster) for cluster in simplified.pipeline.clusters
            ],
            "reports": paths,
            "graphs": graph_files,
            "frequency_results": [_subrange_record(item, substitutions) for item in simplified.subrange_results],
            "final_global_error": _result_fields(simplified.final_error, (
                "max_relative_error", "max_magnitude_error_db", "max_phase_error_deg",
            )),
        }
