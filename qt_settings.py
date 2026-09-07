"""Settings panel for the Qt interface."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QDoubleSpinBox,
    QVBoxLayout,
)

from app_runtime import save_app_settings
from quality_analysis import DEFAULT_DEEPSEEK_MODEL, fetch_deepseek_models, test_deepseek_model
from qt_base import BasePanel
from ui_config import CUSTOMIZATION_SLOTS


class SettingsSignals(QObject):
    models = Signal(object)
    models_error = Signal(str)
    tested = Signal(str, str)
    test_error = Signal(str)


class SettingsPanel(BasePanel):
    def __init__(self, app: object) -> None:
        super().__init__(app, "设置", "集中管理 OCR 识别参数、DeepSeek 质量优化和界面个性化。")
        self.signals = SettingsSignals()
        self.signals.models.connect(self._apply_deepseek_models)
        self.signals.models_error.connect(self._deepseek_models_failed)
        self.signals.tested.connect(self._deepseek_test_succeeded)
        self.signals.test_error.connect(self._deepseek_test_failed)
        self.build()

    def build(self) -> None:
        card, layout = self.card("程序设置")
        form = QFormLayout()
        form.setHorizontalSpacing(28)
        form.setVerticalSpacing(12)
        self.story_dir = QLineEdit("data\\stories")
        self.voice_cache_dir = QLineEdit("data\\voice_cache")
        self.story_dir.setReadOnly(True)
        self.voice_cache_dir.setReadOnly(True)
        self.story_dir.setToolTip("便携版使用程序目录内的固定路径")
        self.voice_cache_dir.setToolTip("便携版使用程序目录内的固定路径")
        form.addRow("剧情数据目录", self.story_dir)
        form.addRow("语音缓存目录", self.voice_cache_dir)

        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.1, 5.0)
        self.interval.setSingleStep(0.1)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(self.app.ocr_interval)
        form.addRow("OCR 轮询间隔", self.interval)

        self.deepseek_key = QLineEdit(str(self.app.settings.get("deepseek_api_key", "")))
        self.deepseek_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("DeepSeek API Key", self.deepseek_key)

        model_row = QHBoxLayout()
        self.deepseek_model = QComboBox()
        current_model = str(self.app.settings.get("deepseek_model", DEFAULT_DEEPSEEK_MODEL)) or DEFAULT_DEEPSEEK_MODEL
        self.deepseek_model.addItem(current_model)
        refresh = QPushButton("获取模型列表")
        test = QPushButton("测试当前模型")
        refresh.clicked.connect(self.refresh_deepseek_models)
        test.clicked.connect(self.test_current_deepseek_model)
        model_row.addWidget(self.deepseek_model, 1)
        model_row.addWidget(refresh)
        model_row.addWidget(test)
        self.refresh_models_button = refresh
        self.test_model_button = test
        form.addRow("DeepSeek 模型", model_row)
        layout.addLayout(form)
        hint = QLabel("仅在语音生成中勾选 DeepSeek 优化时使用；剧情文本会发送到 DeepSeek。")
        hint.setObjectName("pageDescription")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.add_card(card)

        custom_card, custom_layout = self.card("界面个性化")
        self.customization_summary = QLabel()
        self.customization_summary.setObjectName("pageDescription")
        custom_layout.addWidget(self.customization_summary)
        custom_layout.addWidget(QLabel("统一管理侧边栏和工作区标题图片，图片会自动适配标准区域。"))
        open_button = QPushButton("打开个性化设置")
        open_button.clicked.connect(self.open_customization_dialog)
        custom_layout.addWidget(open_button, 0, alignment=Qt.AlignmentFlag.AlignRight)
        save_button = self.make_button("保存设置", primary=True)
        save_button.clicked.connect(self.save_settings)
        custom_layout.addWidget(save_button, 0, alignment=Qt.AlignmentFlag.AlignRight)
        self.add_card(custom_card)
        self.add_stretch()
        self.update_customization_summary()

    def update_customization_summary(self) -> None:
        count = sum(1 for slot in CUSTOMIZATION_SLOTS if self.app.custom_image_path(slot))
        self.customization_summary.setText(f"已设置 {count}/{len(CUSTOMIZATION_SLOTS)} 个区域")

    def open_customization_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("界面个性化设置")
        dialog.setMinimumWidth(650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("图片会保持比例，自动缩放到对应区域；透明 PNG 的效果最佳。"))
        names: dict[str, QLabel] = {}
        for slot, (label, target) in CUSTOMIZATION_SLOTS.items():
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{label}（{target[0]}×{target[1]}）"))
            name = QLabel(self.app.custom_image_name(slot))
            name.setObjectName("pageDescription")
            names[slot] = name
            row.addWidget(name, 1)
            choose = QPushButton("选择图片")
            clear = QPushButton("清除")
            choose.clicked.connect(lambda _checked=False, selected=slot: self._choose_image(selected, names))
            clear.clicked.connect(lambda _checked=False, selected=slot: self._clear_image(selected, names))
            row.addWidget(choose)
            row.addWidget(clear)
            layout.addLayout(row)
        close = QPushButton("完成")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, alignment=Qt.AlignmentFlag.AlignRight)
        self.custom_dialog = dialog
        dialog.exec()

    def _choose_image(self, slot: str, names: dict[str, QLabel]) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, f"选择{CUSTOMIZATION_SLOTS[slot][0]}", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if path and self.app.set_custom_image(slot, Path(path)):
            names[slot].setText(self.app.custom_image_name(slot))
            self.update_customization_summary()

    def _clear_image(self, slot: str, names: dict[str, QLabel]) -> None:
        if self.app.custom_image_path(slot):
            self.app.clear_custom_image(slot)
            names[slot].setText(self.app.custom_image_name(slot))
            self.update_customization_summary()

    def refresh_deepseek_models(self) -> None:
        api_key = self.deepseek_key.text().strip()
        if not api_key:
            self.warning("缺少 API Key", "请先填写 DeepSeek API Key。")
            return
        self.app.settings["deepseek_api_key"] = api_key
        self.refresh_models_button.setEnabled(False)
        self.test_model_button.setEnabled(False)
        self.set_status("正在获取 DeepSeek 可用模型列表…")

        def worker() -> None:
            try:
                self.signals.models.emit(fetch_deepseek_models(api_key))
            except Exception as exc:
                self.signals.models_error.emit(str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_deepseek_models(self, models: object) -> None:
        values = tuple(dict.fromkeys(str(value) for value in (models or ())))
        self.deepseek_model.clear()
        self.deepseek_model.addItems(values)
        if values:
            preferred = str(self.app.settings.get("deepseek_model", DEFAULT_DEEPSEEK_MODEL))
            self.deepseek_model.setCurrentText(preferred if preferred in values else values[0])
        self.refresh_models_button.setEnabled(True)
        self.test_model_button.setEnabled(True)
        self.set_status(f"已获取 {len(values)} 个 DeepSeek 模型")

    def _deepseek_models_failed(self, error: str) -> None:
        self.refresh_models_button.setEnabled(True)
        self.test_model_button.setEnabled(True)
        self.error("获取模型列表失败", error)

    def test_current_deepseek_model(self) -> None:
        api_key = self.deepseek_key.text().strip()
        model = self.deepseek_model.currentText().strip() or DEFAULT_DEEPSEEK_MODEL
        if not api_key:
            self.warning("缺少 API Key", "请先填写 DeepSeek API Key。")
            return
        self.app.settings["deepseek_api_key"] = api_key
        self.app.settings["deepseek_model"] = model
        self.refresh_models_button.setEnabled(False)
        self.test_model_button.setEnabled(False)
        self.set_status(f"正在测试 DeepSeek 模型：{model}")

        def worker() -> None:
            try:
                self.signals.tested.emit(model, test_deepseek_model(api_key, model))
            except Exception as exc:
                self.signals.test_error.emit(str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _deepseek_test_succeeded(self, model: str, instruction: str) -> None:
        self.refresh_models_button.setEnabled(True)
        self.test_model_button.setEnabled(True)
        self.set_status(f"DeepSeek 模型测试成功：{model}")
        self.info("DeepSeek 测试成功", f"模型：{model}\n返回的情绪指令：\n{instruction}")

    def _deepseek_test_failed(self, error: str) -> None:
        self.refresh_models_button.setEnabled(True)
        self.test_model_button.setEnabled(True)
        self.error("DeepSeek 测试失败", error)

    def save_settings(self) -> None:
        value = max(0.1, min(5.0, self.interval.value()))
        self.app.set_ocr_interval(value)
        self.app.settings["deepseek_api_key"] = self.deepseek_key.text().strip()
        self.app.settings["deepseek_model"] = self.deepseek_model.currentText().strip() or DEFAULT_DEEPSEEK_MODEL
        save_app_settings(self.app.settings)
        self.set_status("设置已保存")
