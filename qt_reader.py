"""Reader status and voice-pack selection panel for Qt."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QWidget

from app_runtime import project_dir
from qt_base import BasePanel


class ReaderPanel(BasePanel):
    def __init__(self, app: object) -> None:
        super().__init__(app, "朗读监听", "查看 OCR 识别、当前语音包和匹配状态；开始、停止和选区控制固定在左侧。")
        self.build()

    def build(self) -> None:
        card, layout = self.card("监听状态")
        form = QFormLayout()
        form.setHorizontalSpacing(28)
        form.setVerticalSpacing(12)
        self.listener_value = QLabel("未启动")
        self.region_value = QLabel("未选择")
        self.pack_value = QLabel("未选择")
        self.ocr_value = QLabel("等待识别…")
        self.match_value = QLabel("等待匹配…")
        for label, widget in (
            ("监听状态", self.listener_value),
            ("台词区域", self.region_value),
            ("当前语音包", self.pack_value),
            ("最近 OCR 文本", self.ocr_value),
            ("匹配结果", self.match_value),
        ):
            widget.setWordWrap(True)
            form.addRow(label, widget)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.reader_pack_box = QComboBox()
        self.reader_pack_box.setMinimumWidth(360)
        self.reader_pack_paths: list[Path] = []
        refresh = QPushButton("刷新")
        apply_button = QPushButton("应用")
        refresh.clicked.connect(self.refresh_reader_packs)
        apply_button.clicked.connect(self.select_reader_pack)
        self.reader_pack_box.currentIndexChanged.connect(lambda _index: self.select_reader_pack())
        row.addWidget(QLabel("选择当前语音包"))
        row.addWidget(self.reader_pack_box, 1)
        row.addWidget(refresh)
        row.addWidget(apply_button)
        layout.addLayout(row)
        self.add_card(card)
        self.add_stretch()
        self.refresh_reader_packs()
        self.update_region(self.app.region)
        self.update_voice_pack(self.app.selected_voice_pack, self.app.selected_voice_manifest)
        self.update_listening(self.app.listening)

    def update_region(self, region: tuple[int, int, int, int] | None) -> None:
        if region is None:
            self.region_value.setText("未选择")
        else:
            x, y, width, height = region
            self.region_value.setText(f"屏幕区域：{x}, {y}  {width}×{height}")

    def update_voice_pack(self, path: Path | None, manifest: dict | None) -> None:
        if path is None:
            self.pack_value.setText("未选择")
            return
        segment = (manifest or {}).get("segment") or {}
        event_name = segment.get("event_name", path.parent.parent.name)
        code = segment.get("story_code", path.parent.name)
        self.pack_value.setText(f"{event_name} / {code}")

    def refresh_reader_packs(self) -> None:
        root = project_dir() / "data" / "voice_packs"
        self.reader_pack_paths = sorted(root.rglob("manifest.json")) if root.exists() else []
        labels: list[str] = []
        for path in self.reader_pack_paths:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                segment = manifest.get("segment") or {}
                event = segment.get("event_name") or path.parents[3].name
                code = segment.get("story_code") or segment.get("story_txt") or path.parent.name
                labels.append(f"{event} / {code} / {manifest.get('engine', '')} / {manifest.get('voice', '')}")
            except (OSError, ValueError):
                labels.append(path.parent.name)
        self.reader_pack_box.blockSignals(True)
        self.reader_pack_box.clear()
        self.reader_pack_box.addItems(labels)
        if self.app.selected_voice_pack in self.reader_pack_paths:
            self.reader_pack_box.setCurrentIndex(self.reader_pack_paths.index(self.app.selected_voice_pack))
        self.reader_pack_box.blockSignals(False)

    def select_reader_pack(self) -> None:
        index = self.reader_pack_box.currentIndex()
        if index < 0 or index >= len(self.reader_pack_paths):
            self.set_status("请先选择当前语音包")
            return
        path = self.reader_pack_paths[index]
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.error("语音包错误", f"无法读取语音包清单：\n\n{exc}")
            return
        self.app.set_voice_pack(path, manifest)
        self.set_status(f"已设置当前监听语音包：{path}")

    def update_listening(self, listening: bool) -> None:
        self.listener_value.setText("监听中" if listening else "未启动")

    def update_ocr(self, text: str) -> None:
        self.ocr_value.setText(text or "未识别到文字")

    def update_match(self, text: str) -> None:
        self.match_value.setText(text)

