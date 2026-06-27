"""Tests for Podcast Player media source helpers."""

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import Unresolvable

from custom_components.podcast_player.const import DOMAIN
from custom_components.podcast_player.media_source import (
    PodcastMediaSource,
    _duration_label,
    _parts,
    media_source_id_for_episode,
)
from custom_components.podcast_player.speaker_proxy import verify_proxy_token
from custom_components.podcast_player.storage import default_data


class FakeStorage:
    """Minimal storage object for media source tests."""

    def __init__(self) -> None:
        self.data = default_data()
        self.saved = False

    async def async_save(self) -> None:
        """Record saves."""
        self.saved = True

    def enabled_feeds(self) -> list[dict]:
        """Return enabled feeds."""
        return [feed for feed in self.data["feeds"].values() if feed.get("enabled", True)]

    def get_feed(self, feed_id: str) -> dict | None:
        """Return a feed."""
        return self.data["feeds"].get(feed_id)

    def get_episode(self, episode_id: str) -> dict | None:
        """Return an episode."""
        return self.data["episodes"].get(episode_id)


class FakeCoordinator:
    """Minimal coordinator object for media source tests."""

    def __init__(self, storage: FakeStorage) -> None:
        self.storage = storage

    def active_episodes(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> list[dict]:
        """Return active episodes sorted newest first."""
        allowed_feed_ids = {feed_id} if feed_id else {
            feed["feed_id"] for feed in self.storage.enabled_feeds()
        }
        if feed_ids:
            allowed_feed_ids &= feed_ids
        episodes = [
            episode
            for episode in self.storage.data["episodes"].values()
            if episode.get("feed_id") in allowed_feed_ids
        ]
        episodes.sort(key=lambda episode: episode.get("published") or "", reverse=True)
        return episodes


def _runtime() -> SimpleNamespace:
    storage = FakeStorage()
    return SimpleNamespace(storage=storage, coordinator=FakeCoordinator(storage))


def _source(runtime: SimpleNamespace) -> PodcastMediaSource:
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    return PodcastMediaSource(hass)


def _item(identifier: str | None, target_media_player: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(identifier=identifier, target_media_player=target_media_player)


def _add_feed(storage: FakeStorage, feed_id: str, title: str, *, enabled: bool = True) -> None:
    storage.data["feeds"][feed_id] = {
        "feed_id": feed_id,
        "title": title,
        "enabled": enabled,
        "artwork_url": f"https://example.test/{feed_id}.jpg",
    }


def _add_episode(
    storage: FakeStorage,
    episode_id: str,
    feed_id: str,
    title: str,
    published: str,
    *,
    duration: int = 0,
    played: bool = False,
    position: int = 0,
) -> None:
    storage.data["episodes"][episode_id] = {
        "episode_id": episode_id,
        "feed_id": feed_id,
        "title": title,
        "published": published,
        "duration_seconds": duration,
        "audio_url": f"https://cdn.example.test/{episode_id}.mp3",
        "audio_type": "audio/mpeg",
        "artwork_url": f"https://cdn.example.test/{episode_id}.jpg",
    }
    storage.data["progress"][episode_id] = {
        "episode_id": episode_id,
        "played": played,
        "position": position,
        "duration": None,
    }


def test_media_source_identifier_parts() -> None:
    """Media source identifiers are normalized into path parts."""
    assert _parts(None) == []
    assert _parts("") == []
    assert _parts("/feed/feed_123/latest/") == ["feed", "feed_123", "latest"]


def test_media_source_id_for_episode() -> None:
    """Episode media source IDs use the integration domain."""
    assert media_source_id_for_episode("ep_123") == f"media-source://{DOMAIN}/episode/ep_123"


def test_duration_label() -> None:
    """Durations are formatted compactly."""
    assert _duration_label(None) is None
    assert _duration_label(0) is None
    assert _duration_label(65) == "1:05"
    assert _duration_label(3665) == "1:01:05"


def test_root_contains_categories_and_feeds_directory() -> None:
    """Root browse node exposes stable categories and a dedicated feeds folder."""
    runtime = _runtime()
    root = _source(runtime)._root(runtime)

    assert root.media_class == MediaClass.APP
    assert root.media_content_type == MediaType.APP
    assert [child.identifier for child in root.children] == ["latest", "unplayed", "in_progress", "all", "feeds"]
    assert root.children[-1].title == "Feeds"
    assert root.children[-1].children_media_class == MediaClass.PODCAST


def test_feeds_directory_lists_enabled_feeds_sorted() -> None:
    """Feeds directory lists only enabled feeds, sorted by title."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_b", "Beta")
    _add_feed(runtime.storage, "feed_a", "Alpha")
    _add_feed(runtime.storage, "feed_disabled", "Disabled", enabled=False)

    feeds = _source(runtime)._feeds(runtime)

    assert feeds.identifier == "feeds"
    assert feeds.children_media_class == MediaClass.PODCAST
    assert [(child.identifier, child.title) for child in feeds.children] == [
        ("feed/feed_a", "Alpha"),
        ("feed/feed_b", "Beta"),
    ]


def test_episode_list_filters_and_metadata() -> None:
    """Episode categories filter correctly and expose clean media metadata."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_new", "feed_1", "Newest", "2026-01-03T00:00:00+00:00", duration=3665)
    _add_episode(runtime.storage, "ep_played", "feed_1", "Played", "2026-01-02T00:00:00+00:00", played=True)
    _add_episode(runtime.storage, "ep_progress", "feed_1", "Started", "2026-01-01T00:00:00+00:00", position=120)

    source = _source(runtime)
    latest = source._episode_list(runtime, "latest")
    unplayed = source._episode_list(runtime, "unplayed")
    in_progress = source._episode_list(runtime, "in_progress")
    feed_latest = source._episode_list(runtime, "latest", feed_id="feed_1", title_prefix="Feed One")

    assert latest.children[0].identifier == "episode/ep_new"
    assert latest.children[0].media_class == MediaClass.EPISODE
    assert latest.children[0].media_content_type == MediaType.EPISODE
    assert latest.children[0].title == "Newest — Feed One (1:01:05)"
    assert [child.identifier for child in unplayed.children] == ["episode/ep_new", "episode/ep_progress"]
    assert [child.identifier for child in in_progress.children] == ["episode/ep_progress"]
    assert feed_latest.children[0].title == "Newest (1:01:05)"


def test_latest_episode_list_reports_not_shown_count() -> None:
    """Latest is capped and reports hidden items through not_shown."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    for index in range(30):
        _add_episode(
            runtime.storage,
            f"ep_{index:02d}",
            "feed_1",
            f"Episode {index:02d}",
            f"2026-01-{index + 1:02d}T00:00:00+00:00",
        )

    latest = _source(runtime)._episode_list(runtime, "latest")

    assert len(latest.children) == 25
    assert latest.not_shown == 5


def test_browse_unknown_feed_raises() -> None:
    """Missing feeds return a browse error."""
    from homeassistant.components.media_player import BrowseError

    runtime = _runtime()

    with pytest.raises(BrowseError):
        asyncio.run(_source(runtime).async_browse_media(_item("feed/missing")))


def test_resolve_browser_playback_uses_relative_proxy() -> None:
    """Browser Media Browser playback uses a relative signed HA proxy URL."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1")))

    parsed = urlparse(resolved.url)
    query = parse_qs(parsed.query)

    assert parsed.path == "/api/podcast_player/speaker_proxy/ep_1"
    assert verify_proxy_token(
        runtime.storage.data["settings"]["speaker_proxy_secret"],
        "ep_1",
        query["expires"][0],
        query["token"][0],
    )
    assert resolved.mime_type == "audio/mpeg"
    assert runtime.storage.saved


