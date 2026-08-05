"""Arknights TTS Reader ver0.5 - modular desktop UI shell.

This first ver0.5 build intentionally contains only the application shell.
Each feature owns a workspace panel, while the reader controls stay global in
the lower-left corner so they remain available when switching modules.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, Optional, Type

from story_catalog import StorySegment
from prts_catalog import PRTSStoryClient, PRTSTaskCancelled
from reader_engine import ListenerEngine
from voice_generation import (
    ENGINE_EDGE,
    ENGINE_LABELS,
    ENGINE_QWEN,
    ENGINE_SYSTEM,
    GenerationInterrupted,
    GenerationPartiallyFailed,
    QWEN_CLONE_VOICE,
    QWEN_MODE_CLONE,
    QWEN_MODE_CUSTOM,
    QWEN_MODE_LABELS,
    TTSBackendManager,
    generate_voice_pack,
    voice_options,
)
from quality_analysis import (
    DEFAULT_DEEPSEEK_MODEL,
    fetch_deepseek_models,
    load_or_create_analysis,
    test_deepseek_model,
)


APP_TITLE = "明日方舟剧情阅读器 ver0.5"


def project_dir() -> Path:
    """Return the portable folder, not PyInstaller's temporary extraction path."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def configure_windows_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def load_app_settings() -> dict[str, object]:
    path = project_dir() / "data" / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_settings(settings: dict[str, object]) -> None:
    path = project_dir() / "data" / "settings.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    title: str
    description: str
    panel: Type["BasePanel"]


class BasePanel(ttk.Frame):
    """Base class for a functional module workspace."""

    def __init__(self, master: tk.Misc, app: "ReaderApp") -> None:
        super().__init__(master, style="Workspace.TFrame")
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def add_header(self, title: str, description: str) -> ttk.Frame:
        header = ttk.Frame(self, style="Workspace.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 14))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=title, style="PageTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, text=description, style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        return header

    def card(self, parent: tk.Misc, row: int, column: int = 0) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=18)
        frame.grid(row=row, column=column, sticky="nsew", padx=28, pady=8)
        return frame

    def show_coming_soon(self, feature: str) -> None:
        messagebox.showinfo("功能准备中", f"“{feature}”将在后续版本接入。", parent=self)


