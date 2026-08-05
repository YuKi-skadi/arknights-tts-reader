"""Pre-generate voice packs for exported Arknights story documents.

Edge-TTS stays in the GUI process as an optional dependency. Qwen3-TTS is
launched from the preserved portable ROCm runtime as a separate process, so
the GUI never imports torch and can return VRAM by stopping that process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


ENGINE_EDGE = "edge-tts"
ENGINE_QWEN = "qwen3-tts"
ENGINE_SYSTEM = "windows-sapi"

QWEN_MODE_CUSTOM = "custom_voice"
QWEN_MODE_CLONE = "voice_clone"
QWEN_MODE_LABELS = {
    QWEN_MODE_CUSTOM: "预设音色（CustomVoice）",
    QWEN_MODE_CLONE: "声音克隆（Base）",
}
QWEN_CLONE_VOICE = "自定义克隆"

ENGINE_LABELS = {
    ENGINE_EDGE: "Edge-TTS（在线）",
    ENGINE_QWEN: "Qwen3-TTS（本地 ROCm）",
    ENGINE_SYSTEM: "Windows 系统语音",
}

EDGE_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunyangNeural",
)
QWEN_VOICES = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee", "Ryan", "Aiden")


class GenerationInterrupted(RuntimeError):
    def __init__(self, manifest_path: Path) -> None:
        super().__init__("voice generation interrupted")
        self.manifest_path = manifest_path


class GenerationPartiallyFailed(RuntimeError):
    def __init__(self, manifest_path: Path, failed_entries: list[dict]) -> None:
        super().__init__(f"{len(failed_entries)} voice segments failed")
        self.manifest_path = manifest_path
        self.failed_entries = failed_entries


def project_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def speed_to_edge_rate(speed: float) -> str:
    return f"{round((float(speed) - 1.0) * 100):+d}%"


def prepare_external_runtime() -> None:
    """Make packages beside a frozen EXE importable without bundling them."""
    runtime_python = project_dir() / "runtime" / "python"
    site_packages = runtime_python / "Lib" / "site-packages"
    if site_packages.is_dir() and str(site_packages) not in sys.path:
        sys.path.insert(0, str(site_packages))
    if os.name == "nt":
        dll_dir = runtime_python / "DLLs"
        pywin32_dll_dir = site_packages / "pywin32_system32"
        cv2_dir = site_packages / "cv2"
        onnxruntime_dll_dir = site_packages / "onnxruntime" / "capi"
        dll_dirs = [runtime_python, dll_dir, cv2_dir, onnxruntime_dll_dir, pywin32_dll_dir]
        dll_dirs = [path for path in dll_dirs if path.is_dir()]
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            handles = globals().setdefault("_EXTERNAL_DLL_HANDLES", [])
            registered = globals().setdefault("_EXTERNAL_DLL_PATHS", set())
            for path in dll_dirs:
                key = str(path).lower()
                if key not in registered:
                    handles.append(add_dll_directory(str(path)))
                    registered.add(key)
        prefix = [str(path) for path in dll_dirs]
        prefix_keys = {os.path.normcase(os.path.normpath(path)) for path in prefix}
        existing_path: list[str] = []
        for item in os.environ.get("PATH", "").split(os.pathsep):
            if not item:
                continue
            key = os.path.normcase(os.path.normpath(item))
            if key not in prefix_keys:
                existing_path.append(item)
        os.environ["PATH"] = os.pathsep.join(prefix + existing_path)


def runtime_environment(cache_root: Path) -> dict[str, str]:
    environment = {key.upper(): value for key, value in os.environ.items()}
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "kernels").mkdir(parents=True, exist_ok=True)
    environment["PYTHONUTF8"] = "1"
    environment["MIOPEN_USER_DB_PATH"] = str(cache_root)
    environment["MIOPEN_CUSTOM_CACHE_DIR"] = str(cache_root / "kernels")
    return environment


def _safe_part(value: str, default: str = "item") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value)).strip(" .")
    return value[:100] or default


def _qwen_runtime_candidates() -> list[Path]:
    root = project_dir()
    return [root / "runtime"]


def locate_qwen_runtime(mode: str = QWEN_MODE_CUSTOM) -> tuple[Path, Path, Path, Path] | None:
    """Return runtime root, Python, model and tokenizer from ver0.5."""
    for runtime in _qwen_runtime_candidates():
        python = runtime / "python" / "python.exe"
        script = runtime / "tts_server.py"
        tokenizer = runtime / "models" / "Qwen3-TTS-Tokenizer-12Hz"
        if not python.exists() or not script.exists():
            continue
        model_names = {
            QWEN_MODE_CUSTOM: ("Qwen3-TTS-12Hz-1.7B-CustomVoice",),
            QWEN_MODE_CLONE: ("Qwen3-TTS-12Hz-1.7B-Base",),
        }.get(mode, ())
        for model_name in model_names:
            model = runtime / "models" / model_name
            if (model / "config.json").exists():
                return runtime, python, model, tokenizer
    return None


class QwenClient:
    """HTTP client that owns a separate Qwen/ROCm server process."""

    def __init__(self, status: Callable[[str], None] | None = None, port: int = 47832) -> None:
        self.status = status or (lambda _text: None)
        self.port = port
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        mode: str = QWEN_MODE_CUSTOM,
        reference_audio: str = "",
        reference_text: str = "",
        instruct: str = "",
    ) -> bytes:
        self._ensure_server(mode)
        try:
            return self._request(
                "/synthesize",
                {
                    "text": text,
                    "speaker": voice,
                    "speed": round(float(speed), 1),
                    "mode": mode,
                    "reference_audio": reference_audio,
                    "reference_text": reference_text,
                    "instruct": instruct,
                },
                timeout=240,
            )
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            # Always call the endpoint, even when this GUI instance lost its
            # Popen handle after a module was recreated. This also releases a
            # Qwen server left behind by an older ver0.5 process.
            request_sent = False
            try:
                self._request("/shutdown", {}, timeout=3)
                request_sent = True
            except Exception:
                pass
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            if process is not None or request_sent:
                self.status("Qwen3-TTS 服务已停止，正在回收显存")
            else:
                self.status("未发现正在运行的 Qwen3-TTS 服务")

    close = release

    def _ensure_server(self, mode: str) -> None:
        if self.process is not None and self.process.poll() is None:
            if getattr(self, "active_mode", mode) == mode:
                return
            self.release()
        located = locate_qwen_runtime(mode)
        if located is None:
            raise RuntimeError(
                "找不到 ver0.5/runtime 中的 Qwen3-TTS 运行时，请确认软件目录完整。"
            )
        runtime, python, model, tokenizer = located
        script = runtime / "tts_server.py"
        command = [str(python), str(script), "--port", str(self.port), "--model-dir", str(model)]
        if tokenizer.is_dir():
            command.extend(["--tokenizer-dir", str(tokenizer)])
        self.process = subprocess.Popen(
            command,
            cwd=str(runtime),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=runtime_environment(project_dir() / "data" / "qwen_cache"),
        )
        self.active_mode = mode
        self.status(f"正在启动 Qwen3-TTS：{runtime}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.process = None
                raise RuntimeError("Qwen3-TTS 服务启动失败，请检查 ROCm 运行时和模型文件")
            try:
                self._request("/health", None, timeout=2)
                return
            except Exception:
                time.sleep(0.3)
        self.release()
        raise RuntimeError("Qwen3-TTS 服务启动超时")

    def _request(self, endpoint: str, payload: object | None, timeout: float) -> bytes:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{endpoint}",
            data=data,
            method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()


class TTSBackendManager:
    def __init__(self, status: Callable[[str], None] | None = None) -> None:
        self.status = status or (lambda _text: None)
        self.qwen = QwenClient(self.status)

    def synthesize(
        self,
        engine: str,
        text: str,
        voice: str,
        speed: float,
        qwen_options: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        if engine == ENGINE_EDGE:
            return self._edge(text, voice, speed), "mp3"
        if engine == ENGINE_QWEN:
            options = qwen_options or {}
            return self.qwen.synthesize(
                text,
                voice,
                speed,
                str(options.get("mode", QWEN_MODE_CUSTOM)),
                str(options.get("reference_audio", "")),
                str(options.get("reference_text", "")),
                str(options.get("instruct", "")),
            ), "wav"
        if engine == ENGINE_SYSTEM:
            return self._system(text, voice, speed), "wav"
        raise RuntimeError(f"未知语音引擎：{engine}")

    def release_qwen(self) -> None:
        self.qwen.release()

    close = release_qwen

    @staticmethod
    def _edge(text: str, voice: str, speed: float) -> bytes:
        prepare_external_runtime()
        import edge_tts

        async def save() -> bytes:
            communicate = edge_tts.Communicate(text, voice, rate=speed_to_edge_rate(speed))
            chunks: list[bytes] = []
            async for item in communicate.stream():
                if item["type"] == "audio":
                    chunks.append(item["data"])
            return b"".join(chunks)

        return asyncio.run(save())

    @staticmethod
    def _system(text: str, voice: str, speed: float) -> bytes:
        if os.name != "nt":
            raise RuntimeError("Windows 系统语音只能在 Windows 上使用")
        import tempfile
        import wave

        with tempfile.TemporaryDirectory(prefix="arknights-tts-") as folder:
            output = Path(folder) / "output.wav"
            payload = base64.b64encode(
                json.dumps({"text": text, "voice": voice, "rate": max(-10, min(10, round((speed - 1) * 10))), "output": str(output)}, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            script = r'''
Add-Type -AssemblyName System.Speech
$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:ARKNIGHTS_SAPI_PAYLOAD)) | ConvertFrom-Json
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
  if ($data.voice -and $data.voice -ne "default") { try { $synth.SelectVoice([string]$data.voice) } catch {} }
  $synth.Rate = [int]$data.rate
  $synth.SetOutputToWaveFile([string]$data.output)
  $synth.Speak([string]$data.text)
} finally { $synth.Dispose() }
'''
            environment = {key.upper(): value for key, value in os.environ.items()}
            environment["ARKNIGHTS_SAPI_PAYLOAD"] = payload
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                env=environment,
                capture_output=True,
                check=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = output.read_bytes()
            if not data:
                raise RuntimeError("Windows 系统语音没有生成音频")
            return data


def voice_options(engine: str) -> tuple[str, ...]:
    if engine == ENGINE_EDGE:
        return EDGE_VOICES
    if engine == ENGINE_QWEN:
        return QWEN_VOICES
    return ("default",)


def _legacy_generate_voice_pack(
    story_path: Path,
    output_root: Path,
    engine: str,
    voice: str,
    speed: float,
    progress: Callable[[int, int, str], None],
    cancelled: threading.Event,
    backend: TTSBackendManager,
    paused: threading.Event | None = None,
    qwen_options: dict[str, str] | None = None,
    line_instructions: dict[str, str] | None = None,
) -> Path:
    payload = json.loads(story_path.read_text(encoding="utf-8"))
    segment = payload.get("segment") or {}
    source_lines = payload.get("lines") or []
    lines = [
        line for line in source_lines
        if line.get("should_speak", str(line.get("prop", "")).lower() != "name")
        and str(line.get("text", "")).strip()
    ]
    if not lines:
        raise RuntimeError("该剧情文件没有可生成的对白行")

    event_id = _safe_part(segment.get("event_id", story_path.parent.name))
    story_id = _safe_part(segment.get("story_txt", story_path.stem))
    qwen_options = qwen_options or {}
    line_instructions = line_instructions or {}
    qwen_mode = str(qwen_options.get("mode", QWEN_MODE_CUSTOM))
    if engine == ENGINE_QWEN:
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(qwen_mode) / _safe_part(voice)
    else:
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(voice)
    pack_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    extension = "mp3" if engine == ENGINE_EDGE else "wav"
    total = len(lines)
    for index, line in enumerate(lines, start=1):
        while paused is not None and paused.is_set():
            if cancelled.is_set():
                raise RuntimeError("语音生成已停止")
            progress(index - 1, total, f"已暂停，等待继续：{index}/{total}")
            time.sleep(0.2)
        if cancelled.is_set():
            raise RuntimeError("语音生成已停止")
        output = pack_dir / f"{index:04d}.{extension}"
        text = str(line["text"])
        line_options = dict(qwen_options)
        line_id = str(line.get("line_id", f"line{index}"))
        line_options["instruct"] = str(
            line_instructions.get(line_id)
            or line.get("tts_instruct")
            or line_options.get("instruct", "")
        )
        if not output.exists() or output.stat().st_size == 0:
            progress(index - 1, total, f"生成 {index}/{total}：{text[:24]}")
            audio, actual_extension = backend.synthesize(engine, text, voice, speed, line_options)
            if actual_extension != extension:
                output = output.with_suffix(f".{actual_extension}")
            output.write_bytes(audio)
        entries.append(
            {
                "index": index,
                "line_id": line_id,
                "speaker": line.get("speaker", ""),
                "text": text,
                "match_text": line.get("match_text", text),
                "audio": output.name,
                "tts_instruct": line_options.get("instruct", "") if engine == ENGINE_QWEN else "",
            }
        )
        progress(index, total, f"已完成 {index}/{total}")

    manifest = {
        "format": "arknights-tts-voice-pack-v1",
        "engine": engine,
        "voice": voice,
        "speed": round(float(speed), 1),
        "qwen": {
            "mode": qwen_mode,
            "reference_audio": str(qwen_options.get("reference_audio", "")),
            "reference_text": str(qwen_options.get("reference_text", "")),
            "instruct": str(qwen_options.get("instruct", "")),
        } if engine == ENGINE_QWEN else None,
        "source_story": str(story_path),
        "segment": segment,
        "lines": entries,
    }
    manifest_path = pack_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def generate_voice_pack(
    story_path: Path,
    output_root: Path,
    engine: str,
    voice: str,
    speed: float,
    progress: Callable[[int, int, str], None],
    cancelled: threading.Event,
    backend: TTSBackendManager,
    paused: threading.Event | None = None,
    qwen_options: dict[str, str] | None = None,
    line_instructions: dict[str, str] | None = None,
) -> Path:
    """Generate a pack while persisting per-line status for resume and retry."""
    payload = json.loads(story_path.read_text(encoding="utf-8"))
    segment = payload.get("segment") or {}
    source_lines = payload.get("lines") or []
    lines = [
        line for line in source_lines
        if line.get("should_speak", str(line.get("prop", "")).lower() != "name")
        and str(line.get("text", "")).strip()
    ]
    if not lines:
        raise RuntimeError("story has no speakable lines")

    qwen_options = qwen_options or {}
    line_instructions = line_instructions or {}
    qwen_mode = str(qwen_options.get("mode", QWEN_MODE_CUSTOM))
    event_id = _safe_part(segment.get("event_id", story_path.parent.name))
    story_id = _safe_part(segment.get("story_txt", story_path.stem))
    if engine == ENGINE_QWEN:
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(qwen_mode) / _safe_part(voice)
    else:
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(voice)
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = pack_dir / "manifest.json"
    extension = "mp3" if engine == ENGINE_EDGE else "wav"
    total = len(lines)
    entries: list[dict] = []
    for index, line in enumerate(lines, start=1):
        expected = pack_dir / f"{index:04d}.{extension}"
        if not expected.exists() or expected.stat().st_size == 0:
            expected = next(
                (
                    candidate for candidate in sorted(pack_dir.glob(f"{index:04d}.*"))
                    if candidate.is_file() and candidate.stat().st_size > 0
                ),
                expected,
            )
        complete = expected.exists() and expected.is_file() and expected.stat().st_size > 0
        text = str(line["text"])
        entries.append(
            {
                "index": index,
                "line_id": str(line.get("line_id", f"line{index}")),
                "speaker": line.get("speaker", ""),
                "text": text,
                "match_text": line.get("match_text", text),
                "audio": expected.name if complete else "",
                "status": "completed" if complete else "pending",
                "error": "",
                "tts_instruct": "",
            }
        )

    manifest = {
        "format": "arknights-tts-voice-pack-v1",
        "status": "generating",
        "engine": engine,
        "voice": voice,
        "speed": round(float(speed), 1),
        "qwen": {
            "mode": qwen_mode,
            "reference_audio": str(qwen_options.get("reference_audio", "")),
            "reference_text": str(qwen_options.get("reference_text", "")),
            "instruct": str(qwen_options.get("instruct", "")),
        } if engine == ENGINE_QWEN else None,
        "source_story": str(story_path),
        "segment": segment,
        "lines": entries,
    }

    def save_state() -> None:
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)

    def mark_interrupted() -> None:
        for entry in entries:
            if entry.get("status") in {"pending", "generating"}:
                entry["status"] = "interrupted"
                entry["error"] = "stopped by user"
        manifest["status"] = "interrupted"
        save_state()

    save_state()
    for index, line in enumerate(lines, start=1):
        while paused is not None and paused.is_set():
            if cancelled.is_set():
                mark_interrupted()
                raise GenerationInterrupted(manifest_path)
            progress(index - 1, total, f"paused, waiting to continue: {index}/{total}")
            time.sleep(0.2)
        if cancelled.is_set():
            mark_interrupted()
            raise GenerationInterrupted(manifest_path)

        entry = entries[index - 1]
        if entry.get("status") == "completed" and entry.get("audio"):
            progress(index, total, f"already exists: {index}/{total}")
            continue
        entry["status"] = "generating"
        entry["error"] = ""
        save_state()
        text = str(line["text"])
        line_id = str(line.get("line_id", f"line{index}"))
        line_options = dict(qwen_options)
        line_options["instruct"] = str(
            line_instructions.get(line_id)
            or line.get("tts_instruct")
            or line_options.get("instruct", "")
        )
        output = pack_dir / f"{index:04d}.{extension}"
        try:
            progress(index - 1, total, f"generating {index}/{total}: {text[:24]}")
            audio, actual_extension = backend.synthesize(engine, text, voice, speed, line_options)
            if actual_extension != extension:
                output = output.with_suffix(f".{actual_extension}")
            output.write_bytes(audio)
        except Exception as exc:
            if cancelled.is_set():
                mark_interrupted()
                raise GenerationInterrupted(manifest_path) from exc
            entry["status"] = "failed"
            entry["error"] = str(exc)[:1000]
            entry["tts_instruct"] = line_options.get("instruct", "") if engine == ENGINE_QWEN else ""
            save_state()
            progress(index, total, f"failed {index}/{total}: {text[:24]}")
            continue
        entry["status"] = "completed"
        entry["audio"] = output.name
        entry["tts_instruct"] = line_options.get("instruct", "") if engine == ENGINE_QWEN else ""
        save_state()
        progress(index, total, f"completed {index}/{total}")

    failed_entries = [entry for entry in entries if entry.get("status") == "failed"]
    if failed_entries:
        manifest["status"] = "partial"
        save_state()
        raise GenerationPartiallyFailed(manifest_path, failed_entries)
    manifest["status"] = "completed"
    save_state()
    return manifest_path
