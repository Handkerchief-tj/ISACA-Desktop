"""Strict, provenance-aware handling of symbolic netlist parameters."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Iterable

import sympy as sp

from .catalog import DEVICE_CATALOG
from .models import ParameterSource, ParameterSpec


_SCALE_FACTORS = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
    "a": 1e-18,
}
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SCALED_NUMBER = re.compile(rf"(?<![A-Za-z0-9_\.])({_NUMBER})(meg|[TtGgKkMmUuNnPpFfAa])(?![A-Za-z0-9_])")
_PARAM_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)\s*=\s*(\{[^}]*\}|[^\s]+)")
_BRACED = re.compile(r"\{([^{}]+)\}")


def expand_scale_factors(expression: str) -> str:
    """Convert SLiCAP engineering suffixes into explicit decimal factors."""

    def replace(match: re.Match[str]) -> str:
        number, suffix = match.groups()
        factor = _SCALE_FACTORS[suffix.lower()]
        return f"({number}*{factor:.17g})"

    return _SCALED_NUMBER.sub(replace, expression)


def parse_expression(value: str | float | int) -> sp.Expr:
    """Parse a SLiCAP-style numeric or symbolic expression with SymPy."""

    if isinstance(value, (float, int)):
        return sp.sympify(value)
    text = str(value).strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return sp.sympify(expand_scale_factors(text), evaluate=True)


def _logical_lines(netlist_text: str) -> list[str]:
    """Join SLiCAP continuation lines that start with `+`."""

    logical: list[str] = []
    current: str | None = None
    for raw_line in netlist_text.splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("+") and current is not None:
            current += " " + line[1:].strip()
            continue
        if current is not None:
            logical.append(current)
        current = line
    if current is not None:
        logical.append(current)
    return logical


def extract_param_definitions(netlist_text: str) -> dict[str, str]:
    """Read all `.param name=value` assignments from a netlist."""

    definitions: dict[str, str] = {}
    for line in _logical_lines(netlist_text):
        if not line.lower().startswith(".param"):
            continue
        for name, value in _PARAM_ASSIGNMENT.findall(line[6:]):
            definitions[name] = value.strip("{}")
    return definitions


def _element_contexts(
    netlist_text: str,
) -> tuple[
    dict[str, set[str]],
    dict[str, list[tuple[str, str]]],
    list[tuple[str, str, str]],
]:
    contexts: dict[str, set[str]] = defaultdict(set)
    defaults: dict[str, list[tuple[str, str]]] = defaultdict(list)
    inline_values: list[tuple[str, str, str]] = []
    for line in _logical_lines(netlist_text):
        if not line or line.startswith(("*", ".")):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        refdes = tokens[0]
        model = next((token for token in tokens[1:] if token in DEVICE_CATALOG), None)
        if model is None and refdes[0].upper() in DEVICE_CATALOG:
            model = refdes[0].upper()
        for key, raw_value in _PARAM_ASSIGNMENT.findall(line):
            value = raw_value.strip("{}")
            try:
                parsed = parse_expression(value)
                symbols = parsed.free_symbols
            except (TypeError, ValueError, SyntaxError, sp.SympifyError):
                symbols = set()
                parsed = None
            if parsed is not None and not symbols:
                inline_values.append((f"{refdes}.{key}", value, f"{refdes}.{key}"))
            for symbol in symbols:
                contexts[str(symbol)].add(f"{refdes}.{key}")
                if model in DEVICE_CATALOG:
                    default = DEVICE_CATALOG[model].get("slicap_defaults", {}).get(key)
                    if default is not None and value == str(symbol):
                        defaults[str(symbol)].append((str(default), f"{model}.{key}"))
    return contexts, defaults, inline_values


def _required_for_numeric(contexts: set[str]) -> bool:
    """Return False for symbols used only as independent-source stimuli."""

    if not contexts:
        return True
    for context in contexts:
        refdes, _, parameter = context.partition(".")
        if refdes[:1].upper() not in {"V", "I"} or parameter.lower() not in {
            "value",
            "dc",
            "noise",
        }:
            return True
    return False


def _resolve_numeric(name: str, definitions: dict[str, str], seen: set[str] | None = None) -> float | None:
    seen = set() if seen is None else set(seen)
    if name in seen or name not in definitions:
        return None
    seen.add(name)
    try:
        expression = parse_expression(definitions[name])
    except (TypeError, ValueError, SyntaxError, sp.SympifyError):
        return None
    substitutions: dict[sp.Symbol, float] = {}
    for symbol in expression.free_symbols:
        resolved = _resolve_numeric(str(symbol), definitions, seen)
        if resolved is None:
            return None
        substitutions[symbol] = resolved
    try:
        value = complex(sp.N(expression.subs(substitutions)))
    except (TypeError, ValueError):
        return None
    if abs(value.imag) > 1e-12 or not math.isfinite(value.real):
        return None
    return float(value.real)


def collect_parameter_specs(
    netlist_text: str,
    overrides: dict[str, str | float] | None = None,
    use_slicap_defaults: bool = False,
    slicap_version: str = "5.2.1",
) -> list[ParameterSpec]:
    """Collect parameters with values and provenance; never silently use one."""

    overrides = overrides or {}
    definitions = extract_param_definitions(netlist_text)
    contexts, default_candidates, inline_values = _element_contexts(netlist_text)
    symbols: set[str] = set(definitions)
    for expression in _BRACED.findall(netlist_text):
        try:
            symbols.update(str(symbol) for symbol in parse_expression(expression).free_symbols)
        except (TypeError, ValueError, SyntaxError, sp.SympifyError):
            continue

    effective = {**definitions, **{name: str(value) for name, value in overrides.items()}}
    specs: list[ParameterSpec] = []
    for name in sorted(symbols):
        default_value = default_candidates[name][0][0] if default_candidates.get(name) else None
        if name in overrides:
            expression = str(overrides[name])
            source = ParameterSource.USER
            numeric = _resolve_numeric(name, effective)
        elif name in definitions:
            expression = definitions[name]
            source = ParameterSource.NETLIST
            numeric = _resolve_numeric(name, effective)
        elif use_slicap_defaults and default_value is not None:
            expression = default_value
            source = ParameterSource.SLICAP_DEFAULT
            numeric = float(parse_expression(default_value))
        else:
            expression = name
            source = ParameterSource.UNRESOLVED
            numeric = None
        specs.append(
            ParameterSpec(
                name=name,
                expression=expression,
                numeric_value=numeric,
                source=source,
                contexts=sorted(contexts.get(name, set())),
                required_for_numeric=_required_for_numeric(contexts.get(name, set())),
                default_model_value=default_value,
                slicap_version=slicap_version if source == ParameterSource.SLICAP_DEFAULT else None,
            )
        )
    for name, expression, context in inline_values:
        try:
            numeric = float(parse_expression(expression))
        except (TypeError, ValueError, sp.SympifyError):
            continue
        specs.append(
            ParameterSpec(
                name=name,
                expression=expression,
                numeric_value=numeric,
                source=ParameterSource.INLINE,
                contexts=[context],
            )
        )
    return sorted(specs, key=lambda item: (item.name, item.source.value))


def numeric_substitutions(specs: Iterable[ParameterSpec]) -> tuple[dict[str, float], list[str]]:
    """Split parameter specs into usable substitutions and unresolved names."""

    substitutions: dict[str, float] = {}
    missing: list[str] = []
    for spec in specs:
        if spec.numeric_value is None:
            if spec.required_for_numeric:
                missing.append(spec.name)
        elif spec.source != ParameterSource.INLINE:
            substitutions[spec.name] = spec.numeric_value
    return substitutions, missing
