"""Portable queue checkpoints and recovery from per-sentence manifests."""

from __future__ import annotations

from copy import deepcopy
import threading
from pathlib import Path
from typing import Any

from app_runtime import project_dir, atomic_json_save, read_json_object

QUEUE_FORMAT = "arknights-tts-generation-queue-v1"


def portable_path(value: object) -> str:
    if not value:
        return ""
    path = Path(str(value))
    try:
        return path.resolve().relative_to(project_dir().resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def restore_path(value: object) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        return project_dir() / path
    if path.exists():
        return path
    # Repair only a known portable data subtree from a legacy absolute path.
    parts = path.parts
    for index, part in enumerate(parts):
        if part.lower() == "data":
            candidate = project_dir().joinpath(*parts[index:])
            if candidate.exists():
                return candidate
    return path


def manifest_progress(path: Path, payload: dict | None = None) -> dict:
    payload = read_json_object(path) if payload is None else payload
    lines = payload.get("lines") or []
    if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
        raise ValueError("语音清单中的逐句记录无效")
    done = failed = 0
    next_line = None
    current_text = ""
    for index, entry in enumerate(lines, 1):
        audio = str(entry.get("audio") or "")
        target = path.parent / Path(audio).name
        complete = entry.get("status", "completed") == "completed" and audio and target.is_file() and target.stat().st_size > 0
        if complete:
            done += 1
        else:
            failed += entry.get("status") == "failed"
            if next_line is None:
                next_line = index
                current_text = str(entry.get("text", ""))
    return {"done": done, "total": len(lines), "failed": failed,
            "next_line": next_line, "current_text": current_text}


class VoiceQueueStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or project_dir() / "data" / "voice_generation_queue.json"
        self._lock = threading.RLock()
        self.load_warning = ""

    _portable_path = staticmethod(portable_path)
    _restore_path = staticmethod(restore_path)

    @staticmethod
    def _config(config: dict, *, restore: bool) -> dict:
        result = deepcopy(config)
        options = result.get("qwen_options")
        if isinstance(options, dict) and options.get("reference_audio"):
            transform = restore_path if restore else portable_path
            options["reference_audio"] = str(transform(options["reference_audio"]))
        return result

    def load(self) -> tuple[list[dict], dict[str, Any]]:
        payload = {}
        for candidate in (self.path, self.path.with_name(self.path.name + ".bak")):
            try:
                payload = read_json_object(candidate)
                if not isinstance(payload.get("items"), list):
                    raise ValueError("队列任务列表无效")
                if candidate != self.path:
                    self.load_warning = "主队列文件无法读取，已从上一次备份恢复；进度已按语音清单校正。"
                break
            except (OSError, ValueError, TypeError):
                if candidate.exists():
                    self.load_warning = "队列记录无法读取；原文件保留，可点击“恢复历史任务”扫描已有语音清单。"
        else:
            return [], {}
        config = payload.get("config")
        config = self._config(config, restore=True) if isinstance(config, dict) else {}
        items = []
        for raw in payload["items"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str) or not raw["path"]:
                continue
            item = dict(raw)
            item["path"] = restore_path(raw["path"])
            if item.get("manifest"):
                item["manifest"] = restore_path(item["manifest"])
            if item.get("status") == "生成中":
                item["status"] = "中断"
            item.setdefault("status", "待处理")
            task_config = item.get("generation_config")
            item["generation_config"] = self._config(task_config, restore=True) if isinstance(task_config, dict) else deepcopy(config)
            self.reconcile(item)
            items.append(item)
        return items, config

    @staticmethod
    def reconcile(item: dict) -> None:
        if not item.get("manifest"):
            return
        try:
            progress = manifest_progress(Path(item["manifest"]))
            item.update(progress)
            if progress["total"] and progress["done"] == progress["total"]:
                item["status"] = "已完成"
            elif item.get("status") == "已完成":
                item["status"] = "部分完成"
        except (OSError, ValueError, TypeError):
            if item.get("status") == "已完成":
                item["status"] = "部分完成"
            item["last_error"] = "语音清单缺失或损坏，可继续任务检查并重新生成缺失片段。"

    def save(self, items: list[dict], config: dict[str, Any] | None = None) -> None:
        with self._lock:
            payload_items = deepcopy(items)
            for item in payload_items:
                item["path"] = portable_path(item.get("path", ""))
                if item.get("manifest"):
                    item["manifest"] = portable_path(item["manifest"])
                if isinstance(item.get("generation_config"), dict):
                    item["generation_config"] = self._config(item["generation_config"], restore=False)
            atomic_json_save(self.path, {"format": QUEUE_FORMAT, "version": 2,
                "config": self._config(config or {}, restore=False), "items": payload_items}, backup=True)

    def recover_manifests(self, items: list[dict]) -> int:
        known = {str(Path(item["manifest"]).resolve()).casefold() for item in items if item.get("manifest")}
        added = 0
        for path in sorted((project_dir() / "data" / "voice_packs").rglob("manifest.json")):
            if str(path.resolve()).casefold() in known:
                continue
            try:
                payload = read_json_object(path)
                if not payload.get("source_story") or not payload.get("engine"):
                    continue
                config = payload.get("generation_config") or {
                    "engine": payload["engine"], "voice": payload.get("voice", ""),
                    "speed": payload.get("speed", 1.0), "qwen_options": payload.get("qwen") or {},
                    "optimize_quality": False}
                source = restore_path(payload["source_story"])
                item = {"path": source, "manifest": path, "status": "中断",
                        "generation_config": self._config(config, restore=True)}
                item.update(manifest_progress(path, payload))
                self.reconcile(item)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            existing = next((entry for entry in items if not entry.get("manifest")
                             and Path(entry["path"]) == source
                             and entry.get("generation_config") == item["generation_config"]), None)
            if existing is not None:
                existing.update(item)
            else:
                items.append(item)
            added += 1
        return added
