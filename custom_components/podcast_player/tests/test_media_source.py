"""Tests for Podcast Player media source helpers."""

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from async_upnp_client.profiles.dlna import didl_lite
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import Unresolvable
from homeassistant.exceptions import HomeAssistantError

from custom_components.podcast_player.const import DOMAIN
from custom_components.podcast_player.media_source import (
    PodcastMediaSource,
    _clean_didl_text,
    _didl_metadata_for_target,
    _duration_label,
    _episode_display_title,
    _parts,
    _target_prefers_proxy,
    async_get_media_source,
    media_source_id_for_episode,
)
from custom_components.podcast_player.media_source import (
    _runtime as media_source_runtime,
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
        self.prepared_media_source_playback: list[dict] = []
        self.target_statuses: dict[str, dict] = {}

    def active_episodes(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> list[dict]:
        """Return active episodes sorted newest first."""
        allowed_feed_ids = {feed_id} if feed_id else {
            feed["feed_id"] for feed in self.storage.enabled_feeds() if feed.get("feed_id")
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

    def media_source_target_status(self, entity_id: str) -> dict:
        """Return fake target status for Media Source resolution."""
        return self.target_statuses.get(
            entity_id,
            {
                "playable": True,
                "reason": None,
                "capabilities": {
                    "play_media": True,
                    "progress": False,
                    "seek": "none",
                    "raw_avtransport": False,
                },
            },
        )

    async def async_prepare_media_source_playback(self, **kwargs) -> None:
        """Record media-source playback preparation."""
        self.prepared_media_source_playback.append(kwargs)


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


def test_media_source_runtime_lookup_and_async_factory() -> None:
    """Media source factory returns a Podcast media source and runtime lookup is optional."""
    empty_hass = SimpleNamespace(data={})
    runtime = _runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})

    assert media_source_runtime(empty_hass) is None
    assert media_source_runtime(hass) is runtime
    assert isinstance(asyncio.run(async_get_media_source(hass)), PodcastMediaSource)


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
    assert _duration_label("bad") is None
    assert _duration_label(65) == "1:05"
    assert _duration_label(3665) == "1:01:05"


def test_media_browser_title_helpers() -> None:
    """Media browser title helpers handle fallback titles and optional metadata."""
    assert _clean_didl_text("  text  ") == "text"
    assert _clean_didl_text("") is None
    assert _episode_display_title({}, {}, {}, include_feed_title=True) == "Untitled episode"
    assert _episode_display_title(
        {"title": "Episode", "duration_seconds": 61},
        {"title": "Feed"},
        {},
        include_feed_title=True,
    ) == "Episode — Feed (1:01)"


def test_root_contains_categories_and_feeds_directory() -> None:
    """Root browse node exposes stable categories and a dedicated feeds folder."""
    runtime = _runtime()
    root = _source(runtime)._root(runtime)

    assert root.media_class == MediaClass.APP
    assert root.media_content_type == MediaType.APP
    assert [child.identifier for child in root.children] == ["latest", "unplayed", "in_progress", "all", "feeds"]
    assert root.children[-1].title == "Feeds"
    assert root.children[-1].children_media_class == MediaClass.PODCAST


def test_async_browse_media_routes_all_directory_paths() -> None:
    """Async browse entrypoint routes root, category, feeds, and feed directories."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_feed(runtime.storage, "feed_disabled", "Disabled", enabled=False)
    runtime.storage.data["feeds"]["missing_id"] = {"title": "Missing ID", "enabled": True}
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    source = _source(runtime)

    root = asyncio.run(source.async_browse_media(_item(None)))
    latest = asyncio.run(source.async_browse_media(_item("latest")))
    feeds = asyncio.run(source.async_browse_media(_item("feeds")))
    feed = asyncio.run(source.async_browse_media(_item("feed/feed_1")))
    feed_unplayed = asyncio.run(source.async_browse_media(_item("feed/feed_1/unplayed")))

    assert root.identifier is None
    assert latest.identifier == "latest"
    assert [(child.identifier, child.title) for child in feeds.children] == [("feed/feed_1", "Feed One")]
    assert feed.identifier == "feed/feed_1"
    assert feed.title == "Feed One"
    assert feed_unplayed.identifier == "feed/feed_1/unplayed"
    assert feed_unplayed.title == "Feed One: Unplayed episodes"

    with pytest.raises(BrowseError, match="not configured"):
        asyncio.run(PodcastMediaSource(SimpleNamespace(data={})).async_browse_media(_item(None)))
    with pytest.raises(BrowseError, match="not found"):
        asyncio.run(source.async_browse_media(_item("feed/feed_disabled")))
    with pytest.raises(BrowseError, match="path"):
        asyncio.run(source.async_browse_media(_item("feed/feed_1/unknown")))


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
    assert runtime.coordinator.prepared_media_source_playback == []


def test_resolve_direct_first_speaker_target_uses_direct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct-first speaker-target resolve prepares the backend session with the direct URL."""
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
    assert runtime.coordinator.prepared_media_source_playback == [
        {
            "episode_id": "ep_1",
            "media_player_entity_id": "media_player.speaker",
            "media_content_id": "https://cdn.example.test/ep_1.mp3",
            "media_content_type": "audio/mpeg",
            "url_mode": "direct",
        }
    ]
    assert not runtime.storage.saved


def test_resolve_direct_first_dlna_target_prefers_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """DLNA Media Browser playback receives a simple HA URL even in direct-first mode."""
    runtime = _runtime()
    runtime.coordinator.target_statuses["media_player.dlna_speaker"] = {
        "playable": True,
        "reason": None,
        "platform": "dlna_dmr",
        "capabilities": {
            "play_media": True,
            "progress": True,
            "seek": "best_effort",
            "raw_avtransport": True,
        },
    }
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")

    def fake_proxy(_hass, settings, _episode_id) -> str:
        settings["speaker_proxy_secret"] = "new-secret"
        return "https://ha.example.test/proxy"

    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_proxy_url", fake_proxy)
    monkeypatch.setattr(
        "custom_components.podcast_player.media_source.make_signed_speaker_artwork_proxy_url",
        lambda *args: "https://ha.example.test/artwork",
    )

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.dlna_speaker")))

    assert resolved.url == "https://ha.example.test/proxy"
    assert resolved.mime_type == "audio/mpeg"
    assert resolved.didl_metadata.title == "Episode"
    assert resolved.didl_metadata.creator == "Feed One"
    assert resolved.didl_metadata.artist == "Feed One"
    assert resolved.didl_metadata.album == "Feed One"
    didl_xml = didl_lite.to_xml_string(resolved.didl_metadata).decode("utf-8")
    assert "<dc:title>Episode</dc:title>" in didl_xml
    assert "<upnp:artist>Feed One</upnp:artist>" in didl_xml
    assert "<upnp:albumArtURI>https://ha.example.test/artwork</upnp:albumArtURI>" in didl_xml
    assert runtime.coordinator.prepared_media_source_playback == [
        {
            "episode_id": "ep_1",
            "media_player_entity_id": "media_player.dlna_speaker",
            "media_content_id": "https://ha.example.test/proxy",
            "media_content_type": "audio/mpeg",
            "url_mode": "signed_proxy",
        }
    ]
    assert runtime.storage.saved


def test_resolve_direct_first_dlna_target_falls_back_to_direct_without_ha_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """DLNA proxy preference falls back to direct when HA cannot build a URL."""
    runtime = _runtime()
    runtime.coordinator.target_statuses["media_player.dlna_speaker"] = {
        "playable": True,
        "reason": None,
        "platform": "dlna_dmr",
        "capabilities": {
            "play_media": True,
            "progress": True,
            "seek": "best_effort",
            "raw_avtransport": True,
        },
    }
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_proxy_url", lambda *args: None)
    monkeypatch.setattr(
        "custom_components.podcast_player.media_source.make_signed_speaker_artwork_proxy_url",
        lambda *args: None,
    )

    resolved = asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.dlna_speaker")))

    assert resolved.url == "https://cdn.example.test/ep_1.mp3"
    assert resolved.didl_metadata.title == "Episode"
    assert runtime.coordinator.prepared_media_source_playback[0]["url_mode"] == "direct"
    assert not runtime.storage.saved


def test_resolve_prefers_proxy_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy-first target resolve returns the signed proxy and prepares the backend session."""
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
    assert runtime.coordinator.prepared_media_source_playback == [
        {
            "episode_id": "ep_1",
            "media_player_entity_id": "media_player.speaker",
            "media_content_id": "https://ha.example.test/proxy",
            "media_content_type": "audio/mpeg",
            "url_mode": "signed_proxy",
        }
    ]
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
    assert runtime.coordinator.prepared_media_source_playback[0]["url_mode"] == "direct"
    assert not runtime.storage.saved


def test_resolve_unavailable_target_fails_before_play_media() -> None:
    """Media Browser playback fails clearly when the selected target is unavailable."""
    runtime = _runtime()
    runtime.coordinator.target_statuses["media_player.offline"] = {
        "playable": False,
        "reason": "Speaker is unavailable.",
        "capabilities": {"play_media": False},
    }
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")

    with pytest.raises(Unresolvable, match="Speaker is unavailable"):
        asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.offline")))

    assert runtime.coordinator.prepared_media_source_playback == []


def test_resolve_target_prepare_error_becomes_unresolvable() -> None:
    """Media source target preparation errors become clean resolve errors."""
    runtime = _runtime()
    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")

    async def fail_prepare(**kwargs) -> None:
        raise HomeAssistantError("Target rejected playback")

    runtime.coordinator.async_prepare_media_source_playback = fail_prepare

    with pytest.raises(Unresolvable, match="Target rejected playback"):
        asyncio.run(_source(runtime).async_resolve_media(_item("episode/ep_1", "media_player.speaker")))


def test_resolve_missing_or_unplayable_episode_raises() -> None:
    """Missing and unplayable episodes fail clearly."""
    runtime = _runtime()
    source = _source(runtime)

    with pytest.raises(Unresolvable, match="not configured"):
        asyncio.run(PodcastMediaSource(SimpleNamespace(data={})).async_resolve_media(_item("episode/ep_1")))

    with pytest.raises(Unresolvable, match="not playable"):
        asyncio.run(source.async_resolve_media(_item("feed/feed_1")))

    with pytest.raises(Unresolvable):
        asyncio.run(source.async_resolve_media(_item("episode/missing")))

    _add_feed(runtime.storage, "feed_1", "Feed One")
    _add_episode(runtime.storage, "ep_1", "feed_1", "Episode", "2026-01-01T00:00:00+00:00")
    runtime.storage.data["episodes"]["ep_1"]["audio_url"] = None

    with pytest.raises(Unresolvable):
        asyncio.run(source.async_resolve_media(_item("episode/ep_1")))


def test_target_proxy_preference_and_didl_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIDL metadata helper handles non-proxy targets and constructor failures."""
    runtime = _runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})

    assert _target_prefers_proxy(None) is False
    assert _target_prefers_proxy({"platform": "dlna_dmr", "capabilities": {}}) is True
    assert _target_prefers_proxy({"capabilities": {"raw_avtransport": True}}) is True
    assert _target_prefers_proxy({"capabilities": {"limited_controls": True}}) is True
    assert _didl_metadata_for_target(hass, {}, "ep_1", {}, {}, "https://cdn.example.test/ep_1.mp3", "audio/mpeg", None) is None

    monkeypatch.setattr("custom_components.podcast_player.media_source.make_signed_speaker_artwork_proxy_url", lambda *args: None)

    class BrokenMusicTrack:
        """MusicTrack replacement that fails construction."""

        def __init__(self, **kwargs) -> None:
            raise RuntimeError("bad metadata")

    monkeypatch.setattr(didl_lite, "MusicTrack", BrokenMusicTrack)

    assert _didl_metadata_for_target(
        hass,
        {},
        "ep_1",
        {"title": "Episode", "artwork_url": "https://cdn.example.test/ep_1.jpg"},
        {"title": "Feed"},
        "https://cdn.example.test/ep_1.mp3",
        "audio/mpeg",
        {"capabilities": {"limited_controls": True}},
    ) is None
