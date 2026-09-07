"""Device metadata sourced from the pinned SLiCAP 5.2.1 symbol library."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
import re

import SLiCAP
from SLiCAP.SLiCAPprotos import _MODELS
from SLiCAP.schematic.symbol_library import NO_BUNDLE, SLICAP_SVG, build_library


EXPECTED_SLICAP_VERSION = "5.2.1"

# Stable editor names mapped to official IDs from SLiCAP's Symbols.svg bundle.
_CORE_SYMBOLS = {
    "R": "R",
    "C": "C",
    "L": "L",
    "V": "V",
    "I": "I",
    "G": "VCCS",
    "E": "VCVS",
    "F": "CCCS",
    "H": "CCVS",
    "M": "M",
    "QV": "QV",
    "GROUND": "0",
    "PORT": "port",
}


@lru_cache(maxsize=1)
def _official_library():
    """Load the pinned official symbol library once per API process."""

    return build_library(config=None, overlay=NO_BUNDLE)


@lru_cache(maxsize=1)
def _expanded_model_defaults() -> dict[str, dict[str, str]]:
    """Read expansion-model defaults from SLiCAP's installed model library."""

    library = Path(SLiCAP.__file__).parent / "files" / "lib" / "SLiCAPmodels.lib"
    defaults: dict[str, dict[str, str]] = {}
    for raw_line in library.read_text(encoding="utf-8").splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line.lower().startswith(".subckt "):
            continue
        fields = line.split()
        model_name = fields[1]
        defaults[model_name] = {
            match.group(1): match.group(2).strip("{}")
            for match in re.finditer(r"([A-Za-z_]\w*)\s*=\s*([^\s]+)", line)
        }
    return defaults


def _default_model_values(model_name: str, parameters: list[str]) -> dict[str, str]:
    """Return parser defaults for stamped models without duplicating tables."""

    model = _MODELS.get(model_name)
    if model is None:
        return {}
    if model.stamp:
        return {name: "0" for name in parameters}
    expanded = _expanded_model_defaults().get(model_name, {})
    return {name: expanded[name] for name in parameters if name in expanded}


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict]:
    """Build the supported web catalog from the official SLiCAP library."""

    installed = version("SLiCAP")
    if installed != EXPECTED_SLICAP_VERSION:
        raise RuntimeError(
            f"Web schematic requires SLiCAP {EXPECTED_SLICAP_VERSION}; found {installed}."
        )

    library = _official_library()
    catalog: dict[str, dict] = {}
    for device, symbol_name in _CORE_SYMBOLS.items():
        symbol = library.symbol(symbol_name)
        if symbol is None:
            raise RuntimeError(f"SLiCAP symbol {symbol_name!r} is unavailable.")
        pin_positions = {
            name: {"x": float(position[0]), "y": float(position[1])}
            for name, position in zip(symbol.nodes, symbol.pins)
        }
        catalog[device] = {
            "label": symbol.description or symbol.name,
            "description": symbol.description,
            "info": symbol.info,
            "prefix": symbol.prefix or None,
            "model": symbol.model or None,
            "model_show": bool(symbol.model_show),
            "symbol": symbol.name,
            "symbol_url": f"/api/v1/catalog/symbols/{symbol.name}.svg",
            "view_box": {
                "x": float(symbol.select_box[0]),
                "y": float(symbol.select_box[1]),
                "width": float(symbol.select_box[2]),
                "height": float(symbol.select_box[3]),
            },
            "pins": list(symbol.nodes),
            "pin_positions": pin_positions,
            "refs": list(symbol.refs),
            "defaults": dict(symbol.param_defaults),
            "param_display": {
                name: {"show_value": bool(flags[0]), "show_name": bool(flags[1])}
                for name, flags in symbol.param_display.items()
            },
            "show_pinnames": bool(symbol.show_pinnames),
            "slicap_defaults": _default_model_values(symbol.model, symbol.params),
        }

    # Project subcircuits have project-local SVG symbols and pin orders.
    catalog["X"] = {
        "label": "Subcircuit",
        "description": "Project-defined SLiCAP subcircuit",
        "info": "",
        "prefix": "X",
        "model": None,
        "model_show": True,
        "symbol": "",
        "symbol_url": None,
        "view_box": {"x": -25.0, "y": -25.0, "width": 50.0, "height": 50.0},
        "pins": [],
        "pin_positions": {},
        "refs": [],
        "defaults": {},
        "param_display": {},
        "show_pinnames": True,
        "slicap_defaults": {},
    }
    catalog["JUNCTION"] = {
        "label": "Junction",
        "description": "Electrical wire junction",
        "info": "",
        "prefix": None,
        "model": None,
        "model_show": False,
        "symbol": "",
        "symbol_url": None,
        "view_box": {"x": -4.0, "y": -4.0, "width": 8.0, "height": 8.0},
        "pins": ["junction"],
        "pin_positions": {"junction": {"x": 0.0, "y": 0.0}},
        "refs": [],
        "defaults": {},
        "param_display": {},
        "show_pinnames": False,
        "slicap_defaults": {},
    }
    return catalog


# Compatibility view for existing adapters. It is generated from SLiCAP rather
# than maintained as a second source of electrical metadata.
DEVICE_CATALOG = _catalog()


def device_catalog() -> dict[str, dict]:
    """Return a defensive copy of the official, version-pinned catalog."""

    return deepcopy(DEVICE_CATALOG)


def symbol_svg(symbol_name: str) -> bytes | None:
    """Return one official SLiCAP symbol as a standalone SVG document."""

    return _official_library().svg_bytes(symbol_name)


def symbol_bundle(library_name: str) -> bytes | None:
    """Return an official SLiCAP SVG bundle by stable API name."""

    paths = {
        "core": SLICAP_SVG,
        "extended": SLICAP_SVG.with_name("Symbols-extended.svg"),
    }
    path = paths.get(library_name)
    return path.read_bytes() if path is not None and path.is_file() else None
