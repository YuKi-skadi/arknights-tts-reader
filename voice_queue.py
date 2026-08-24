"""Persistent storage for the voice-generation queue."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app_runtime import project_dir


QUEUE_FORMAT = "arknights-tts-generation-queue-v1"


class VoiceQueueStore:
    """Load and save queue tasks without storing machine-specific paths when possible."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or project_dir() / "data" / "voice_generation_queue.json"
        self._lock = threading.Lock()

    @staticmethod
    def _portable_path(value: object) -> str:
        path = Path(str(value))
        try:
            return str(path.resolve().relative_to(project_dir().resolve())).replace("\\", "/")
        except (OSError, ValueError):
            return str(path)

    @staticmethod
    def _restore_path(value: object) -> Path:
        path = Path(str(value))
        if not path.is_absolute():
            path = project_dir() / path
        return path

    def load(self) -> tuple[list[dict], dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return [], {}
        if not isinstance(payload, dict):
            return [], {}

        config = payload.get("config")
        config = dict(config) if isinstance(config, dict) else {}
        items: list[dict] = []
        for raw in payload.get("items", []):
            if not isinstance(raw, dict) or not raw.get("path"):
                continue
            item = dict(raw)
            item["path"] = self._restore_path(item["path"])
            if item.get("manifest"):
                item["manifest"] = self._restore_path(item["manifest"])
            status = str(item.get("status", "待处理"))
            # These states cannot survive an application exit. The manifest and
            # already generated audio remain available for the next run.
            if status in {"生成中", "已暂停"}:
                status = "待处理"
            item["status"] = status
            task_config = item.get("generation_config")
            if isinstance(task_config, dict):
                item["generation_config"] = dict(task_config)
            elif config:
                # Migrate queues created before generation settings were stored
                # per task. The old queue-wide config is the best available lock.
                item["generation_config"] = dict(config)
            items.append(item)

        return items, config

    def save(self, items: list[dict], config: dict[str, Any] | None = None) -> None:
        payload_items: list[dict] = []
        for item in items:
            payload_item = dict(item)
            payload_item["path"] = self._portable_path(item.get("path", ""))
            if item.get("manifest"):
                payload_item["manifest"] = self._portable_path(item["manifest"])
            payload_items.append(payload_item)
        payload = {
            "format": QUEUE_FORMAT,
            "version": 1,
            "config": config or {},
            "items": payload_items,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(data, encoding="utf-8")
            temporary.replace(self.path)
