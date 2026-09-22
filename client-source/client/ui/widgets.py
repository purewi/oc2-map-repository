from __future__ import annotations

from PySide6.QtWidgets import QComboBox


class ClickOnlyComboBox(QComboBox):
    """A combo box whose selection cannot be changed by the mouse wheel."""

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        event.ignore()
