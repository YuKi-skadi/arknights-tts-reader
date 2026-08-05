"""ASTR story catalog client used by the ver0.5 story-download module.

The site is a JavaScript viewer. Its hierarchy and story text are JSON files,
so ver0.5 talks to those files directly and caches them locally.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ASTR_CDN = "https://r2.m31ns.top/"
ASTR_GITHUB = "https://raw.githubusercontent.com/050644zf/ArknightsStoryJson/main/"
INTERMEZZI_IDS = frozenset(
    {
        "act9d0", "act18d0", "act18d3", "act17side",
        "act25side", "act33side", "act37side",
    }
)


@dataclass(frozen=True)
class StorySegment:
    event_id: str
    event_name: str
    category: str
    story_id: str
    story_code: str
    story_name: str
    tag: str
    story_txt: str
    sort: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DialogueLine:
    line_id: str
    source_id: int
    prop: str
    speaker: str
    text: str
    match_text: str
    should_speak: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StoryDocument:
    segment: StorySegment
    lines: tuple[DialogueLine, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"segment": self.segment.to_dict(), "lines": [line.to_dict() for line in self.lines]}


def normalize_story_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text))
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"\{@[^}]+\}", "", value)
    value = value.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", "", value).strip()


def make_match_text(text: str) -> str:
    normalized = normalize_story_text(text)
    key = "".join(char for char in normalized if not unicodedata.category(char).startswith("P"))
    return key or normalized


def extract_dialogue_lines(story_data: dict[str, Any], story_txt: str) -> tuple[DialogueLine, ...]:
    lines: list[DialogueLine] = []
    for item in story_data.get("storyList", []):
        prop = str(item.get("prop", ""))
        attributes = item.get("attributes") or {}
        if prop.lower() not in {"name", "multiline"}:
            continue
        text = normalize_story_text(attributes.get("content", ""))
        if not text:
            continue
        source_id = int(item.get("id", len(lines)))
        lines.append(
            DialogueLine(
                line_id=f"{story_txt}#line{source_id}",
                source_id=source_id,
                prop=prop,
                speaker=normalize_story_text(attributes.get("name", "")),
                text=text,
                match_text=make_match_text(text),
                should_speak=prop.lower() != "name",
            )
        )
    return tuple(lines)


class ASTRStoryClient:
    """Fetch ASTR metadata and stories with CDN/GitHub fallback."""

    def __init__(self, server: str = "zh_CN", cache_dir: str | Path | None = None, timeout: float = 30.0) -> None:
        self.server = server
        self.timeout = timeout
        default_cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ArknightsTTSReader" / "astr-story-cache"
        self.cache_dir = Path(cache_dir or default_cache)
        self._catalog: dict[str, dict[str, Any]] | None = None

    def _get_json(self, path: str, refresh: bool = False) -> Any:
        path = path.lstrip("/")
        cache_path = self.cache_dir / self.server / path
        if cache_path.exists() and not refresh:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        last_error: Exception | None = None
        for base in (ASTR_CDN, ASTR_GITHUB):
            try:
                request = Request(f"{base}{self.server}/{path}", headers={"User-Agent": "ArknightsTTSReader/0.5"})
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
                return data
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                last_error = exc
        raise RuntimeError(f"无法读取剧情数据：{path}") from last_error

    @staticmethod
    def category_for(event_id: str, entry_type: str) -> str | None:
        if entry_type == "MAINLINE":
            return "maintheme"
        if entry_type == "ACTIVITY":
            return "intermezzi" if event_id in INTERMEZZI_IDS else "sidestory"
        if entry_type == "MINI_ACTIVITY":
            return "storyset"
        return None

    def load_catalog(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._catalog is None or refresh:
            data = self._get_json("gamedata/excel/story_review_table.json", refresh=refresh)
            if not isinstance(data, dict):
                raise RuntimeError("剧情目录数据格式异常")
            self._catalog = data
        return self._catalog

    def list_events(self, category: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event_id, raw in self.load_catalog().items():
            event_category = self.category_for(event_id, raw.get("entryType", ""))
            if event_category is None or (category and event_category != category):
                continue
            event = dict(raw)
            event.update(id=event_id, category=event_category)
            events.append(event)
        return events

    def list_segments(self, event_id: str) -> list[StorySegment]:
        event = self.load_catalog().get(event_id)
        if not event:
            raise KeyError(f"找不到活动：{event_id}")
        category = self.category_for(event_id, event.get("entryType", ""))
        if category is None:
            return []
        result: list[StorySegment] = []
        for item in event.get("infoUnlockDatas", []):
            story_txt = str(item.get("storyTxt") or "")
            if not story_txt:
                continue
            result.append(
                StorySegment(
                    event_id=event_id,
                    event_name=str(event.get("name", event_id)),
                    category=category,
                    story_id=str(item.get("storyId", "")),
                    story_code=str(item.get("storyCode") or ""),
                    story_name=str(item.get("storyName") or ""),
                    tag=str(item.get("avgTag") or ""),
                    story_txt=story_txt,
                    sort=int(item.get("storySort") or 0),
                )
            )
        return result

    def load_document(self, segment: StorySegment) -> StoryDocument:
        data = self._get_json(f"gamedata/story/{segment.story_txt}.json")
        return StoryDocument(segment=segment, lines=extract_dialogue_lines(data, segment.story_txt))
