"""Narrow compatibility boundary around SLiCAP 5.2.1 desktop internals."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any


EXPECTED_SLICAP_VERSION = "5.2.1"


class DesktopCompatibilityError(RuntimeError):
    """Raised when the installed SLiCAP UI cannot satisfy the desktop shell."""


@dataclass(frozen=True)
class DesktopCapabilities:
    """Auditable record of the private UI surface used by ISACA."""

    slicap_version: str
    main_window_class: str
    canvas_panel_class: str
    schematic_data_class: str


class SLiCAPDesktopAdapter:
    """Validate and access the smallest possible official schematic API surface."""

    def __init__(self) -> None:
        installed = version("SLiCAP")
        if installed != EXPECTED_SLICAP_VERSION:
            raise DesktopCompatibilityError(
                f"ISACA Desktop requires SLiCAP=={EXPECTED_SLICAP_VERSION}; found {installed}."
            )
        from SLiCAP.schematic.canvas import SchematicScene
        from SLiCAP.schematic.schematic_data import SchematicData
        from SLiCAP.schematic.window import CanvasPanel, MainWindow

        required_main = ("add_canvas_panel", "load_file")
        required_panel = ("_on_save", "panel_dirty", "panel_save")
        required_scene = ("to_data", "from_data")
        missing = [
            *(f"MainWindow.{name}" for name in required_main if not hasattr(MainWindow, name)),
            *(f"CanvasPanel.{name}" for name in required_panel if not hasattr(CanvasPanel, name)),
            *(f"SchematicScene.{name}" for name in required_scene if not hasattr(SchematicScene, name)),
        ]
        signature = inspect.signature(MainWindow)
        for parameter in ("config", "file", "schematic_only"):
            if parameter not in signature.parameters:
                missing.append(f"MainWindow({parameter}=...)")
        if missing:
            raise DesktopCompatibilityError(
                "The installed SLiCAP schematic API is incompatible: " + ", ".join(missing)
            )
        self.MainWindow = MainWindow
        self.CanvasPanel = CanvasPanel
        self.SchematicData = SchematicData
        self.capabilities = DesktopCapabilities(
            slicap_version=installed,
            main_window_class=f"{MainWindow.__module__}.{MainWindow.__name__}",
            canvas_panel_class=f"{CanvasPanel.__module__}.{CanvasPanel.__name__}",
            schematic_data_class=f"{SchematicData.__module__}.{SchematicData.__name__}",
        )

    def active_panel(self, window: Any) -> Any | None:
        """Return the focused official canvas panel without leaking lookup logic."""

        resolver = getattr(window, "_active_canvas_panel", None)
        return resolver() if callable(resolver) else None

    def save_panel(self, panel: Any) -> Path | None:
        """Invoke the official save workflow and return the resulting file path."""

        if not all(hasattr(panel, name) for name in ("_current_path", "_scene")):
            raise DesktopCompatibilityError("CanvasPanel instance has no schematic state.")
        if not panel.panel_save():
            return None
        current = getattr(panel, "_current_path", None)
        return Path(current).resolve() if current and Path(current).is_file() else None

    def panel_path(self, panel: Any) -> Path | None:
        """Return a panel's current schematic path."""

        current = getattr(panel, "_current_path", None)
        return Path(current).resolve() if current else None
