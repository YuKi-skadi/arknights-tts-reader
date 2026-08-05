"""PRTS wiki catalog and story parser.

PRTS exposes a stable MediaWiki raw view. The navigation template contains
the story hierarchy, while each story page keeps the original AVG commands,
including ``[name="..."]`` speaker markers. Parsing that raw form lets us
keep narration and discard speaker-label commands before TTS generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from story_catalog import DialogueLine, StoryDocument, StorySegment, make_match_text, normalize_story_text


PRTS_BASE = "https://prts.wiki"
PRTS_NAV_TEMPLATE = "Template:剧情导航"
_CATEGORY_NAMES = {
    "maintheme": "主题曲",
    "sidestory": "别传",
    "intermezzi": "别传",
    "storyset": "故事集",
}
_LINK_RE = re.compile(r"\[\[([^|\]]+?)(?:\|([^\]]+?))?\]\]")
_COMMAND_RE = re.compile(r"\[([A-Za-z]+)([^\]]*)\]")
_SPEAKER_RE = re.compile(r"^\s*=\s*[\"']([^\"']+)[\"']")
_STAR_LABEL_RE = re.compile(r"^\s*\*[^*\r\n]{1,40}\*\s*$")


class PRTSTaskCancelled(RuntimeError):
    """Raised when the user stops the current PRTS download."""


def _clean_display(value: str) -> str:
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    return unicodedata.normalize("NFKC", value).strip()


def _clean_story_text(value: str) -> str:
    value = value.replace("\\n", "\n").replace("\\r", "")
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"\{@[^}]+\}", "", value)
    value = unicodedata.normalize("NFKC", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if not _STAR_LABEL_RE.fullmatch(line)]
    return "\n".join(lines).strip()


def _cells(block: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line.startswith("!"):
            continue
        cell = line[1:].strip()
        if "|" in cell:
            attrs, value = cell.split("|", 1)
        else:
            attrs, value = "", cell
        result.append((attrs.strip(), _clean_display(value)))
    return result


def _event_name(cells: list[tuple[str, str]], category: str) -> str:
    if not cells:
        return "未分类剧情"
    values = [value for _attrs, value in cells if value]
    if category == "maintheme":
        return values[0] if values else "主线剧情"
    if "rowspan" in cells[0][0] and len(values) > 1:
        return values[1] if values[1] not in {"支线", "剧情"} else f"{values[0]} {values[1]}"
    if len(values) > 1 and values[1] in {"支线", "剧情"}:
        return values[0]
    return values[0]


def parse_navigation(raw: str) -> tuple[StorySegment, ...]:
    """Parse the two tables in Template:剧情导航 into story segments."""
    segments: list[StorySegment] = []
    order = 0
    active_table: str | None = None
    for block in raw.split("|-"):
        if "主线剧情一览" in block:
            active_table = "maintheme"
        elif "活动剧情一览" in block:
            active_table = "activity"
        if active_table is None or "|}" in block:
            continue

        links = [(target.strip(), (label or target).strip()) for target, label in _LINK_RE.findall(block)]
        links = [item for item in links if item[0] not in {"剧情一览", "Template:剧情导航"}]
        if not links:
            continue
        cell_data = _cells(block)
        if active_table == "maintheme":
            category = "maintheme"
        else:
            values = {value for _attrs, value in cell_data}
            category = "storyset" if "剧情" in values else "sidestory"
        name = _event_name(cell_data, category)
        event_key = f"{name}|{links[0][0]}"
        event_id = f"prts-{category}-{hashlib.sha1(event_key.encode('utf-8')).hexdigest()[:10]}"
        for target, label in links:
            phase = target.rsplit("/", 1)[-1]
            segments.append(
                StorySegment(
                    event_id=event_id,
                    event_name=name,
                    category=category,
                    story_id=target,
                    story_code=label,
                    story_name=label,
                    tag=phase,
                    story_txt=target,
                    sort=order,
                )
            )
            order += 1
    return tuple(segments)


def parse_story_wikitext(raw: str, story_txt: str) -> tuple[DialogueLine, ...]:
    """Extract dialogue and narration from PRTS AVG command text."""
    marker = "|文本数据="
    start = raw.find(marker)
    body = raw[start + len(marker):] if start >= 0 else raw
    end = body.find("}} {{剧情导航")
    if end < 0:
        end = body.rfind("}}")
    if end >= 0:
        body = body[:end]

    lines: list[DialogueLine] = []
    cursor = 0
    active_speaker = ""
    active_dialogue = False
    for match in _COMMAND_RE.finditer(body):
        text = _clean_story_text(body[cursor:match.start()])
        if text and not text.startswith("{{"):
            prop = "dialogue" if active_dialogue and active_speaker else "narration"
            lines.append(
                DialogueLine(
                    line_id=f"{story_txt}#line{len(lines)}",
                    source_id=len(lines),
                    prop=prop,
                    speaker=active_speaker if active_dialogue else "",
                    text=text,
                    match_text=make_match_text(text),
                    should_speak=True,
                )
            )

        command = match.group(1).lower()
        attributes = match.group(2)
        if command == "name":
            speaker_match = _SPEAKER_RE.search(attributes)
            active_speaker = _clean_display(speaker_match.group(1)) if speaker_match else ""
            active_dialogue = bool(active_speaker)
        else:
            active_dialogue = False
        cursor = match.end()

    tail = _clean_story_text(body[cursor:])
    if tail and not tail.startswith("{{"):
        prop = "dialogue" if active_dialogue and active_speaker else "narration"
        lines.append(
            DialogueLine(
                line_id=f"{story_txt}#line{len(lines)}",
                source_id=len(lines),
                prop=prop,
                speaker=active_speaker if active_dialogue else "",
                text=tail,
                match_text=make_match_text(tail),
                should_speak=True,
            )
        )
    return tuple(lines)


class PRTSStoryClient:
    """Read PRTS navigation and raw story pages with a local project cache."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        timeout: float = 30.0,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.cache_dir = Path(cache_dir or Path(__file__).resolve().parent / "data" / "cache" / "prts")
        self.progress_callback = progress_callback
        self.pause_event = None
        self.stop_event = None
        self._segments: tuple[StorySegment, ...] | None = None

    def set_task_controls(self, pause_event: Any = None, stop_event: Any = None) -> None:
        self.pause_event = pause_event
        self.stop_event = stop_event

    def _wait_if_paused(self) -> None:
        while self.pause_event is not None and self.pause_event.is_set():
            if self.stop_event is not None and self.stop_event.is_set():
                raise PRTSTaskCancelled("剧情下载已停止")
            time.sleep(0.15)
        if self.stop_event is not None and self.stop_event.is_set():
            raise PRTSTaskCancelled("剧情下载已停止")

    def _progress(self, message: str, current: int = 0, total: int = 0) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message, current, total)

    @staticmethod
    def _safe_name(value: str, fallback: str = "未命名") -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
        value = value.rstrip(". ")
        return (value or fallback)[:100]

    def cache_path_for(self, segment: StorySegment) -> Path:
        category = _CATEGORY_NAMES.get(segment.category, segment.category or "其他")
        event_name = self._safe_name(segment.event_name, segment.event_id)
        story_name = self._safe_name(segment.story_code or segment.story_name or segment.story_id)
        story_key = hashlib.sha1(segment.story_id.encode("utf-8")).hexdigest()[:8]
        return self.cache_dir / category / event_name / f"{story_name}__{story_key}.txt"

    def catalog_cache_path(self) -> Path:
        return self.cache_dir / "目录" / "剧情导航.txt"

    def _legacy_cache_path(self, page: str) -> Path:
        key = hashlib.sha1(page.encode("utf-8")).hexdigest()
        return self.cache_dir / "legacy" / f"{key}.txt"

    def _url(self, page: str) -> str:
        return f"{PRTS_BASE}/w/{quote(page, safe='')}?action=raw"

    def _get_raw(
        self,
        page: str,
        refresh: bool = False,
        cache_path: Path | None = None,
    ) -> str:
        cache_path = cache_path or self.cache_dir / "目录" / f"{self._safe_name(page)}.txt"
        self._wait_if_paused()
        if cache_path.exists() and not refresh:
            self._progress(f"读取缓存：{cache_path.parent.name} / {cache_path.name}", 1, 1)
            return cache_path.read_text(encoding="utf-8")
        legacy_path = self._legacy_cache_path(page)
        if legacy_path.exists() and not refresh:
            text = legacy_path.read_text(encoding="utf-8")
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
            except OSError:
                pass
            self._progress(f"迁移旧缓存：{cache_path.parent.name} / {cache_path.name}", 1, 1)
            return text
        self._progress(f"正在连接 PRTS：{page}")
        request = Request(self._url(page), headers={"User-Agent": "ArknightsTTSReader/0.5"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                chunks: list[bytes] = []
                while True:
                    self._wait_if_paused()
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    self._progress(f"正在下载：{page}", received, total)
                text = b"".join(chunks).decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"无法读取 PRTS 页面：{page}") from exc
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
            self._progress(f"已缓存：{cache_path.parent.name} / {cache_path.name}", 1, 1)
        except OSError:
            pass
        return text

    def load_catalog(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        raw = self._get_raw(PRTS_NAV_TEMPLATE, refresh=refresh, cache_path=self.catalog_cache_path())
        self._segments = parse_navigation(raw)
        events: dict[str, dict[str, Any]] = {}
        for segment in self._segments:
            events.setdefault(
                segment.event_id,
                {"id": segment.event_id, "name": segment.event_name, "category": segment.category},
            )
        return events

    def list_events(self, category: str | None = None) -> list[dict[str, Any]]:
        if self._segments is None:
            self.load_catalog()
        assert self._segments is not None
        events: dict[str, dict[str, Any]] = {}
        for segment in self._segments:
            if category and segment.category != category:
                continue
            events.setdefault(segment.event_id, {"id": segment.event_id, "name": segment.event_name, "category": segment.category})
        return list(events.values())

    def list_segments(self, event_id: str) -> list[StorySegment]:
        if self._segments is None:
            self.load_catalog()
        assert self._segments is not None
        return [segment for segment in self._segments if segment.event_id == event_id]

    def load_document(self, segment: StorySegment) -> StoryDocument:
        raw = self._get_raw(segment.story_txt, cache_path=self.cache_path_for(segment))
        return StoryDocument(segment=segment, lines=parse_story_wikitext(raw, segment.story_txt))
