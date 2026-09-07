#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helpers for obtaining a flattened, tool-independent network model from SLiCAP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sympy as sp

from .slicap_compat import (
    check_circuit as _checkCircuit,
    circuit_type as circuit,
    full_subs as fullSubs,
    ini,
    initialize_parser as _initializeParser,
    replace_scale_factors as _replaceScaleFactors,
    yacc_module as _yacc_module,
)

_PARSER_READY = False


def _ensure_parser_ready() -> None:
    """
    Initialize the SLiCAP parser once so built-in libraries are available.
    """
    global _PARSER_READY
    if not _PARSER_READY:
        _initializeParser()
        _PARSER_READY = True


@dataclass(frozen=True)
class LinearElement:
    """
    Normalized copy of a flattened SLiCAP element.
    """

    refdes: str
    model: str
    element_type: str
    nodes: tuple[str, ...]
    refs: tuple[str, ...]
    params: dict[str, Any]


@dataclass(frozen=True)
class NumericExpression:
    """
    Pair a symbolic expression with its value after numeric parameter substitution.
    """

    symbolic: sp.Expr
    numeric: sp.Expr
    remaining_symbols: tuple[sp.Symbol, ...]
    status: str

    def to_dict(self, notation: str = "exact", precision: int = 6) -> dict[str, Any]:
        """
        Return a JSON-friendly representation for reports and debugging.
        """
        return {
            "symbolic": sp.sstr(self.symbolic),
            "numeric": format_numeric_expression(self.numeric, notation=notation, precision=precision),
            "remaining_symbols": [sp.sstr(symbol) for symbol in self.remaining_symbols],
            "status": self.status,
        }


