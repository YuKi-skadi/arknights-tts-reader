"""Voice generation workspace for the Qt interface."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)

from app_runtime import project_dir
from quality_analysis import DEFAULT_DEEPSEEK_MODEL, load_or_create_analysis
from qt_base import BasePanel
from voice_generation import (
    ENGINE_EDGE,
    ENGINE_LABELS,
    ENGINE_QWEN_06B,
    ENGINE_QWEN_17B,
    ENGINE_SYSTEM,
    GenerationInterrupted,
    GenerationPartiallyFailed,
    GPU_BACKEND_AUTO,
    GPU_BACKEND_LABELS,
    GPU_BACKEND_ROCM,
    QWEN_CLONE_VOICE,
    QWEN_MODE_CLONE,
    QWEN_MODE_CUSTOM,
    QWEN_MODE_LABELS,
    TTSBackendManager,
    generate_voice_pack,
    is_qwen_engine,
    locate_qwen_runtime,
    normalize_qwen_engine,
    qwen_model_size,
    resolve_gpu_backend,
    voice_options,
)
from voice_queue import VoiceQueueStore


class VoiceSignals(QObject):
    status = Signal(str)
    progress = Signal(int, int, str)
    refresh = Signal()
    finished = Signal()
    manifest = Signal(str)


class ConnectedVoiceGenerationPanel(BasePanel):
    CUSTOM_TEXT_EXTENSIONS = {".txt", ".md", ".log"}
    QUEUE_FILTERS = ("未完成", "已完成", "全部", "部分完成", "失败/中断")

    def __init__(self, app: object) -> None:
        self.signals = VoiceSignals()
        self.backend = TTSBackendManager(self._backend_status)
        self.story_items: list[Path] = []
        self.custom_text_items: list[Path] = []
        self.queue_store = VoiceQueueStore()
        self.queue_items, saved_queue_config = self.queue_store.load()
        self.visible_queue_indices: list[int] = []
        self.queue_thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.pause_event = threading.Event()
        self.current_queue_index: int | None = None
        self.user_stopped = False
        self.queue_config: dict[str, object] = {
            "engine": ENGINE_EDGE,
            "voice": voice_options(ENGINE_EDGE)[0],
            "speed": 1.0,
            "qwen_options": {},
            "optimize_quality": False,
        }
        self.queue_config.update(saved_queue_config)
        super().__init__(app, "语音生成", "选择剧情或自定义文本，锁定引擎、模型和显卡后逐句生成语音包。")
        self.build()
        self.signals.status.connect(self._status_ui)
        self.signals.progress.connect(self._progress_ui)
        self.signals.refresh.connect(self._refresh_queue_view)
        self.signals.finished.connect(self._queue_finished)
        self.signals.manifest.connect(lambda path: self.progress_text.setText(f"生成完成：{path}"))

    def build(self) -> None:
        options, layout = self.card("生成设置", "6GB 显存建议选择 Qwen3-TTS 0.6B")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.engine = QComboBox()
        engine_ids = (ENGINE_EDGE, ENGINE_QWEN_06B, ENGINE_QWEN_17B, ENGINE_SYSTEM)
        for engine in engine_ids:
            self.engine.addItem(ENGINE_LABELS[engine], engine)
        self.engine.currentIndexChanged.connect(self._on_engine_changed)
        self.qwen_mode_box = QComboBox()
        for mode, label in QWEN_MODE_LABELS.items():
            self.qwen_mode_box.addItem(label, mode)
        self.qwen_mode_box.currentIndexChanged.connect(self._on_qwen_mode_changed)
        self.gpu_backend = QComboBox()
        for backend, label in GPU_BACKEND_LABELS.items():
            self.gpu_backend.addItem(label, backend)
        self.gpu_backend.currentIndexChanged.connect(self._on_gpu_backend_changed)
        self.voice = QComboBox()
        self.reference_audio = QLineEdit()
        self.reference_audio.setPlaceholderText("声音克隆参考音频")
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_reference_audio)
        ref_row = QHBoxLayout()
        ref_row.addWidget(self.reference_audio, 1)
        ref_row.addWidget(choose)
        ref_widget = QWidget()
        ref_widget.setLayout(ref_row)
        self.reference_text = QLineEdit()
        self.reference_text.setPlaceholderText("可选：与参考音频对应的原文")
        self.optimize_quality = QCheckBox("使用 DeepSeek 优化每句情绪")
        self.generation_speed = QDoubleSpinBox()
        self.generation_speed.setRange(0.1, 2.0)
        self.generation_speed.setSingleStep(0.1)
        self.generation_speed.setValue(1.0)
        self.release_button = QPushButton("释放 Qwen 服务")
        self.release_button.clicked.connect(self.release_qwen)
        self.qwen_hint = QLabel("Qwen 首次生成会启动独立 GPU 服务。6GB 显存（如 RTX 3060 Laptop）请选择 0.6B；1.7B 通常需要更大显存。")
        self.qwen_hint.setWordWrap(True)
        self.qwen_hint.setObjectName("pageDescription")
        labels = (("语音引擎", self.engine), ("音色", self.voice), ("Qwen 模式", self.qwen_mode_box), ("显卡后端", self.gpu_backend), ("参考音频", ref_widget), ("参考文本", self.reference_text))
        grid.addWidget(QLabel("语音引擎"), 0, 0)
        grid.addWidget(self.engine, 0, 1)
        grid.addWidget(QLabel("音色"), 0, 2)
        grid.addWidget(self.voice, 0, 3)
        grid.addWidget(QLabel("Qwen 模式"), 1, 0)
        grid.addWidget(self.qwen_mode_box, 1, 1)
        grid.addWidget(QLabel("显卡后端"), 1, 2)
        grid.addWidget(self.gpu_backend, 1, 3)
        grid.addWidget(QLabel("参考音频"), 2, 0)
        grid.addWidget(ref_widget, 2, 1, 1, 3)
        grid.addWidget(QLabel("参考文本"), 3, 0)
        grid.addWidget(self.reference_text, 3, 1, 1, 3)
        grid.addWidget(self.optimize_quality, 4, 1, 1, 2)
        grid.addWidget(QLabel("生成语速"), 5, 0)
        grid.addWidget(self.generation_speed, 5, 1)
        grid.addWidget(self.qwen_hint, 6, 0, 1, 3)
        grid.addWidget(self.release_button, 6, 3)
        layout.addLayout(grid)
        self.add_card(options)

        stories, story_layout = self.card("已导出的关卡")
        self.stories = QListWidget()
        self.stories.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.stories.setMinimumHeight(130)
        story_layout.addWidget(self.stories)
        story_actions = QHBoxLayout()
        self.story_status = QLabel("尚未找到导出的剧情")
        self.story_status.setObjectName("pageDescription")
        refresh_stories = QPushButton("刷新关卡")
        self.add_queue_button = QPushButton("加入队列")
        refresh_stories.clicked.connect(self.refresh_stories)
        self.add_queue_button.clicked.connect(self.add_selected_to_queue)
        story_actions.addWidget(self.story_status, 1)
        story_actions.addWidget(refresh_stories)
        story_actions.addWidget(self.add_queue_button)
        story_layout.addLayout(story_actions)
        self.add_card(stories)

        custom, custom_layout = self.card("已导入的自定义文本")
        self.custom_texts = QListWidget()
        self.custom_texts.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.custom_texts.setMinimumHeight(100)
        custom_layout.addWidget(self.custom_texts)
        custom_actions = QHBoxLayout()
        self.custom_text_status = QLabel("尚未导入自定义文本")
        self.custom_text_status.setObjectName("pageDescription")
        refresh_text = QPushButton("刷新文本")
        import_text = QPushButton("导入文本")
        self.add_custom_queue_button = QPushButton("加入队列")
        refresh_text.clicked.connect(self.refresh_custom_texts)
        import_text.clicked.connect(self.import_custom_text)
        self.add_custom_queue_button.clicked.connect(self.add_selected_custom_to_queue)
        custom_actions.addWidget(self.custom_text_status, 1)
        custom_actions.addWidget(refresh_text)
        custom_actions.addWidget(import_text)
        custom_actions.addWidget(self.add_custom_queue_button)
        custom_layout.addLayout(custom_actions)
        self.add_card(custom)

        progress_card, progress_layout = self.card("生成进度")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress_text = QLabel("等待任务")
        self.progress_text.setObjectName("pageDescription")
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.progress_text)
        self.add_card(progress_card)

        queue, queue_layout = self.card("生成队列")
        queue_head = QHBoxLayout()
        self.queue_filter = QComboBox()
        self.queue_filter.addItems(self.QUEUE_FILTERS)
        self.queue_filter.currentTextChanged.connect(lambda _text: self._refresh_queue_view())
        queue_head.addWidget(QLabel("任务筛选"))
        queue_head.addWidget(self.queue_filter)
        queue_head.addStretch(1)
        queue_head.addWidget(QLabel("每项锁定生成方式；本轮失败不会自动重试"))
        queue_layout.addLayout(queue_head)
        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.queue_list.setMinimumHeight(170)
        queue_layout.addWidget(self.queue_list)
        self.queue_status = QLabel("队列为空")
        self.queue_status.setObjectName("pageDescription")
        queue_layout.addWidget(self.queue_status)
        actions = QHBoxLayout()
        up = QPushButton("上移")
        down = QPushButton("下移")
        remove = QPushButton("移除")
        up.clicked.connect(lambda: self.move_queue_item(-1))
        down.clicked.connect(lambda: self.move_queue_item(1))
        remove.clicked.connect(self.remove_queue_item)
        actions.addWidget(up)
        actions.addWidget(down)
        actions.addWidget(remove)
        actions.addStretch(1)
        inspect = QPushButton("查看日志 / 重试失败")
        inspect.clicked.connect(self.inspect_generation_result)
        self.queue_start_button = self.make_button("开始队列", primary=True)
        self.queue_pause_button = QPushButton("暂停")
        self.queue_stop_button = QPushButton("停止当前")
        self.queue_start_button.clicked.connect(self.start_queue)
        self.queue_pause_button.clicked.connect(self.toggle_pause)
        self.queue_stop_button.clicked.connect(self.stop_current)
        actions.addWidget(inspect)
        actions.addWidget(self.queue_start_button)
        actions.addWidget(self.queue_pause_button)
        actions.addWidget(self.queue_stop_button)
        queue_layout.addLayout(actions)
        self.add_card(queue)
        self.add_stretch()

        self._update_voice_options()
        self._apply_saved_queue_config()
        self.refresh_stories()
        self.refresh_custom_texts()
        self._update_qwen_controls()
        self._refresh_queue_view()
        self.queue_pause_button.setEnabled(False)
        self.queue_stop_button.setEnabled(False)

    def _on_engine_changed(self, _index: int = -1) -> None:
        self._update_voice_options()
        self._update_qwen_controls()

    def _on_gpu_backend_changed(self, _index: int = -1) -> None:
        backend = self._selected_gpu_backend()
        if backend == GPU_BACKEND_AUTO:
            self.set_status(f"已选择自动检测，当前将使用 {GPU_BACKEND_LABELS.get(resolve_gpu_backend(GPU_BACKEND_AUTO), '未知')}")
        else:
            self.set_status(f"Qwen3-TTS 将使用 {GPU_BACKEND_LABELS.get(backend, backend)}")

    def _on_qwen_mode_changed(self, _index: int = -1) -> None:
        self._update_voice_options()
        self._update_qwen_controls()

    def choose_reference_audio(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择声音克隆参考音频", "", "音频文件 (*.wav *.mp3 *.flac *.m4a)")
        if path:
            self.reference_audio.setText(str(Path(path).resolve()))

    def _selected_gpu_backend(self) -> str:
        return str(self.gpu_backend.currentData() or GPU_BACKEND_AUTO)

    def _selected_engine(self) -> str:
        return str(self.engine.currentData() or ENGINE_EDGE)

    def _update_qwen_controls(self) -> None:
        qwen_enabled = is_qwen_engine(self._selected_engine())
        clone_enabled = qwen_enabled and str(self.qwen_mode_box.currentData()) == QWEN_MODE_CLONE
        for widget in (self.qwen_mode_box, self.gpu_backend):
            widget.setEnabled(qwen_enabled)
        self.reference_audio.setEnabled(clone_enabled)
        self.reference_text.setEnabled(clone_enabled)
        self.optimize_quality.setEnabled(qwen_enabled)
        self.release_button.setEnabled(qwen_enabled)
        self.qwen_hint.setVisible(qwen_enabled)

    def _update_voice_options(self) -> None:
        engine = self._selected_engine()
        clone = is_qwen_engine(engine) and str(self.qwen_mode_box.currentData()) == QWEN_MODE_CLONE
        values = (QWEN_CLONE_VOICE,) if clone else voice_options(engine)
        current = self.voice.currentText()
        self.voice.blockSignals(True)
        self.voice.clear()
        self.voice.addItems(values)
        if current in values:
            self.voice.setCurrentText(current)
        self.voice.blockSignals(False)

    def refresh_stories(self) -> None:
        root = project_dir() / "data" / "stories"
        self.story_items = sorted(root.rglob("*.json")) if root.exists() else []
        self.stories.clear()
        if not self.story_items:
            self.stories.addItem("尚未找到导出的剧情，请先在“剧情下载”中导出关卡")
            self.story_status.setText("尚未找到导出的剧情")
            self.add_queue_button.setEnabled(False)
            return
        for path in self.story_items:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                segment = payload.get("segment") or {}
                event_name = segment.get("event_name") or path.parent.name
                code = segment.get("story_code") or segment.get("story_txt") or path.stem
                count = len([line for line in payload.get("lines", []) if line.get("should_speak", line.get("prop", "").lower() != "name")])
                self.stories.addItem(f"{event_name} / {code}（{count} 条对白）")
            except Exception:
                self.stories.addItem(f"{path.name}（文件格式异常）")
        self.stories.setCurrentRow(0)
        self.story_status.setText(f"已找到 {len(self.story_items)} 个关卡")
        self.add_queue_button.setEnabled(True)

    @staticmethod
    def _read_custom_text_lines(path: Path) -> list[str]:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = path.read_text(encoding="gb18030")
        return [line for line in content.splitlines() if line.strip()]

    def refresh_custom_texts(self) -> None:
        root = project_dir() / "data" / "custom_texts"
        self.custom_text_items = sorted([path for path in root.iterdir() if path.is_file() and path.suffix.lower() in self.CUSTOM_TEXT_EXTENSIONS], key=lambda path: path.name.casefold()) if root.exists() else []
        self.custom_texts.clear()
        if not self.custom_text_items:
            self.custom_texts.addItem("尚未导入自定义文本，请点击“导入文本”")
            self.custom_text_status.setText("尚未导入自定义文本")
            self.add_custom_queue_button.setEnabled(False)
            return
        for path in self.custom_text_items:
            try:
                count = len(self._read_custom_text_lines(path))
            except (OSError, UnicodeError):
                count = 0
            self.custom_texts.addItem(f"{path.stem}（{count} 条文本）")
        self.custom_texts.setCurrentRow(0)
        self.custom_text_status.setText(f"已导入 {len(self.custom_text_items)} 个自定义文本")
        self.add_custom_queue_button.setEnabled(True)

    def import_custom_text(self) -> None:
        source, _filter = QFileDialog.getOpenFileName(self, "导入自定义文本", "", "文本文件 (*.txt *.md *.log)")
        if not source:
            return
        source_path = Path(source)
        try:
            lines = self._read_custom_text_lines(source_path)
            if not lines:
                self.warning("文本为空", "导入的文件没有可生成语音的文本内容。")
                return
            root = project_dir() / "data" / "custom_texts"
            root.mkdir(parents=True, exist_ok=True)
            destination = root / source_path.name
            duplicate_index = 1
            while destination.exists() and source_path.resolve() != destination.resolve():
                destination = root / f"{source_path.stem}（{duplicate_index}）{source_path.suffix}"
                duplicate_index += 1
            if source_path.resolve() != destination.resolve():
                shutil.copy2(source_path, destination)
        except (OSError, UnicodeError, ValueError) as exc:
            self.error("导入失败", f"无法导入文本文件：\n\n{exc}")
            return
        self.refresh_custom_texts()
        self.set_status(f"已导入自定义文本：{destination.name}，共 {len(lines)} 条")

    def add_selected_custom_to_queue(self) -> None:
        paths = [self.custom_text_items[index.row()] for index in self.custom_texts.selectedIndexes() if index.row() < len(self.custom_text_items)]
        if not paths:
            self.set_status("请先选择自定义文本，可按住 Ctrl 多选")
            return
        self._add_paths_to_queue(paths, "自定义文本")

    def add_selected_to_queue(self) -> None:
        paths = [self.story_items[index.row()] for index in self.stories.selectedIndexes() if index.row() < len(self.story_items)]
        if not paths:
            self.set_status("请先选择关卡，可按住 Ctrl 多选")
            return
        self._add_paths_to_queue(paths, "关卡")

    def _add_paths_to_queue(self, paths: list[Path], source_label: str) -> None:
        existing = {str(item["path"]) for item in self.queue_items}
        config = self._current_generation_config()
        added = 0
        for path in paths:
            if str(path) in existing:
                continue
            self.queue_items.append({"path": path, "status": "待处理", "generation_config": dict(config)})
            existing.add(str(path))
            added += 1
        self.queue_config = dict(config)
        self._save_queue_state()
        self._refresh_queue_view()
        self.set_status(f"已加入语音生成队列 {added} 项（{source_label}）")

    def _current_generation_config(self) -> dict[str, object]:
        engine = self._selected_engine()
        options = {"mode": str(self.qwen_mode_box.currentData() or QWEN_MODE_CUSTOM), "reference_audio": self.reference_audio.text().strip(), "reference_text": self.reference_text.text().strip()}
        if is_qwen_engine(engine):
            options["backend"] = resolve_gpu_backend(self._selected_gpu_backend())
        return {"engine": engine, "voice": self.voice.currentText() or voice_options(engine)[0], "speed": self.generation_speed.value(), "qwen_options": options, "optimize_quality": self.optimize_quality.isChecked()}

    def _task_generation_config(self, item: dict) -> dict[str, object]:
        config = item.get("generation_config")
        if not isinstance(config, dict):
            config = dict(self.queue_config)
            item["generation_config"] = config
        engine = normalize_qwen_engine(str(config.get("engine", ENGINE_EDGE)))
        config["engine"] = engine
        if is_qwen_engine(engine):
            options = dict(config.get("qwen_options") or {})
            options.setdefault("backend", GPU_BACKEND_ROCM)
            config["qwen_options"] = options
        return config

    def _apply_generation_config(self, config: dict[str, object]) -> None:
        engine = normalize_qwen_engine(str(config.get("engine", ENGINE_EDGE)))
        index = self.engine.findData(engine)
        if index >= 0:
            self.engine.setCurrentIndex(index)
        self._update_voice_options()
        voice = str(config.get("voice", ""))
        if self.voice.findText(voice) >= 0:
            self.voice.setCurrentText(voice)
        self.generation_speed.setValue(max(0.1, min(2.0, float(config.get("speed", 1.0)))))
        options = config.get("qwen_options")
        if isinstance(options, dict):
            self.qwen_mode_box.setCurrentIndex(max(0, self.qwen_mode_box.findData(str(options.get("mode", QWEN_MODE_CUSTOM)))))
            reference = str(options.get("reference_audio", ""))
            self.reference_audio.setText(reference if reference and Path(reference).is_file() else "")
            self.reference_text.setText(str(options.get("reference_text", "")))
            backend = str(options.get("backend", GPU_BACKEND_AUTO))
            index = self.gpu_backend.findData(backend)
            if index >= 0:
                self.gpu_backend.setCurrentIndex(index)
        self.optimize_quality.setChecked(bool(config.get("optimize_quality", False)))
        self._update_qwen_controls()

    def _apply_saved_queue_config(self) -> None:
        self._apply_generation_config(self.queue_config)

    def _generation_config_label(self, config: dict[str, object]) -> str:
        engine = normalize_qwen_engine(str(config.get("engine", ENGINE_EDGE)))
        label = ENGINE_LABELS.get(engine, engine)
        voice = str(config.get("voice", ""))
        options = config.get("qwen_options")
        if is_qwen_engine(engine) and isinstance(options, dict):
            mode = str(options.get("mode", QWEN_MODE_CUSTOM))
            backend = str(options.get("backend", GPU_BACKEND_AUTO))
            label = f"{label}/{GPU_BACKEND_LABELS.get(backend, backend)}/{QWEN_MODE_LABELS.get(mode, mode)}"
        return f"{label}/{voice}"

    def _queue_label(self, item: dict) -> str:
        path = Path(item["path"])
        source = f"自定义文本 / {path.stem}" if path.parent.name == "custom_texts" else f"{path.parent.parent.name} / {path.stem}"
        missing = "（文件不存在）" if not path.exists() else ""
        return f"[{item.get('status', '待处理')}] {source} · {self._generation_config_label(self._task_generation_config(item))}{missing}"

    def _save_queue_state(self) -> None:
        try:
            self.queue_store.save(self.queue_items, self.queue_config)
        except (OSError, TypeError, ValueError) as exc:
            self.set_status(f"生成队列保存失败：{exc}")

    def save_queue_state(self) -> None:
        self._save_queue_state()

    def _queue_filter_matches(self, status: str, queue_filter: str) -> bool:
        if queue_filter == "全部":
            return True
        if queue_filter == "已完成":
            return status == "已完成"
        if queue_filter == "部分完成":
            return status == "部分完成"
        if queue_filter == "失败/中断":
            return status in {"失败", "中断", "已停止"}
        return status != "已完成"

    def _selected_queue_index(self) -> int | None:
        selected = self.queue_list.selectedIndexes()
        return self.visible_queue_indices[selected[0].row()] if selected and selected[0].row() < len(self.visible_queue_indices) else None

    def _refresh_queue_view(self) -> None:
        selected = self._selected_queue_index()
        queue_filter = self.queue_filter.currentText()
        self.visible_queue_indices = [index for index, item in enumerate(self.queue_items) if self._queue_filter_matches(str(item.get("status", "待处理")), queue_filter)]
        self.queue_list.clear()
        for index in self.visible_queue_indices:
            self.queue_list.addItem(self._queue_label(self.queue_items[index]))
        restore = self.current_queue_index if self.current_queue_index in self.visible_queue_indices else selected
        if restore in self.visible_queue_indices:
            self.queue_list.setCurrentRow(self.visible_queue_indices.index(restore))
        pending = sum(item.get("status") != "已完成" for item in self.queue_items)
        completed = len(self.queue_items) - pending
        self.queue_status.setText(f"筛选：{queue_filter} · 当前 {len(self.visible_queue_indices)} 项 · 未完成 {pending} 项 · 已完成 {completed} 项")

    def move_queue_item(self, direction: int) -> None:
        if self.queue_filter.currentText() == "已完成" or self.current_queue_index is not None:
            return
        index = self._selected_queue_index()
        if index is None:
            return
        position = self.visible_queue_indices.index(index)
        target_position = position + direction
        if not 0 <= target_position < len(self.visible_queue_indices):
            return
        target = self.visible_queue_indices[target_position]
        self.queue_items[index], self.queue_items[target] = self.queue_items[target], self.queue_items[index]
        self._save_queue_state()
        self._refresh_queue_view()
        self.queue_list.setCurrentRow(target_position)

    def remove_queue_item(self) -> None:
        if self.current_queue_index is not None:
            return
        index = self._selected_queue_index()
        if index is None:
            return
        self.queue_items.pop(index)
        self._save_queue_state()
        self._refresh_queue_view()

    def _next_queue_index(self, excluded: set[int] | None = None) -> int | None:
        excluded = excluded or set()
        for index, item in enumerate(self.queue_items):
            if index not in excluded and item.get("status") != "已完成":
                return index
        return None

    def start_queue(self) -> None:
        if self.queue_thread is not None and self.queue_thread.is_alive():
            self.set_status("当前队列正在运行；暂停后请点击“继续”")
            return
        index = self._next_queue_index()
        if index is None:
            self.set_status("生成队列中没有待处理任务")
            return
        config = self._task_generation_config(self.queue_items[index])
        engine = str(config.get("engine", ENGINE_EDGE))
        options = dict(config.get("qwen_options") or {})
        mode = str(options.get("mode", QWEN_MODE_CUSTOM))
        if is_qwen_engine(engine) and mode == QWEN_MODE_CLONE:
            reference = str(options.get("reference_audio", ""))
            if not reference or not Path(reference).is_file():
                replacement = self.reference_audio.text().strip()
                if replacement and Path(replacement).is_file():
                    options["reference_audio"] = replacement
                    config["qwen_options"] = options
                else:
                    self.warning("缺少参考音频", "当前任务原先使用的参考音频已找不到，请重新选择后再继续。")
                    return
        if is_qwen_engine(engine):
            backend = str(options.get("backend", GPU_BACKEND_ROCM))
            if locate_qwen_runtime(mode, qwen_model_size(engine), backend) is None:
                self.warning("缺少 Qwen3-TTS 运行时", f"找不到 Qwen3-TTS {qwen_model_size(engine)} 的 {backend.upper()} 运行时或模型文件。")
                return
        self.queue_config = dict(config)
        self._apply_generation_config(config)
        self._save_queue_state()
        self.cancelled.clear()
        self.pause_event.clear()
        self.user_stopped = False
        self.queue_start_button.setEnabled(False)
        self.queue_pause_button.setEnabled(True)
        self.queue_stop_button.setEnabled(True)
        self.queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.queue_thread.start()

    def toggle_pause(self) -> None:
        if self.current_queue_index is None:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.queue_pause_button.setText("暂停")
            self.set_status("已继续当前语音生成任务")
        else:
            self.pause_event.set()
            self.queue_pause_button.setText("继续")
            self.set_status("已暂停当前语音生成任务")

    def stop_current(self) -> None:
        if self.current_queue_index is None:
            return
        self.user_stopped = True
        self.cancelled.set()
        self.pause_event.clear()
        self.set_status("正在停止当前语音生成任务，当前句完成后生效")

    def _queue_worker(self) -> None:
        attempted: set[int] = set()
        while True:
            index = self._next_queue_index(attempted)
            if index is None:
                self.signals.finished.emit()
                return
            attempted.add(index)
            self.current_queue_index = index
            item = self.queue_items[index]
            item["status"] = "已暂停" if self.pause_event.is_set() else "生成中"
            config = self._task_generation_config(item)
            self.queue_config = dict(config)
            self._save_queue_state()
            self.signals.refresh.emit()
            try:
                engine = str(config.get("engine", ENGINE_EDGE))
                voice = str(config.get("voice", voice_options(ENGINE_EDGE)[0]))
                speed = float(config.get("speed", 1.0))
                options = dict(config.get("qwen_options") or {})
                optimize = bool(config.get("optimize_quality", False))
                line_instructions = {}
                story_path = Path(item["path"])
                if optimize and is_qwen_engine(engine):
                    line_instructions = load_or_create_analysis(story_path, project_dir() / "data" / "tts_analysis", str(self.app.settings.get("deepseek_api_key", "")), self._backend_status, model=str(self.app.settings.get("deepseek_model", DEFAULT_DEEPSEEK_MODEL)))
                manifest = generate_voice_pack(story_path, project_dir() / "data" / "voice_packs", engine, voice, speed, self._generation_progress, self.cancelled, self.backend, self.pause_event, options, line_instructions)
                item["manifest"] = manifest
                item["status"] = "已完成"
                self.signals.manifest.emit(str(manifest))
            except GenerationInterrupted as exc:
                item["status"] = "中断"
                item["manifest"] = exc.manifest_path
                self._backend_status("当前语音生成已中断，未安排自动关机")
            except GenerationPartiallyFailed as exc:
                item["status"] = "部分完成"
                item["manifest"] = exc.manifest_path
                self._backend_status(f"语音生成部分完成：{len(exc.failed_entries)} 个片段失败")
            except Exception as exc:
                item["status"] = "已停止" if self.cancelled.is_set() else "部分完成"
                item["last_error"] = str(exc)[:1000]
                self._backend_status(f"语音生成{item['status']}：{exc}")
            finally:
                self.cancelled.clear()
                self.current_queue_index = None
                self._save_queue_state()
                self.signals.refresh.emit()

    def _queue_finished(self) -> None:
        self.queue_thread = None
        self.queue_start_button.setEnabled(True)
        self.queue_pause_button.setEnabled(False)
        self.queue_pause_button.setText("暂停")
        self.queue_stop_button.setEnabled(False)
        self.current_queue_index = None
        self._refresh_queue_view()
        successful = bool(self.queue_items) and all(item.get("status") == "已完成" for item in self.queue_items)
        if successful:
            self.progress.setValue(100)
            self.progress_text.setText("全部语音生成完成")
            self.set_status("全部语音生成完成")
        else:
            self.progress_text.setText("本轮队列已结束，请检查部分完成或中断任务")
            self.set_status("本轮语音生成队列已结束，请检查部分完成或中断任务")

    def _generation_progress(self, done: int, total: int, text: str) -> None:
        self.signals.progress.emit(done, total, text)

    def _progress_ui(self, done: int, total: int, text: str) -> None:
        self.progress.setValue(round(done * 100 / total) if total else 0)
        self.progress_text.setText(text)
        self.set_status(text)

    def _backend_status(self, text: str) -> None:
        self.signals.status.emit(text)

    def _status_ui(self, text: str) -> None:
        self.set_status(text)

    def release_qwen(self) -> None:
        self.set_status("正在释放 Qwen3-TTS 服务…")
        threading.Thread(target=self.backend.release_qwen, daemon=True).start()

    def inspect_generation_result(self) -> None:
        candidates = [item for item in self.queue_items if item.get("status") in {"部分完成", "失败", "中断", "已停止"}]
        if not candidates:
            self.info("生成结果", "当前没有可查看的失败或中断任务。")
            return
        item = candidates[0]
        manifest_path = Path(item.get("manifest", "")) if item.get("manifest") else None
        failed_count = 0
        if manifest_path and manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                failed_count = sum(entry.get("status") in {"failed", "interrupted"} for entry in payload.get("lines") or [])
            except (OSError, ValueError):
                pass
        if QMessageBox.question(self, "发现未完成片段", f"任务：{Path(item['path']).stem}\n\n未成功片段：{failed_count or '未知'} 个\n\n是否重新加入队列？已完成片段会直接复用。") != QMessageBox.StandardButton.Yes:
            return
        item["status"] = "待处理"
        self._save_queue_state()
        self._refresh_queue_view()
        self.set_status("已将失败片段所在任务重新加入队列")
        if self.queue_thread is None or not self.queue_thread.is_alive():
            self.start_queue()