def test_resolve_direct_first_speaker_target_uses_direct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct-first speaker-target resolve does not create a proxy URL when direct audio exists."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    called = False

    def fake_proxy(*args) -> str:
        nonlocal called
        called = True
        return "https://ha.example.test/proxy"

    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_proxy_url", fake_proxy)

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.speaker")))

    assert resolved.url == "https://cdn.example.test/ep_1.mp3"
    assert resolved.mime_type == "audio/mpeg"
    assert not called
    assert not runtime.storage.saved


def test_resolve_prefers_proxy_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy-first resolve returns the signed proxy and persists a new proxy secret."""
    runtime = _runtime()
    runtime.storage.data["settings"]["direct_first"] = False
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")

    def fake_proxy(_hass, settings, _episode_id) -> str:
        settings["speaker_proxy_secret"] = "new-secret"
        return "https://ha.example.test/proxy"

    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_proxy_url", fake_proxy)

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.speaker")))

    assert resolved.url == "https://ha.example.test/proxy"
    assert runtime.storage.saved


def test_resolve_proxy_mode_falls_back_to_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy-first resolve falls back to direct audio if no HA URL is available."""
    runtime = _runtime()
    runtime.storage.data["settings"]["direct_first"] = False
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_proxy_url", lambda *args: None)

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.speaker")))

    assert resolved.url == "https://cdn.example.test/ep_1.mp3"
    assert not runtime.storage.saved


def test_resolve_missing_or_unplayable_episode_raises() -> None:
    """Missing and unplayable episodes fail clearly."""
    runtime = _runtime()
    source = _source(runtime)

    with pytest.raises(Unresolvable):
        asyncio.run(source.async_resolve_media(_item("episode/missing")))

    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    runtime.storage.data["episodes"]["ep_1"]["audio_url"] = None

    with pytest.raises(Unresolvable):
        asyncio.run(source.async_resolve_media(_item("episode/ep_1")))
