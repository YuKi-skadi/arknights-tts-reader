"""Pre-generate voice packs for exported Arknights story documents.

Edge-TTS stays in the GUI process as an optional dependency. Qwen3-TTS is
launched from the preserved portable ROCm runtime as a separate process, so
the GUI never imports torch and can return VRAM by stopping that process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import socket
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from app_runtime import atomic_json_save, read_json_object
from voice_queue import portable_path, restore_path


ENGINE_EDGE = "edge-tts"
# Keep the old identifier for queue files created before the model selector
# was split into 0.6B and 1.7B. It is normalized to the existing 1.7B model.
ENGINE_QWEN = "qwen3-tts"
ENGINE_QWEN_06B = "qwen3-tts-0.6b"
ENGINE_QWEN_17B = "qwen3-tts-1.7b"
ENGINE_SYSTEM = "windows-sapi"

QWEN_ENGINE_IDS = (ENGINE_QWEN_06B, ENGINE_QWEN_17B)

GPU_BACKEND_AUTO = "auto"
GPU_BACKEND_CUDA = "cuda"
GPU_BACKEND_ROCM = "rocm"
GPU_BACKEND_LABELS = {
    GPU_BACKEND_AUTO: "自动检测",
    GPU_BACKEND_CUDA: "NVIDIA CUDA",
    GPU_BACKEND_ROCM: "AMD ROCm",
}

QWEN_MODE_CUSTOM = "custom_voice"
QWEN_MODE_CLONE = "voice_clone"
QWEN_MODE_LABELS = {
    QWEN_MODE_CUSTOM: "预设音色（CustomVoice）",
    QWEN_MODE_CLONE: "声音克隆（Base）",
}
QWEN_CLONE_VOICE = "自定义克隆"

ENGINE_LABELS = {
    ENGINE_EDGE: "Edge-TTS（在线）",
    ENGINE_QWEN: "Qwen3-TTS 1.7B（旧任务）",
    ENGINE_QWEN_06B: "Qwen3-TTS 0.6B",
    ENGINE_QWEN_17B: "Qwen3-TTS 1.7B",
    ENGINE_SYSTEM: "Windows 系统语音",
}

EDGE_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunyangNeural",
)
# Edge-TTS occasionally returns an empty stream when the online service or
# connection is temporarily unavailable. Keep retries local to one segment so
# completed audio is preserved and the queue does not need to restart.
EDGE_RETRY_DELAYS = (1.0, 2.0, 4.0)
EDGE_REQUEST_GAP = 0.2
QWEN_VOICES = ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ono_Anna", "Sohee", "Ryan", "Aiden")


def is_qwen_engine(engine: str) -> bool:
    return engine in QWEN_ENGINE_IDS or engine == ENGINE_QWEN


def normalize_qwen_engine(engine: str) -> str:
    """Migrate the pre-model-selector engine id without changing its behavior."""
    return ENGINE_QWEN_17B if engine == ENGINE_QWEN else engine


def qwen_model_size(engine: str) -> str:
    engine = normalize_qwen_engine(engine)
    if engine == ENGINE_QWEN_06B:
        return "0.6B"
    if engine == ENGINE_QWEN_17B:
        return "1.7B"
    return ""


def detect_gpu_backend() -> str | None:
    """Detect a vendor without importing torch into the GUI process."""
    if os.name == "nt" and shutil.which("nvidia-smi"):
        return GPU_BACKEND_CUDA
    if shutil.which("rocminfo") or os.environ.get("ROCM_PATH") or os.environ.get("HIP_PATH"):
        return GPU_BACKEND_ROCM
    return None


def resolve_gpu_backend(selected: str = GPU_BACKEND_AUTO) -> str:
    """Resolve the UI choice to a locked backend used by a queue task."""
    if selected in {GPU_BACKEND_CUDA, GPU_BACKEND_ROCM}:
        return selected
    detected = detect_gpu_backend()
    if detected:
        return detected
    # The existing portable runtime is ROCm. Keep old AMD installations
    # usable even when rocminfo is not on PATH.
    root = project_dir()
    if (root / "runtime" / "python" / "python.exe").exists():
        return GPU_BACKEND_ROCM
    return GPU_BACKEND_CUDA


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
    if getattr(sys, "frozen", False):
        # GUI/OCR dependencies are bundled. Importing packages from the GPU
        # Python here can mix two incompatible numpy/Qt/ONNX installations.
        return
    runtime_python = project_dir() / "runtime" / "python"
    standard_library = runtime_python / "Lib"
    site_packages = runtime_python / "Lib" / "site-packages"
    if standard_library.is_dir() and str(standard_library) not in sys.path:
        sys.path.insert(0, str(standard_library))
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


def runtime_environment(cache_root: Path, backend: str = GPU_BACKEND_ROCM) -> dict[str, str]:
    environment = {key.upper(): value for key, value in os.environ.items()}
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "kernels").mkdir(parents=True, exist_ok=True)
    environment["PYTHONUTF8"] = "1"
    if backend == GPU_BACKEND_CUDA:
        # Keep CUDA's kernel/cache files separate from the ROCm runtime.
        environment["CUDA_MODULE_LOADING"] = "LAZY"
        environment.pop("MIOPEN_USER_DB_PATH", None)
        environment.pop("MIOPEN_CUSTOM_CACHE_DIR", None)
    else:
        environment["MIOPEN_USER_DB_PATH"] = str(cache_root)
        environment["MIOPEN_CUSTOM_CACHE_DIR"] = str(cache_root / "kernels")
    return environment


def _safe_part(value: str, default: str = "item") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value)).strip(" .")
    return value[:100] or default


def _qwen_runtime_candidates(backend: str) -> list[Path]:
    root = project_dir()
    if backend == GPU_BACKEND_CUDA:
        return [root / "runtime_cuda", root / "runtime" / "cuda"]
    return [root / "runtime"]


def locate_qwen_runtime(
    mode: str = QWEN_MODE_CUSTOM,
    model_size: str = "1.7B",
    backend: str = GPU_BACKEND_AUTO,
) -> tuple[Path, Path, Path, Path] | None:
    """Return the optional local-model runtime beside the application."""
    backend = resolve_gpu_backend(backend)
    root = project_dir()
    for runtime in _qwen_runtime_candidates(backend):
        python = runtime / "python" / "python.exe"
        script = runtime / "tts_server.py"
        if not python.exists() or not script.exists():
            continue
        # CUDA and ROCm may use different Python runtimes, but the large
        # weights are shared from the existing runtime/models directory.
        model_root = runtime / "models"
        shared_model_root = root / "runtime" / "models"
        if not (model_root / "Qwen3-TTS-Tokenizer-12Hz").is_dir() and shared_model_root.is_dir():
            model_root = shared_model_root
        tokenizer = model_root / "Qwen3-TTS-Tokenizer-12Hz"
        prefix = f"Qwen3-TTS-12Hz-{model_size}-"
        model_names = {
            QWEN_MODE_CUSTOM: (f"{prefix}CustomVoice",),
            QWEN_MODE_CLONE: (f"{prefix}Base",),
        }.get(mode, ())
        for model_name in model_names:
            model = model_root / model_name
            if (model / "config.json").exists():
                return runtime, python, model, tokenizer
    return None


class QwenClient:
    """HTTP client that owns a separate Qwen/CUDA or Qwen/ROCm process."""

    def __init__(self, status: Callable[[str], None] | None = None, port: int = 47832) -> None:
        self.status = status or (lambda _text: None)
        self.port = port
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.active_mode = QWEN_MODE_CUSTOM
        self.active_model_size = ""
        self.active_backend = ""

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        mode: str = QWEN_MODE_CUSTOM,
        model_size: str = "1.7B",
        backend: str = GPU_BACKEND_AUTO,
        reference_audio: str = "",
        reference_text: str = "",
        instruct: str = "",
    ) -> bytes:
        self._ensure_server(mode, model_size, backend)
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
            # Only stop the subprocess owned by this client.
            request_sent = False
            if process is not None:
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

    def _ensure_server(self, mode: str, model_size: str, backend: str) -> None:
        backend = resolve_gpu_backend(backend)
        if self.process is not None and self.process.poll() is None:
            if (
                getattr(self, "active_mode", mode) == mode
                and getattr(self, "active_model_size", model_size) == model_size
                and getattr(self, "active_backend", backend) == backend
            ):
                return
            self.release()
        located = locate_qwen_runtime(mode, model_size, backend)
        if located is None:
            raise RuntimeError(
                f"找不到 {backend.upper()} 的 Qwen3-TTS {model_size} 运行时或对应模型，请检查 runtime_{'cuda' if backend == GPU_BACKEND_CUDA else ''} 和 models 目录。"
            )
        runtime, python, model, tokenizer = located
        # Each GUI owns its server. Do not connect to (or stop) another app's
        # service just because the old fixed port is already occupied.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        script = runtime / "tts_server.py"
        command = [
            str(python), str(script),
            "--port", str(self.port),
            "--model-dir", str(model),
            "--backend", backend,
        ]
        if tokenizer.is_dir():
            command.extend(["--tokenizer-dir", str(tokenizer)])
        log_dir = project_dir() / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"qwen-{backend}.log"
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            log_path.replace(log_path.with_suffix(".log.bak"))
        with log_path.open("ab") as log:
            self.process = subprocess.Popen(
                command, cwd=str(runtime), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=runtime_environment(project_dir() / "data" / "qwen_cache" / backend, backend),
            )
        self.active_mode = mode
        self.active_model_size = model_size
        self.active_backend = backend
        self.status(f"正在启动 Qwen3-TTS {model_size}（{GPU_BACKEND_LABELS.get(backend, backend)}）：{runtime}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.process = None
                raise RuntimeError(
                    f"Qwen3-TTS 服务启动失败，请检查运行时、驱动和模型。详细日志：{log_path}"
                )
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
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = str(body.get("error", "")) if isinstance(body, dict) else ""
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            raise RuntimeError(detail or f"Qwen 服务返回 HTTP {exc.code}") from exc


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
        if is_qwen_engine(engine):
            options = qwen_options or {}
            engine = normalize_qwen_engine(engine)
            return self.qwen.synthesize(
                text,
                voice,
                speed,
                str(options.get("mode", QWEN_MODE_CUSTOM)),
                qwen_model_size(engine),
                str(options.get("backend", GPU_BACKEND_AUTO)),
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
            last_error: Exception | None = None
            for attempt in range(len(EDGE_RETRY_DELAYS) + 1):
                if attempt:
                    await asyncio.sleep(EDGE_RETRY_DELAYS[attempt - 1])
                elif EDGE_REQUEST_GAP:
                    await asyncio.sleep(EDGE_REQUEST_GAP)
                try:
                    communicate = edge_tts.Communicate(text, voice, rate=speed_to_edge_rate(speed))
                    chunks: list[bytes] = []
                    async for item in communicate.stream():
                        if item.get("type") == "audio" and item.get("data"):
                            chunks.append(item["data"])
                    audio = b"".join(chunks)
                    if audio:
                        return audio
                    last_error = RuntimeError("No audio was received")
                except Exception as exc:
                    last_error = exc
            raise RuntimeError(
                f"Edge-TTS failed after {len(EDGE_RETRY_DELAYS) + 1} attempts: {last_error}"
            ) from last_error

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
    if is_qwen_engine(engine):
        return QWEN_VOICES
    return ("default",)


PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".log"}


def _read_plain_text_lines(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = path.read_text(encoding="gb18030")
    # Imported text is already prepared by the user. Preserve each non-empty
    # line as-is instead of applying story-specific normalization.
    return [line for line in content.splitlines() if line.strip()]


def _load_generation_source(path: Path) -> tuple[dict, list[dict]]:
    if path.suffix.lower() in PLAIN_TEXT_EXTENSIONS:
        raw_lines = _read_plain_text_lines(path)
        segment = {
            "event_id": "custom_text",
            "event_name": "自定义文本",
            "story_name": path.stem,
            "story_code": path.stem,
            "story_txt": path.stem,
        }
        lines = [
            {
                "line_id": f"custom_{index:04d}",
                "speaker": "",
                "text": line,
                "match_text": line,
                "should_speak": True,
            }
            for index, line in enumerate(raw_lines, start=1)
        ]
        return segment, lines

    payload = json.loads(path.read_text(encoding="utf-8"))
    segment = payload.get("segment") or {}
    source_lines = payload.get("lines") or []
    lines = [
        line for line in source_lines
        if line.get("should_speak", str(line.get("prop", "")).lower() != "name")
        and str(line.get("text", "")).strip()
    ]
    return segment, lines


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
    segment, lines = _load_generation_source(story_path)
    if not lines:
        raise RuntimeError("该剧情文件没有可生成的对白行")

    event_id = _safe_part(segment.get("event_id", story_path.parent.name))
    story_id = _safe_part(segment.get("story_txt", story_path.stem))
    qwen_options = qwen_options or {}
    line_instructions = line_instructions or {}
    qwen_mode = str(qwen_options.get("mode", QWEN_MODE_CUSTOM))
    if is_qwen_engine(engine):
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
                "tts_instruct": line_options.get("instruct", "") if is_qwen_engine(engine) else "",
            }
        )
        progress(index, total, f"已完成 {index}/{total}")

    manifest = {
        "format": "arknights-tts-voice-pack-v1",
        "engine": engine,
        "voice": voice,
        "speed": round(float(speed), 1),
        "qwen": {
            "model_size": qwen_model_size(engine),
            "backend": str(qwen_options.get("backend", GPU_BACKEND_AUTO)),
            "mode": qwen_mode,
            "reference_audio": str(qwen_options.get("reference_audio", "")),
            "reference_text": str(qwen_options.get("reference_text", "")),
            "instruct": str(qwen_options.get("instruct", "")),
        } if is_qwen_engine(engine) else None,
        "source_story": str(story_path),
        "segment": segment,
        "lines": entries,
    }
    manifest_path = pack_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _resume_fingerprint(lines: list[dict], engine: str, voice: str, speed: float,
                        options: dict, instructions: dict) -> str:
    relevant = dict(options) if is_qwen_engine(engine) else {}
    relevant.pop("backend", None)
    reference = relevant.pop("reference_audio", "")
    if reference and is_qwen_engine(engine):
        reference_path = restore_path(reference)
        if reference_path.is_file():
            with reference_path.open("rb") as stream:
                relevant["reference_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            relevant["reference_sha256"] = str(reference)
    payload = {"lines": [(line.get("line_id"), line["text"], instructions.get(str(line.get("line_id", ""))) or line.get("tts_instruct") or relevant.get("instruct", "")) for line in lines],
               "engine": normalize_qwen_engine(engine), "voice": voice,
               "speed": round(float(speed), 1), "options": relevant}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _compatible_legacy_manifest(payload: dict, lines: list[dict], engine: str,
                                voice: str, speed: float, options: dict, instructions: dict) -> bool:
    if not payload or normalize_qwen_engine(str(payload.get("engine"))) != normalize_qwen_engine(engine):
        return False
    if payload.get("voice") != voice or payload.get("speed") != round(float(speed), 1):
        return False
    old_lines = payload.get("lines") or []
    if len(old_lines) != len(lines) or any(old.get("text") != line["text"] for old, line in zip(old_lines, lines)):
        return False
    if is_qwen_engine(engine):
        old_options = payload.get("qwen") or {}
        for key in ("mode", "reference_text", "instruct"):
            default = QWEN_MODE_CUSTOM if key == "mode" else ""
            if str(old_options.get(key, default)) != str(options.get(key, default)):
                return False
        if options.get("mode") == QWEN_MODE_CLONE:
            # An old record has no reference-audio hash, so do not guess that
            # an overwritten reference still contains the same voice.
            return False
        for old, line in zip(old_lines, lines):
            expected = instructions.get(str(line.get("line_id", ""))) or line.get("tts_instruct") or options.get("instruct", "")
            if old.get("status", "completed") == "completed" and old.get("tts_instruct", "") != expected:
                return False
    return True


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
    state_changed: Callable[[Path, dict], None] | None = None,
    generation_config: dict | None = None,
    resume_manifest: Path | None = None,
) -> Path:
    """Generate a pack while persisting per-line status for resume and retry."""
    segment, lines = _load_generation_source(story_path)
    if not lines:
        raise RuntimeError("story has no speakable lines")

    qwen_options = qwen_options or {}
    line_instructions = line_instructions or {}
    qwen_mode = str(qwen_options.get("mode", QWEN_MODE_CUSTOM))
    event_id = _safe_part(segment.get("event_id", story_path.parent.name))
    story_id = _safe_part(segment.get("story_txt", story_path.stem))
    if is_qwen_engine(engine):
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(qwen_mode) / _safe_part(voice)
    else:
        pack_dir = output_root / event_id / story_id / _safe_part(engine) / _safe_part(voice)
    fingerprint = _resume_fingerprint(lines, engine, voice, speed, qwen_options, line_instructions)
    def read_manifest(path: Path) -> dict:
        try:
            return read_json_object(path)
        except (OSError, ValueError):
            return {}
    def compatible(payload: dict) -> bool:
        return payload.get("generation_fingerprint") == fingerprint or (
            not payload.get("generation_fingerprint") and
            _compatible_legacy_manifest(payload, lines, engine, voice, speed, qwen_options, line_instructions))
    previous = read_manifest(pack_dir / "manifest.json")
    if resume_manifest is not None:
        candidate = restore_path(resume_manifest).resolve()
        if candidate.is_relative_to(output_root.resolve()):
            resumed = read_manifest(candidate)
            if compatible(resumed):
                pack_dir, previous = candidate.parent, resumed
    if not compatible(previous):
        # A different text/speed/voice reference must never overwrite old audio.
        pack_dir = pack_dir / f"v_{fingerprint[:16]}"
        previous = read_manifest(pack_dir / "manifest.json")
        if not compatible(previous):
            previous = {}
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = pack_dir / "manifest.json"
    extension = "mp3" if engine == ENGINE_EDGE else "wav"
    total = len(lines)
    entries: list[dict] = []
    previous_lines = previous.get("lines") or []
    for index, line in enumerate(lines, start=1):
        expected = pack_dir / f"{index:04d}.{extension}"
        old = previous_lines[index - 1] if index <= len(previous_lines) else {}
        if old.get("audio"):
            expected = pack_dir / Path(str(old["audio"])).name
        complete = old.get("status", "completed") in {"completed", "generating"} and bool(old) and expected.is_file() and expected.stat().st_size > 0
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
                "tts_instruct": old.get("tts_instruct", "") if complete else "",
            }
        )

    manifest = {
        "format": "arknights-tts-voice-pack-v1",
        "generation_fingerprint": fingerprint,
        "generation_config": generation_config or {"engine": engine, "voice": voice, "speed": speed, "qwen_options": qwen_options, "optimize_quality": bool(line_instructions)},
        "status": "generating",
        "engine": engine,
        "voice": voice,
        "speed": round(float(speed), 1),
        "qwen": {
            "model_size": qwen_model_size(engine),
            "backend": str(qwen_options.get("backend", GPU_BACKEND_AUTO)),
            "mode": qwen_mode,
            "reference_audio": str(qwen_options.get("reference_audio", "")),
            "reference_text": str(qwen_options.get("reference_text", "")),
            "instruct": str(qwen_options.get("instruct", "")),
        } if is_qwen_engine(engine) else None,
        "source_story": portable_path(story_path),
        "segment": segment,
        "lines": entries,
    }

    def save_state() -> None:
        atomic_json_save(manifest_path, manifest)
        if state_changed is not None:
            state_changed(manifest_path, manifest)

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
            progress(index - 1, total, f"已暂停，下一句 {index}/{total}")
            time.sleep(0.2)
        if cancelled.is_set():
            mark_interrupted()
            raise GenerationInterrupted(manifest_path)

        entry = entries[index - 1]
        if entry.get("status") == "completed" and entry.get("audio"):
            progress(index, total, f"复用已完成音频：{index}/{total}")
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
            progress(index - 1, total, f"正在生成第 {index}/{total} 句：{text[:60]}")
            audio, actual_extension = backend.synthesize(engine, text, voice, speed, line_options)
            if actual_extension != extension:
                output = output.with_suffix(f".{actual_extension}")
            if not audio:
                raise RuntimeError("语音服务返回了空音频")
            temporary_audio = output.with_suffix(output.suffix + ".tmp")
            with temporary_audio.open("wb") as stream:
                stream.write(audio)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_audio.replace(output)
        except Exception as exc:
            if cancelled.is_set():
                mark_interrupted()
                raise GenerationInterrupted(manifest_path) from exc
            entry["status"] = "failed"
            entry["error"] = str(exc)[:1000]
            entry["tts_instruct"] = line_options.get("instruct", "") if is_qwen_engine(engine) else ""
            save_state()
            progress(index, total, f"第 {index}/{total} 句失败：{text[:24]}")
            continue
        entry["status"] = "completed"
        entry["audio"] = output.name
        entry["tts_instruct"] = line_options.get("instruct", "") if is_qwen_engine(engine) else ""
        save_state()
        progress(index, total, f"已完成第 {index}/{total} 句")

    failed_entries = [entry for entry in entries if entry.get("status") == "failed"]
    if failed_entries:
        manifest["status"] = "partial"
        save_state()
        raise GenerationPartiallyFailed(manifest_path, failed_entries)
    manifest["status"] = "completed"
    save_state()
    return manifest_path
