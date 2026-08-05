"""Screen OCR and generated voice-pack matching for the Windows reader."""

from __future__ import annotations

import difflib
import json
import threading
import time
from pathlib import Path
from typing import Callable

from story_catalog import make_match_text
from voice_generation import prepare_external_runtime, project_dir


ListenerCallback = Callable[[str, str, dict | None], None]


def _ocr_text(value: str) -> str:
    return make_match_text(str(value or ""))


class VoicePackMatcher:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.entries: list[dict] = [
            entry for entry in (payload.get("lines") or [])
            if entry.get("status", "completed") == "completed"
            and entry.get("audio")
            and (manifest_path.parent / str(entry.get("audio"))).exists()
        ]
        self.keys = [_ocr_text(entry.get("match_text") or entry.get("text", "")) for entry in self.entries]

    def find(self, text: str, after_index: int | None = None, window: int = 5) -> tuple[int, dict, float] | None:
        key = _ocr_text(text)
        if len(key) < 3:
            return None
        if after_index is None:
            # The first match may start in the middle of a chapter, but short
            # OCR fragments are too ambiguous to search through the whole pack.
            scope = range(len(self.entries))
        else:
            left = max(0, after_index - 1)
            right = min(len(self.entries), after_index + window + 1)
            scope = list(range(after_index + 1, right)) + list(range(left, after_index + 1))
        exact_matches: list[tuple[int, dict, float]] = []
        for index in scope:
            candidate = self.keys[index]
            if candidate and candidate == key:
                exact_matches.append((index, self.entries[index], 1.0))
            elif len(key) >= 6 and candidate and (candidate in key or key in candidate):
                exact_matches.append((index, self.entries[index], 0.95))
        if exact_matches:
            # Prefer the longest matching line so a short line such as “你”
            # cannot steal a longer current line containing the same character.
            return max(exact_matches, key=lambda item: len(self.keys[item[0]]))
        if after_index is None and len(key) < 8:
            return None
        best: tuple[float, int] | None = None
        for index in scope:
            candidate = self.keys[index]
            if len(candidate) < 6:
                continue
            score = difflib.SequenceMatcher(None, key, candidate).ratio()
            if best is None or score > best[0]:
                best = (score, index)
        threshold = 0.80 if after_index is None else 0.72
        if best is not None and best[0] >= threshold:
            index = best[1]
            return index, self.entries[index], best[0]
        return None


class AudioPlayer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sound = None

    def play(self, path: Path) -> None:
        prepare_external_runtime()
        from playsound3 import playsound

        with self._lock:
            self.stop()
            self._sound = playsound(path, block=False)

    def stop(self) -> None:
        sound = self._sound
        self._sound = None
        if sound is not None:
            try:
                sound.stop()
            except Exception:
                pass


class ListenerEngine:
    """Capture a fixed screen ROI, OCR it, and play matching pack entries."""

    def __init__(
        self,
        region: tuple[int, int, int, int],
        manifest_path: Path,
        callback: ListenerCallback,
        interval: float = 0.25,
    ) -> None:
        self.region = region
        self.manifest_path = manifest_path
        self.callback = callback
        self.interval = max(0.1, float(interval))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.player = AudioPlayer()
        self.debug_path = project_dir() / "data" / "debug" / "last_ocr_region.png"

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.player.stop()

    def _emit(self, kind: str, text: str = "", data: dict | None = None) -> None:
        try:
            self.callback(kind, text, data)
        except Exception:
            pass

    @staticmethod
    def recognize_with_score(image, ocr) -> tuple[str, float]:
        result, _elapsed = ocr(image)
        if not result:
            return "", 0.0
        valid = [item for item in result if len(item) >= 3 and float(item[2]) >= 0.30]
        if not valid:
            return "", 0.0
        pieces = [str(item[1]) for item in valid]
        confidence = sum(float(item[2]) for item in valid) / len(valid)
        return "".join(pieces).strip(), confidence

    @staticmethod
    def recognize(image, ocr) -> str:
        return ListenerEngine.recognize_with_score(image, ocr)[0]

    @staticmethod
    def _variants(image, image_ops):
        """Return variants for light text, dark text, gradients and outlines."""
        gray = image.convert("L")
        contrast = image_ops.autocontrast(gray)
        inverted = image_ops.invert(contrast)
        threshold = contrast.point(lambda value: 255 if value >= 150 else 0)
        return (
            image,
            contrast.convert("RGB"),
            inverted.convert("RGB"),
            threshold.convert("RGB"),
        )

    def _recognize_best(self, image, ocr, image_ops) -> tuple[str, float]:
        candidates: list[tuple[str, float]] = []
        for variant in self._variants(image, image_ops):
            text, score = self.recognize_with_score(variant, ocr)
            if text:
                candidates.append((text, score))
            # A strong result is usually already correct; avoid four OCR calls
            # on every polling cycle when the game has stable text.
            if score >= 0.86 and len(text) >= 3:
                break
        if not candidates:
            return "", 0.0
        return max(candidates, key=lambda item: (item[1], len(item[0])))

    def _run(self) -> None:
        try:
            prepare_external_runtime()
            from PIL import ImageGrab, ImageOps
            from rapidocr_onnxruntime import RapidOCR

            matcher = VoicePackMatcher(self.manifest_path)
            ocr = RapidOCR()
            self._emit("status", "OCR 引擎已启动")
            last_text = ""
            stable_since = 0.0
            last_index: int | None = None
            matched_text = ""
            emitted_text = ""
            last_debug_save = 0.0
            self.debug_path.parent.mkdir(parents=True, exist_ok=True)
            self._emit("status", f"OCR 调试截图：{self.debug_path}")
            while not self.stop_event.is_set():
                x, y, width, height = self.region
                image = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
                now = time.monotonic()
                if now - last_debug_save >= 2.0:
                    try:
                        image.save(self.debug_path)
                    except OSError:
                        pass
                    last_debug_save = now
                text, _score = self._recognize_best(image.convert("RGB"), ocr, ImageOps)
                if text != emitted_text:
                    self._emit("ocr", text)
                    emitted_text = text
                now = time.monotonic()
                if text and text == last_text:
                    if stable_since == 0.0:
                        stable_since = now
                elif text:
                    last_text = text
                    stable_since = now
                    matched_text = ""
                else:
                    stable_since = 0.0
                    matched_text = ""
                if text and text != matched_text and stable_since and now - stable_since >= 0.45:
                    match = matcher.find(text, last_index)
                    matched_text = text
                    if match is not None:
                        index, entry, score = match
                        if index != last_index:
                            audio = self.manifest_path.parent / str(entry.get("audio", ""))
                            if audio.exists():
                                self.player.play(audio)
                                last_index = index
                                self._emit("match", str(entry.get("text", "")), {"score": score, "audio": str(audio), "index": index})
                            else:
                                self._emit("error", f"找不到语音文件：{audio}")
                    else:
                        self._emit("unmatched", "未匹配到当前语音包")
                time.sleep(self.interval)
        except Exception as exc:
            self._emit("error", str(exc))
        finally:
            self.player.stop()
            self._emit("stopped", "OCR 监听已停止")
