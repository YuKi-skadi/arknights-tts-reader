"""Categorized resource management panel for Qt."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QComboBox, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton

from app_runtime import project_dir
from qt_base import BasePanel


class ConnectedVoicePackPanel(BasePanel):
    CATEGORY_LABELS = {
        "story": "剧情",
        "voice": "语音",
        "custom_voice": "自定义语音",
        "custom_text": "自定义文本",
        "download_cache": "下载缓存",
        "runtime_cache": "运行缓存",
    }
    CUSTOM_TEXT_EXTENSIONS = {".txt", ".md", ".log"}

    def __init__(self, app: object) -> None:
        self.resources_by_category: dict[str, list[dict[str, object]]] = {key: [] for key in self.CATEGORY_LABELS}
        self.resources: list[dict[str, object]] = []
        super().__init__(app, "资源管理", "按类别查看和整理本地资源；自定义语音可以按语音名导出。")
        self.build()

    def build(self) -> None:
        card, layout = self.card("本地资源")
        toolbar = QHBoxLayout()
        self.category = QComboBox()
        self.category.addItem("全部", "all")
        for key, label in self.CATEGORY_LABELS.items():
            self.category.addItem(label, key)
        self.category.currentIndexChanged.connect(self._refresh_resource_view)
        toolbar.addWidget(QLabel("资源分类"))
        toolbar.addWidget(self.category)
        toolbar.addStretch(1)
        refresh = QPushButton("刷新资源")
        delete = QPushButton("删除选中")
        self.export_button = QPushButton("导出生成语音")
        refresh.clicked.connect(self.refresh_resources)
        delete.clicked.connect(self.delete_selected_resources)
        self.export_button.clicked.connect(self.export_selected_custom_voices)
        toolbar.addWidget(refresh)
        toolbar.addWidget(delete)
        toolbar.addWidget(self.export_button)
        layout.addLayout(toolbar)

        self.resources_view = QListWidget()
        self.resources_view.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.resources_view.itemSelectionChanged.connect(self._update_export_state)
        self.resources_view.setMinimumHeight(430)
        layout.addWidget(self.resources_view, 1)
        self.resource_status = QLabel("尚未扫描资源")
        self.resource_status.setObjectName("pageDescription")
        layout.addWidget(self.resource_status)
        self.add_card(card)
        self.add_stretch()
        self.refresh_resources()

    def refresh_resources(self) -> None:
        data_root = project_dir() / "data"
        grouped = {key: [] for key in self.CATEGORY_LABELS}

        def add(category: str, resource: dict[str, object]) -> None:
            resource["category"] = category
            grouped[category].append(resource)

        story_root = data_root / "stories"
        for path in sorted(story_root.rglob("*.json")) if story_root.exists() else []:
            add("story", {"kind": "剧情资源", "path": path, "delete_path": path, "label": f"[剧情] {path.relative_to(story_root)}"})

        custom_text_root = data_root / "custom_texts"
        if custom_text_root.exists():
            for path in sorted(custom_text_root.iterdir(), key=lambda item: item.name.casefold()):
                if path.is_file() and path.suffix.lower() in self.CUSTOM_TEXT_EXTENSIONS:
                    add("custom_text", {"kind": "自定义文本", "path": path, "delete_path": path, "label": f"[自定义文本] {path.name}"})

        voice_root = data_root / "voice_packs"
        manifests = sorted(voice_root.rglob("manifest.json")) if voice_root.exists() else []
        for manifest_path in manifests:
            category = "voice"
            export_name = ""
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                segment = manifest.get("segment") or {}
                event = segment.get("event_name") or manifest_path.parents[3].name
                code = segment.get("story_code") or segment.get("story_txt") or manifest_path.parent.name
                entries = manifest.get("lines") or []
                completed = sum(entry.get("status", "completed") == "completed" for entry in entries)
                source_story = str(manifest.get("source_story", "")).replace("\\", "/")
                is_custom = str(segment.get("event_id", "")) == "custom_text" or "/custom_texts/" in f"/{source_story.strip('/')}"
                category = "custom_voice" if is_custom else "voice"
                export_name = str(segment.get("story_name") or segment.get("story_txt") or code)
                label_prefix = "自定义语音" if is_custom else "语音"
                label = f"[{label_prefix}] {event} / {code} / {manifest.get('engine', '')} / {manifest.get('voice', '')} ({completed}/{len(entries)})"
            except (OSError, ValueError, IndexError):
                label = f"[语音] {manifest_path.parent.name}（清单格式异常）"
            add(category, {"kind": "语音资源", "path": manifest_path, "delete_path": manifest_path.parent, "label": label, "export_name": export_name or manifest_path.parent.name})

        prts_root = data_root / "cache" / "prts"
        if prts_root.exists():
            for path in sorted(prts_root.rglob("*")):
                if path.is_file():
                    add("download_cache", {"kind": "下载缓存", "path": path, "delete_path": path, "label": f"[下载缓存] {path.relative_to(prts_root)}"})

        analysis_root = data_root / "tts_analysis"
        if analysis_root.exists():
            for path in sorted(analysis_root.rglob("*.json")):
                add("runtime_cache", {"kind": "运行缓存", "path": path, "delete_path": path, "label": f"[运行缓存/情绪分析] {path.relative_to(analysis_root)}"})

        for cache_name, label in (("qwen_cache", "Qwen3/ROCm 缓存"), ("voice_cache", "语音缓存")):
            cache_path = data_root / cache_name
            if cache_path.exists():
                add("runtime_cache", {"kind": "运行缓存", "path": cache_path, "delete_path": cache_path, "label": f"[运行缓存] {label}（可清理）"})

        self.resources_by_category = grouped
        self._refresh_resource_view()

    def _refresh_resource_view(self, _index: int = 0) -> None:
        key = str(self.category.currentData() or "all")
        self.resources = [item for values in self.resources_by_category.values() for item in values] if key == "all" else list(self.resources_by_category.get(key, []))
        self.resources_view.clear()
        for resource in self.resources:
            self.resources_view.addItem(str(resource["label"]))
        label = "全部" if key == "all" else self.CATEGORY_LABELS.get(key, "资源")
        self.resource_status.setText(f"{label}：共找到 {len(self.resources)} 个资源")
        self._update_export_state()

    def _update_export_state(self) -> None:
        enabled = self.category.currentData() == "custom_voice" and bool(self.resources_view.selectedItems())
        self.export_button.setEnabled(enabled)

    def delete_selected_resources(self) -> None:
        if any(getattr(panel, "queue_thread", None) is not None for panel in self.app.panel_instances.values()):
            self.warning("任务正在运行", "请先停止生成或下载队列，再删除资源，避免丢失断点和音频。")
            return
        indexes = [self.resources_view.row(item) for item in self.resources_view.selectedItems()]
        if not indexes:
            self.set_status("请先选择要删除的资源")
            return
        selected = [self.resources[index] for index in indexes]
        labels = "\n".join(str(item["label"]) for item in selected[:12])
        if len(selected) > 12:
            labels += f"\n……还有 {len(selected) - 12} 项"
        if QMessageBox.question(self, "确认删除资源", f"以下资源将被删除：\n\n{labels}\n\n此操作不可恢复，是否继续？") != QMessageBox.StandardButton.Yes:
            return
        data_root = (project_dir() / "data").resolve()
        deleted = 0
        for resource in selected:
            target = Path(resource["delete_path"]).resolve()
            if target == data_root:
                continue
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
                self.set_status(f"删除资源失败：{exc}")
        self.refresh_resources()
        self.set_status(f"已删除 {deleted} 个资源")

    @staticmethod
    def _safe_export_name(value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(value)).strip(" .")
        return cleaned or "未命名语音"

    @staticmethod
    def _unique_export_dir(root: Path, name: str) -> Path:
        candidate = root / name
        index = 1
        while candidate.exists():
            candidate = root / f"{name}（{index}）"
            index += 1
        return candidate

    def export_selected_custom_voices(self) -> None:
        if self.category.currentData() != "custom_voice":
            self.set_status("请先切换到“自定义语音”分类")
            return
        indexes = [self.resources_view.row(item) for item in self.resources_view.selectedItems()]
        if not indexes:
            self.set_status("请先选择要导出的自定义语音")
            return
        destination_text = QFileDialog.getExistingDirectory(self, "选择语音导出目录")
        if not destination_text:
            return
        destination = Path(destination_text)
        exported = skipped = 0
        for index in indexes:
            resource = self.resources[index]
            manifest_path = Path(resource["path"])
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                target = self._unique_export_dir(destination, self._safe_export_name(str(resource.get("export_name") or manifest_path.parent.name)))
                target.mkdir(parents=True, exist_ok=False)
                copied = 0
                for entry in manifest.get("lines") or []:
                    audio_name = Path(str(entry.get("audio", ""))).name
                    audio_path = manifest_path.parent / audio_name if audio_name else None
                    if audio_path and audio_path.is_file():
                        shutil.copy2(audio_path, target / audio_name)
                        copied += 1
                shutil.copy2(manifest_path, target / "manifest.json")
                if copied:
                    exported += 1
                else:
                    skipped += 1
                    shutil.rmtree(target, ignore_errors=True)
            except (OSError, ValueError, TypeError) as exc:
                skipped += 1
                self.set_status(f"导出语音失败：{exc}")
        detail = f"，跳过 {skipped} 个" if skipped else ""
        self.set_status(f"已按语音名导出 {exported} 个自定义语音包{detail}")
