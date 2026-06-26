"""Persistent storage helpers for HA Podcast Player."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    ALLOWED_SPEEDS,
    DEFAULT_MAX_EPISODES_PER_FEED,
    DEFAULT_PLAYBACK_SPEED,
    DEFAULT_PLAYED_THRESHOLD,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def utcnow_iso() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str, length: int = 24) -> str:
    """Create a short stable id."""
    return sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def normalize_url_for_id(url: str) -> str:
    """Normalize URL enough for stable IDs without removing query tokens."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def make_feed_id(rss_url: str) -> str:
    """Return stable feed id."""
    return f"feed_{stable_hash(normalize_url_for_id(rss_url))}"


def make_episode_id(feed_id: str, guid: str | None, audio_url: str | None, title: str | None, published: str | None) -> str:
    """Return stable episode id."""
    if guid:
        key = f"{feed_id}|guid|{guid}"
    elif audio_url:
        key = f"{feed_id}|audio|{audio_url}"
    else:
        key = f"{feed_id}|fallback|{title or ''}|{published or ''}"
    return f"ep_{stable_hash(key, 32)}"


def default_data() -> dict[str, Any]:
    """Return a new default storage document."""
    return {
        "schema_version": STORAGE_VERSION,
        "settings": {
            "refresh_interval_minutes": 120,
            "default_playback_speed": DEFAULT_PLAYBACK_SPEED,
            "played_threshold": DEFAULT_PLAYED_THRESHOLD,
            "max_episodes_per_feed": DEFAULT_MAX_EPISODES_PER_FEED,
            "direct_first": True,
            "speaker_proxy_secret": None,
        },
        "feeds": {},
        "episodes": {},
        "progress": {},
        "player": {
            "state": "idle",
            "current_episode_id": None,
            "current_feed_id": None,
            "position": 0,
            "duration": None,
            "speed": DEFAULT_PLAYBACK_SPEED,
            "updated_at": None,
            "output_mode": "browser",
            "target_media_player": None,
            "target_media_player_name": None,
            "speaker_url_mode": None,
            "speaker_media_content_type": None,
            "speaker_last_error": None,
        },
        "ui": {
            "selected_feed_id": "all",
            "filter": "all",
        },
    }


