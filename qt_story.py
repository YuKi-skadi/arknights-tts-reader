"""Story download workspace for the Qt interface."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QProgressBar, QPushButton

from app_runtime import project_dir, atomic_json_save, read_json_object
from prts_catalog import PRTSStoryClient, PRTSTaskCancelled
from story_catalog import StorySegment
from qt_base import BasePanel


class StorySignals(QObject):
    progress = Signal(str, int, int)
    log = Signal(str)
    catalog = Signal(object, object)
    segments = Signal(object)
    queue_update = Signal()
    finished = Signal()


class ConnectedStoryDownloadPanel(BasePanel):
    CATEGORY_FILTERS = {
        "主题曲": {"maintheme"},
        "别传": {"sidestory", "intermezzi"},
        "故事集": {"storyset"},
    }

    def __init__(self, app: object) -> None:
        self.signals = StorySignals()
        self.client = PRTSStoryClient(cache_dir=project_dir() / "data" / "cache" / "prts", progress_callback=self._download_progress)
        self.all_events: list[dict] = []
        self.event_by_label: dict[str, dict] = {}
        self.segment_items: list[StorySegment] = []
        self.queue_items: list[dict] = []
        self.queue_path = project_dir() / "data" / "story_download_queue.json"
        self.queue_lock = threading.RLock()
        self.shutting_down = False
        self._load_queue_state()
        self.queue_thread: threading.Thread | None = None
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.current_queue_index: int | None = None
        super().__init__(app, "剧情下载", "从 PRTS 剧情目录选择主题曲、别传或故事集，再加入下载队列。")
        self.build()
        self._connect_signals()
        self._refresh_queue_view()

    def _load_queue_state(self) -> None:
        for path in (self.queue_path, self.queue_path.with_name(self.queue_path.name + ".bak")):
            try:
                payload = read_json_object(path)
                items = []
                for raw in payload["items"]:
                    item = dict(raw)
                    item["segment"] = StorySegment(**item["segment"])
                    if item.get("status") in {"下载中", "已暂停"}:
                        item["status"] = "待处理"
                    items.append(item)
                self.queue_items = items
                break
            except (OSError, ValueError, TypeError, KeyError):
                continue

    def save_queue_state(self) -> None:
        with self.queue_lock:
            try:
                atomic_json_save(self.queue_path, {"version": 1, "items": [
                    {**item, "segment": item["segment"].to_dict()} for item in self.queue_items]}, backup=True)
            except (OSError, ValueError) as exc:
                self.stop_event.set()
                self.signals.log.emit(f"下载队列保存失败：{exc}")

    def request_shutdown(self) -> None:
        self.shutting_down = True
        self.stop_event.set()
        self.pause_event.clear()
        self.save_queue_state()

    def _connect_signals(self) -> None:
        self.signals.progress.connect(self._update_progress)
        self.signals.log.connect(self._append_log)
        self.signals.catalog.connect(self._catalog_ready)
        self.signals.queue_update.connect(self._refresh_queue_view)
        self.signals.finished.connect(self._queue_finished)

    def build(self) -> None:
        filters, layout = self.card("剧情目录")
        row = QHBoxLayout()
        self.category = QComboBox()
        self.category.addItems(("主题曲", "别传", "故事集"))
        self.category.currentTextChanged.connect(self._refresh_activity_values)
        self.activity = QComboBox()
        self.activity.setMinimumWidth(310)
        self.activity.currentTextChanged.connect(self._on_activity_changed)
        self.refresh_button = QPushButton("刷新目录")
        self.refresh_button.clicked.connect(self.refresh_catalog)
        row.addWidget(QLabel("剧情分类"))
        row.addWidget(self.category)
        row.addWidget(QLabel("活动名称"))
        row.addWidget(self.activity, 1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.add_card(filters)

        list_card, list_layout = self.card("关卡 / 剧情段落")
        self.segments = QListWidget()
        self.segments.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.segments.setMinimumHeight(210)
        list_layout.addWidget(self.segments, 1)
        action_row = QHBoxLayout()
        self.download_status = QLabel("尚未连接剧情目录")
        self.download_status.setObjectName("pageDescription")
        self.export_button = QPushButton("加入下载队列")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.add_selected_to_queue)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        action_row.addWidget(self.download_status, 1)
        action_row.addWidget(self.download_progress, 1)
        action_row.addWidget(self.export_button)
        list_layout.addLayout(action_row)
        self.add_card(list_card)

        log_card, log_layout = self.card("下载日志")
        self.download_log = QPlainTextEdit()
        self.download_log.setReadOnly(True)
        self.download_log.setMaximumBlockCount(100)
        self.download_log.setMinimumHeight(100)
        log_layout.addWidget(self.download_log)
        self._append_log("等待操作。点击“刷新目录”读取剧情活动。")
        self.add_card(log_card)

        queue_card, queue_layout = self.card("下载队列")
        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(150)
        queue_layout.addWidget(self.queue_list)
        self.queue_status = QLabel("队列为空")
        self.queue_status.setObjectName("pageDescription")
        queue_layout.addWidget(self.queue_status)
        edit = QHBoxLayout()
        add = QPushButton("加入队列")
        up = QPushButton("上移")
        down = QPushButton("下移")
        remove = QPushButton("移除")
        add.clicked.connect(self.add_selected_to_queue)
        up.clicked.connect(lambda: self.move_queue_item(-1))
        down.clicked.connect(lambda: self.move_queue_item(1))
        remove.clicked.connect(self.remove_queue_item)
        edit.addWidget(add)
        edit.addWidget(up)
        edit.addWidget(down)
        edit.addWidget(remove)
        edit.addStretch(1)
        self.queue_start_button = self.make_button("开始队列", primary=True)
        self.queue_pause_button = QPushButton("暂停")
        self.queue_stop_button = QPushButton("停止队列")
        self.queue_pause_button.setEnabled(False)
        self.queue_stop_button.setEnabled(False)
        self.queue_start_button.clicked.connect(self.start_queue)
        self.queue_pause_button.clicked.connect(self.toggle_pause)
        self.queue_stop_button.clicked.connect(self.stop_current)
        edit.addWidget(self.queue_start_button)
        edit.addWidget(self.queue_pause_button)
        edit.addWidget(self.queue_stop_button)
        queue_layout.addLayout(edit)
        self.add_card(queue_card)
        self.add_stretch()

    def _append_log(self, text: str) -> None:
        self.download_log.appendPlainText(text)

    def _download_progress(self, message: str, current: int = 0, total: int = 0) -> None:
        self.signals.progress.emit(message, current, total)
        if not message.startswith("正在下载"):
            self.signals.log.emit(message)

    def _update_progress(self, message: str, current: int, total: int) -> None:
        if total > 0:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(min(100, round(current * 100 / total)))
            self.download_status.setText(f"{message} ({self.download_progress.value()}%)")
        else:
            self.download_progress.setRange(0, 0)
            self.download_status.setText(message)

    def refresh_catalog(self) -> None:
        if self.queue_thread is not None and self.queue_thread.is_alive():
            return
        self.refresh_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self._append_log("开始读取 PRTS 剧情目录…")
        self.download_status.setText("正在读取剧情目录…")
        self.set_status("正在读取 PRTS 剧情目录，首次读取可能需要一点时间")

        def worker() -> None:
            try:
                self.client.load_catalog(refresh=True)
                self.signals.catalog.emit(self.client.list_events(), None)
            except Exception as exc:
                self.signals.catalog.emit([], exc)
        threading.Thread(target=worker, daemon=True).start()

    def _catalog_ready(self, events: object, error: object) -> None:
        self.refresh_button.setEnabled(True)
        if error is not None:
            self.download_progress.setValue(0)
            self._append_log(f"读取目录失败：{error}")
            self.download_status.setText("剧情目录读取失败")
            self.error("读取失败", f"无法读取剧情目录。\n\n{error}")
            return
        self.all_events = list(events or [])
        self._refresh_activity_values()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(100)
        self._append_log(f"目录读取完成，共 {len(self.all_events)} 个活动。")
        self.download_status.setText(f"已读取 {len(self.all_events)} 个活动")
        self.set_status(f"剧情目录读取完成，共 {len(self.all_events)} 个活动")

    def _refresh_activity_values(self, _value: str = "") -> None:
        allowed = self.CATEGORY_FILTERS.get(self.category.currentText(), set())
        matching = [event for event in self.all_events if event.get("category") in allowed]
        self.event_by_label.clear()
        labels: list[str] = []
        for event in matching:
            label = str(event.get("name") or event.get("id") or "未命名活动")
            if label in self.event_by_label:
                label = f"{label} [{event.get('id')}]"
            self.event_by_label[label] = event
            labels.append(label)
        self.activity.blockSignals(True)
        self.activity.clear()
        self.activity.addItems(labels or ["暂无活动，请先刷新目录"])
        self.activity.blockSignals(False)
        self._clear_segments()
        if matching:
            self._load_segments(str(matching[0]["id"]))

    def _on_activity_changed(self, label: str) -> None:
        event = self.event_by_label.get(label)
        if event:
            self._load_segments(str(event["id"]))

    def _clear_segments(self) -> None:
        self.segment_items = []
        self.segments.clear()
        self.export_button.setEnabled(False)

    def _load_segments(self, event_id: str) -> None:
        self._clear_segments()
        try:
            segments = self.client.list_segments(event_id)
        except Exception as exc:
            self.segments.addItem(f"读取关卡失败：{exc}")
            return
        self.segment_items = segments
        if not segments:
            self.segments.addItem("该活动没有可用的剧情段落")
            return
        for segment in segments:
            code = segment.story_code or segment.story_id or "未命名关卡"
            name = segment.story_name or "未命名剧情段落"
            label = f"[{code}] {name}"
            if segment.tag:
                label += f" · {segment.tag}"
            self.segments.addItem(label)
        self.segments.setCurrentRow(0)
        self.export_button.setEnabled(True)

    def _queue_label(self, item: dict) -> str:
        segment = item["segment"]
        return f"[{item.get('status', '待处理')}] {segment.event_name} / {segment.story_code or segment.story_txt}"

    def _refresh_queue_view(self) -> None:
        self.queue_list.clear()
        for item in self.queue_items:
            self.queue_list.addItem(self._queue_label(item))
        if self.current_queue_index is not None and self.current_queue_index < len(self.queue_items):
            self.queue_list.setCurrentRow(self.current_queue_index)
        pending = sum(item.get("status") == "待处理" for item in self.queue_items)
        self.queue_status.setText(f"共 {len(self.queue_items)} 项，待处理 {pending} 项")

    def add_selected_to_queue(self) -> None:
        indexes = [index.row() for index in self.segments.selectedIndexes()]
        if not indexes or not self.segment_items:
            self.set_status("请先选择剧情段落，可按住 Ctrl 多选")
            return
        existing = {item["segment"].story_id for item in self.queue_items}
        added = 0
        for index in indexes:
            segment = self.segment_items[index]
            if segment.story_id in existing:
                continue
            self.queue_items.append({"segment": segment, "status": "待处理"})
            existing.add(segment.story_id)
            added += 1
        self._refresh_queue_view()
        self.set_status(f"已加入下载队列 {added} 项")
        self.save_queue_state()

    def move_queue_item(self, direction: int) -> None:
        selected = self.queue_list.currentRow()
        if selected < 0 or self.queue_thread is not None:
            return
        target = selected + direction
        if not 0 <= target < len(self.queue_items):
            return
        self.queue_items[selected], self.queue_items[target] = self.queue_items[target], self.queue_items[selected]
        self._refresh_queue_view()
        self.queue_list.setCurrentRow(target)
        self.save_queue_state()

    def remove_queue_item(self) -> None:
        selected = self.queue_list.currentRow()
        if selected < 0 or self.queue_thread is not None:
            return
        self.queue_items.pop(selected)
        self.save_queue_state()
        self._refresh_queue_view()

    def _next_queue_index(self) -> int | None:
        for index, item in enumerate(self.queue_items):
            if item.get("status") == "待处理":
                return index
        return None

    def start_queue(self) -> None:
        if self.shutting_down:
            return
        if self.queue_thread is not None and self.queue_thread.is_alive():
            self.set_status("下载队列正在运行，暂停后请点击“继续”")
            return
        for item in self.queue_items:
            if item.get("status") in {"已停止", "失败"}:
                item["status"] = "待处理"
        if self._next_queue_index() is None:
            self.set_status("下载队列中没有待处理任务")
            return
        self.pause_event.clear()
        self.stop_event.clear()
        self.queue_start_button.setEnabled(False)
        self.queue_pause_button.setEnabled(True)
        self.queue_stop_button.setEnabled(True)
        self.refresh_button.setEnabled(False)
        self.queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.queue_thread.start()

    def toggle_pause(self) -> None:
        if self.current_queue_index is None:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.queue_items[self.current_queue_index]["status"] = "下载中"
            self.queue_pause_button.setText("暂停")
            self._append_log("已继续当前下载任务。")
        else:
            self.pause_event.set()
            self.queue_items[self.current_queue_index]["status"] = "已暂停"
            self.queue_pause_button.setText("继续")
            self._append_log("已暂停当前下载任务。")
        self.save_queue_state()
        self._refresh_queue_view()

    def stop_current(self) -> None:
        if self.current_queue_index is None:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self._append_log("正在停止当前下载任务，当前网络数据块结束后生效。")

    def _queue_worker(self) -> None:
        while True:
            if self.shutting_down or self.stop_event.is_set():
                self.signals.finished.emit()
                return
            index = self._next_queue_index()
            if index is None:
                self.signals.finished.emit()
                return
            self.current_queue_index = index
            item = self.queue_items[index]
            segment = item["segment"]
            item["status"] = "下载中"
            self.save_queue_state()
            self.client.set_task_controls(self.pause_event, self.stop_event)
            self.signals.queue_update.emit()
            self.signals.log.emit(f"开始下载：{segment.event_name} / {segment.story_code or segment.story_txt}")
            try:
                document = self.client.load_document(segment)
                category_name = {"maintheme": "主题曲", "sidestory": "别传", "storyset": "故事集"}.get(segment.category, "其他")
                folder = project_dir() / "data" / "stories" / self.client._safe_name(category_name) / self.client._safe_name(segment.event_name, segment.event_id)
                folder.mkdir(parents=True, exist_ok=True)
                output = folder / f"{self.client._safe_name(segment.story_code or segment.story_name or segment.story_id)}.json"
                atomic_json_save(output, document.to_dict())
                item["status"] = "已完成"
                self.signals.log.emit(f"下载完成：{output}")
            except PRTSTaskCancelled:
                item["status"] = "已停止"
                self.signals.log.emit(f"已停止：{segment.story_code or segment.story_txt}")
            except Exception as exc:
                item["status"] = "失败"
                self.signals.log.emit(f"下载失败：{exc}")
            finally:
                self.pause_event.clear()
                self.current_queue_index = None
                self.save_queue_state()
                self.signals.queue_update.emit()

    def _queue_finished(self) -> None:
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(100)
        self.queue_thread = None
        self.queue_start_button.setEnabled(True)
        self.queue_pause_button.setEnabled(False)
        self.queue_pause_button.setText("暂停")
        self.queue_stop_button.setEnabled(False)
        self.refresh_button.setEnabled(True)
        self.current_queue_index = None
        self._refresh_queue_view()
        message = "下载队列已完成" if all(item.get("status") == "已完成" for item in self.queue_items) else "下载队列已停止或有失败任务，可点击开始继续"
        self.download_status.setText(message)
        self.set_status(message)