@dataclass
class LinearNetwork:
    """
    Tool-independent representation of the flattened small-signal network N.
    """

    title: str | None
    file_path: str | None
    elements: dict[str, LinearElement]
    nodes: list[str]
    dep_vars: list[str]
    indep_vars: list[str]
    controlled: list[str]
    references: list[str]
    par_defs: dict[sp.Symbol, Any]
    params: list[sp.Symbol]
    source: list[str | None] | None = None
    detector: list[str | None] | None = None
    lg_ref: list[str | None] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def active_nodes(self) -> list[str]:
        """
        Circuit nodes except the global reference node.
        """
        return [node for node in self.nodes if node != "0"]

    def summary(self) -> dict[str, int]:
        """
        Compact counts that are convenient during debugging.
        """
        return {
            "elements": len(self.elements),
            "nodes": len(self.nodes),
            "active_nodes": len(self.active_nodes),
            "dep_vars": len(self.dep_vars),
            "indep_vars": len(self.indep_vars),
            "controlled": len(self.controlled),
            "params": len(self.params),
            "par_defs": len(self.par_defs),
        }

    def numeric_substitutions(
        self,
        extra: dict[str | sp.Symbol, Any] | None = None,
        recursive: bool = True,
        numeric: bool = False,
    ) -> dict[sp.Symbol, sp.Expr]:
        """
        Return numeric parameter definitions from ``.param`` plus optional overrides.

        The original symbolic element expressions are not changed. This method only
        builds a substitution table that later numeric algorithms can apply.
        """
        substitutions: dict[sp.Symbol, sp.Expr] = {
            _symbol_key(key): _parse_parameter_value(value)
            for key, value in self.par_defs.items()
        }
        if extra:
            for key, value in extra.items():
                substitutions[_symbol_key(key)] = _parse_parameter_value(value)
        if recursive:
            substitutions = {
                key: sp.sympify(fullSubs(value, substitutions), rational=True)
                for key, value in substitutions.items()
            }
        if numeric:
            substitutions = {key: sp.N(value) for key, value in substitutions.items()}
        return substitutions

    def substitute_numeric(
        self,
        expr: Any,
        extra: dict[str | sp.Symbol, Any] | None = None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> NumericExpression:
        """
        Substitute known numeric parameter values into one symbolic expression.
        """
        substitutions = self.numeric_substitutions(extra=extra, numeric=numeric)
        return evaluate_numeric_expression(expr, substitutions, keep_laplace=keep_laplace, numeric=numeric)

    def element_numeric_params(
        self,
        refdes: str,
        extra: dict[str | sp.Symbol, Any] | None = None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> dict[str, NumericExpression]:
        """
        Return symbolic and numeric views of all parameters of one element.
        """
        if refdes not in self.elements:
            raise KeyError(f"Unknown element reference designator: {refdes}")
        return {
            name: self.substitute_numeric(value, extra=extra, keep_laplace=keep_laplace, numeric=numeric)
            for name, value in self.elements[refdes].params.items()
        }

    def all_element_numeric_params(
        self,
        extra: dict[str | sp.Symbol, Any] | None = None,
        keep_laplace: bool = True,
        numeric: bool = False,
    ) -> dict[str, dict[str, NumericExpression]]:
        """
        Return numeric parameter views for every flattened element.
        """
        return {
            refdes: self.element_numeric_params(
                refdes,
                extra=extra,
                keep_laplace=keep_laplace,
                numeric=numeric,
            )
            for refdes in self.elements
        }


def network_from_circuit(cir: circuit, file_path: str | os.PathLike[str] | None = None) -> LinearNetwork:
    """
    Copy a flattened SLiCAP circuit object into a stable intermediate model.
    """
    elements: dict[str, LinearElement] = {}
    for refdes, element in cir.elements.items():
        model = element.model if isinstance(element.model, str) else getattr(element.model, "title", str(element.model))
        params = {str(key): value for key, value in element.params.items()}
        elements[refdes] = LinearElement(
            refdes=refdes,
            model=str(model),
            element_type=str(element.type),
            nodes=tuple(str(node) for node in element.nodes),
            refs=tuple(str(ref) for ref in element.refs),
            params=params,
        )
    return LinearNetwork(
        title=cir.title,
        file_path=str(file_path) if file_path is not None else getattr(cir, "file", None),
        elements=elements,
        nodes=[str(node) for node in cir.nodes],
        dep_vars=[str(var) for var in cir.dep_vars],
        indep_vars=[str(var) for var in cir.indepVars],
        controlled=[str(var) for var in cir.controlled],
        references=[str(ref) for ref in cir.references],
        par_defs={sp.Symbol(str(key)): value for key, value in cir.parDefs.items()},
        params=[sp.Symbol(str(par)) for par in cir.params],
        source=None if cir.source is None else [None if item is None else str(item) for item in cir.source],
        detector=None if cir.detector is None else [None if item is None else str(item) for item in cir.detector],
        lg_ref=None if cir.lgRef is None else [None if item is None else str(item) for item in cir.lgRef],
    )


def flatten_netlist(file_path: str | os.PathLike[str]) -> LinearNetwork:
    """
    Parse a netlist file with SLiCAP and return the flattened network model N.

    The helper temporarily points ``ini.cir_path`` to the netlist directory so
    the existing SLiCAP parser can be reused without changing its internals.
    """
    _ensure_parser_ready()
    netlist_path = Path(file_path).expanduser().resolve()
    if not netlist_path.exists():
        raise FileNotFoundError(netlist_path)
    if netlist_path.suffix.lower() != ".cir":
        raise ValueError(f"Expected a .cir file, got: {netlist_path.name}")
    previous_cir_path = ini.cir_path
    previous_html_page = _yacc_module.htmlPage
    try:
        ini.cir_path = str(netlist_path.parent) + os.sep
        # _checkCircuit() normally creates HTML report pages as a side effect.
        # For a programmatic API we suppress that behavior, because callers may
        # only want parsing/flattening and may not have an initialized SLiCAP
        # project directory with an ``html/`` folder.
        _yacc_module.htmlPage = lambda *args, **kwargs: None
        cir = _checkCircuit(netlist_path.name)
    finally:
        ini.cir_path = previous_cir_path
        _yacc_module.htmlPage = previous_html_page
    if cir.elements and (not cir.nodes or not cir.dep_vars):
        raise RuntimeError(
            "SLiCAP failed to complete circuit data update. Check the netlist errors printed above; "
            "common causes are unknown model names or model parameters with the wrong case."
        )
    cir.file = str(netlist_path)
    return network_from_circuit(cir, netlist_path)


def evaluate_numeric_expression(
    expr: Any,
    substitutions: dict[str | sp.Symbol, Any] | None = None,
    keep_laplace: bool = True,
    numeric: bool = False,
) -> NumericExpression:
    """
    Apply a numeric substitution table to an expression without losing symbols.
    """
    symbolic = _parse_parameter_value(expr)
    sub_map = {
        _symbol_key(key): _parse_parameter_value(value)
        for key, value in (substitutions or {}).items()
    }
    substituted = sp.sympify(fullSubs(symbolic, sub_map), rational=True)
    substituted = sp.simplify(substituted)
    if numeric:
        substituted = sp.N(substituted)
    ignored = _analysis_symbols() if keep_laplace else set()
    original_symbols = _relevant_symbols(symbolic, ignored)
    remaining_symbols = tuple(sorted(_relevant_symbols(substituted, ignored), key=sp.sstr))
    if not remaining_symbols:
        status = "fully_numeric_except_analysis_vars" if keep_laplace else "fully_numeric"
    elif set(remaining_symbols) == original_symbols:
        status = "symbolic_only"
    else:
        status = "partially_numeric"
    return NumericExpression(
        symbolic=symbolic,
        numeric=substituted,
        remaining_symbols=remaining_symbols,
        status=status,
    )


def format_numeric_expression(expr: Any, notation: str = "exact", precision: int = 6) -> str:
    """
    Format an expression for reports without changing the stored exact value.
    """
    value = sp.sympify(expr)
    if notation == "exact":
        return sp.sstr(value)
    if notation not in {"scientific", "float"}:
        raise ValueError("notation must be 'exact', 'scientific', or 'float'.")
    digits = max(1, int(precision))
    evaluated = sp.N(value, digits)
    if notation == "float":
        return sp.sstr(evaluated)
    return _format_scientific(evaluated, digits)


def _parse_parameter_value(value: Any) -> sp.Expr:
    """
    Parse values with SLiCAP metric suffixes before converting to SymPy.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1]
        text = _replaceScaleFactors(text)
        return sp.sympify(text, rational=True)
    return sp.sympify(value, rational=True)


def _format_scientific(expr: sp.Expr, precision: int) -> str:
    """
    Convert numeric atoms in an expression to compact scientific notation text.
    """
    expr = sp.sympify(expr)
    if not expr.free_symbols:
        return _format_number_scientific(expr, precision)
    replacements = {}
    for atom in sorted(expr.atoms(sp.Number), key=sp.sstr):
        if atom in {sp.Integer(-1), sp.Integer(0), sp.Integer(1)}:
            continue
        replacements[atom] = sp.Symbol(_format_number_scientific(atom, precision))
    return sp.sstr(expr.xreplace(replacements))


def _format_number_scientific(value: sp.Expr, precision: int) -> str:
    """
    Format one numeric SymPy atom as ``1.23e-4`` style text.
    """
    number = complex(sp.N(value, precision))
    if abs(number.imag) > 0:
        real = f"{number.real:.{precision}e}"
        imag = f"{abs(number.imag):.{precision}e}"
        sign = "+" if number.imag >= 0 else "-"
        return f"({real}{sign}{imag}j)"
    return f"{number.real:.{precision}e}"


def _symbol_key(key: str | sp.Symbol) -> sp.Symbol:
    """
    Normalize substitution keys to SymPy symbols.
    """
    return key if isinstance(key, sp.Symbol) else sp.Symbol(str(key))


def _analysis_symbols() -> set[sp.Symbol]:
    """
    Return symbols that describe the analysis variable rather than parameters.
    """
    symbols = {ini.laplace}
    frequency = getattr(ini, "frequency", None)
    if isinstance(frequency, sp.Symbol):
        symbols.add(frequency)
    return symbols


def _relevant_symbols(expr: sp.Expr, ignored: set[sp.Symbol]) -> set[sp.Symbol]:
    """
    Return free symbols after excluding analysis variables such as ``s``.
    """
    return set(sp.sympify(expr).free_symbols) - ignored