class PodcastStorage:
    """Manage persisted podcast data."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize storage."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = default_data()

    async def async_load(self) -> None:
        """Load storage."""
        stored = await self._store.async_load()
        if not stored:
            self.data = default_data()
            await self.async_save()
            return

        defaults = default_data()
        merged = defaults
        merged.update(stored)

        # Keep existing libraries/progress intact, while allowing new default
        # nested keys to appear after upgrades.
        for section in ("settings", "player", "ui"):
            base = defaults.get(section, {}).copy()
            existing = stored.get(section) or {}
            if isinstance(existing, dict):
                base.update(existing)
            merged[section] = base

        for section in ("feeds", "episodes", "progress"):
            if section not in merged or merged[section] is None:
                merged[section] = defaults[section]

        merged["schema_version"] = STORAGE_VERSION
        self.data = merged

    async def async_save(self) -> None:
        """Persist storage."""
        await self._store.async_save(self.data)

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy snapshot."""
        return deepcopy(self.data)

    def get_feed(self, feed_id: str) -> dict[str, Any] | None:
        """Get feed by id."""
        return self.data["feeds"].get(feed_id)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        """Get episode by id."""
        return self.data["episodes"].get(episode_id)

    def get_progress(self, episode_id: str) -> dict[str, Any]:
        """Get progress for an episode."""
        progress = self.data["progress"].setdefault(
            episode_id,
            {
                "episode_id": episode_id,
                "played": False,
                "position": 0,
                "duration": None,
                "last_played_at": None,
                "completed_at": None,
                "playback_speed": self.data["settings"].get("default_playback_speed", DEFAULT_PLAYBACK_SPEED),
            },
        )
        return progress

    def enabled_feeds(self) -> list[dict[str, Any]]:
        """Return enabled feeds."""
        return [feed for feed in self.data["feeds"].values() if feed.get("enabled", True)]

    def episodes_for_feed(self, feed_id: str) -> list[dict[str, Any]]:
        """Return episodes for a feed."""
        return [ep for ep in self.data["episodes"].values() if ep.get("feed_id") == feed_id]

    def upsert_feed(self, feed: dict[str, Any]) -> bool:
        """Insert or update feed. Return True if newly added."""
        feed_id = feed["feed_id"]
        is_new = feed_id not in self.data["feeds"]
        existing = self.data["feeds"].get(feed_id, {})
        merged = {**existing, **feed}
        merged.setdefault("enabled", True)
        merged.setdefault("created_at", utcnow_iso())
        merged["updated_at"] = utcnow_iso()
        self.data["feeds"][feed_id] = merged
        return is_new

    def mark_feed_failed(self, feed_id: str, error_code: str, message: str) -> None:
        """Mark a feed as failed without deleting old cache."""
        feed = self.data["feeds"].get(feed_id)
        if not feed:
            return
        feed["status"] = "failed"
        feed["last_error"] = {"code": error_code, "message": message, "at": utcnow_iso()}
        feed["updated_at"] = utcnow_iso()

    def remove_feed(self, feed_id: str, keep_history: bool = True) -> bool:
        """Remove a feed."""
        if feed_id not in self.data["feeds"]:
            return False
        del self.data["feeds"][feed_id]
        if not keep_history:
            episode_ids = [eid for eid, ep in self.data["episodes"].items() if ep.get("feed_id") == feed_id]
            for episode_id in episode_ids:
                self.data["episodes"].pop(episode_id, None)
                self.data["progress"].pop(episode_id, None)
        return True

    def upsert_episodes(self, feed_id: str, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert or update episodes. Return newly added episodes."""
        new_episodes: list[dict[str, Any]] = []
        now = utcnow_iso()
        for episode in episodes:
            episode_id = episode["episode_id"]
            # Progress records intentionally outlive the bounded episode cache.
            # Treat an episode with existing progress as already discovered so
            # that a trimmed RSS item does not emit a new-episode event again.
            is_new = (
                episode_id not in self.data["episodes"]
                and episode_id not in self.data["progress"]
            )
            existing = self.data["episodes"].get(episode_id, {})
            merged = {**existing, **episode}
            merged["feed_id"] = feed_id
            merged.setdefault("discovered_at", now)
            merged["updated_at"] = now
            self.data["episodes"][episode_id] = merged
            self.get_progress(episode_id)
            if is_new:
                new_episodes.append(merged)

        self._trim_feed_episodes(feed_id)
        return new_episodes

    def _trim_feed_episodes(self, feed_id: str) -> None:
        """Trim old cached episodes per feed while preserving progress records."""
        max_count = int(self.data["settings"].get("max_episodes_per_feed", DEFAULT_MAX_EPISODES_PER_FEED))
        episodes = self.episodes_for_feed(feed_id)
        if len(episodes) <= max_count:
            return

        def sort_key(ep: dict[str, Any]) -> tuple[str, str]:
            return (ep.get("published") or "", ep.get("discovered_at") or "")

        episodes.sort(key=sort_key, reverse=True)
        keep_ids = {ep["episode_id"] for ep in episodes[:max_count]}
        for ep in episodes[max_count:]:
            episode_id = ep["episode_id"]
            progress = self.data["progress"].get(episode_id, {})
            # Keep in-progress episodes even if they are old.
            if progress.get("position", 0) and not progress.get("played"):
                keep_ids.add(episode_id)
        for ep in episodes:
            if ep["episode_id"] not in keep_ids:
                self.data["episodes"].pop(ep["episode_id"], None)

    def save_progress(
        self,
        episode_id: str,
        position: float | int,
        duration: float | int | None = None,
        playing: bool | None = None,
        speed: float | None = None,
    ) -> dict[str, Any]:
        """Save playback progress."""
        progress = self.get_progress(episode_id)
        now = utcnow_iso()
        position_value = max(0, int(float(position or 0)))
        progress["position"] = position_value
        if duration is not None:
            try:
                progress["duration"] = max(0, int(float(duration)))
            except (TypeError, ValueError):
                pass
        if speed is not None and float(speed) in ALLOWED_SPEEDS:
            progress["playback_speed"] = float(speed)
        progress["last_played_at"] = now

        threshold = float(self.data["settings"].get("played_threshold", DEFAULT_PLAYED_THRESHOLD))
        duration_value = progress.get("duration")
        if duration_value and duration_value > 0 and position_value / duration_value >= threshold:
            if not progress.get("played"):
                progress["completed_at"] = now
            progress["played"] = True

        episode = self.get_episode(episode_id)
        player = self.data["player"]
        player["current_episode_id"] = episode_id
        player["current_feed_id"] = episode.get("feed_id") if episode else None
        player["position"] = position_value
        player["duration"] = progress.get("duration")
        player["speed"] = progress.get("playback_speed")
        player["updated_at"] = now
        if playing is not None:
            player["state"] = "playing" if playing else "paused"
        return progress

    def set_player_state(self, state: str, episode_id: str | None = None) -> None:
        """Set player state."""
        player = self.data["player"]
        player["state"] = state
        if episode_id is not None:
            episode = self.get_episode(episode_id)
            player["current_episode_id"] = episode_id
            player["current_feed_id"] = episode.get("feed_id") if episode else None
            progress = self.get_progress(episode_id)
            player["position"] = progress.get("position", 0)
            player["duration"] = progress.get("duration") or (episode or {}).get("duration_seconds")
            player["speed"] = progress.get("playback_speed", DEFAULT_PLAYBACK_SPEED)
        player["updated_at"] = utcnow_iso()

    def set_speed(self, speed: float, episode_id: str | None = None) -> None:
        """Set playback speed."""
        speed = float(speed)
        if speed not in ALLOWED_SPEEDS:
            raise ValueError("Unsupported playback speed")
        target = episode_id or self.data["player"].get("current_episode_id")
        if target:
            self.get_progress(target)["playback_speed"] = speed
        self.data["player"]["speed"] = speed
        self.data["settings"]["default_playback_speed"] = speed
        self.data["player"]["updated_at"] = utcnow_iso()

    def mark_played(self, episode_id: str, played: bool) -> dict[str, Any]:
        """Mark episode played/unplayed."""
        progress = self.get_progress(episode_id)
        progress["played"] = played
        progress["completed_at"] = utcnow_iso() if played else None
        return progress

    def counts(self) -> dict[str, Any]:
        """Return summary counts for the active library."""
        feeds = self.data["feeds"]
        progress = self.data["progress"]
        episodes = self.data["episodes"]
        enabled = [feed for feed in feeds.values() if feed.get("enabled", True)]
        active_feed_ids = {feed.get("feed_id") for feed in enabled if feed.get("feed_id")}
        active_episode_ids = {
            eid
            for eid, episode in episodes.items()
            if episode.get("feed_id") in active_feed_ids
        }
        failed = [feed for feed in enabled if feed.get("status") == "failed"]
        unplayed = [eid for eid in active_episode_ids if not progress.get(eid, {}).get("played")]
        partial = [
            eid
            for eid, p in progress.items()
            if eid in active_episode_ids and p.get("position", 0) > 0 and not p.get("played")
        ]
        return {
            "total_feeds": len(feeds),
            "enabled_feeds": len(enabled),
            "failed_feeds": len(failed),
            "total_episodes": len(active_episode_ids),
            "unplayed": len(unplayed),
            "partially_played": len(partial),
        }