class StoryDownloadPanel(BasePanel):
    def build(self) -> None:
        self.add_header("剧情下载", "从剧情文本站点选择主题曲、别传或故事集，并导出可用于语音生成的文本。")

        body = ttk.Frame(self, style="Workspace.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        filters = self.card(body, 0)
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)
        ttk.Label(filters, text="剧情分类").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.category = ttk.Combobox(
            filters,
            state="readonly",
            values=("主题曲", "别传", "故事集"),
            width=18,
        )
        self.category.current(0)
        self.category.grid(row=0, column=1, sticky="w")

        ttk.Label(filters, text="活动名称").grid(row=0, column=2, sticky="w", padx=(28, 10))
        self.activity = ttk.Combobox(
            filters,
            state="readonly",
            values=("等待加载剧情目录…",),
            width=28,
        )
        self.activity.current(0)
        self.activity.grid(row=0, column=3, sticky="ew")
        ttk.Button(filters, text="刷新目录", command=self.refresh_catalog).grid(
            row=0, column=4, padx=(18, 0)
        )

        list_card = self.card(body, 1)
        list_card.columnconfigure(0, weight=1)
        list_card.rowconfigure(1, weight=1)
        ttk.Label(list_card, text="关卡 / 剧情段落", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        list_area = ttk.Frame(list_card, style="Card.TFrame")
        list_area.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.segments = tk.Listbox(
            list_area,
            height=12,
            activestyle="none",
            exportselection=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightcolor="#1683d8",
            highlightbackground="#d6dce3",
            font=("Microsoft YaHei UI", 10),
        )
        self.segments.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.segments.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.segments.configure(yscrollcommand=scrollbar.set)
        self.segments.insert("end", "  选择活动后，这里会显示对应的关卡和剧情段落")

        actions = ttk.Frame(list_card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        self.download_status = ttk.Label(actions, text="尚未连接剧情目录", style="Hint.TLabel")
        self.download_status.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="导出所选剧情", command=self.export_story).grid(
            row=0, column=1, padx=(10, 0)
        )

    def refresh_catalog(self) -> None:
        self.download_status.configure(text="目录模块已准备，下一步接入网络数据源")
        self.app.set_status("剧情目录刷新入口已就绪")

    def export_story(self) -> None:
        self.show_coming_soon("剧情文本导出")


class ConnectedStoryDownloadPanel(BasePanel):
    """Connected story selector used by the ver0.5 workspace."""

    CATEGORY_FILTERS = {
        "主题曲": {"maintheme"},
        "别传": {"sidestory", "intermezzi"},
        "故事集": {"storyset"},
    }

    def __init__(self, master: tk.Misc, app: "ReaderApp") -> None:
        self.client = PRTSStoryClient(
            cache_dir=project_dir() / "data" / "cache" / "prts",
            progress_callback=self._download_progress,
        )
        self.all_events: list[dict] = []
        self.event_by_label: dict[str, dict] = {}
        self.segment_items: list[StorySegment] = []
        self._log_lines: list[str] = []
        self.queue_items: list[dict] = []
        self.queue_thread: threading.Thread | None = None
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.current_queue_index: int | None = None
        super().__init__(master, app)

    def build(self) -> None:
        self.add_header("剧情下载", "从 PRTS 剧情目录选择主题曲、别传或故事集，再选择活动和关卡导出文本。")
        body = ttk.Frame(self, style="Workspace.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        filters = self.card(body, 0)
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)
        ttk.Label(filters, text="剧情分类").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.category = ttk.Combobox(filters, state="readonly", values=("主题曲", "别传", "故事集"), width=16)
        self.category.current(0)
        self.category.grid(row=0, column=1, sticky="w")
        self.category.bind("<<ComboboxSelected>>", self._on_category_changed)

        ttk.Label(filters, text="活动名称").grid(row=0, column=2, sticky="w", padx=(28, 10))
        self.activity = ttk.Combobox(filters, state="readonly", values=("请先刷新剧情目录",), width=30)
        self.activity.current(0)
        self.activity.grid(row=0, column=3, sticky="ew")
        self.activity.bind("<<ComboboxSelected>>", self._on_activity_changed)
        self.refresh_button = ttk.Button(filters, text="刷新目录", command=self.refresh_catalog)
        self.refresh_button.grid(row=0, column=4, padx=(18, 0))

        list_card = self.card(body, 1)
        list_card.columnconfigure(0, weight=1)
        list_card.rowconfigure(1, weight=1)
        ttk.Label(list_card, text="关卡 / 剧情段落", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        list_area = ttk.Frame(list_card, style="Card.TFrame")
        list_area.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.segments = tk.Listbox(
            list_area,
            height=12,
            selectmode=tk.EXTENDED,
            activestyle="none",
            exportselection=False,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightcolor="#1683d8",
            highlightbackground="#d6dce3",
            font=("Microsoft YaHei UI", 10),
        )
        self.segments.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.segments.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.segments.configure(yscrollcommand=scrollbar.set)
        self.segments.insert("end", "  请先点击“刷新目录”读取剧情活动")

        actions = ttk.Frame(list_card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        self.download_status = ttk.Label(actions, text="尚未连接剧情目录", style="Hint.TLabel")
        self.download_status.grid(row=0, column=0, sticky="w")
        self.download_progress = ttk.Progressbar(actions, mode="determinate", maximum=100, value=0)
        self.download_progress.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        self.export_button = ttk.Button(actions, text="加入下载队列", command=self.add_selected_to_queue, state="disabled")
        self.export_button.grid(row=0, column=1, padx=(10, 0))

        log_card = self.card(body, 2)
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="下载日志", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        log_area = ttk.Frame(log_card, style="Card.TFrame")
        log_area.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        log_area.columnconfigure(0, weight=1)
        log_area.rowconfigure(0, weight=1)
        self.download_log = tk.Text(
            log_area,
            height=4,
            state="disabled",
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            font=("Consolas", 9),
            background="#f7f9fb",
            foreground="#536170",
        )
        self.download_log.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_area, orient="vertical", command=self.download_log.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.download_log.configure(yscrollcommand=log_scrollbar.set)
        self._append_log("等待操作。点击“刷新目录”或“导出所选剧情”开始下载。")

        queue_card = self.card(body, 3)
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)
        ttk.Label(queue_card, text="下载队列（Ctrl/Shift 可多选）", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        queue_area = ttk.Frame(queue_card, style="Card.TFrame")
        queue_area.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        queue_area.columnconfigure(0, weight=1)
        queue_area.rowconfigure(0, weight=1)
        self.queue_list = tk.Listbox(queue_area, height=5, activestyle="none", exportselection=False, relief="flat", borderwidth=0, selectmode=tk.SINGLE)
        self.queue_list.grid(row=0, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_area, orient="vertical", command=self.queue_list.yview)
        queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_list.configure(yscrollcommand=queue_scroll.set)
        queue_actions = ttk.Frame(queue_card, style="Card.TFrame")
        queue_actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        queue_actions.columnconfigure(0, weight=1)
        self.queue_status = ttk.Label(queue_actions, text="队列为空", style="Hint.TLabel")
        self.queue_status.grid(row=0, column=0, sticky="w")
        ttk.Button(queue_actions, text="加入队列", command=self.add_selected_to_queue).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(queue_actions, text="上移", command=lambda: self.move_queue_item(-1)).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(queue_actions, text="下移", command=lambda: self.move_queue_item(1)).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(queue_actions, text="移除", command=self.remove_queue_item).grid(row=0, column=4, padx=(6, 0))
        self.queue_start_button = ttk.Button(queue_actions, text="开始队列", command=self.start_queue)
        self.queue_start_button.grid(row=0, column=5, padx=(6, 0))
        self.queue_pause_button = ttk.Button(queue_actions, text="暂停", command=self.toggle_pause, state="disabled")
        self.queue_pause_button.grid(row=0, column=6, padx=(6, 0))
        self.queue_stop_button = ttk.Button(queue_actions, text="停止当前", command=self.stop_current, state="disabled")
        self.queue_stop_button.grid(row=0, column=7, padx=(6, 0))

    def _append_log(self, text: str) -> None:
        self._log_lines.append(text)
        self._log_lines = self._log_lines[-8:]
        self.download_log.configure(state="normal")
        self.download_log.delete("1.0", "end")
        self.download_log.insert("1.0", "\n".join(self._log_lines))
        self.download_log.configure(state="disabled")
        self.download_log.see("end")

    def _log(self, text: str) -> None:
        self.after(0, self._append_log, text)

    def _download_progress(self, message: str, current: int = 0, total: int = 0) -> None:
        if total > 0:
            percent = min(100.0, current * 100.0 / total)
            def update_progress() -> None:
                self.download_progress.stop()
                self.download_progress.configure(mode="determinate", value=percent)
                self.download_status.configure(text=f"{message} ({percent:.0f}%)")

            self.after(0, update_progress)
        else:
            def update_waiting() -> None:
                self.download_progress.configure(mode="indeterminate")
                self.download_progress.start(12)
                self.download_status.configure(text=message)

            self.after(0, update_waiting)
        if not message.startswith("正在下载"):
            self._log(message)

    def _reset_progress(self) -> None:
        self.download_progress.stop()
        self.download_progress.configure(mode="determinate", value=0)

    def _finish_progress(self) -> None:
        self.download_progress.stop()
        self.download_progress.configure(mode="determinate", value=100)

    def refresh_catalog(self) -> None:
        self.refresh_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self._reset_progress()
        self._append_log("开始读取 PRTS 剧情目录…")
        self.download_status.configure(text="正在读取剧情目录…")
        self.app.set_status("正在读取 PRTS 剧情目录，首次读取可能需要一点时间")
        threading.Thread(target=self._catalog_worker, daemon=True).start()

    def _catalog_worker(self) -> None:
        try:
            self.client.load_catalog(refresh=True)
            events = self.client.list_events()
            self.after(0, lambda: self._catalog_ready(events, None))
        except Exception as exc:
            self.after(0, lambda: self._catalog_ready([], exc))

    def _catalog_ready(self, events: list[dict], error: Exception | None) -> None:
        self.refresh_button.configure(state="normal")
        if error is not None:
            self._reset_progress()
            self._append_log(f"读取目录失败：{error}")
            self.download_status.configure(text="剧情目录读取失败")
            self.app.set_status(f"剧情目录读取失败：{error}")
            messagebox.showerror("读取失败", f"无法读取剧情目录。\n\n{error}", parent=self)
            return
        self.all_events = events
        self._refresh_activity_values()
        self._finish_progress()
        self._append_log(f"目录读取完成，共 {len(events)} 个活动。")
        self.download_status.configure(text=f"已读取 {len(events)} 个活动")
        self.app.set_status(f"剧情目录读取完成，共 {len(events)} 个活动")

    def _on_category_changed(self, _event: object = None) -> None:
        self._refresh_activity_values()

    def _refresh_activity_values(self) -> None:
        selected_category = self.category.get()
        allowed = self.CATEGORY_FILTERS.get(selected_category, set())
        matching = [event for event in self.all_events if event.get("category") in allowed]
        self.event_by_label.clear()
        labels: list[str] = []
        for event in matching:
            label = str(event.get("name") or event.get("id") or "未命名活动")
            if label in self.event_by_label:
                label = f"{label} [{event.get('id')}]"
            self.event_by_label[label] = event
            labels.append(label)
        if not labels:
            labels = ["暂无活动，请先刷新目录"]
        self.activity["values"] = labels
        self.activity.current(0)
        self._clear_segments()
        if matching:
            self._load_segments(matching[0]["id"])

    def _on_activity_changed(self, _event: object = None) -> None:
        event = self.event_by_label.get(self.activity.get())
        if event:
            self._load_segments(str(event["id"]))

    def _clear_segments(self) -> None:
        self.segments.delete(0, "end")
        self.segment_items = []

    def _load_segments(self, event_id: str) -> None:
        self._clear_segments()
        try:
            segments = self.client.list_segments(event_id)
        except Exception as exc:
            self.segments.insert("end", f"  读取关卡失败：{exc}")
            return
        self.segment_items = segments
        if not segments:
            self.segments.insert("end", "  该活动没有可用的剧情段落")
            self.export_button.configure(state="disabled")
            return
        for segment in segments:
            code = segment.story_code or segment.story_id or "未命名关卡"
            name = segment.story_name or "未命名剧情段落"
            label = f"  [{code}] {name}"
            if segment.tag:
                label += f"  · {segment.tag}"
            self.segments.insert("end", label)
        self.segments.selection_set(0)
        self.export_button.configure(state="normal")

    def _queue_label(self, item: dict) -> str:
        segment = item["segment"]
        status = item.get("status", "待处理")
        return f"[{status}] {segment.event_name} / {segment.story_code or segment.story_txt}"

    def _refresh_queue_view(self) -> None:
        self.queue_list.delete(0, "end")
        for item in self.queue_items:
            self.queue_list.insert("end", self._queue_label(item))
        if self.current_queue_index is not None and self.current_queue_index < len(self.queue_items):
            self.queue_list.selection_set(self.current_queue_index)
        pending = sum(item.get("status") == "待处理" for item in self.queue_items)
        self.queue_status.configure(text=f"共 {len(self.queue_items)} 项，待处理 {pending} 项")

    def add_selected_to_queue(self) -> None:
        selections = self.segments.curselection()
        if not selections or not self.segment_items:
            self.app.set_status("请先选择一个剧情段落，可按住 Ctrl 多选")
            return
        existing = {item["segment"].story_id for item in self.queue_items}
        added = 0
        for index in selections:
            segment = self.segment_items[index]
            if segment.story_id in existing:
                continue
            self.queue_items.append({"segment": segment, "status": "待处理"})
            existing.add(segment.story_id)
            added += 1
        self._refresh_queue_view()
        self.app.set_status(f"已加入下载队列 {added} 项")

    def move_queue_item(self, direction: int) -> None:
        selection = self.queue_list.curselection()
        if not selection or self.current_queue_index is not None:
            return
        index = selection[0]
        target = index + direction
        if target < 0 or target >= len(self.queue_items):
            return
        self.queue_items[index], self.queue_items[target] = self.queue_items[target], self.queue_items[index]
        self._refresh_queue_view()
        self.queue_list.selection_set(target)

    def remove_queue_item(self) -> None:
        selection = self.queue_list.curselection()
        if not selection or selection[0] == self.current_queue_index:
            return
        self.queue_items.pop(selection[0])
        self._refresh_queue_view()

    def _next_queue_index(self) -> int | None:
        for index, item in enumerate(self.queue_items):
            if item.get("status") == "待处理":
                return index
        return None

    def start_queue(self) -> None:
        if self.queue_thread is not None and self.queue_thread.is_alive():
            if self.pause_event.is_set():
                self.pause_event.clear()
                self.queue_pause_button.configure(text="暂停")
                self._append_log("已继续下载队列。")
            return
        if self._next_queue_index() is None:
            self.app.set_status("下载队列中没有待处理任务")
            return
        self.pause_event.clear()
        self.stop_event.clear()
        self.queue_pause_button.configure(text="暂停", state="normal")
        self.queue_stop_button.configure(state="normal")
        self.refresh_button.configure(state="disabled")
        self.queue_start_button.configure(state="disabled")
        self.queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.queue_thread.start()

    def toggle_pause(self) -> None:
        if self.current_queue_index is None:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.queue_pause_button.configure(text="暂停")
            self._append_log("已继续当前下载任务。")
        else:
            self.pause_event.set()
            self.queue_pause_button.configure(text="继续")
            self._append_log("已暂停当前下载任务。")

    def stop_current(self) -> None:
        if self.current_queue_index is None:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self._append_log("正在停止当前下载任务，当前网络数据块结束后生效。")

    def _queue_worker(self) -> None:
        while True:
            index = self._next_queue_index()
            if index is None:
                self.after(0, self._queue_finished)
                return
            self.current_queue_index = index
            item = self.queue_items[index]
            segment = item["segment"]
            item["status"] = "下载中"
            self.client.set_task_controls(self.pause_event, self.stop_event)
            self.after(0, self._refresh_queue_view)
            self.after(0, self._reset_progress)
            self._append_log(f"开始下载：{segment.event_name} / {segment.story_code or segment.story_txt}")
            try:
                document = self.client.load_document(segment)
                category_name = {"maintheme": "主题曲", "sidestory": "别传", "storyset": "故事集"}.get(segment.category, "其他")
                folder = project_dir() / "data" / "stories" / self.client._safe_name(category_name) / self.client._safe_name(segment.event_name, segment.event_id)
                folder.mkdir(parents=True, exist_ok=True)
                output = folder / f"{self.client._safe_name(segment.story_code or segment.story_name or segment.story_id)}.json"
                output.write_text(json.dumps(document.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                item["status"] = "已完成"
                self._append_log(f"下载完成：{output}")
            except PRTSTaskCancelled:
                item["status"] = "已停止"
                self._append_log(f"已停止：{segment.story_code or segment.story_txt}")
            except Exception as exc:
                item["status"] = "失败"
                self._append_log(f"下载失败：{exc}")
            finally:
                self.stop_event.clear()
                self.pause_event.clear()
                self.current_queue_index = None
                self.after(0, self._refresh_queue_view)

    def _queue_finished(self) -> None:
        self._finish_progress()
        self.queue_thread = None
        self.queue_start_button.configure(state="normal")
        self.queue_pause_button.configure(state="disabled", text="暂停")
        self.queue_stop_button.configure(state="disabled")
        self.refresh_button.configure(state="normal")
        self.current_queue_index = None
        self._refresh_queue_view()
        self.download_status.configure(text="下载队列已完成")
        self.app.set_status("下载队列已完成")

class VoiceGenerationPanel(BasePanel):
    def build(self) -> None:
        self.add_header("语音生成", "为已下载的剧情预先生成语音文件，生成过程与阅读监听相互独立。")
        body = ttk.Frame(self, style="Workspace.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        model_card = self.card(body, 0)
        model_card.columnconfigure(1, weight=1)
        ttk.Label(model_card, text="语音引擎", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 20)
        )
        self.engine = ttk.Combobox(
            model_card,
            state="readonly",
            values=("Qwen3-TTS（本地模型）", "Edge-TTS（在线）", "Windows 系统语音"),
            width=34,
        )
        self.engine.current(0)
        self.engine.grid(row=0, column=1, sticky="w")
        ttk.Label(model_card, text="模型和缓存将放在 ver0.5 目录的 data 文件夹中。", style="Hint.TLabel").grid(
            row=1, column=1, sticky="w", pady=(9, 0)
        )

        queue_card = self.card(body, 1)
        queue_card.columnconfigure(0, weight=1)
        ttk.Label(queue_card, text="生成队列", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            queue_card,
            text="请先在“剧情下载”中选择并导出剧情，这里会显示待生成的关卡。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(12, 20))
        progress = ttk.Progressbar(queue_card, mode="determinate", value=0)
        progress.grid(row=2, column=0, sticky="ew")
        ttk.Button(queue_card, text="开始生成", command=lambda: self.show_coming_soon("批量语音生成")).grid(
            row=3, column=0, sticky="e", pady=(16, 0)
        )


class ConnectedVoiceGenerationPanel(BasePanel):
    """Batch voice generation workspace for one exported story segment."""

    def __init__(self, master: tk.Misc, app: "ReaderApp") -> None:
        self.backend = TTSBackendManager(self._backend_status)
        self.story_items: list[Path] = []
        self.queue_items: list[dict] = []
        self.queue_thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.pause_event = threading.Event()
        self.current_queue_index: int | None = None
        self.user_stopped = False
        self.auto_shutdown = tk.BooleanVar(value=False)
        self.qwen_mode = tk.StringVar(value=QWEN_MODE_CUSTOM)
        self.reference_audio = tk.StringVar()
        self.reference_text = tk.StringVar()
        self.qwen_instruct = tk.StringVar()
        self.optimize_quality = tk.BooleanVar(value=False)
        self.queue_config = (ENGINE_EDGE, voice_options(ENGINE_EDGE)[0], 1.0)
        super().__init__(master, app)

    def build(self) -> None:
        self.add_header("语音生成", "选择已导出的关卡，使用 Edge-TTS 或备份中的 Qwen3-TTS 逐句生成语音包。")
        body = ttk.Frame(self, style="Workspace.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        options = self.card(body, 0)
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)
        ttk.Label(options, text="语音引擎", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.engine_labels = tuple(ENGINE_LABELS[key] for key in (ENGINE_EDGE, ENGINE_QWEN, ENGINE_SYSTEM))
        self.engine_by_label = dict(zip(self.engine_labels, (ENGINE_EDGE, ENGINE_QWEN, ENGINE_SYSTEM)))
        self.engine = ttk.Combobox(options, state="readonly", values=self.engine_labels, width=26)
        self.engine.current(0)
        self.engine.grid(row=0, column=1, sticky="w")
        self.engine.bind("<<ComboboxSelected>>", self._on_engine_changed)

        ttk.Label(options, text="音色", style="Section.TLabel").grid(row=0, column=2, sticky="w", padx=(28, 10))
        self.voice = ttk.Combobox(options, state="readonly", width=22)
        self.voice.grid(row=0, column=3, sticky="w")
        self._update_voice_options()

        self.qwen_mode_label = ttk.Label(options, text="Qwen 模式", style="Section.TLabel")
        self.qwen_mode_label.grid(row=1, column=0, sticky="w", pady=(14, 0))
        self.qwen_mode_box = ttk.Combobox(
            options,
            state="readonly",
            values=tuple(QWEN_MODE_LABELS.values()),
            width=26,
        )
        self.qwen_mode_box.current(0)
        self.qwen_mode_box.grid(row=1, column=1, sticky="w", pady=(14, 0))
        self.qwen_mode_box.bind("<<ComboboxSelected>>", self._on_qwen_mode_changed)

        self.reference_label = ttk.Label(options, text="参考音频", style="Section.TLabel")
        self.reference_label.grid(row=1, column=2, sticky="w", padx=(28, 10), pady=(14, 0))
        reference_area = ttk.Frame(options, style="Card.TFrame")
        reference_area.grid(row=1, column=3, sticky="ew", pady=(14, 0))
        reference_area.columnconfigure(0, weight=1)
        self.reference_entry = ttk.Entry(reference_area, textvariable=self.reference_audio, width=22)
        self.reference_entry.grid(row=0, column=0, sticky="ew")
        self.reference_button = ttk.Button(reference_area, text="选择", width=6, command=self.choose_reference_audio)
        self.reference_button.grid(row=0, column=1, padx=(6, 0))

        self.reference_text_label = ttk.Label(options, text="参考文本", style="Section.TLabel")
        self.reference_text_label.grid(row=2, column=0, sticky="w", pady=(14, 0))
        self.reference_text_entry = ttk.Entry(options, textvariable=self.reference_text, width=38)
        self.reference_text_entry.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(14, 0))

        self.instruct_label = ttk.Label(options, text="情绪提示", style="Section.TLabel")
        self.instruct_label.grid(row=3, column=0, sticky="w", pady=(14, 0))
        self.instruct_entry = ttk.Entry(options, textvariable=self.qwen_instruct, width=38)
        self.instruct_entry.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(14, 0))

        self.optimize_quality_check = ttk.Checkbutton(
            options,
            text="使用 DeepSeek 优化每句情绪",
            variable=self.optimize_quality,
        )
        self.optimize_quality_check.grid(row=4, column=1, sticky="w", pady=(14, 0))

        ttk.Label(options, text="生成语速", style="Section.TLabel").grid(row=5, column=0, sticky="w", pady=(14, 0))
        self.generation_speed = tk.DoubleVar(value=1.0)
        ttk.Spinbox(options, from_=0.1, to=2.0, increment=0.1, textvariable=self.generation_speed, width=8).grid(
            row=5, column=1, sticky="w", pady=(14, 0)
        )
        ttk.Label(options, text="Qwen 首次生成会启动独立 ROCm 服务，速度会明显慢于 Edge-TTS。", style="Hint.TLabel").grid(
            row=6, column=2, columnspan=2, sticky="w", padx=(28, 0), pady=(14, 0)
        )
        self.release_button = ttk.Button(options, text="释放 Qwen 服务", command=self.release_qwen)
        self.release_button.grid(row=6, column=3, sticky="e", pady=(14, 0))

        queue_card = self.card(body, 1)
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)
        ttk.Label(queue_card, text="已导出的关卡", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        list_area = ttk.Frame(queue_card, style="Card.TFrame")
        list_area.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.stories = tk.Listbox(list_area, height=9, activestyle="none", exportselection=False, selectmode=tk.EXTENDED, relief="flat", borderwidth=0, highlightthickness=1)
        self.stories.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.stories.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.stories.configure(yscrollcommand=scroll.set)
        self.stories.insert("end", "  尚未找到导出的剧情，请先在“剧情下载”中导出关卡")

        actions = ttk.Frame(queue_card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        self.queue_status = ttk.Label(actions, text="队列为空", style="Hint.TLabel")
        self.queue_status.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="刷新关卡", command=self.refresh_stories).grid(row=0, column=1, padx=(8, 0))
        self.add_queue_button = ttk.Button(actions, text="加入队列", command=self.add_selected_to_queue)
        self.add_queue_button.grid(row=0, column=2, padx=(8, 0))

        self.progress = ttk.Progressbar(queue_card, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.progress_text = ttk.Label(queue_card, text="等待任务", style="Hint.TLabel")
        self.progress_text.grid(row=4, column=0, sticky="w", pady=(6, 0))

        queue_label = ttk.Label(queue_card, text="生成队列（Ctrl/Shift 可多选）", style="Section.TLabel")
        queue_label.grid(row=5, column=0, sticky="w", pady=(14, 0))
        queue_area = ttk.Frame(queue_card, style="Card.TFrame")
        queue_area.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
        queue_area.columnconfigure(0, weight=1)
        queue_area.rowconfigure(0, weight=1)
        self.queue_list = tk.Listbox(queue_area, height=5, activestyle="none", exportselection=False, relief="flat", borderwidth=0)
        self.queue_list.grid(row=0, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_area, orient="vertical", command=self.queue_list.yview)
        queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_list.configure(yscrollcommand=queue_scroll.set)
        queue_actions = ttk.Frame(queue_card, style="Card.TFrame")
        queue_actions.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        queue_actions.columnconfigure(0, weight=1)
        self.queue_status = ttk.Label(queue_actions, text="队列为空", style="Hint.TLabel")
        self.queue_status.grid(row=0, column=0, sticky="w")
        ttk.Button(queue_actions, text="上移", command=lambda: self.move_queue_item(-1)).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(queue_actions, text="下移", command=lambda: self.move_queue_item(1)).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(queue_actions, text="移除", command=self.remove_queue_item).grid(row=0, column=3, padx=(6, 0))
        self.queue_start_button = ttk.Button(queue_actions, text="开始队列", command=self.start_queue)
        self.queue_start_button.grid(row=0, column=4, padx=(6, 0))
        self.queue_pause_button = ttk.Button(queue_actions, text="暂停", command=self.toggle_pause, state="disabled")
        self.queue_pause_button.grid(row=0, column=5, padx=(6, 0))
        self.queue_stop_button = ttk.Button(queue_actions, text="停止当前", command=self.stop_current, state="disabled")
        self.queue_stop_button.grid(row=0, column=6, padx=(6, 0))
        ttk.Button(queue_actions, text="查看结果/重试失败", command=self.inspect_generation_result).grid(
            row=0, column=9, padx=(6, 0)
        )
        ttk.Checkbutton(queue_actions, text="全部完成后关机", variable=self.auto_shutdown).grid(row=0, column=7, padx=(12, 0))
        ttk.Button(queue_actions, text="取消关机", command=self._cancel_scheduled_shutdown).grid(row=0, column=8, padx=(6, 0))
        self.refresh_stories()
        self._update_qwen_controls()

    def _on_engine_changed(self, _event: object = None) -> None:
        self._update_voice_options()
        self._update_qwen_controls()

    def _on_qwen_mode_changed(self, _event: object = None) -> None:
        selected = self.qwen_mode_box.get()
        for mode, label in QWEN_MODE_LABELS.items():
            if label == selected:
                self.qwen_mode.set(mode)
                break
        self._update_voice_options()
        self._update_qwen_controls()

    def choose_reference_audio(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择声音克隆参考音频",
            filetypes=(("音频文件", "*.wav *.mp3 *.flac *.m4a"), ("所有文件", "*.*")),
        )
        if path:
            self.reference_audio.set(str(Path(path).resolve()))

    def _update_qwen_controls(self) -> None:
        engine = self.engine_by_label.get(self.engine.get(), ENGINE_EDGE)
        qwen_enabled = engine == ENGINE_QWEN
        clone_enabled = qwen_enabled and self.qwen_mode.get() == QWEN_MODE_CLONE
        mode_state = "readonly" if qwen_enabled else "disabled"
        self.qwen_mode_box.configure(state=mode_state)
        entry_state = "normal" if clone_enabled else "disabled"
        self.reference_entry.configure(state=entry_state)
        self.reference_text_entry.configure(state=entry_state)
        self.reference_button.configure(state="normal" if clone_enabled else "disabled")
        self.instruct_entry.configure(state="normal" if qwen_enabled else "disabled")
        self.optimize_quality_check.configure(state="normal" if qwen_enabled else "disabled")

    def _update_voice_options(self) -> None:
        engine = self.engine_by_label.get(self.engine.get(), ENGINE_EDGE)
        if engine == ENGINE_QWEN and self.qwen_mode.get() == QWEN_MODE_CLONE:
            values = (QWEN_CLONE_VOICE,)
        else:
            values = voice_options(engine)
        self.voice["values"] = values
        self.voice.current(0)

    def refresh_stories(self) -> None:
        root = project_dir() / "data" / "stories"
        self.story_items = sorted(root.rglob("*.json")) if root.exists() else []
        self.stories.delete(0, "end")
        if not self.story_items:
            self.stories.insert("end", "  尚未找到导出的剧情，请先在“剧情下载”中导出关卡")
            self.queue_status.configure(text="队列为空")
            self.add_queue_button.configure(state="disabled")
            return
        for path in self.story_items:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                segment = payload.get("segment") or {}
                event_name = segment.get("event_name") or path.parent.name
                code = segment.get("story_code") or segment.get("story_txt") or path.stem
                count = len([
                    line for line in payload.get("lines", [])
                    if line.get("should_speak", line.get("prop", "").lower() != "name")
                ])
                self.stories.insert("end", f"  {event_name} / {code}  （{count} 条对白）")
            except Exception:
                self.stories.insert("end", f"  {path.name}  （文件格式异常）")
        self.stories.selection_set(0)
        self.queue_status.configure(text=f"已找到 {len(self.story_items)} 个关卡")
        self.add_queue_button.configure(state="normal")

    def _queue_label(self, item: dict) -> str:
        path = item["path"]
        status = item.get("status", "待处理")
        return f"[{status}] {path.parent.parent.name} / {path.stem}"

    def _refresh_queue_view(self) -> None:
        self.queue_list.delete(0, "end")
        for item in self.queue_items:
            self.queue_list.insert("end", self._queue_label(item))
        if self.current_queue_index is not None and self.current_queue_index < len(self.queue_items):
            self.queue_list.selection_set(self.current_queue_index)
        pending = sum(item.get("status") == "待处理" for item in self.queue_items)
        self.queue_status.configure(text=f"共 {len(self.queue_items)} 项，待处理 {pending} 项")

    def add_selected_to_queue(self) -> None:
        selections = self.stories.curselection()
        if not selections or not self.story_items:
            self.app.set_status("请先选择关卡，可按住 Ctrl 多选")
            return
        existing = {str(item["path"]) for item in self.queue_items}
        added = 0
        for index in selections:
            path = self.story_items[index]
            if str(path) in existing:
                continue
            self.queue_items.append({"path": path, "status": "待处理"})
            existing.add(str(path))
            added += 1
        self._refresh_queue_view()
        self.app.set_status(f"已加入语音生成队列 {added} 项")

    def move_queue_item(self, direction: int) -> None:
        selection = self.queue_list.curselection()
        if not selection or self.current_queue_index is not None:
            return
        index = selection[0]
        target = index + direction
        if target < 0 or target >= len(self.queue_items):
            return
        self.queue_items[index], self.queue_items[target] = self.queue_items[target], self.queue_items[index]
        self._refresh_queue_view()
        self.queue_list.selection_set(target)

    def remove_queue_item(self) -> None:
        selection = self.queue_list.curselection()
        if not selection or selection[0] == self.current_queue_index:
            return
        self.queue_items.pop(selection[0])
        self._refresh_queue_view()

    def _next_queue_index(self) -> int | None:
        for index, item in enumerate(self.queue_items):
            if item.get("status") == "待处理":
                return index
        return None

    def start_queue(self) -> None:
        if self.queue_thread is not None and self.queue_thread.is_alive():
            if self.pause_event.is_set():
                self.pause_event.clear()
                self.queue_pause_button.configure(text="暂停")
                self.app.set_status("已继续语音生成队列")
            return
        if self._next_queue_index() is None:
            self.app.set_status("生成队列中没有待处理任务")
            return
        self.pause_event.clear()
        self.cancelled.clear()
        self.user_stopped = False
        engine = self.engine_by_label.get(self.engine.get(), ENGINE_EDGE)
        voice = self.voice.get() or voice_options(engine)[0]
        try:
            speed = max(0.1, min(2.0, round(float(self.generation_speed.get()), 1)))
        except (TypeError, ValueError, tk.TclError):
            speed = 1.0
            self.generation_speed.set(speed)
        qwen_options = {
            "mode": self.qwen_mode.get(),
            "reference_audio": self.reference_audio.get().strip(),
            "reference_text": self.reference_text.get().strip(),
            "instruct": self.qwen_instruct.get().strip(),
        }
        if engine == ENGINE_QWEN and qwen_options["mode"] == QWEN_MODE_CLONE and not qwen_options["reference_audio"]:
            messagebox.showwarning("缺少参考音频", "声音克隆模式需要先选择参考音频。", parent=self)
            return
        if engine == ENGINE_QWEN and self.optimize_quality.get() and not str(self.app.settings.get("deepseek_api_key", "")).strip():
            messagebox.showwarning("缺少 DeepSeek API Key", "已勾选质量优化，请先在设置中填写 DeepSeek API Key。", parent=self)
            return
        self.queue_config = (engine, voice, speed, qwen_options, self.optimize_quality.get())
        self.queue_start_button.configure(state="disabled")
        self.queue_pause_button.configure(state="normal", text="暂停")
        self.queue_stop_button.configure(state="normal")
        self.queue_thread = threading.Thread(target=self._queue_worker, daemon=True)
        self.queue_thread.start()

    def toggle_pause(self) -> None:
        if self.current_queue_index is None:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.queue_pause_button.configure(text="暂停")
            if self.current_queue_index is not None:
                self.queue_items[self.current_queue_index]["status"] = "生成中"
                self._refresh_queue_view()
            self.app.set_status("已继续当前语音生成任务")
        else:
            self.pause_event.set()
            self.queue_pause_button.configure(text="继续")
            if self.current_queue_index is not None:
                self.queue_items[self.current_queue_index]["status"] = "已暂停"
                self._refresh_queue_view()
            self.app.set_status("已暂停当前语音生成任务")

    def stop_current(self) -> None:
        if self.current_queue_index is None:
            return
        self.user_stopped = True
        self.cancelled.set()
        self.pause_event.clear()
        self.app.set_status("正在停止当前语音生成任务，当前句完成后生效")

    def _queue_worker(self) -> None:
        while True:
            index = self._next_queue_index()
            if index is None:
                self.after(0, self._queue_finished)
                return
            self.current_queue_index = index
            item = self.queue_items[index]
            item["status"] = "生成中"
            self.after(0, self._refresh_queue_view)
            story_path = item["path"]
            try:
                engine, voice, speed, qwen_options, optimize_quality = self.queue_config
            except (TypeError, ValueError, tk.TclError):
                engine, voice, speed = ENGINE_EDGE, voice_options(ENGINE_EDGE)[0], 1.0
                qwen_options, optimize_quality = {}, False
            try:
                output_root = project_dir() / "data" / "voice_packs"
                line_instructions = {}
                if optimize_quality and engine == ENGINE_QWEN:
                    line_instructions = load_or_create_analysis(
                        story_path,
                        project_dir() / "data" / "tts_analysis",
                        str(self.app.settings.get("deepseek_api_key", "")),
                        self._backend_status,
                        model=str(self.app.settings.get("deepseek_model", DEFAULT_DEEPSEEK_MODEL)),
                    )
                manifest = generate_voice_pack(
                    story_path, output_root, engine, voice, speed,
                    self._generation_progress,
                    self.cancelled,
                    self.backend,
                    self.pause_event,
                    qwen_options,
                    line_instructions,
                )
                item["manifest"] = manifest
                item["status"] = "已完成"
                self.after(0, lambda m=manifest: self.progress_text.configure(text=f"生成完成：{m}"))
            except GenerationInterrupted as exc:
                item["status"] = "中断"
                item["manifest"] = exc.manifest_path
                self._backend_status("当前语音生成已中断，未安排自动关机")
            except GenerationPartiallyFailed as exc:
                item["status"] = "部分完成"
                item["manifest"] = exc.manifest_path
                self._backend_status(f"语音生成部分完成：{len(exc.failed_entries)} 个片段失败")
            except Exception as exc:
                item["status"] = "已停止" if self.cancelled.is_set() else "失败"
                self._backend_status(f"语音生成{item['status']}：{exc}")
            finally:
                self.cancelled.clear()
                self.pause_event.clear()
                self.current_queue_index = None
                self.after(0, self._refresh_queue_view)

    def _queue_finished(self) -> None:
        self.queue_thread = None
        self.queue_start_button.configure(state="normal")
        self.queue_pause_button.configure(state="disabled", text="暂停")
        self.queue_stop_button.configure(state="disabled")
        self.current_queue_index = None
        self._refresh_queue_view()
        successful = bool(self.queue_items) and all(item.get("status") == "已完成" for item in self.queue_items)
        terminal_statuses = {"已完成", "部分完成", "失败", "中断", "已停止"}
        all_terminal = bool(self.queue_items) and all(item.get("status") in terminal_statuses for item in self.queue_items)
        qwen_queue = bool(self.queue_config) and self.queue_config[0] == ENGINE_QWEN
        self.progress.configure(value=100 if successful else self.progress["value"])
        if successful:
            self.progress_text.configure(text="全部语音生成完成")
            self.app.set_status("全部语音生成完成")
        else:
            self.progress_text.configure(text="队列已结束，请检查失败或中断片段")
            self.app.set_status("语音生成队列已结束，请检查失败或中断片段")
        if self.auto_shutdown.get() and not self.user_stopped and all_terminal and (successful or qwen_queue):
            self._schedule_shutdown()

    def _schedule_shutdown(self) -> None:
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["shutdown.exe", "/s", "/t", "60"],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.app.set_status("全部任务完成，电脑将在 60 秒后关机")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip() if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            self.app.set_status(f"自动关机安排失败：{detail or exc}")
        except Exception as exc:
            self.app.set_status(f"自动关机安排失败：{exc}")

    def _cancel_scheduled_shutdown(self) -> None:
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["shutdown.exe", "/a"],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.app.set_status("已取消自动关机")
        except Exception:
            self.app.set_status("当前没有可取消的自动关机任务")

    def cancel_generation(self) -> None:
        self.stop_current()

    def inspect_generation_result(self) -> None:
        selection = self.queue_list.curselection()
        item = self.queue_items[selection[0]] if selection else next(
            (candidate for candidate in self.queue_items if candidate.get("status") in {"部分完成", "失败", "中断"}),
            None,
        )
        if item is None:
            messagebox.showinfo("生成结果", "当前没有可查看的失败或中断任务。", parent=self)
            return
        manifest_path = item.get("manifest")
        if not isinstance(manifest_path, Path):
            manifest_path = Path(manifest_path) if manifest_path else None
        payload: dict = {}
        if manifest_path is not None and manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        lines = payload.get("lines") or []
        failed = [
            entry for entry in lines
            if entry.get("status") in {"failed", "interrupted"}
            or not (entry.get("audio") and manifest_path and (manifest_path.parent / str(entry.get("audio"))).exists())
        ]
        if not failed:
            if item.get("status") == "失败":
                failed = [{"index": "?", "text": "任务在生成前失败，无法定位到具体片段。"}]
            else:
                messagebox.showinfo("生成结果", "没有发现缺失的语音片段。", parent=self)
                return
        details = "\n".join(
            f"{entry.get('index', '?'):>4}  {str(entry.get('text', ''))[:80]}"
            + (f"\n      原因：{entry.get('error')}" if entry.get("error") else "")
            for entry in failed[:30]
        )
        if len(failed) > 30:
            details += f"\n……还有 {len(failed) - 30} 个片段"
        retry = messagebox.askyesno(
            "发现未完成片段",
            f"任务：{item['path'].stem}\n\n未成功片段：{len(failed)} 个\n\n{details}\n\n是否只重试这些片段？已完成片段会直接复用。",
            parent=self,
        )
        if not retry:
            return
        item["status"] = "待处理"
        self._refresh_queue_view()
        self.app.set_status("已将失败片段所在任务重新加入队列")
        if self.queue_thread is None or not self.queue_thread.is_alive():
            self.start_queue()

    def _generation_progress(self, done: int, total: int, text: str) -> None:
        self.after(0, lambda: self._show_progress(done, total, text))

    def _show_progress(self, done: int, total: int, text: str) -> None:
        self.progress.configure(value=(done / total * 100) if total else 0)
        self.progress_text.configure(text=text)
        self.app.set_status(text)

    def release_qwen(self) -> None:
        self.app.set_status("正在释放 Qwen3-TTS 服务…")
        threading.Thread(target=self.backend.release_qwen, daemon=True).start()

    def _backend_status(self, text: str) -> None:
        self.after(0, lambda: self.app.set_status(text))



class VoicePackPanel(BasePanel):
    def build(self) -> None:
        self.add_header("语音包管理", "查看已生成的语音包，并设置阅读监听时使用的剧情包。")
        card = self.card(self, 1)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        ttk.Label(card, text="本地语音包", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.packs = tk.Listbox(card, height=12, relief="flat", borderwidth=0, highlightthickness=1)
        self.packs.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        self.packs.insert("end", "  还没有生成语音包")
        ttk.Button(card, text="扫描语音包目录", command=lambda: self.app.set_status("语音包扫描入口已就绪")).grid(
            row=2, column=0, sticky="e"
        )


class _LegacyConnectedVoicePackPanel(BasePanel):
    """Browse generated manifests that will later be used by OCR matching."""

    def __init__(self, master: tk.Misc, app: "ReaderApp") -> None:
        self.manifest_items: list[Path] = []
        super().__init__(master, app)

    def build(self) -> None:
        self.add_header("语音包管理", "查看已经生成的关卡语音包；后续 OCR 监听会从这里选择匹配包。")
        card = self.card(self, 1)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        ttk.Label(card, text="本地语音包", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        list_area = ttk.Frame(card, style="Card.TFrame")
        list_area.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.packs = tk.Listbox(list_area, height=12, activestyle="none", exportselection=False, relief="flat", borderwidth=0, highlightthickness=1)
        self.packs.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.packs.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.packs.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.pack_status = ttk.Label(actions, text="尚未扫描语音包", style="Hint.TLabel")
        self.pack_status.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="刷新语音包", command=self.refresh_packs).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="设为监听包", command=self.select_pack).grid(row=0, column=2, padx=(8, 0))
        self.refresh_packs()

    def refresh_packs(self) -> None:
        root = project_dir() / "data" / "voice_packs"
        self.manifest_items = sorted(root.rglob("manifest.json")) if root.exists() else []
        if self.app.selected_voice_pack is not None and not self.app.selected_voice_pack.exists():
            self.app.set_voice_pack(None, None)
        self.packs.delete(0, "end")
        if not self.manifest_items:
            self.packs.insert("end", "  尚未生成语音包")
            self.pack_status.configure(text="语音包为空")
            return
        for path in self.manifest_items:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                segment = manifest.get("segment") or {}
                code = segment.get("story_code") or segment.get("story_txt") or path.parent.name
                marker = "★ 当前监听" if self.app.selected_voice_pack == path else ""
                self.packs.insert("end", f"  {marker} {segment.get('event_name', path.parents[3].name)} / {code}  · {manifest.get('engine')} / {manifest.get('voice')}")
            except Exception:
                self.packs.insert("end", f"  {path.parent.name}  （清单格式异常）")
        self.packs.selection_set(0)
        self.pack_status.configure(text=f"已找到 {len(self.manifest_items)} 个语音包")

    def select_pack(self) -> None:
        selection = self.packs.curselection()
        if not selection or not self.manifest_items:
            self.app.set_status("请先选择一个语音包")
            return
        selected = self.manifest_items[selection[0]]
        try:
            manifest = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            messagebox.showerror("语音包错误", f"无法读取语音包清单：\n\n{exc}", parent=self)
            return
        self.app.set_voice_pack(selected, manifest)
        self.refresh_packs()
        self.app.set_status(f"已选择监听语音包：{selected}")



class ConnectedVoicePackPanel(BasePanel):
    """Unified manager for downloaded stories, voice packs and caches."""

    def __init__(self, master: tk.Misc, app: "ReaderApp") -> None:
        self.resources: list[dict[str, object]] = []
        super().__init__(master, app)

    def build(self) -> None:
        self.add_header("资源管理", "统一管理剧情资源、语音资源、下载缓存和情绪分析缓存；支持 Ctrl 多选后右键删除。")
        card = self.card(self, 1)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        ttk.Label(card, text="本地资源", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        list_area = ttk.Frame(card, style="Card.TFrame")
        list_area.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.resources_view = tk.Listbox(
            list_area,
            height=16,
            activestyle="none",
            exportselection=False,
            selectmode=tk.EXTENDED,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
        )
        self.resources_view.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.resources_view.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.resources_view.configure(yscrollcommand=scroll.set)
        self.resources_view.bind("<Button-3>", self._show_resource_menu)
        self.resource_menu = tk.Menu(self, tearoff=False)
        self.resource_menu.add_command(label="删除选中资源", command=self.delete_selected_resources)

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.resource_status = ttk.Label(actions, text="尚未扫描资源", style="Hint.TLabel")
        self.resource_status.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="刷新资源", command=self.refresh_resources).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="删除选中", command=self.delete_selected_resources).grid(row=0, column=2, padx=(8, 0))
        self.refresh_resources()

    def refresh_resources(self) -> None:
        data_root = project_dir() / "data"
        resources: list[dict[str, object]] = []
        story_root = data_root / "stories"
        for path in sorted(story_root.rglob("*.json")) if story_root.exists() else []:
            resources.append({
                "kind": "剧情资源",
                "path": path,
                "delete_path": path,
                "label": f"[剧情] {path.relative_to(story_root)}",
            })

        voice_root = data_root / "voice_packs"
        for manifest_path in sorted(voice_root.rglob("manifest.json")) if voice_root.exists() else []:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                segment = manifest.get("segment") or {}
                event = segment.get("event_name") or manifest_path.parents[3].name
                code = segment.get("story_code") or segment.get("story_txt") or manifest_path.parent.name
                entries = manifest.get("lines") or []
                completed = sum(entry.get("status", "completed") == "completed" for entry in entries)
                label = f"[语音] {event} / {code} / {manifest.get('engine', '')} / {manifest.get('voice', '')} ({completed}/{len(entries)})"
            except (OSError, ValueError):
                label = f"[语音] {manifest_path.parent.name}（清单异常）"
            resources.append({
                "kind": "语音资源",
                "path": manifest_path,
                "delete_path": manifest_path.parent,
                "label": label,
            })

        prts_root = data_root / "cache" / "prts"
        for path in sorted(prts_root.rglob("*")) if prts_root.exists() else []:
            if path.is_file():
                resources.append({
                    "kind": "下载缓存",
                    "path": path,
                    "delete_path": path,
                    "label": f"[下载缓存] {path.relative_to(prts_root)}",
                })

        analysis_root = data_root / "tts_analysis"
        for path in sorted(analysis_root.rglob("*.json")) if analysis_root.exists() else []:
            resources.append({
                "kind": "情绪分析缓存",
                "path": path,
                "delete_path": path,
                "label": f"[情绪缓存] {path.relative_to(analysis_root)}",
            })

        qwen_cache = data_root / "qwen_cache"
        if qwen_cache.exists():
            resources.append({
                "kind": "运行缓存",
                "path": qwen_cache,
                "delete_path": qwen_cache,
                "label": "[运行缓存] Qwen3/ROCm 缓存（可清理）",
            })

        self.resources = resources
        self.resources_view.delete(0, "end")
        for resource in resources:
            self.resources_view.insert("end", str(resource["label"]))
        self.resource_status.configure(text=f"共找到 {len(resources)} 个资源")

    def _show_resource_menu(self, event: tk.Event) -> None:
        index = self.resources_view.nearest(event.y)
        if index < 0 or index >= len(self.resources):
            return
        if index not in self.resources_view.curselection():
            self.resources_view.selection_clear(0, "end")
            self.resources_view.selection_set(index)
        self.resource_menu.tk_popup(event.x_root, event.y_root)

    def delete_selected_resources(self) -> None:
        selections = self.resources_view.curselection()
        if not selections:
            self.app.set_status("请先选择要删除的资源")
            return
        selected = [self.resources[index] for index in selections]
        labels = "\n".join(str(item["label"]) for item in selected[:12])
        if len(selected) > 12:
            labels += f"\n……还有 {len(selected) - 12} 项"
        if not messagebox.askyesno("确认删除资源", f"以下资源将被删除：\n\n{labels}\n\n此操作不可恢复，是否继续？", parent=self):
            return

        data_root = (project_dir() / "data").resolve()
        deleted = 0
        for resource in selected:
            target = Path(resource["delete_path"]).resolve()
            try:
                target.relative_to(data_root)
            except ValueError:
                continue
            active = self.app.selected_voice_pack
            if active is not None:
                try:
                    active.resolve().relative_to(target)
                except ValueError:
                    pass
                else:
                    self.app.stop_listening()
                    self.app.set_voice_pack(None, None)
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()
                else:
                    continue
                deleted += 1
            except OSError as exc:
                self.app.set_status(f"删除资源失败：{exc}")
        self.refresh_resources()
        story_panel = self.app.panel_instances.get("story_download")
        if isinstance(story_panel, ConnectedStoryDownloadPanel):
            story_panel.refresh_stories()
        generation_panel = self.app.panel_instances.get("voice_generation")
        if isinstance(generation_panel, ConnectedVoiceGenerationPanel):
            generation_panel.refresh_stories()
        self.app.set_status(f"已删除 {deleted} 个资源")


class ReaderPanel(BasePanel):
    def build(self) -> None:
        self.add_header("朗读监听", "当前模块用于查看匹配状态；具体的开始、停止、选区和调速控制固定在左下角。")
        card = self.card(self, 1)
        card.columnconfigure(1, weight=1)
        rows = (
            ("监听状态", "未启动"),
            ("台词区域", "未选择"),
            ("当前语音包", "未选择"),
            ("最近 OCR 文本", "等待识别…"),
            ("匹配结果", "等待匹配…"),
        )
        for index, (label, value) in enumerate(rows):
            ttk.Label(card, text=label, style="Section.TLabel").grid(
                row=index, column=0, sticky="w", padx=(0, 28), pady=10
            )
            ttk.Label(card, text=value, style="Value.TLabel").grid(
                row=index, column=1, sticky="w", pady=10
            )
        self.region_value = card.grid_slaves(row=1, column=1)[0]
        self.pack_value = card.grid_slaves(row=2, column=1)[0]
        self.listener_value = card.grid_slaves(row=0, column=1)[0]
        self.ocr_value = card.grid_slaves(row=3, column=1)[0]
        self.match_value = card.grid_slaves(row=4, column=1)[0]
        ttk.Label(card, text="选择当前语音包", style="Section.TLabel").grid(
            row=5, column=0, sticky="w", padx=(0, 28), pady=(16, 10)
        )
        pack_area = ttk.Frame(card, style="Card.TFrame")
        pack_area.grid(row=5, column=1, sticky="ew", pady=(16, 10))
        pack_area.columnconfigure(0, weight=1)
        self.reader_pack_var = tk.StringVar(value="")
        self.reader_pack_box = ttk.Combobox(pack_area, textvariable=self.reader_pack_var, state="readonly", width=38)
        self.reader_pack_box.grid(row=0, column=0, sticky="ew")
        self.reader_pack_box.bind("<<ComboboxSelected>>", lambda _event: self.select_reader_pack())
        ttk.Button(pack_area, text="刷新", command=self.refresh_reader_packs).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(pack_area, text="应用", command=self.select_reader_pack).grid(row=0, column=2, padx=(8, 0))
        self.reader_pack_paths: list[Path] = []
        self.refresh_reader_packs()
        self.update_region(self.app.region)
        self.update_voice_pack(self.app.selected_voice_pack, self.app.selected_voice_manifest)
        self.update_listening(self.app.listening)

    def update_region(self, region: tuple[int, int, int, int] | None) -> None:
        if region is None:
            self.region_value.configure(text="未选择")
        else:
            x, y, width, height = region
            self.region_value.configure(text=f"屏幕区域：{x}, {y}  {width}×{height}")

    def update_voice_pack(self, path: Path | None, manifest: dict | None) -> None:
        if path is None:
            self.pack_value.configure(text="未选择")
            if hasattr(self, "reader_pack_var"):
                self.reader_pack_var.set("")
            return
        segment = (manifest or {}).get("segment") or {}
        event_name = segment.get("event_name", path.parent.parent.name)
        code = segment.get("story_code", path.parent.name)
        self.pack_value.configure(text=f"{event_name} / {code}")
        if hasattr(self, "reader_pack_paths"):
            self.refresh_reader_packs()
            if path in self.reader_pack_paths:
                self.reader_pack_var.set(self.reader_pack_box["values"][self.reader_pack_paths.index(path)])

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
        self.reader_pack_box.configure(values=labels)
        if self.app.selected_voice_pack in self.reader_pack_paths:
            self.reader_pack_var.set(labels[self.reader_pack_paths.index(self.app.selected_voice_pack)])

    def select_reader_pack(self) -> None:
        index = self.reader_pack_box.current()
        if index < 0 or index >= len(self.reader_pack_paths):
            self.app.set_status("请先选择当前语音包")
            return
        path = self.reader_pack_paths[index]
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            messagebox.showerror("语音包错误", f"无法读取语音包清单：\n\n{exc}", parent=self)
            return
        self.app.set_voice_pack(path, manifest)
        self.app.set_status(f"已设置当前监听语音包：{path}")

    def update_listening(self, listening: bool) -> None:
        self.listener_value.configure(text="监听中" if listening else "未启动")

    def update_ocr(self, text: str) -> None:
        self.ocr_value.configure(text=text or "未识别到文字")

    def update_match(self, text: str) -> None:
        self.match_value.configure(text=text)


class SettingsPanel(BasePanel):
    def build(self) -> None:
        self.add_header("设置", "集中管理数据目录、OCR 识别参数、语音缓存和程序行为。")
        card = self.card(self, 1)
        card.columnconfigure(1, weight=1)
        settings = (("剧情数据目录", r"data\stories"), ("语音缓存目录", r"data\voice_cache"))
        for index, (label, value) in enumerate(settings):
            ttk.Label(card, text=label).grid(row=index, column=0, sticky="w", padx=(0, 24), pady=10)
            ttk.Entry(card, width=42).grid(row=index, column=1, sticky="ew", pady=10)
            card.grid_slaves(row=index, column=1)[0].insert(0, value)

        row = len(settings)
        ttk.Label(card, text="OCR 轮询间隔").grid(row=row, column=0, sticky="w", padx=(0, 24), pady=10)
        interval_area = ttk.Frame(card, style="Card.TFrame")
        interval_area.grid(row=row, column=1, sticky="w", pady=10)
        self.interval_var = tk.StringVar(value=f"{self.app.ocr_interval:.1f}")
        validate = (self.register(lambda value: value == "" or bool(re.fullmatch(r"\d{0,2}(?:\.\d{0,2})?", value))), "%P")
        self.interval_spinbox = ttk.Spinbox(
            interval_area,
            from_=0.1,
            to=5.0,
            increment=0.1,
            format="%.1f",
            width=8,
            textvariable=self.interval_var,
            validate="key",
            validatecommand=validate,
            command=self.save_settings,
        )
        self.interval_spinbox.grid(row=0, column=0, sticky="w")
        ttk.Label(interval_area, text="秒", style="Hint.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.interval_spinbox.bind("<FocusOut>", lambda _event: self.save_settings())
        self.interval_spinbox.bind("<Return>", lambda _event: self.save_settings())

        api_row = row + 1
        ttk.Label(card, text="DeepSeek API Key").grid(row=api_row, column=0, sticky="w", padx=(0, 24), pady=10)
        self.deepseek_key_var = tk.StringVar(value=str(self.app.settings.get("deepseek_api_key", "")))
        ttk.Entry(card, textvariable=self.deepseek_key_var, show="*", width=42).grid(
            row=api_row, column=1, sticky="ew", pady=10
        )
        model_row = api_row + 1
        ttk.Label(card, text="DeepSeek 模型").grid(
            row=model_row, column=0, sticky="w", padx=(0, 24), pady=10
        )
        model_area = ttk.Frame(card, style="Card.TFrame")
        model_area.grid(row=model_row, column=1, sticky="ew", pady=10)
        model_area.columnconfigure(0, weight=1)
        selected_model = str(self.app.settings.get("deepseek_model", DEFAULT_DEEPSEEK_MODEL)).strip()
        self.deepseek_model_var = tk.StringVar(value=selected_model or DEFAULT_DEEPSEEK_MODEL)
        self.deepseek_model_box = ttk.Combobox(
            model_area,
            textvariable=self.deepseek_model_var,
            state="readonly",
            width=30,
            values=(self.deepseek_model_var.get(),),
        )
        self.deepseek_model_box.grid(row=0, column=0, sticky="ew")
        self.deepseek_model_box.bind("<<ComboboxSelected>>", self._remember_deepseek_model)
        self.refresh_models_button = ttk.Button(
            model_area, text="获取模型列表", command=self.refresh_deepseek_models
        )
        self.refresh_models_button.grid(row=0, column=1, padx=(8, 0))
        self.test_model_button = ttk.Button(
            model_area, text="测试当前模型", command=self.test_current_deepseek_model
        )
        self.test_model_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            card,
            text="仅在语音生成中勾选 DeepSeek 优化时使用；剧情文本会发送到 DeepSeek。",
            style="Hint.TLabel",
        ).grid(row=model_row + 1, column=1, sticky="w")

        ttk.Button(card, text="保存设置", command=self.save_settings).grid(
            row=model_row + 2, column=1, sticky="e", pady=(16, 0)
        )

    def refresh_deepseek_models(self) -> None:
        api_key = self.deepseek_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("缺少 API Key", "请先填写 DeepSeek API Key。", parent=self)
            return
        self.app.settings["deepseek_api_key"] = api_key
        self.refresh_models_button.configure(state="disabled")
        self.test_model_button.configure(state="disabled")
        self.app.set_status("正在获取 DeepSeek 可用模型列表...")

        def worker() -> None:
            try:
                models = fetch_deepseek_models(api_key)
            except Exception as exc:
                self.after(0, lambda error=str(exc): self._deepseek_models_failed(error))
                return
            self.after(0, lambda: self._apply_deepseek_models(models))

        threading.Thread(target=worker, name="deepseek-model-list", daemon=True).start()

    def _apply_deepseek_models(self, models: tuple[str, ...]) -> None:
        values = tuple(dict.fromkeys(models))
        self.deepseek_model_box.configure(values=values)
        current = self.deepseek_model_var.get().strip()
        if current not in values:
            current = DEFAULT_DEEPSEEK_MODEL if DEFAULT_DEEPSEEK_MODEL in values else values[0]
            self.deepseek_model_var.set(current)
        self._remember_deepseek_model()
        self.refresh_models_button.configure(state="normal")
        self.test_model_button.configure(state="normal")
        self.app.set_status(f"已获取 {len(values)} 个 DeepSeek 模型，当前选择：{current}")

    def _deepseek_models_failed(self, error: str) -> None:
        self.refresh_models_button.configure(state="normal")
        self.test_model_button.configure(state="normal")
        self.app.set_status("获取 DeepSeek 模型列表失败")
        messagebox.showerror("获取模型列表失败", error, parent=self)

    def test_current_deepseek_model(self) -> None:
        api_key = self.deepseek_key_var.get().strip()
        model = self.deepseek_model_var.get().strip() or DEFAULT_DEEPSEEK_MODEL
        if not api_key:
            messagebox.showwarning("缺少 API Key", "请先填写 DeepSeek API Key。", parent=self)
            return
        self.app.settings["deepseek_api_key"] = api_key
        self.app.settings["deepseek_model"] = model
        self.refresh_models_button.configure(state="disabled")
        self.test_model_button.configure(state="disabled")
        self.app.set_status(f"正在测试 DeepSeek 模型：{model}")

        def worker() -> None:
            try:
                instruction = test_deepseek_model(api_key, model)
            except Exception as exc:
                self.after(0, lambda error=str(exc): self._deepseek_test_failed(error))
                return
            self.after(0, lambda: self._deepseek_test_succeeded(model, instruction))

        threading.Thread(target=worker, name="deepseek-model-test", daemon=True).start()

    def _remember_deepseek_model(self, _event: object | None = None) -> None:
        self.app.settings["deepseek_model"] = (
            self.deepseek_model_var.get().strip() or DEFAULT_DEEPSEEK_MODEL
        )

    def _deepseek_test_succeeded(self, model: str, instruction: str) -> None:
        self.refresh_models_button.configure(state="normal")
        self.test_model_button.configure(state="normal")
        self.app.set_status(f"DeepSeek 模型测试成功：{model}")
        messagebox.showinfo(
            "DeepSeek 测试成功",
            f"模型：{model}\n返回的情绪指令：\n{instruction}",
            parent=self,
        )

    def _deepseek_test_failed(self, error: str) -> None:
        self.refresh_models_button.configure(state="normal")
        self.test_model_button.configure(state="normal")
        self.app.set_status("DeepSeek 模型测试失败")
        messagebox.showerror("DeepSeek 测试失败", error, parent=self)

    def save_settings(self) -> None:
        try:
            value = max(0.1, min(5.0, round(float(self.interval_var.get()), 1)))
        except (TypeError, ValueError, tk.TclError):
            value = self.app.ocr_interval
        self.interval_var.set(f"{value:.1f}")
        self.app.set_ocr_interval(value)
        self.app.settings["deepseek_api_key"] = self.deepseek_key_var.get().strip()
        self.app.settings["deepseek_model"] = (
            self.deepseek_model_var.get().strip() or DEFAULT_DEEPSEEK_MODEL
        )
        save_app_settings(self.app.settings)


class ReaderApp(tk.Tk):
    def __init__(self) -> None:
        configure_windows_dpi()
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(960, 620)
        self.configure(bg="#eef1f5")
        self._configure_styles()
        self.settings = load_app_settings()
        try:
            self.ocr_interval = max(0.1, min(5.0, float(self.settings.get("ocr_interval", 0.25))))
        except (TypeError, ValueError):
            self.ocr_interval = 0.25
        self.region: tuple[int, int, int, int] | None = None
        self._region_overlay: tk.Toplevel | None = None
        self.selected_voice_pack: Path | None = None
        self.selected_voice_manifest: dict | None = None
        self.listening = False
        self.listener_engine: ListenerEngine | None = None
        self._build_shell()
        self._build_modules()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_module("story_download")

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#eef1f5")
        style.configure("Sidebar.TFrame", background="#f7f8fa")
        style.configure("Workspace.TFrame", background="#ffffff")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("Header.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#ffffff", foreground="#1f2937", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("PageTitle.TLabel", background="#ffffff", foreground="#1f2937", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", background="#ffffff", foreground="#273444", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Value.TLabel", background="#ffffff", foreground="#526170", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#7b8794", font=("Microsoft YaHei UI", 9))
        style.configure("SidebarTitle.TLabel", background="#f7f8fa", foreground="#273444", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("SidebarHint.TLabel", background="#f7f8fa", foreground="#8793a1", font=("Microsoft YaHei UI", 9))
        style.configure("Nav.TButton", anchor="w", padding=(16, 11), font=("Microsoft YaHei UI", 10))
        style.configure("Global.TFrame", background="#fffaf0", relief="solid", borderwidth=1)
        style.configure("GlobalTitle.TLabel", background="#fffaf0", foreground="#573b17", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Global.TLabel", background="#fffaf0", foreground="#5f4b2d", font=("Microsoft YaHei UI", 9))

    def _build_shell(self) -> None:
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 15))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.module_title = ttk.Label(header, text="", style="Hint.TLabel")
        self.module_title.grid(row=0, column=1, sticky="e", padx=(20, 0))
        separator = ttk.Separator(self, orient="horizontal")
        separator.grid(row=0, column=0, sticky="sew")

        body = ttk.Frame(self, style="App.TFrame", padding=(12, 12, 12, 12))
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, minsize=236, weight=0)
        body.columnconfigure(1, weight=1)

        self.sidebar = ttk.Frame(body, style="Sidebar.TFrame", padding=(12, 14))
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.sidebar.rowconfigure(2, weight=1)
        self.sidebar.columnconfigure(0, weight=1)
        ttk.Label(self.sidebar, text="功能模块", style="SidebarTitle.TLabel").grid(
            row=0, column=0, sticky="w", padx=6
        )
        ttk.Label(self.sidebar, text="选择模块后，在右侧工作区操作", style="SidebarHint.TLabel").grid(
            row=1, column=0, sticky="w", padx=6, pady=(5, 12)
        )
        self.nav_frame = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        self.nav_frame.grid(row=2, column=0, sticky="new")
        self.nav_frame.columnconfigure(0, weight=1)

        self.global_controls = ttk.Frame(self.sidebar, style="Global.TFrame", padding=12)
        self.global_controls.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        self._build_global_controls()

        self.workspace = ttk.Frame(body, style="Workspace.TFrame")
        self.workspace.grid(row=0, column=1, sticky="nsew")
        self.workspace.rowconfigure(0, weight=1)
        self.workspace.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(16, 5), style="Hint.TLabel")
        status.grid(row=2, column=0, sticky="ew")

    def _build_global_controls(self) -> None:
        ttk.Label(self.global_controls, text="朗读控制", style="GlobalTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Button(self.global_controls, text="选择台词区域", command=self.select_region).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(10, 7)
        )
        self.start_button = ttk.Button(self.global_controls, text="开始监听", command=self.start_listening)
        self.start_button.grid(row=2, column=0, sticky="ew", padx=(0, 4))
        self.stop_button = ttk.Button(self.global_controls, text="停止监听", command=self.stop_listening, state="disabled")
        self.stop_button.grid(row=2, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(self.global_controls, text="语速", style="Global.TLabel").grid(
            row=3, column=0, sticky="w", pady=(12, 0)
        )
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_value = ttk.Label(self.global_controls, text="1.0x", style="Global.TLabel")
        self.speed_value.grid(row=3, column=1, sticky="e", pady=(12, 0))
        ttk.Scale(
            self.global_controls,
            from_=0.1,
            to=2.0,
            variable=self.speed_var,
            command=self.on_speed_changed,
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        ttk.Label(self.global_controls, text="0.1x        0.5x        1.0x        1.5x        2.0x", style="Global.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0)
        )
        self.listener_status = ttk.Label(self.global_controls, text="状态：未监听", style="Global.TLabel")
        self.listener_status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_modules(self) -> None:
        self.modules = {
            "story_download": ModuleSpec("story_download", "剧情下载", "选择和导出剧情文本", StoryDownloadPanel),
            "voice_generation": ModuleSpec("voice_generation", "语音生成", "批量预生成语音", VoiceGenerationPanel),
            "voice_packs": ModuleSpec("voice_packs", "资源管理", "管理剧情、语音和缓存资源", VoicePackPanel),
            "reader": ModuleSpec("reader", "朗读监听", "查看 OCR 和匹配状态", ReaderPanel),
            "settings": ModuleSpec("settings", "设置", "程序和数据目录设置", SettingsPanel),
        }
        self.modules["story_download"] = ModuleSpec(
            "story_download",
            self.modules["story_download"].title,
            self.modules["story_download"].description,
            ConnectedStoryDownloadPanel,
        )
        self.modules["voice_generation"] = ModuleSpec(
            "voice_generation",
            self.modules["voice_generation"].title,
            self.modules["voice_generation"].description,
            ConnectedVoiceGenerationPanel,
        )
        self.modules["voice_packs"] = ModuleSpec(
            "voice_packs",
            self.modules["voice_packs"].title,
            self.modules["voice_packs"].description,
            ConnectedVoicePackPanel,
        )
        self.nav_buttons: Dict[str, ttk.Button] = {}
        for row, spec in enumerate(self.modules.values()):
            button = ttk.Button(
                self.nav_frame,
                text=spec.title,
                style="Nav.TButton",
                command=lambda key=spec.key: self.show_module(key),
            )
            button.grid(row=row, column=0, sticky="ew", pady=3)
            self.nav_buttons[spec.key] = button
        self.active_panel: Optional[BasePanel] = None
        self.panel_instances: dict[str, BasePanel] = {}

    def show_module(self, key: str) -> None:
        spec = self.modules[key]
        if self.active_panel is not None:
            self.active_panel.grid_remove()
        panel = self.panel_instances.get(key)
        if panel is None:
            panel = spec.panel(self.workspace, self)
            self.panel_instances[key] = panel
        panel.grid(row=0, column=0, sticky="nsew")
        self.active_panel = panel
        self.module_title.configure(text=f"当前模块：{spec.title}")
        for module_key, button in self.nav_buttons.items():
            button.configure(state="disabled" if module_key == key else "normal")
        self.set_status(f"已切换到：{spec.title}")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_ocr_interval(self, value: float) -> None:
        self.ocr_interval = max(0.1, min(5.0, round(float(value), 1)))
        self.settings["ocr_interval"] = self.ocr_interval
        save_app_settings(self.settings)
        self.set_status(f"OCR 轮询间隔已保存：{self.ocr_interval:.1f} 秒")

    def set_voice_pack(self, path: Path | None, manifest: dict | None) -> None:
        self.selected_voice_pack = path
        self.selected_voice_manifest = manifest
        reader = self.panel_instances.get("reader")
        if isinstance(reader, ReaderPanel):
            reader.update_voice_pack(path, manifest)

    def _update_reader_listening(self, listening: bool) -> None:
        reader = self.panel_instances.get("reader")
        if isinstance(reader, ReaderPanel):
            reader.update_listening(listening)

    def _listener_event(self, kind: str, text: str, data: dict | None = None) -> None:
        self.after(0, lambda: self._handle_listener_event(kind, text, data))

    def _handle_listener_event(self, kind: str, text: str, data: dict | None = None) -> None:
        reader = self.panel_instances.get("reader")
        if isinstance(reader, ReaderPanel):
            if kind == "ocr":
                reader.update_ocr(text)
            elif kind == "match":
                score = float((data or {}).get("score", 1.0))
                reader.update_match(f"已匹配（{score:.0%}）：{text}")
            elif kind == "unmatched":
                reader.update_match(text)
        if kind == "status":
            self.set_status(text)
        elif kind == "match":
            self.set_status(f"已匹配并播放：{text}")
        elif kind == "unmatched":
            self.set_status("OCR 已识别文字，但当前语音包没有匹配项")
        elif kind == "error":
            self.listening = False
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.listener_status.configure(text="状态：监听错误")
            self._update_reader_listening(False)
            if reader is not None:
                reader.update_match(f"监听错误：{text}")
            self.set_status(f"OCR 监听失败：{text}")
            messagebox.showerror("OCR 监听失败", text, parent=self)
        elif kind == "stopped" and not self.listening:
            self.set_status(text)

    def select_region(self) -> None:
        if self._region_overlay is not None:
            return
        self.listener_status.configure(text="状态：请拖拽框选台词区域")
        self.set_status("请在屏幕上拖拽框选游戏台词区域，按 Esc 取消")
        self.update_idletasks()
        origin_x = int(self.winfo_vrootx())
        origin_y = int(self.winfo_vrooty())
        width = int(self.winfo_vrootwidth() or self.winfo_screenwidth())
        height = int(self.winfo_vrootheight() or self.winfo_screenheight())
        overlay = tk.Toplevel(self)
        self._region_overlay = overlay
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        try:
            overlay.attributes("-alpha", 0.25)
        except tk.TclError:
            pass
        overlay.geometry(f"{width}x{height}+{origin_x}+{origin_y}")
        canvas = tk.Canvas(overlay, background="#102030", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(24, 24, anchor="nw", fill="white", font=("Microsoft YaHei UI", 14, "bold"), text="拖拽框选台词区域 · Esc 取消")
        state: dict[str, int | None] = {"x": None, "y": None, "rect": None}

        def on_press(event: tk.Event) -> None:
            state["x"], state["y"] = event.x, event.y
            if state["rect"] is not None:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#35a7ff", width=3)

        def on_move(event: tk.Event) -> None:
            if state["rect"] is not None and state["x"] is not None and state["y"] is not None:
                canvas.coords(state["rect"], state["x"], state["y"], event.x, event.y)

        def finish(cancelled: bool = False, end_x: int | None = None, end_y: int | None = None) -> None:
            if cancelled:
                overlay.destroy()
                self._region_overlay = None
                self.listener_status.configure(text="状态：未监听")
                self.set_status("已取消台词区域选择")
                return
            if state["x"] is None or state["y"] is None:
                return
            end_x = int(end_x if end_x is not None else canvas.winfo_pointerx() - overlay.winfo_rootx())
            end_y = int(end_y if end_y is not None else canvas.winfo_pointery() - overlay.winfo_rooty())
            x1, x2 = sorted((int(state["x"]), end_x))
            y1, y2 = sorted((int(state["y"]), end_y))
            if x2 - x1 < 8 or y2 - y1 < 8:
                self.set_status("选区太小，请重新拖拽选择")
                return
            self.region = (origin_x + x1, origin_y + y1, x2 - x1, y2 - y1)
            overlay.destroy()
            self._region_overlay = None
            self.listener_status.configure(text="状态：已选择台词区域")
            self.set_status(f"台词区域已保存：{self.region[2]}×{self.region[3]}")
            reader = self.panel_instances.get("reader")
            if isinstance(reader, ReaderPanel):
                reader.update_region(self.region)

        def on_release(event: tk.Event) -> None:
            finish(False, event.x, event.y)

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", lambda _event: finish(True))
        overlay.focus_force()
        self.show_module("reader")

    def start_listening(self) -> None:
        if self.selected_voice_pack is None or not self.selected_voice_pack.exists():
            messagebox.showwarning("未选择语音包", "请先在“朗读监听”模块中选择当前语音包。", parent=self)
            self.set_status("无法开始监听：尚未选择语音包")
            return
        if self.region is None:
            self.set_status("请先点击“选择台词区域”完成框选")
            return
        try:
            self.listener_engine = ListenerEngine(self.region, self.selected_voice_pack, self._listener_event, self.ocr_interval)
            self.listener_engine.start()
        except Exception as exc:
            messagebox.showerror("OCR 启动失败", str(exc), parent=self)
            self.set_status(f"OCR 启动失败：{exc}")
            return
        self.listening = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.listener_status.configure(text="状态：监听中")
        self._update_reader_listening(True)
        self.set_status("朗读监听已启动（OCR匹配功能接入中）")

    def stop_listening(self) -> None:
        if self.listener_engine is not None:
            self.listener_engine.stop()
            self.listener_engine = None
        self.listening = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.listener_status.configure(text="状态：未监听")
        self._update_reader_listening(False)
        self.set_status("朗读监听已停止")

    def on_speed_changed(self, value: str) -> None:
        speed = float(value)
        self.speed_value.configure(text=f"{speed:.1f}x")

    def _on_close(self) -> None:
        if self.listener_engine is not None:
            self.listener_engine.stop()
        for panel in self.panel_instances.values():
            if isinstance(panel, ConnectedVoiceGenerationPanel):
                panel.backend.release_qwen()
        self.destroy()

    def run(self) -> None:
        self.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--check", action="store_true", help="检查 GUI 外壳模块是否可以导入")
    args = parser.parse_args()
    if args.check:
        print(f"{APP_TITLE}: GUI shell import OK")
        print("modules: story_download, voice_generation, voice_packs, reader, settings")
        return
    ReaderApp().run()


if __name__ == "__main__":
    main()
