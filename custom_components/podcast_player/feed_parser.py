"""Podcast RSS parsing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser

from .storage import make_episode_id

AUDIO_MIME_PREFIX = "audio/"
AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/x-wav",
}
AUDIO_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
)


class PodcastParseError(Exception):
    """Raised when a feed cannot be parsed as a playable podcast."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _get_first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip() or None


def _parse_datetime(value: Any) -> str | None:
    """Parse RSS-ish date into ISO UTC, if possible."""
    if not value:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 6:
        try:
            dt = datetime(*value[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return None


def _duration_to_seconds(value: Any) -> int | None:
    """Convert iTunes duration values to seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        nums = [int(float(part)) for part in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def _image_from_obj(obj: dict[str, Any] | None) -> str | None:
    if not obj:
        return None
    image = obj.get("image")
    if isinstance(image, dict):
        href = _get_first(image, "href", "url")
        if href:
            return href
    images = obj.get("images")
    if isinstance(images, list) and images:
        for item in images:
            if isinstance(item, dict):
                href = _get_first(item, "href", "url")
                if href:
                    return href
    media_thumbnail = obj.get("media_thumbnail")
    if isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    return _get_first(obj, "itunes_image", "image_href")


def _is_audio_enclosure(enclosure: dict[str, Any]) -> bool:
    href = _as_text(_get_first(enclosure, "href", "url"))
    if not href:
        return False
    mime = (_as_text(enclosure.get("type")) or "").lower()
    if mime.startswith(AUDIO_MIME_PREFIX) or mime in AUDIO_MIME_TYPES:
        return True
    lower_href = href.lower().split("?", 1)[0]
    return lower_href.endswith(AUDIO_EXTENSIONS)


def _pick_audio(entry: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []

    enclosures = entry.get("enclosures") or []
    if isinstance(enclosures, list):
        candidates.extend([enc for enc in enclosures if isinstance(enc, dict)])

    media_content = entry.get("media_content") or []
    if isinstance(media_content, list):
        candidates.extend([item for item in media_content if isinstance(item, dict)])

    links = entry.get("links") or []
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict):
                rel = (link.get("rel") or "").lower()
                if rel in ("enclosure", "alternate", ""):
                    candidates.append(link)

    for candidate in candidates:
        if not _is_audio_enclosure(candidate):
            continue
        href = _as_text(_get_first(candidate, "href", "url"))
        if not href:
            continue
        return {
            "audio_url": urljoin(base_url, href),
            "audio_type": _as_text(candidate.get("type")),
            "audio_size": _as_text(_get_first(candidate, "length", "fileSize")),
        }
    return None


def parse_podcast_feed(raw_text: str | bytes, feed_url: str, feed_id: str) -> dict[str, Any]:
    """Parse feed text into normalized feed and episode data."""
    parsed = feedparser.parse(raw_text)
    if getattr(parsed, "bozo", False) and not parsed.get("entries"):
        raise PodcastParseError("parse_error", f"Feed could not be parsed: {getattr(parsed, 'bozo_exception', '')}")

    feed = parsed.get("feed") or {}
    entries = parsed.get("entries") or []
    if not entries:
        raise PodcastParseError("no_episodes", "Feed was parsed, but it has no episodes/items.")

    title = _as_text(_get_first(feed, "title", "itunes_title")) or "Untitled podcast"
    website = _as_text(_get_first(feed, "link", "href"))
    artwork = _image_from_obj(feed)
    description = _as_text(_get_first(feed, "subtitle", "description", "summary"))

    normalized_feed = {
        "feed_id": feed_id,
        "rss_url": feed_url,
        "title": title,
        "description": description,
        "author": _as_text(_get_first(feed, "author", "itunes_author", "publisher")),
        "website": website,
        "artwork_url": artwork,
        "status": "ok",
        "last_error": None,
    }

    episodes: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        audio = _pick_audio(entry, feed_url)
        if not audio:
            continue

        guid = _as_text(_get_first(entry, "id", "guid"))
        published = (
            _parse_datetime(entry.get("published_parsed"))
            or _parse_datetime(entry.get("updated_parsed"))
            or _parse_datetime(entry.get("published"))
            or _parse_datetime(entry.get("updated"))
        )
        episode_title = _as_text(entry.get("title")) or "Untitled episode"
        episode_id = make_episode_id(feed_id, guid, audio.get("audio_url"), episode_title, published)
        duration = _duration_to_seconds(_get_first(entry, "itunes_duration", "duration"))
        episode_artwork = _image_from_obj(entry) or artwork

        episodes.append(
            {
                "episode_id": episode_id,
                "feed_id": feed_id,
                "guid": guid,
                "title": episode_title,
                "description": _as_text(_get_first(entry, "summary", "description", "subtitle")),
                "published": published,
                "duration_seconds": duration,
                "audio_url": audio["audio_url"],
                "audio_type": audio.get("audio_type"),
                "audio_size": audio.get("audio_size"),
                "artwork_url": episode_artwork,
                "website_url": _as_text(entry.get("link")),
                "explicit": _as_text(_get_first(entry, "itunes_explicit", "explicit")),
                "season": _as_text(_get_first(entry, "itunes_season", "season")),
                "episode_number": _as_text(_get_first(entry, "itunes_episode", "episode")),
            }
        )

    if not episodes:
        raise PodcastParseError("no_audio_enclosures", "Feed has episodes, but none include a playable audio enclosure.")

    normalized_feed["episode_count"] = len(episodes)
    return {"feed": normalized_feed, "episodes": episodes}
