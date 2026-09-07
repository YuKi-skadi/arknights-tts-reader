"""Optional DeepSeek-assisted per-line TTS instruction generation.

The analysis is deliberately kept outside the GUI and cached by story
content.  This makes quality optimization an offline pre-generation step:
the API is called at most once for an unchanged story, while the generated
instructions remain portable with the project.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from app_runtime import atomic_json_save


DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODELS_ENDPOINT = "https://api.deepseek.com/models"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
ANALYSIS_VERSION = 2
PLAIN_TEXT_EXTENSIONS = {".txt", ".md", ".log"}


def fetch_deepseek_models(api_key: str) -> tuple[str, ...]:
    """Return the models currently available to this DeepSeek API key."""
    key = api_key.strip()
    if not key:
        raise RuntimeError("请先填写 DeepSeek API Key")
    request = urllib.request.Request(
        DEEPSEEK_MODELS_ENDPOINT,
        method="GET",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"DeepSeek 模型列表请求失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek 网络连接失败：{exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"DeepSeek 模型列表返回内容无效：{exc}") from exc

    rows = result.get("data", []) if isinstance(result, dict) else []
    models = tuple(
        str(row.get("id", "")).strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("id", "")).strip()
    )
    if not models:
        raise RuntimeError("DeepSeek 返回的模型列表为空")
    return models


def _source_lines(payload: dict) -> list[dict]:
    return [
        line
        for line in payload.get("lines", [])
        if line.get("should_speak", str(line.get("prop", "")).lower() != "name")
        and str(line.get("text", "")).strip()
    ]


def _load_source_lines(story_path: Path) -> tuple[list[dict], str]:
    if story_path.suffix.lower() in PLAIN_TEXT_EXTENSIONS:
        try:
            content = story_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = story_path.read_text(encoding="gb18030")
        lines = [line for line in content.splitlines() if line.strip()]
        return [
            {"line_id": f"custom_{index:04d}", "speaker": "", "text": line}
            for index, line in enumerate(lines, start=1)
        ], story_path.stem

    payload = json.loads(story_path.read_text(encoding="utf-8"))
    context = (payload.get("segment") or {}).get("story_name") or story_path.stem
    return _source_lines(payload), context


def _fingerprint(lines: list[dict]) -> str:
    material = "\n".join(
        f"{line.get('line_id', '')}\t{line.get('speaker', '')}\t{line.get('text', '')}"
        for line in lines
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def analysis_cache_path(story_path: Path, cache_root: Path) -> Path:
    try:
        relative = story_path.resolve().relative_to((story_path.parents[2]).resolve())
    except ValueError:
        relative = Path(story_path.name)
    return cache_root / relative.with_suffix(".tts-analysis.json")


def _clean_instruction(value: object) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:500]


def _request_batch(
    api_key: str,
    lines: list[dict],
    context: str,
    model: str = DEFAULT_DEEPSEEK_MODEL,
) -> dict[str, str]:
    numbered = [
        {
            "line_id": line.get("line_id", ""),
            "speaker": line.get("speaker", "旁白"),
            "text": line.get("text", ""),
        }
        for line in lines
    ]
    system = (
        "你是中文游戏剧情配音导演。请为每句台词生成适合神经网络TTS的中文朗读指导。"
        "只返回严格JSON，不要Markdown，不要解释。格式必须是："
        '{"lines":[{"line_id":"原样照抄","instruction":"一句简短的朗读指导"}]}。'
        "instruction应包含情绪、强度、语速、语调和必要的停顿；不要修改台词内容，"
        "不要加入角色名，不要使用无法执行的音效描述。旁白也要分析。"
    )
    user = json.dumps(
        {
            "剧情上下文": context[:3000],
            "待分析台词": numbered,
        },
        ensure_ascii=False,
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        DEEPSEEK_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"DeepSeek API 请求失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"DeepSeek API 网络连接失败：{exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"DeepSeek API 返回内容无效：{exc}") from exc

    try:
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        rows = parsed.get("lines", [])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("DeepSeek 返回的情绪分析不是有效的 JSON 结构") from exc

    allowed = {str(line.get("line_id", "")) for line in lines}
    output: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        line_id = str(row.get("line_id", ""))
        instruction = _clean_instruction(row.get("instruction"))
        if line_id in allowed and instruction:
            output[line_id] = instruction
    return output


def test_deepseek_model(api_key: str, model: str) -> str:
    """Make a small uncached request and return its generated instruction."""
    result = _request_batch(
        api_key,
        [{"line_id": "__test__", "speaker": "旁白", "text": "测试一下这句台词的情绪。"}],
        "DeepSeek API 连接测试",
        model=model.strip() or DEFAULT_DEEPSEEK_MODEL,
    )
    instruction = result.get("__test__", "").strip()
    if not instruction:
        raise RuntimeError("DeepSeek 测试请求成功，但没有返回有效的情绪指令")
    return instruction


def load_or_create_analysis(
    story_path: Path,
    cache_root: Path,
    api_key: str,
    status: Callable[[str], None] | None = None,
    model: str = DEFAULT_DEEPSEEK_MODEL,
    cancelled: threading.Event | None = None,
    paused: threading.Event | None = None,
) -> dict[str, str]:
    """Return cached or newly generated line_id -> instruct mappings."""
    model = model.strip() or DEFAULT_DEEPSEEK_MODEL
    lines, context = _load_source_lines(story_path)
    fingerprint = _fingerprint(lines)
    cache_path = analysis_cache_path(story_path, cache_root)
    merged: dict[str, str] = {}
    processed = 0
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cached.get("version") == ANALYSIS_VERSION
            and cached.get("fingerprint") == fingerprint
            and cached.get("model") == model
        ):
            result = cached.get("instructions") or {}
            if isinstance(result, dict):
                merged = {str(key): str(value) for key, value in result.items()}
                processed = max(0, min(len(lines), int(cached.get("processed", len(lines)))))
                if processed == len(lines):
                    if status:
                        status("已读取缓存的 DeepSeek 情绪分析")
                    return merged
    except (OSError, ValueError):
        pass

    if not api_key.strip():
        raise RuntimeError("已勾选质量优化，但设置中没有填写 DeepSeek API Key")
    batch_size = 24
    for offset in range(processed, len(lines), batch_size):
        while paused is not None and paused.is_set():
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("情绪分析已中断，已完成批次已保存")
            time.sleep(0.2)
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("情绪分析已中断，已完成批次已保存")
        batch = lines[offset:offset + batch_size]
        if status:
            status(f"DeepSeek 情绪分析：{min(offset + len(batch), len(lines))}/{len(lines)}")
        merged.update(_request_batch(api_key, batch, str(context), model=model))
        atomic_json_save(cache_path, {"version": ANALYSIS_VERSION,
            "source_story": str(story_path), "fingerprint": fingerprint,
            "model": model, "instructions": merged,
            "processed": min(offset + len(batch), len(lines))})

    return merged
