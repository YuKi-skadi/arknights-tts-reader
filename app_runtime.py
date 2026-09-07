from __future__ import annotations

import ctypes
import json
import os
import sys
import shutil
import threading
import time
from pathlib import Path


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


_json_lock = threading.RLock()


def read_json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点应为对象：{path.name}")
    return value


def atomic_json_save(path: Path, payload: dict, *, backup: bool = False) -> None:
    """Never truncate the last checkpoint; keep a valid previous revision."""
    with _json_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if backup and path.exists():
            try:
                read_json_object(path)
            except (OSError, ValueError):
                shutil.copy2(path, path.with_name(path.name + f".corrupt-{time.time_ns()}"))
            else:
                backup_tmp = path.with_name(path.name + ".bak.tmp")
                shutil.copy2(path, backup_tmp)
                backup_tmp.replace(path.with_name(path.name + ".bak"))
        temporary.replace(path)


def load_app_settings() -> dict[str, object]:
    path = project_dir() / "data" / "settings.json"
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            return read_json_object(candidate)
        except (OSError, ValueError):
            continue
    return {}


def save_app_settings(settings: dict[str, object]) -> None:
    path = project_dir() / "data" / "settings.json"
    atomic_json_save(path, settings, backup=True)
