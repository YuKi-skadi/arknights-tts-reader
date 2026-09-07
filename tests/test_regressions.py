"""Offline regression suite: real checkpoints/Qt, deterministic TTS fixtures."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import app_runtime
import voice_generation as vg
import voice_queue as vq
import qt_main
import qt_voice
import qt_story
import qt_reader
import qt_voice_packs
import quality_analysis
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PIL import Image


def audio_fixture() -> bytes:
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    return data.getvalue()


class Backend:
    def __init__(self, action=None):
        self.calls = []
        self.action = action
    def synthesize(self, engine, text, voice, speed, options):
        self.calls.append(text)
        if self.action:
            self.action(text)
        return audio_fixture(), "wav"
    def release_qwen(self):
        pass


@contextlib.contextmanager
def portable_root(root):
    with contextlib.ExitStack() as stack:
        for module in (app_runtime, vg, vq, qt_main, qt_voice, qt_story, qt_reader, qt_voice_packs):
            stack.enter_context(patch.object(module, "project_dir", lambda: root))
        yield


def fixture(root):
    story = root / "data" / "custom_texts" / "测试.txt"
    story.parent.mkdir(parents=True, exist_ok=True)
    story.write_text("第一句测试。\n第二句测试。\n第三句测试。", encoding="utf-8")
    config = {"engine": vg.ENGINE_SYSTEM, "voice": "default", "speed": 1.0,
              "qwen_options": {}, "optimize_quality": False}
    return story, config


def crash_worker(root):
    with portable_root(root):
        story, config = fixture(root)
        item = {"path": story, "status": "生成中", "generation_config": config}
        store = vq.VoiceQueueStore()
        def checkpoint(path, payload):
            item["manifest"] = path
            item.update(vq.manifest_progress(path, payload))
            store.save([item], config)
        def progress(done, total, text):
            if done == 1 and text.startswith("已完成"):
                os._exit(23)
        vg.generate_voice_pack(story, root / "data/voice_packs", vg.ENGINE_SYSTEM,
            "default", 1.0, progress, threading.Event(), Backend(), state_changed=checkpoint)


class Regressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reader-test-")
        self.root = Path(self.temp.name)
        self.scope = contextlib.ExitStack()
        self.scope.enter_context(portable_root(self.root))
        for method in ("warning", "critical", "information"):
            self.scope.enter_context(patch.object(QMessageBox, method, return_value=QMessageBox.StandardButton.Ok))
        self.story, self.config = fixture(self.root)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.wait(lambda: all(w._close_ready for w in self.windows))
        self.app.processEvents()
        self.scope.close()
        self.temp.cleanup()

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "operation did not complete")

    def window(self):
        window = qt_main.ReaderApp()
        self.windows.append(window)
        return window

    def generate(self, backend=None, cancelled=None, progress=None, **kwargs):
        return vg.generate_voice_pack(self.story, self.root / "data/voice_packs",
            vg.ENGINE_SYSTEM, "default", kwargs.pop("speed", 1.0),
            progress or (lambda *args: None), cancelled or threading.Event(),
            backend or Backend(), **kwargs)

    def test_interrupted_resume_only_missing_sentences(self):
        stop = threading.Event()
        backend = Backend(lambda text: stop.set())
        with self.assertRaises(vg.GenerationInterrupted) as caught:
            self.generate(backend, stop)
        path = caught.exception.manifest_path
        progress = vq.manifest_progress(path)
        self.assertEqual((progress["done"], progress["next_line"]), (1, 2))
        original = (path.parent / "0001.wav").read_bytes()
        resumed = Backend()
        result = self.generate(resumed, resume_manifest=path)
        self.assertEqual(result, path)
        self.assertEqual(len(resumed.calls), 2)
        self.assertEqual((path.parent / "0001.wav").read_bytes(), original)
        self.assertEqual(vq.manifest_progress(path)["done"], 3)

    def test_process_crash_and_restart(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash-worker", str(self.root)],
            capture_output=True, timeout=20, env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))
        items, config = vq.VoiceQueueStore().load()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["status"], items[0]["done"], items[0]["next_line"]), ("中断", 1, 2))
        backend = Backend()
        self.generate(backend, resume_manifest=items[0]["manifest"])
        self.assertEqual(len(backend.calls), 2)

    def test_failed_sentence_retry_and_empty_audio(self):
        def fail_second(text):
            if "第二句" in text:
                raise RuntimeError("fixture synthesis failure")
        with self.assertRaises(vg.GenerationPartiallyFailed) as caught:
            self.generate(Backend(fail_second))
        path = caught.exception.manifest_path
        self.assertEqual(vq.manifest_progress(path)["done"], 2)
        self.assertEqual(vq.manifest_progress(path)["failed"], 1)
        backend = Backend()
        self.generate(backend)
        self.assertEqual(backend.calls, ["第二句测试。"])
        with patch.object(Backend, "synthesize", return_value=(b"", "wav")):
            with self.assertRaises(vg.GenerationPartiallyFailed):
                self.generate(speed=1.2)

    def test_changed_speed_and_text_do_not_reuse_old_audio(self):
        first = self.generate()
        backend = Backend()
        second = self.generate(backend, speed=1.2)
        self.assertNotEqual(first, second)
        self.assertEqual(len(backend.calls), 3)
        self.story.write_text("修改后的新内容。", encoding="utf-8")
        backend = Backend()
        third = self.generate(backend)
        self.assertNotEqual(first, third)
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(first.is_file())

    def test_legacy_manifest_and_uncommitted_audio(self):
        path = self.generate()
        payload = app_runtime.read_json_object(path)
        payload.pop("generation_fingerprint")
        app_runtime.atomic_json_save(path, payload)
        backend = Backend()
        self.generate(backend, resume_manifest=path)
        self.assertEqual(backend.calls, [])
        payload = app_runtime.read_json_object(path)
        payload["lines"][1]["status"] = "generating"
        payload["lines"][1]["audio"] = ""
        app_runtime.atomic_json_save(path, payload)
        backend = Backend()
        self.generate(backend, resume_manifest=path)
        self.assertEqual(backend.calls, [])
        (path.parent / "0002.wav").unlink()
        (path.parent / "0002.wav.tmp").write_bytes(b"incomplete")
        backend = Backend()
        self.generate(backend, resume_manifest=path)
        self.assertEqual(backend.calls, ["第二句测试。"])

    def test_queue_backup_and_portable_reference(self):
        reference = self.root / "data/reference_audio/ref.wav"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(audio_fixture())
        config = {**self.config, "qwen_options": {"reference_audio": str(reference)}}
        item = {"path": self.story, "status": "已暂停", "generation_config": config}
        store = vq.VoiceQueueStore()
        store.save([item], config)
        store.save([item], config)
        payload = app_runtime.read_json_object(store.path)
        self.assertEqual(payload["items"][0]["path"], "data/custom_texts/测试.txt")
        self.assertEqual(payload["config"]["qwen_options"]["reference_audio"], "data/reference_audio/ref.wav")
        store.path.write_text("{broken", encoding="utf-8")
        items, _ = store.load()
        self.assertEqual(items[0]["status"], "已暂停")
        self.assertTrue(store.load_warning)
        self.assertEqual(items[0]["generation_config"]["qwen_options"]["reference_audio"], str(reference))

    def test_missing_audio_reconciles_completed_queue(self):
        path = self.generate()
        store = vq.VoiceQueueStore()
        store.save([{"path": self.story, "manifest": path, "status": "已完成", "generation_config": self.config}])
        (path.parent / "0002.wav").unlink()
        items, _ = store.load()
        self.assertEqual((items[0]["status"], items[0]["done"], items[0]["next_line"]), ("部分完成", 2, 2))

    def test_history_recovery_is_idempotent(self):
        self.generate()
        items = []
        store = vq.VoiceQueueStore()
        self.assertEqual(store.recover_manifests(items), 1)
        self.assertEqual(store.recover_manifests(items), 0)
        self.assertEqual(items[0]["done"], 3)

    def test_image_settings_apply_clear_and_restart(self):
        window = self.window()
        image = self.root / "decor.png"
        Image.new("RGBA", (300, 150), "#126a95").save(image)
        from ui_config import CUSTOMIZATION_SLOTS
        for slot in CUSTOMIZATION_SLOTS:
            self.assertTrue(window.set_custom_image(slot, image))
        window.show_module("voice_generation")
        window.show()
        self.app.processEvents()
        panel = window.panel_instances["voice_generation"]
        self.assertFalse(window.sidebar_image.pixmap().isNull())
        self.assertFalse(panel.header_image.pixmap().isNull())
        window.close()
        self.wait(lambda: window._close_ready)
        reopened = self.window()
        reopened.show_module("voice_generation")
        self.assertFalse(reopened.sidebar_image.pixmap().isNull())
        self.assertFalse(reopened.panel_instances["voice_generation"].header_image.pixmap().isNull())
        reopened.clear_custom_image("sidebar")
        self.assertTrue(reopened.sidebar_image.isHidden())
        self.assertFalse(reopened.custom_image_path("sidebar"))

    def test_pause_close_restart_queue_preserves_counts(self):
        window = self.window()
        window.show_module("voice_generation")
        panel = window.panel_instances["voice_generation"]
        entered, release = threading.Event(), threading.Event()
        def block(text):
            entered.set()
            release.wait(5)
        panel.backend = Backend(block)
        panel.queue_items = [{"path": self.story, "status": "待处理", "generation_config": dict(self.config)}]
        panel.start_queue()
        self.wait(entered.is_set)
        panel.toggle_pause()
        items, _ = vq.VoiceQueueStore().load()
        self.assertEqual(items[0]["status"], "已暂停")
        release.set()
        self.wait(lambda: panel.queue_items[0].get("done") == 1)
        window.close()
        self.wait(lambda: window._close_ready)
        reopened = self.window()
        reopened.show_module("voice_generation")
        resumed = reopened.panel_instances["voice_generation"]
        self.assertEqual(len(resumed.queue_items), 1)
        self.assertEqual(resumed.queue_items[0]["done"], 1)
        self.assertIn("1/3", resumed.queue_list.item(0).text())
        resumed.backend = Backend()
        resumed.start_queue()
        self.wait(lambda: resumed.queue_thread is None)
        self.assertEqual(len(resumed.backend.calls), 2)
        self.assertEqual(resumed.queue_items[0]["status"], "已完成")

    def test_stop_queue_does_not_start_next_task(self):
        window = self.window()
        window.show_module("voice_generation")
        panel = window.panel_instances["voice_generation"]
        second = self.story.with_name("另一个.txt")
        second.write_text("不要启动。", encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        def block(text):
            entered.set()
            release.wait(5)
        panel.backend = Backend(block)
        panel.queue_items = [{"path": source, "status": "待处理", "generation_config": dict(self.config)} for source in (self.story, second)]
        panel.start_queue()
        self.wait(entered.is_set)
        panel.stop_current()
        release.set()
        self.wait(lambda: panel.queue_thread is None)
        self.assertEqual(panel.queue_items[1]["status"], "待处理")
        self.assertEqual(len(panel.backend.calls), 1)

    def test_region_overlay_can_be_reopened(self):
        window = self.window()
        window.select_region()
        first = window.region_overlay
        first.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertIsNone(window.region_overlay)
        window.select_region()
        self.assertIsNot(window.region_overlay, first)

    def test_sidebar_functional_controls_fit_at_minimum_window(self):
        window = self.window()
        image = self.root / "sidebar-decor.png"
        Image.new("RGBA", (300, 220), "#126a95").save(image)
        self.assertTrue(window.set_custom_image("sidebar", image))
        window.show()
        for width, height in ((900, 600), (1000, 620), (1180, 760)):
            window.resize(width, height)
            self.app.processEvents()
            side = window.findChild(QWidget, "sideBar")
            if side is None:
                side = window.sidebar_image.parentWidget().parentWidget()
            functional = [*window.nav_buttons.values(), window.start_button,
                          window.stop_button, window.speed_slider, window.listener_status]
            for widget in functional:
                label = widget.objectName() or getattr(widget, "text", lambda: "sidebar widget")()
                self.assertTrue(widget.isVisibleTo(window), f"hidden: {label}")
                top_left = widget.mapTo(side, QPoint(0, 0))
                bottom_right = widget.mapTo(side, QPoint(widget.width(), widget.height()))
                self.assertGreaterEqual(top_left.y(), 0)
                self.assertLessEqual(bottom_right.y(), side.height(), label)
            self.assertGreaterEqual(window.width(), 900)
            self.assertGreaterEqual(window.height(), 600)
            self.assertFalse(window.sidebar_image.pixmap().isNull())
            self.assertTrue(window.sidebar_image.isVisibleTo(window))
            self.assertGreater(window.sidebar_image.width(), 0)
            self.assertGreater(window.sidebar_image.height(), 0)
        self.assertTrue(window.sidebar_image.isVisibleTo(window))
        self.assertGreater(window.sidebar_image.height(), 0)
        image_top_left = window.sidebar_image.mapTo(side, QPoint(0, 0))
        image_bottom_right = window.sidebar_image.mapTo(side, QPoint(window.sidebar_image.width(), window.sidebar_image.height()))
        self.assertGreaterEqual(image_top_left.x(), 0)
        self.assertLessEqual(image_bottom_right.x(), side.width())
        self.assertTrue(window.sidebar_image.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_listener_callbacks_use_gui_thread_and_ignore_old_session(self):
        window = self.window()
        seen = []
        with patch.object(window, "_listener_event_ui", side_effect=lambda *a: seen.append(threading.get_ident())):
            thread = threading.Thread(target=lambda: window._listener_event("status", "test"))
            thread.start()
            thread.join()
            self.wait(lambda: bool(seen))
            self.assertEqual(seen, [threading.get_ident()])
            window.listener_event.emit(-1, "status", "obsolete", None)
            self.app.processEvents()
            self.assertEqual(len(seen), 1)

    def test_story_download_queue_persists(self):
        from story_catalog import StorySegment
        window = self.window()
        panel = window.panel_instances["story_download"]
        segment = StorySegment("test", "测试", "maintheme", "test-1", "1-1", "测试", "", "test_1", 1)
        panel.queue_items = [{"segment": segment, "status": "已暂停"}]
        panel.save_queue_state()
        window.close()
        self.wait(lambda: window._close_ready)
        reopened = self.window().panel_instances["story_download"]
        self.assertEqual(reopened.queue_items[0]["segment"], segment)
        self.assertEqual(reopened.queue_items[0]["status"], "待处理")

    def test_analysis_cancellation_keeps_completed_batches(self):
        self.story.write_text("\n".join(f"第 {i} 条测试" for i in range(30)), encoding="utf-8")
        stop = threading.Event()
        def request(key, lines, context, model):
            stop.set()
            return {line["line_id"]: "平静" for line in lines}
        with patch.object(quality_analysis, "_request_batch", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "中断"):
                quality_analysis.load_or_create_analysis(self.story, self.root / "analysis", "fixture-key", cancelled=stop)
        stop.clear()
        calls = []
        def resume(key, lines, context, model):
            calls.append(len(lines))
            return {line["line_id"]: "平静" for line in lines}
        with patch.object(quality_analysis, "_request_batch", side_effect=resume):
            result = quality_analysis.load_or_create_analysis(self.story, self.root / "analysis", "fixture-key", cancelled=stop)
        self.assertEqual(calls, [6])
        self.assertEqual(len(result), 30)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-worker":
        crash_worker(Path(sys.argv[2]))
    else:
        unittest.main(verbosity=2)
