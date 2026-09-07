"""Explicit, isolated two-process release smoke test (never uses user data)."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import threading
import time
import traceback
import wave
from unittest.mock import patch


def run_smoke(output: Path, phase: str) -> int:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"phase": phase, "ok": False, "checks": []}
    try:
        import app_runtime
        import voice_generation as vg
        import voice_queue as vq
        import qt_main
        import qt_voice
        import qt_story
        import qt_reader
        import qt_voice_packs
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QFontDatabase, QFont
        from PIL import Image, ImageDraw, ImageFont
        from ui_config import CUSTOMIZATION_SLOTS
        app = QApplication.instance() or QApplication([])
        font_path = Path("C:/Windows/Fonts/msyh.ttc")
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
            app.setFont(QFont("Microsoft YaHei", 10))
        app.setQuitOnLastWindowClosed(False)
        root = output / "portable-fixture"
        root.mkdir(exist_ok=True)
        with contextlib.ExitStack() as scope:
            for module in (app_runtime, vg, vq, qt_main, qt_voice, qt_story, qt_reader, qt_voice_packs):
                scope.enter_context(patch.object(module, "project_dir", lambda: root))
            window = qt_main.ReaderApp()
            for key in window.modules:
                window.show_module(key)
            report["checks"].append("all five Qt panels constructed")
            store = vq.VoiceQueueStore()
            backend = vg.TTSBackendManager()
            if phase == "prepare":
                story = root / "data/custom_texts/restart-test.txt"
                story.parent.mkdir(parents=True, exist_ok=True)
                story.write_text("This is sentence one.\nThis is sentence two.\nThis is sentence three.", encoding="utf-8")
                config = {"engine": vg.ENGINE_SYSTEM, "voice": "default", "speed": 1.0, "qwen_options": {}, "optimize_quality": False}
                item = {"path": story, "status": "生成中", "generation_config": config}
                stop = threading.Event()
                def checkpoint(path, payload):
                    item["manifest"] = path
                    item.update(vq.manifest_progress(path, payload))
                    store.save([item], config)
                def progress(done, total, text):
                    if done == 1 and text.startswith("已完成"):
                        stop.set()
                try:
                    vg.generate_voice_pack(story, root / "data/voice_packs", vg.ENGINE_SYSTEM, "default", 1.0,
                        progress, stop, backend, state_changed=checkpoint, generation_config=config)
                except vg.GenerationInterrupted:
                    pass
                assert item["done"] == 1 and item["next_line"] == 2
                item["status"] = "已暂停"
                store.save([item], config)
                image = root / "decoration.png"
                art = Image.new("RGBA", (400, 240), "#16495c")
                draw = ImageDraw.Draw(art)
                draw.rounded_rectangle((20, 20, 380, 220), 24, fill="#e9b871")
                draw.text((80, 100), "READER 0.8", fill="#16495c", font=ImageFont.truetype("arial.ttf", 32))
                art.save(image)
                for slot in CUSTOMIZATION_SLOTS:
                    assert window.set_custom_image(slot, image)
                report["checks"].append("real Windows SAPI sentence committed; paused queue and images saved")
            else:
                items, config = store.load()
                assert len(items) == 1 and items[0]["done"] == 1 and items[0]["next_line"] == 2
                item = items[0]
                assert item["status"] == "已暂停"
                first = Path(item["manifest"]).parent / "0001.wav"
                initial_time = first.stat().st_mtime_ns
                def checkpoint(path, payload):
                    item["manifest"] = path
                    item.update(vq.manifest_progress(path, payload))
                    store.save([item], config)
                result = vg.generate_voice_pack(Path(item["path"]), root / "data/voice_packs", vg.ENGINE_SYSTEM, "default", 1.0,
                    lambda *args: None, threading.Event(), backend, state_changed=checkpoint,
                    generation_config=config, resume_manifest=item["manifest"])
                assert vq.manifest_progress(result)["done"] == 3
                assert first.stat().st_mtime_ns == initial_time
                for audio in result.parent.glob("*.wav"):
                    with wave.open(str(audio)) as stream:
                        assert stream.getnframes() > 0
                item["status"] = "已完成"
                store.save([item], config)
                assert not window.sidebar_image.pixmap().isNull()
                report["checks"].append("separate-process restart reused sentence one and synthesized sentences two/three")
                report["checks"].append("custom images restored after restart")
                from rapidocr_onnxruntime import RapidOCR
                import numpy as np
                sample = Image.new("RGB", (640, 130), "white")
                ImageDraw.Draw(sample).text((20, 35), "READER TEST 123", font=ImageFont.truetype("arial.ttf", 40), fill="black")
                result, _ = RapidOCR()(np.array(sample))
                recognized = " ".join(str(row[1]) for row in result or [])
                assert "TEST" in recognized.upper(), recognized
                report["checks"].append("bundled OCR recognized READER TEST 123")
            panel = window.panel_instances["voice_generation"]
            panel.queue_items, panel.queue_config = store.load()
            panel.queue_filter.setCurrentText("全部")
            panel._refresh_queue_view()
            window.show_module("voice_generation")
            window.show()
            app.processEvents()
            panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum())
            app.processEvents()
            assert window.grab().save(str(output / f"{phase}-queue.png"))
            window.show_module("settings")
            app.processEvents()
            assert window.grab().save(str(output / f"{phase}-settings.png"))
            window.close()
            deadline = time.monotonic() + 10
            while not window._close_ready and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert window._close_ready
            report["checks"].append("graceful shutdown completed")
            report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    (output / f"{phase}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
