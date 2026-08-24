"""Shared Qt UI primitives for the migrated desktop interface."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class UiSignals(QObject):
    status = Signal(str)
    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(object)
    error = Signal(str)


class BasePanel(QScrollArea):
    """Scrollable panel with a compact, consistent card layout."""

    def __init__(self, app: object, title: str, description: str) -> None:
        super().__init__()
        self.app = app
        self.module_key = ""
        self.setObjectName("qtPanelScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content = QWidget()
        self.content.setObjectName("qtPanelContent")
        self.layout = QVBoxLayout(self.content)
        self.layout.setContentsMargins(28, 24, 28, 30)
        self.layout.setSpacing(16)
        self.setWidget(self.content)
        self.add_header(title, description)

    def add_header(self, title: str, description: str) -> None:
        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 4)
        header_layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        desc_label = QLabel(description)
        desc_label.setObjectName("pageDescription")
        desc_label.setWordWrap(True)
        header_layout.addWidget(title_label)
        header_layout.addWidget(desc_label)
        self.layout.addWidget(header)

    def card(self, title: str | None = None, note: str | None = None) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("uiCard")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(16, 14, 16, 16)
        frame_layout.setSpacing(12)
        if title is not None:
            head = QWidget()
            head_layout = QHBoxLayout(head)
            head_layout.setContentsMargins(0, 0, 0, 0)
            title_label = QLabel(title)
            title_label.setObjectName("cardTitle")
            head_layout.addWidget(title_label)
            head_layout.addStretch(1)
            if note:
                note_label = QLabel(note)
                note_label.setObjectName("cardNote")
                head_layout.addWidget(note_label)
            frame_layout.addWidget(head)
        return frame, frame_layout

    def add_card(self, card: QFrame) -> None:
        self.layout.addWidget(card)

    def add_stretch(self) -> None:
        self.layout.addStretch(1)

    def make_button(self, text: str, *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("primary", primary)
        return button

    def set_status(self, text: str) -> None:
        self.app.set_status(text)

    def info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def warning(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    @staticmethod
    def set_enabled(widget: QWidget, enabled: bool) -> None:
        widget.setEnabled(enabled)
