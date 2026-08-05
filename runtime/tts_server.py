from __future__ import annotations

import argparse
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class QwenRuntime:
    def __init__(self, model_dir: Path, tokenizer_dir: Path | None) -> None:
        self.model_dir = model_dir
        self.tokenizer_dir = tokenizer_dir
        self.model = None
        self._clone_prompt_key = None
        self._clone_prompt = None
        self._lock = threading.Lock()

    def load(self):
        if self.model is not None:
            return self.model
        with self._lock:
            if self.model is not None:
                return self.model
            import torch
            from qwen_tts import Qwen3TTSModel

            if torch.cuda.is_available():
                device_map = "cuda:0"
                dtype = torch.bfloat16
            else:
                device_map = "cpu"
                dtype = torch.float32

            # Qwen's public examples use the same from_pretrained API for local
            # directories. The model package contains the tokenizer reference;
            # tokenizer_dir is retained for future versions that expose it.
            self.model = Qwen3TTSModel.from_pretrained(
                str(self.model_dir),
                device_map=device_map,
                dtype=dtype,
                attn_implementation="sdpa",
            )
            return self.model

    def unload(self) -> None:
        with self._lock:
            self.model = None
            self._clone_prompt_key = None
            self._clone_prompt = None
            try:
                import gc
                import torch

                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:
                pass

    def synthesize(
        self,
        text: str,
        speaker: str,
        speed: float,
        mode: str,
        reference_audio: str,
        reference_text: str,
        instruct: str,
    ) -> bytes:
        model = self.load()
        with self._lock:
            if mode == "voice_clone":
                if not reference_audio:
                    raise RuntimeError("声音克隆模式需要选择参考音频")
                key = (reference_audio, reference_text)
                if key != self._clone_prompt_key:
                    prompt_args = {
                        "ref_audio": reference_audio,
                        "x_vector_only_mode": not bool(reference_text),
                    }
                    if reference_text:
                        prompt_args["ref_text"] = reference_text
                    self._clone_prompt = model.create_voice_clone_prompt(**prompt_args)
                    self._clone_prompt_key = key
                wavs, sample_rate = model.generate_voice_clone(
                    text=text,
                    language="Chinese",
                    voice_clone_prompt=self._clone_prompt,
                )
            else:
                prompt = str(instruct or "").strip()
                if speed != 1.0:
                    prompt = f"{prompt} 语速约为 {speed:.1f} 倍。".strip()
                if not prompt:
                    prompt = "请用自然、清晰、有轻微情绪起伏的中文朗读。"
                wavs, sample_rate = model.generate_custom_voice(
                    text=text,
                    language="Chinese",
                    speaker=speaker,
                    instruct=prompt,
                )
        return make_wav_bytes(wavs[0], sample_rate)


def make_wav_bytes(audio, sample_rate: int) -> bytes:
    import numpy as np

    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    samples = np.asarray(audio).reshape(-1)
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16).tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(int(sample_rate))
        output.writeframes(pcm)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    runtime: QwenRuntime
    server_ref: ThreadingHTTPServer

    def log_message(self, _format, *_args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"ok": True, "model_loaded": self.runtime.model is not None})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/synthesize":
                audio = self.runtime.synthesize(
                    str(body["text"]),
                    str(body.get("speaker", "Vivian")),
                    float(body.get("speed", 1.0)),
                    str(body.get("mode", "custom_voice")),
                    str(body.get("reference_audio", "")),
                    str(body.get("reference_text", "")),
                    str(body.get("instruct", "")),
                )
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                return
            if self.path == "/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self._shutdown, daemon=True).start()
                return
            self.send_error(404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def _shutdown(self) -> None:
        self.runtime.unload()
        self.server_ref.shutdown()

    def _json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=47831)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--tokenizer-dir", type=Path)
    args = parser.parse_args()

    runtime = QwenRuntime(args.model_dir, args.tokenizer_dir)

    class RuntimeHandler(Handler):
        pass

    RuntimeHandler.runtime = runtime
    server = ThreadingHTTPServer(("127.0.0.1", args.port), RuntimeHandler)
    RuntimeHandler.server_ref = server
    server.serve_forever()


if __name__ == "__main__":
    main()
