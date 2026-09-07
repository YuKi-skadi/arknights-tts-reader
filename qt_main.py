"""Qt application shell for the Arknights TTS Reader."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, Signal, QTimer, QLockFile
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_runtime import configure_windows_dpi, load_app_settings, project_dir, save_app_settings
from reader_engine import ListenerEngine
from ui_config import APP_TITLE, CUSTOMIZATION_EXTENSIONS, CUSTOMIZATION_SLOTS
from qt_voice import ConnectedVoiceGenerationPanel
from qt_story import ConnectedStoryDownloadPanel
from qt_voice_packs import ConnectedVoicePackPanel
from qt_reader import ReaderPanel
from qt_settings import SettingsPanel


class RegionOverlay(QWidget):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setWindowState(Qt.WindowState.WindowFullScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowOpacity(0.28)
        self.setStyleSheet("background:#102030;")
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.origin = QPoint()
        self.rubber = None
        self.dragging = False
        self.setMouseTracking(True)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(16, 32, 48, 180))
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.Bold))
        painter.drawText(24, 34, "拖拽框选台词区域 · Esc 取消")
        if self.rubber is not None:
            painter.drawRect(self.rubber)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.origin = event.position().toPoint()
        self.dragging = True
        self.rubber = QRect(self.origin, self.origin)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.dragging:
            self.rubber = QRect(self.origin, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if not self.dragging:
            return
        self.dragging = False
        rect = QRect(self.origin, event.position().toPoint()).normalized()
        if rect.width() < 8 or rect.height() < 8:
            return
        top_left = self.mapToGlobal(rect.topLeft())
        self.selected.emit((top_left.x(), top_left.y(), rect.width(), rect.height()))
        self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()


class ReaderApp(QMainWindow):
    """Modern Qt shell while keeping existing backend/data contracts."""

    listener_event = Signal(int, str, str, object)

    def __init__(self) -> None:
        configure_windows_dpi()
        super().__init__()
        self._listener_session = 0
        self.listener_event.connect(self._dispatch_listener_event)
        self._closing = False
        self._close_ready = False
        self._cleanup_thread = None
        self.setWindowTitle(APP_TITLE)
        # Keep the functional sidebar usable at the smallest allowed window;
        # the decorative image is the first area allowed to disappear.
        self.setMinimumSize(900, 600)
        self.resize(1180, 760)
        icon = project_dir() / "assets" / "app_icon.ico"
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.settings = load_app_settings()
        self._migrate_customization()
        self.ocr_interval = self._read_ocr_interval()
        self.region: tuple[int, int, int, int] | None = None
        self.selected_voice_pack: Path | None = None
        self.selected_voice_manifest: dict | None = None
        self.listening = False
        self.listener_engine: ListenerEngine | None = None
        self.region_overlay: RegionOverlay | None = None
        self.panel_instances: dict[str, QWidget] = {}
        self.modules = {
            "story_download": ("剧情下载", "选择和导出剧情", ConnectedStoryDownloadPanel),
            "voice_generation": ("语音生成", "批量预生成语音", ConnectedVoiceGenerationPanel),
            "voice_packs": ("资源管理", "管理剧情、语音和缓存资源", ConnectedVoicePackPanel),
            "reader": ("朗读监听", "查看 OCR 和匹配状态", ReaderPanel),
            "settings": ("设置", "程序和数据目录设置", SettingsPanel),
        }
        self._build_style()
        self._build_shell()
        self.show_module("story_download")

    def _read_ocr_interval(self) -> float:
        try:
            return max(0.1, min(5.0, float(self.settings.get("ocr_interval", 0.25))))
        except (TypeError, ValueError):
            return 0.25

    def _build_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #edf1f5; }
            QWidget { color: #1f2d3d; font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 13px; }
            #topBar { background: #f8fafc; border-bottom: 1px solid #d5dce4; }
            #sideBar { background: #f3f6f9; border-right: 1px solid #d5dce4; }
            #workArea { background: #ffffff; }
            #qtPanelContent { background: #ffffff; }
            #pageTitle { color: #1f2d3d; font-size: 25px; font-weight: 700; }
            #pageDescription, #cardNote, #sideHint { color: #738196; font-size: 12px; }
            #cardTitle, #sideTitle { color: #26384b; font-size: 14px; font-weight: 700; }
            #uiCard { background: #ffffff; border: 1px solid #d5dce4; border-radius: 5px; }
            QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { min-height: 30px; border: 1px solid #bdc8d4; border-radius: 4px; padding: 3px 8px; background: #ffffff; }
            QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #d87924; }
            QComboBox QAbstractItemView::item:selected { background: #ffe8bd; color: #a9521d; }
            QPushButton { min-height: 30px; padding: 4px 12px; border: 1px solid #bdc8d4; border-radius: 4px; background: #ffffff; color: #425468; }
            QPushButton:hover { border-color: #d87924; color: #a9521d; }
            QPushButton[primary="true"] { border-color: #d87924; background: #d87924; color: #ffffff; }
            QPushButton#navButton { border-color: transparent; background: transparent; color: #536477; text-align: left; padding-left: 12px; }
            QPushButton#navButton:hover { border-color: #f0c27b; background: #fff5e5; color: #a9521d; }
            QPushButton#navButton:checked { border-color: #e0a35c; background: #ffe8bd; color: #a9521d; font-weight: 600; }
            QPushButton:disabled { color: #a9b4c0; background: #f1f3f5; }
            QListWidget, QPlainTextEdit, QTextEdit { border: 1px solid #d5dce4; border-radius: 4px; background: #fbfcfd; padding: 4px; }
            QListWidget::item { padding: 7px 6px; border-bottom: 1px solid #edf0f3; }
            QListWidget::item:selected { background: #ffe8bd; color: #a9521d; }
            QProgressBar { min-height: 8px; max-height: 8px; border: 0; border-radius: 4px; background: #e3e8ed; text-align: center; }
            QProgressBar::chunk { border-radius: 4px; background: #d87924; }
            QSlider::groove:horizontal { height: 5px; border-radius: 3px; background: #f0e3d1; }
            QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #d87924; }
            QCheckBox::indicator:checked { background: #d87924; border: 1px solid #c8681d; }
            QScrollBar:vertical { width: 10px; background: #f2f5f7; margin: 0; }
            QScrollBar::handle:vertical { min-height: 30px; background: #c1ccd7; border-radius: 5px; }
            QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
            QSplitter::handle { background: #edf1f5; }
            QStatusBar { background: #f8fafc; border-top: 1px solid #d5dce4; color: #738196; }
        """)

    def _build_shell(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        top = QWidget()
        top.setObjectName("topBar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(18, 10, 18, 10)
        title = QLabel(APP_TITLE)
        title.setObjectName("cardTitle")
        top_layout.addWidget(title)
        top_layout.addStretch(1)
        self.module_title = QLabel()
        self.module_title.setObjectName("pageDescription")
        top_layout.addWidget(self.module_title)
        root.addWidget(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        side = QWidget()
        side.setObjectName("sideBar")
        side.setMinimumWidth(170)
        side.setMaximumWidth(230)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(14, 14, 14, 8)
        side_layout.setSpacing(4)
        side_title = QLabel("功能模块")
        side_title.setObjectName("sideTitle")
        side_hint = QLabel("选择模块后，在右侧工作区操作")
        side_hint.setObjectName("sideHint")
        side_hint.setWordWrap(True)
        side_layout.addWidget(side_title)
        side_layout.addWidget(side_hint)
        side_layout.addSpacing(8)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, (name, _desc, _factory) in self.modules.items():
            button = QPushButton(name)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setObjectName("navButton")
            button.setMinimumWidth(0)
            button.setMinimumHeight(30)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda checked=False, module_key=key: self.show_module(module_key))
            self.nav_buttons[key] = button
            side_layout.addWidget(button)
        side_layout.addSpacing(8)

        # Reserve a real bottom region for the decoration and the functional
        # card. The image has its own bounded area; it can collapse when the
        # sidebar is short, but it can never resize over the reading controls.
        bottom_region = QWidget()
        bottom_region.setObjectName("sidebarBottomRegion")
        bottom_region.setMinimumHeight(0)
        bottom_layout = QVBoxLayout(bottom_region)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(4)
        decoration_area = QWidget()
        decoration_area.setObjectName("sidebarDecorationArea")
        decoration_area.setMinimumHeight(0)
        decoration_area.setMaximumHeight(220)
        # Ignore the image's size hint so this region can collapse completely
        # when the sidebar is short; the reading card remains measurable.
        decoration_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        decoration_layout = QHBoxLayout(decoration_area)
        decoration_layout.setContentsMargins(0, 0, 0, 0)
        decoration_layout.setSpacing(0)
        self.sidebar_image = QLabel()
        self.sidebar_image.setObjectName("sidebarDecoration")
        self.sidebar_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sidebar_image.setMinimumSize(0, 0)
        self.sidebar_image.setMaximumWidth(200)
        self.sidebar_image.setMaximumHeight(220)
        self.sidebar_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.sidebar_image.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        decoration_layout.addWidget(self.sidebar_image, 1)
        bottom_layout.addWidget(decoration_area, 1)

        reading_control = self._build_reading_control()
        reading_control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        reading_control.setMinimumHeight(reading_control.sizeHint().height())
        bottom_layout.addWidget(reading_control, 0)
        side_layout.addWidget(bottom_region, 1)
        self.refresh_customization()
        splitter.addWidget(side)

        work = QWidget()
        work.setObjectName("workArea")
        work_layout = QVBoxLayout(work)
        work_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        work_layout.addWidget(self.stack)
        splitter.addWidget(work)
        splitter.setSizes([205, 975])
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.set_status("就绪")

    def _build_reading_control(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("uiCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        frame.setMinimumWidth(0)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title = QLabel("朗读控制")
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        select = QPushButton("选择台词区域")
        select.clicked.connect(self.select_region)
        layout.addWidget(select)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.start_button = QPushButton("开始监听")
        self.start_button.setMinimumWidth(0)
        self.start_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.start_button.clicked.connect(self.start_listening)
        self.stop_button = QPushButton("停止监听")
        self.stop_button.setMinimumWidth(0)
        self.stop_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_listening)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("语速"))
        self.speed_value = QLabel("1.0x")
        speed_row.addStretch(1)
        speed_row.addWidget(self.speed_value)
        layout.addLayout(speed_row)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 20)
        self.speed_slider.setValue(10)
        self.speed_slider.setEnabled(False)
        self.speed_slider.setMinimumWidth(0)
        self.speed_slider.setFixedHeight(14)
        self.speed_slider.setToolTip("播放使用音频原速；需要调整语速时请在语音生成中设置后重新生成。")
        self.speed_value.setText("原速")
        self.speed_slider.valueChanged.connect(self._speed_changed)
        layout.addWidget(self.speed_slider)
        self.listener_status = QLabel("状态：未监听")
        self.listener_status.setObjectName("sideHint")
        layout.addWidget(self.listener_status)
        return frame

    def _speed_changed(self, value: int) -> None:
        self.speed_value.setText(f"{value / 10:.1f}x")

    def show_module(self, key: str) -> None:
        if key not in self.modules:
            return
        panel = self.panel_instances.get(key)
        if panel is None:
            _title, _desc, factory = self.modules[key]
            panel = factory(self)
            panel.module_key = key
            self.panel_instances[key] = panel
            self.stack.addWidget(panel)
        panel.refresh_customization()
        self.stack.setCurrentWidget(panel)
        self.module_title.setText(f"当前模块：{self.modules[key][0]}")
        self.nav_buttons[key].setChecked(True)
        self.set_status(f"已切换到：{self.modules[key][0]}")

    def set_status(self, text: str) -> None:
        self.status.showMessage(text)

    def set_ocr_interval(self, value: float) -> None:
        self.ocr_interval = max(0.1, min(5.0, round(float(value), 1)))
        self.settings["ocr_interval"] = self.ocr_interval
        save_app_settings(self.settings)
        if self.listener_engine is not None:
            self.listener_engine.interval = self.ocr_interval
        self.set_status(f"OCR 轮询间隔已保存：{self.ocr_interval:.1f} 秒")

    def set_voice_pack(self, path: Path | None, manifest: dict | None) -> None:
        if self.listening and path != self.selected_voice_pack:
            self.stop_listening()
        self.selected_voice_pack = path
        self.selected_voice_manifest = manifest
        panel = self.panel_instances.get("reader")
        if isinstance(panel, ReaderPanel):
            panel.update_voice_pack(path, manifest)

    def select_region(self) -> None:
        if self.region_overlay is not None:
            return
        self.set_status("请在屏幕上拖拽框选游戏台词区域，按 Esc 取消")
        self.region_overlay = RegionOverlay()
        self.region_overlay.selected.connect(self._region_selected)
        self.region_overlay.cancelled.connect(lambda: self.set_status("已取消台词区域选择"))
        self.region_overlay.destroyed.connect(lambda: setattr(self, "region_overlay", None))
        self.region_overlay.show()
        self.region_overlay.raise_()
        self.region_overlay.activateWindow()
        self.listener_status.setText("状态：请拖拽框选台词区域")

    def _region_selected(self, value: object) -> None:
        self.region = tuple(int(x) for x in value)
        self.listener_status.setText("状态：已选择台词区域")
        self.set_status(f"台词区域已保存：{self.region[2]}×{self.region[3]}")
        panel = self.panel_instances.get("reader")
        if isinstance(panel, ReaderPanel):
            panel.update_region(self.region)

    def _listener_event(self, kind: str, text: str, data: dict | None = None) -> None:
        self.listener_event.emit(self._listener_session, kind, text, data)

    def _dispatch_listener_event(self, session: int, kind: str, text: str, data: object) -> None:
        if session == self._listener_session and not self._closing:
            self._listener_event_ui(kind, text, data)

    def _listener_event_ui(self, kind: str, text: str, data: dict | None = None) -> None:
        panel = self.panel_instances.get("reader")
        if isinstance(panel, ReaderPanel):
            if kind == "ocr":
                panel.update_ocr(text)
            elif kind == "match":
                score = float((data or {}).get("score", 1.0))
                panel.update_match(f"已匹配（{score:.0%}）：{text}")
            elif kind == "unmatched":
                panel.update_match(text)
        if kind == "status":
            self.set_status(text)
        elif kind == "match":
            self.set_status(f"已匹配并播放：{text}")
        elif kind == "unmatched":
            self.set_status("OCR 已识别文字，但当前语音包没有匹配项")
        elif kind == "error":
            self.listening = False
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.listener_status.setText("状态：监听错误")
            if isinstance(panel, ReaderPanel):
                panel.update_listening(False)
            self.set_status(f"OCR 监听失败：{text}")
            QMessageBox.critical(self, "OCR 监听失败", text)
        elif kind == "stopped" and not self.listening:
            self.set_status(text)

    def start_listening(self) -> None:
        if self.listening or self._closing:
            return
        if self.selected_voice_pack is None or not self.selected_voice_pack.exists():
            QMessageBox.warning(self, "未选择语音包", "请先在“朗读监听”模块中选择当前语音包。")
            return
        if self.region is None:
            self.set_status("请先点击“选择台词区域”完成框选")
            return
        try:
            self._listener_session += 1
            session = self._listener_session
            self.listener_engine = ListenerEngine(self.region, self.selected_voice_pack, lambda kind, text, data: self.listener_event.emit(session, kind, text, data), self.ocr_interval)
            self.listener_engine.start()
        except Exception as exc:
            QMessageBox.critical(self, "OCR 启动失败", str(exc))
            return
        self.listening = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.listener_status.setText("状态：监听中")
        panel = self.panel_instances.get("reader")
        if isinstance(panel, ReaderPanel):
            panel.update_listening(True)
        self.set_status("朗读监听已启动")

    def stop_listening(self) -> None:
        self._listener_session += 1
        if self.listener_engine is not None:
            self.listener_engine.stop()
            self.listener_engine = None
        self.listening = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.listener_status.setText("状态：未监听")
        panel = self.panel_instances.get("reader")
        if isinstance(panel, ReaderPanel):
            panel.update_listening(False)
        self.set_status("朗读监听已停止")

    def _customization_dir(self) -> Path:
        return project_dir() / "data" / "customization"

    def _customization_target(self, slot: str) -> tuple[int, int]:
        return CUSTOMIZATION_SLOTS[slot][1]

    def _resolve_configured_custom_path(self, key: str) -> Path | None:
        configured = str(self.settings.get(key, "")).strip()
        if not configured:
            return None
        path = Path(configured)
        if not path.is_absolute():
            path = project_dir() / path
        try:
            path = path.resolve()
            path.relative_to(self._customization_dir().resolve())
        except (OSError, ValueError):
            return None
        return path if path.is_file() else None

    def _normalize_custom_image(self, slot: str, source: Path) -> Path:
        from PIL import Image, ImageOps

        target_width, target_height = self._customization_target(slot)
        with Image.open(source) as original:
            image = original.convert("RGBA")
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        fitted = ImageOps.contain(image, (target_width, target_height), method=resampling)
        canvas = Image.new("RGBA", (target_width, target_height), (0, 0, 0, 0))
        offset = ((target_width - fitted.width) // 2, (target_height - fitted.height) // 2)
        canvas.alpha_composite(fitted, offset)
        destination_dir = self._customization_dir()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{slot}.png"
        temporary = destination_dir / f".{slot}.tmp.png"
        canvas.save(temporary, format="PNG", optimize=True)
        temporary.replace(destination)
        for old_path in destination_dir.glob(f"{slot}.*"):
            if old_path != destination and old_path.is_file():
                old_path.unlink()
        return destination

    def _migrate_customization(self) -> None:
        legacy = self._resolve_configured_custom_path("custom_workspace_image")
        changed = False
        if legacy is not None:
            for slot in CUSTOMIZATION_SLOTS:
                if slot == "sidebar":
                    continue
                key = f"custom_{slot}_image"
                if self._resolve_configured_custom_path(key) is None:
                    try:
                        destination = self._normalize_custom_image(slot, legacy)
                    except Exception:
                        continue
                    self.settings[key] = str(destination.relative_to(project_dir())).replace("\\", "/")
                    changed = True
            self.settings.pop("custom_workspace_image", None)
            changed = True
        if changed:
            save_app_settings(self.settings)

    def custom_image_path(self, slot: str) -> Path | None:
        return self._resolve_configured_custom_path(f"custom_{slot}_image")

    def refresh_customization(self) -> None:
        path = self.custom_image_path("sidebar")
        pixmap = QPixmap(str(path)) if path else QPixmap()
        self.sidebar_image.setPixmap(pixmap.scaled(180, 198, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if not pixmap.isNull() else pixmap)
        self.sidebar_image.setVisible(not pixmap.isNull())
        for panel in self.panel_instances.values():
            panel.refresh_customization()

    def custom_image_name(self, slot: str) -> str:
        path = self.custom_image_path(slot)
        return path.name if path else "未设置（保持默认）"

    def set_custom_image(self, slot: str, source: Path) -> bool:
        if slot not in CUSTOMIZATION_SLOTS or source.suffix.lower() not in CUSTOMIZATION_EXTENSIONS:
            QMessageBox.warning(self, "图片格式不支持", "请选择 PNG、JPG、WebP、BMP 或 GIF 图片。")
            return False
        try:
            destination = self._normalize_custom_image(slot, source)
            self.settings[f"custom_{slot}_image"] = str(destination.relative_to(project_dir())).replace("\\", "/")
            save_app_settings(self.settings)
            self.refresh_customization()
            self.set_status(f"已设置{CUSTOMIZATION_SLOTS[slot][0]}：{destination.name}")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "图片保存失败", str(exc))
            return False

    def clear_custom_image(self, slot: str) -> None:
        path = self.custom_image_path(slot)
        if path:
            try:
                path.unlink()
            except OSError:
                pass
        self.settings.pop(f"custom_{slot}_image", None)
        save_app_settings(self.settings)
        self.refresh_customization()
        self.set_status(f"已清除{CUSTOMIZATION_SLOTS.get(slot, ('自定义图片', (0, 0)))[0]}")

    def closeEvent(self, event) -> None:
        if self._close_ready:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.stop_listening()
        if self.region_overlay is not None:
            self.region_overlay.close()
        self.centralWidget().setEnabled(False)
        for panel in self.panel_instances.values():
            shutdown = getattr(panel, "request_shutdown", None)
            if callable(shutdown):
                shutdown()
        self.set_status("正在保存断点并退出；当前语音或分析请求完成后关闭，下次可继续队列…")
        self._poll_shutdown()

    def _poll_shutdown(self) -> None:
        workers = [getattr(panel, "queue_thread", None) for panel in self.panel_instances.values()]
        if any(worker and worker.is_alive() for worker in workers):
            QTimer.singleShot(100, self._poll_shutdown)
            return
        if self._cleanup_thread is None:
            def cleanup() -> None:
                for panel in self.panel_instances.values():
                    release = getattr(getattr(panel, "backend", None), "release_qwen", None)
                    if callable(release):
                        try:
                            release()
                        except Exception:
                            logging.exception("Backend shutdown failed")
            self._cleanup_thread = threading.Thread(target=cleanup, daemon=True)
            self._cleanup_thread.start()
        if self._cleanup_thread.is_alive():
            QTimer.singleShot(100, self._poll_shutdown)
            return
        self._close_ready = True
        self.close()


def run() -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--check", action="store_true", help="检查 Qt GUI 模块是否可以导入")
    parser.add_argument("--self-test-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-test-phase", choices=("prepare", "resume"), default="prepare", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test_output:
        from diagnostics import run_smoke
        return run_smoke(args.self_test_output, args.self_test_phase)
    if args.check:
        print(f"{APP_TITLE}: Qt GUI shell import OK")
        print("modules: story_download, voice_generation, voice_packs, reader, settings")
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    log_dir = project_dir() / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(log_dir.parent / "app.lock"))
    if not instance_lock.tryLock(0):
        QMessageBox.information(None, APP_TITLE, "这个目录中的程序已经打开，请使用现有窗口。")
        return 0
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
    def report_error(exc_type, value, traceback) -> None:
        logging.error("Unhandled exception", exc_info=(exc_type, value, traceback))
        QMessageBox.critical(None, "操作失败", f"{value}\n\n详细信息已记录到 data/logs/app.log。")
    sys.excepthook = report_error
    window = ReaderApp()
    window.show()
    return app.exec()
