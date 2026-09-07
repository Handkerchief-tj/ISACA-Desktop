"""QApplication bootstrap for the ISACA desktop shell."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

def run_desktop(project: str | Path | None = None, file: str | Path | None = None) -> int:
    """Start the native application without changing the official Qt style."""

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    application = QApplication.instance() or QApplication(sys.argv)
    from .main_window import IsacaMainWindow

    application.setApplicationName("ISACA")
    application.setOrganizationName("ISACA")
    window = IsacaMainWindow(project_root=project, file=file)
    window.show()
    return application.exec()
