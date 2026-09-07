"""ISACA-only usability enhancements for the pinned SLiCAP parameter dialog."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from PySide6.QtCore import QEvent, QModelIndex, QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
)


_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_]\w*)")
_RESERVED_NAMES = {
    "s", "pi", "oo", "inf", "nan", "e", "i", "j",
    "abs", "exp", "log", "ln", "sqrt", "sin", "cos", "tan",
    "asin", "acos", "atan", "sinh", "cosh", "tanh", "heaviside",
}


@dataclass(frozen=True)
class ParameterCandidate:
    """One circuit symbol referenced by one or more component properties."""

    name: str
    contexts: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.name}  ({', '.join(self.contexts)})"


def _expression_symbols(value: Any) -> set[str]:
    """Extract likely free symbols without evaluating SLiCAP expressions."""

    text = str(value or "").strip().strip("{}")
    return {
        match.group(1)
        for match in _IDENTIFIER.finditer(text)
        if match.group(1).lower() not in _RESERVED_NAMES
    }


def component_parameter_candidates(components: Iterable[Any]) -> list[ParameterCandidate]:
    """Collect `.param` candidates and retain their component-property origin."""

    contexts: dict[str, set[str]] = defaultdict(set)
    ordered = sorted(components, key=lambda item: str(getattr(item, "instance_id", "")))
    for component in ordered:
        refdes = str(getattr(component, "instance_id", "?") or "?")
        for parameter, expression in getattr(component, "params", {}).items():
            if str(parameter).startswith("_"):
                continue
            for symbol in _expression_symbols(expression):
                contexts[symbol].add(f"{refdes}.{parameter}")
    return [
        ParameterCandidate(name, tuple(sorted(origins)))
        for name, origins in sorted(contexts.items())
    ]


class SpreadsheetDelegate(QStyledItemDelegate):
    """Provide spreadsheet-style arrow-key navigation between editable cells."""

    def __init__(self, table: QTableWidget, candidates: Iterable[ParameterCandidate] = (), parent=None):
        super().__init__(parent or table)
        self.table = table
        self.candidates = tuple(candidates)

    def createEditor(self, parent, option, index: QModelIndex):
        if index.column() == 0 and self.candidates:
            editor = QComboBox(parent)
            editor.setEditable(True)
            editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            for candidate in self.candidates:
                editor.addItem(candidate.label, candidate.name)
            QTimer.singleShot(0, editor.showPopup)
            return editor
        return QLineEdit(parent)

    def setEditorData(self, editor, index: QModelIndex) -> None:
        value = str(index.data(Qt.ItemDataRole.EditRole) or "")
        if isinstance(editor, QComboBox):
            match = next(
                (item for item in range(editor.count()) if editor.itemData(item) == value),
                -1,
            )
            if match >= 0:
                editor.setCurrentIndex(match)
            else:
                editor.setEditText(value)
        else:
            editor.setText(value)
            editor.selectAll()

    def setModelData(self, editor, model, index: QModelIndex) -> None:
        value = editor.currentData() or editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        model.setData(index, str(value), Qt.ItemDataRole.EditRole)

    def eventFilter(self, editor, event) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if isinstance(editor, QComboBox) and editor.view().isVisible() and key in (
                Qt.Key.Key_Up, Qt.Key.Key_Down,
            ):
                return super().eventFilter(editor, event)
            moves = {
                Qt.Key.Key_Left: (0, -1),
                Qt.Key.Key_Right: (0, 1),
                Qt.Key.Key_Up: (-1, 0),
                Qt.Key.Key_Down: (1, 0),
                Qt.Key.Key_Return: (1, 0),
                Qt.Key.Key_Enter: (1, 0),
            }
            if key in moves:
                row = self.table.currentRow()
                column = self.table.currentColumn()
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                delta = moves[key]
                QTimer.singleShot(0, lambda: self._move_to(row + delta[0], column + delta[1]))
                return True
        return super().eventFilter(editor, event)

    def _move_to(self, row: int, column: int) -> None:
        row = max(0, min(row, self.table.rowCount() - 1))
        column = max(0, min(column, self.table.columnCount() - 1))
        item = self.table.item(row, column)
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsEditable:
            return
        self.table.setCurrentItem(item)
        self.table.editItem(item)


def configure_spreadsheet_table(
    table: QTableWidget,
    candidates: Iterable[ParameterCandidate] = (),
) -> SpreadsheetDelegate:
    """Enable single-click editing and keyboard navigation on a Qt table."""

    delegate = SpreadsheetDelegate(table, candidates)
    table.setItemDelegate(delegate)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(
        QAbstractItemView.EditTrigger.CurrentChanged
        | QAbstractItemView.EditTrigger.SelectedClicked
        | QAbstractItemView.EditTrigger.EditKeyPressed
        | QAbstractItemView.EditTrigger.AnyKeyPressed
    )
    return delegate


_COMPONENT_PROVIDER: Callable[[], Iterable[Any]] | None = None
_ORIGINAL_PARAMETER_DIALOG = None


def install_slicap_parameter_dialog_enhancement(
    component_provider: Callable[[], Iterable[Any]],
) -> None:
    """Patch only SLiCAP's dialog class, leaving the installed package untouched."""

    global _COMPONENT_PROVIDER, _ORIGINAL_PARAMETER_DIALOG
    _COMPONENT_PROVIDER = component_provider
    from SLiCAP.schematic import parameter_dialog as module

    if getattr(module.ParameterDialog, "_isaca_enhanced", False):
        return
    original = module.ParameterDialog
    _ORIGINAL_PARAMETER_DIALOG = original

    class IsacaParameterDialog(original):
        _isaca_enhanced = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            components = list(_COMPONENT_PROVIDER() if _COMPONENT_PROVIDER else ())
            candidates = component_parameter_candidates(components)
            self._isaca_delegate = configure_spreadsheet_table(self._table, candidates)
            for label in self.findChildren(QLabel):
                if label.text().startswith("SLiCAP syntax"):
                    label.setText(
                        "Parameter 单击后可选择器件属性已引用的符号；"
                        "Value 支持 10k、1u 和表达式。方向键可切换单元格。"
                    )
                    label.setWordWrap(True)
                    break
            for button in self.findChildren(QPushButton):
                if button.text() == "Add row":
                    try:
                        button.clicked.disconnect()
                    except RuntimeError:
                        pass
                    button.clicked.connect(self._add_isaca_row)
                    break

        def _add_isaca_row(self) -> None:
            super()._add_row("", "")
            row = self._table.rowCount() - 1
            item = self._table.item(row, 0)
            if item is not None:
                self._table.setCurrentItem(item)
                self._table.editItem(item)

    module.ParameterDialog = IsacaParameterDialog
