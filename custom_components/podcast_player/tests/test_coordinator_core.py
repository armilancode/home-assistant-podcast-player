"""Tests for coordinator feed, session, and selection helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.podcast_player.const import (
    EVENT_EPISODE_COMPLETED,
    EVENT_FEED_ADDED,
    EVENT_FEED_REFRESH_FAILED,
    EVENT_NEW_EPISODE,
    EVENT_PLAYBACK_PAUSED,
    EVENT_PLAYBACK_STARTED,
)
from custom_components.podcast_player.coordinator import (
    EXTERNAL_STARTUP_GRACE_SECONDS,
    MAX_FEED_BODY_BYTES,
    PodcastUpdateCoordinator,
    _async_fetch_feed_text,
    async_fetch_and_parse_feed,
    async_validate_feed_url,
)
from custom_components.podcast_player.external_control import ExternalPlaybackStatus
from custom_components.podcast_player.feed_parser import PodcastParseError
from custom_components.podcast_player.storage import PodcastStorage, default_data, make_feed_id


class FakeContent:
    """Minimal aiohttp response content replacement."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        """Yield configured chunks."""
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    """Minimal aiohttp response context manager."""

    def __init__(self, *, status: int = 200, chunks: list[bytes] | None = None, charset: str | None = "utf-8") -> None:
        self.status = status
        self.content = FakeContent(chunks or [b"<rss />"])
        self.charset = charset
        self.url = "https://final.example.test/feed.xml"

    async def __aenter__(self) -> "FakeResponse":
        """Enter response context."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Exit response context."""


class FakeSession:
    """Minimal aiohttp client session replacement."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        """Return configured response."""
        self.calls.append((url, kwargs))
        return self.response


class FakeStates:
    """Minimal hass.states replacement."""

    def __init__(self, states: dict[str, object] | None = None) -> None:
        self._states = states or {}

    def get(self, entity_id: str) -> object | None:
        """Return fake state for an entity."""
        return self._states.get(entity_id)


class FakeBus:
    """Minimal hass.bus replacement."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event_data: dict) -> None:
        """Record fired events."""
        self.events.append((event_type, event_data))


class FakeServices:
    """Minimal hass.services replacement."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def async_call(self, *args, **kwargs) -> None:
        """Record service calls."""
        self.calls.append((args, kwargs))


class FakeHass:
    """Minimal Home Assistant replacement."""

    def __init__(self, states: dict[str, object] | None = None) -> None:
        self.states = FakeStates(states)
        self.services = FakeServices()
        self.bus = FakeBus()
        self.tasks: list[asyncio.Task] = []

    async def async_add_executor_job(self, func, *args):
        """Run executor jobs inline for tests."""
        return func(*args)

    def async_create_task(self, coro):
        """Create and record background tasks."""
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


def _storage() -> PodcastStorage:
    """Return in-memory podcast storage."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.async_save = AsyncMock()
    return storage


def _coordinator(states: dict[str, object] | None = None) -> PodcastUpdateCoordinator:
    """Return a coordinator shell with real helper methods."""
    coord = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coord.storage = _storage()
    coord.hass = FakeHass(states)
    coord._refresh_lock = asyncio.Lock()
    coord._refresh_sem = asyncio.Semaphore(4)
    coord._external_control = SimpleNamespace()
    coord._external_poll_task = None
    coord.updates = []
    coord.async_set_updated_data = lambda data: coord.updates.append(data)
    return coord


def _seed_library(storage: PodcastStorage) -> None:
    """Seed storage with active and inactive library items."""
    storage.data["feeds"] = {
        "feed_a": {"feed_id": "feed_a", "title": "Alpha", "enabled": True},
        "feed_b": {"feed_id": "feed_b", "title": "Beta", "enabled": True},
        "feed_disabled": {"feed_id": "feed_disabled", "title": "Disabled", "enabled": False},
    }
    storage.data["episodes"] = {
        "ep_old": {
            "episode_id": "ep_old",
            "feed_id": "feed_a",
            "title": "Old",
            "published": "2026-01-01T00:00:00+00:00",
            "discovered_at": "2026-01-01T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/old.mp3",
        },
        "ep_new": {
            "episode_id": "ep_new",
            "feed_id": "feed_b",
            "title": "New",
            "published": "2026-01-03T00:00:00+00:00",
            "discovered_at": "2026-01-03T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/new.mp3",
        },
        "ep_played": {
            "episode_id": "ep_played",
            "feed_id": "feed_a",
            "title": "Played",
            "published": "2026-01-02T00:00:00+00:00",
            "discovered_at": "2026-01-02T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/played.mp3",
        },
        "ep_disabled": {
            "episode_id": "ep_disabled",
            "feed_id": "feed_disabled",
            "title": "Disabled",
            "published": "2026-01-04T00:00:00+00:00",
        },
    }
    storage.data["progress"] = {
        "ep_old": {"episode_id": "ep_old", "played": False, "position": 0, "duration": None},
        "ep_new": {"episode_id": "ep_new", "played": False, "position": 0, "duration": None},
        "ep_played": {"episode_id": "ep_played", "played": True, "position": 120, "duration": 120},
        "ep_disabled": {"episode_id": "ep_disabled", "played": False, "position": 0, "duration": None},
    }


def _client_ssl_error() -> aiohttp.ClientSSLError:
    """Return an aiohttp SSL error with enough connection metadata for str()."""
    return aiohttp.ClientSSLError(
        SimpleNamespace(host="example.test", port=443, ssl=True),
        OSError(1, "certificate failed"),
    )


@pytest.mark.asyncio
async def test_async_fetch_feed_text_success_and_safety_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed text fetch handles successful, HTTP error, and oversized responses."""
    session = FakeSession(FakeResponse(chunks=[b"hello", b" world"], charset=None))

    text, final_url = await _async_fetch_feed_text(session, "https://example.test/feed.xml")

    assert text == "hello world"
    assert final_url == "https://final.example.test/feed.xml"
    assert session.calls[0][1]["allow_redirects"] is True

    with pytest.raises(PodcastParseError, match="HTTP 500"):
        await _async_fetch_feed_text(FakeSession(FakeResponse(status=500)), "https://example.test/feed.xml")

    monkeypatch.setattr("custom_components.podcast_player.coordinator.MAX_FEED_BODY_BYTES", 3)
    with pytest.raises(PodcastParseError, match="10 MB"):
        await _async_fetch_feed_text(
            FakeSession(FakeResponse(chunks=[b"ab", b"cd"])),
            "https://example.test/feed.xml",
        )
    monkeypatch.setattr("custom_components.podcast_player.coordinator.MAX_FEED_BODY_BYTES", MAX_FEED_BODY_BYTES)


@pytest.mark.asyncio
async def test_async_fetch_and_parse_feed_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed fetch helper normalizes URLs, parses feeds, and records final URL."""
    fetch = AsyncMock(return_value=("<rss />", "https://final.example.test/feed.xml"))
    monkeypatch.setattr("custom_components.podcast_player.coordinator.async_get_clientsession", lambda hass: object())
    monkeypatch.setattr("custom_components.podcast_player.coordinator._async_fetch_feed_text", fetch)
    monkeypatch.setattr(
        "custom_components.podcast_player.coordinator.parse_podcast_feed",
        lambda raw, rss_url, feed_id: {"feed": {"feed_id": feed_id, "rss_url": rss_url}, "episodes": []},
    )
    hass = FakeHass()

    result = await async_fetch_and_parse_feed(hass, "https://Example.test/feed.xml", "feed_1")

    assert result["feed"] == {"feed_id": "feed_1", "rss_url": "https://example.test/feed.xml"}
    assert result["canonical_url"] == "https://final.example.test/feed.xml"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (asyncio.TimeoutError(), "timeout"),
        (_client_ssl_error(), "ssl_error"),
        (aiohttp.TooManyRedirects(None, ()), "redirect_loop"),
        (aiohttp.ClientError("cannot connect"), "cannot_connect"),
    ],
)
async def test_async_fetch_and_parse_feed_maps_network_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, code: str) -> None:
    """Network exceptions are converted to user-facing feed parse errors."""
    monkeypatch.setattr("custom_components.podcast_player.coordinator.async_get_clientsession", lambda hass: object())
    monkeypatch.setattr("custom_components.podcast_player.coordinator._async_fetch_feed_text", AsyncMock(side_effect=error))

    with pytest.raises(PodcastParseError) as err:
        await async_fetch_and_parse_feed(FakeHass(), "https://example.test/feed.xml")

    assert err.value.code == code


@pytest.mark.asyncio
async def test_validate_feed_url_and_invalid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed validation delegates to fetch/parse and rejects invalid URLs."""
    called: list[str] = []

    async def fake_fetch(hass, rss_url):
        called.append(rss_url)
        return {}

    monkeypatch.setattr("custom_components.podcast_player.coordinator.async_fetch_and_parse_feed", fake_fetch)

    await async_validate_feed_url(FakeHass(), "https://example.test/feed.xml")

    assert called == ["https://example.test/feed.xml"]

    with pytest.raises(PodcastParseError, match="RSS URL"):
        await async_fetch_and_parse_feed(FakeHass(), "not-a-url")


@pytest.mark.asyncio
async def test_coordinator_initialize_shutdown_and_update_failure() -> None:
    """Coordinator lifecycle initializes data, starts polling, and wraps update failures."""
    coord = _coordinator()
    coord.storage.data["player"]["external_session"]["active"] = True

    await coord.async_initialize()

    assert coord.updates
    assert coord._external_poll_task is not None

    await coord.async_shutdown()

    assert coord._external_poll_task is None

    coord.async_refresh_feeds = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_add_remove_and_refresh_feed_lifecycle() -> None:
    """Coordinator feed lifecycle saves storage, updates data, and fires public events."""
    coord = _coordinator()
    rss_url = "https://example.test/feed.xml"
    feed_id = make_feed_id(rss_url)
    coord._async_refresh_single_url = AsyncMock(
        return_value={
            "feed": {
                "feed_id": feed_id,
                "rss_url": rss_url,
                "title": "Example Podcast",
            },
            "episodes": [
                {
                    "episode_id": "ep_1",
                    "feed_id": feed_id,
                    "title": "Episode One",
                    "published": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
    )

    feed = await coord.async_add_feed(rss_url)

    assert feed["feed_id"] == feed_id
    assert coord.storage.async_save.await_count == 1
    assert (EVENT_FEED_ADDED, {"feed_id": feed_id, "title": "Example Podcast"}) in coord.hass.bus.events
    assert coord.hass.bus.events[-1][0] == EVENT_NEW_EPISODE

    assert await coord.async_remove_feed(feed_id, keep_history=False) is True
    assert await coord.async_remove_feed("missing") is False

    await coord.async_refresh_feeds("missing")

    assert coord.updates


@pytest.mark.asyncio
async def test_refresh_existing_feed_handles_connection_error() -> None:
    """Stored feed refreshes mark connection errors as failed and fire failure events."""
    coord = _coordinator()
    feed = {
        "feed_id": "feed_1",
        "rss_url": "https://example.test/feed.xml",
        "title": "Example Podcast",
        "status": "ok",
        "enabled": True,
    }
    coord.storage.data["feeds"]["feed_1"] = feed
    coord._async_refresh_single_url = AsyncMock(side_effect=aiohttp.ClientError("offline"))

    await coord._async_refresh_existing_feed(feed)

    assert coord.storage.data["feeds"]["feed_1"]["status"] == "failed"
    assert coord.hass.bus.events == [
        (EVENT_FEED_REFRESH_FAILED, {"feed_id": "feed_1", "code": "cannot_connect", "message": "offline"})
    ]


def test_external_session_helpers() -> None:
    """External-session helpers normalize, start, clear, and age sessions."""
    coord = _coordinator()
    player = coord.storage.data["player"]
    player["external_session"] = "bad"

    session = coord._external_session()

    assert session["active"] is False
    assert isinstance(player["external_session"], dict)

    started = coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform="dlna_dmr",
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=25,
        duration=120,
    )

    assert started["active"] is True
    assert started["position"] == 25
    assert started["media_content_id_hash"] != "https://cdn.example.test/ep_1.mp3"
    assert coord._external_session_age_seconds({"started_at": "bad"}) is None
    assert coord._external_session_in_startup_grace(started) is True

    started["started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=EXTERNAL_STARTUP_GRACE_SECONDS + 5)).isoformat()
    assert coord._external_session_in_startup_grace(started) is False

    coord._clear_external_session("stopped")
    ended = coord.storage.data["player"]["external_session"]
    assert ended["active"] is False
    assert ended["last_error"] == "stopped"
    assert ended["target_media_player"] == "media_player.kitchen"


def test_external_status_helpers_and_apply_status() -> None:
    """External status helpers match sessions and persist playback progress."""
    coord = _coordinator()
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }
    session = coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform=None,
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=0,
        duration=100,
    )

    idle = ExternalPlaybackStatus(state="idle", current_media_id="https://cdn.example.test/ep_1.mp3")
    assert coord._external_status_is_starting(idle, coord._external_session(), "ep_1") is True
    assert coord._starting_external_status(idle, coord._external_session()).state == "buffering"

    status = ExternalPlaybackStatus(
        state="playing",
        transport_state="PLAYING",
        position=42,
        duration=100,
        current_media_id="https://cdn.example.test/ep_1.mp3",
        supported_actions={"Pause", "Stop"},
        progress_source="dlna",
        control_source="dlna",
    )
    coord._apply_external_status(status)

    player = coord.storage.data["player"]
    session = player["external_session"]
    assert player["state"] == "playing"
    assert player["output_mode"] == "speaker"
    assert session["position"] == 42
    assert session["supported_actions"] == ["Pause", "Stop"]
    assert session["media_matches_session"] is True

    coord._apply_external_status(ExternalPlaybackStatus(state="idle", position=44, duration=100))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["external_session"]["active"] is False


@pytest.mark.asyncio
async def test_retry_external_start_respects_media_match_and_actions() -> None:
    """Startup retry only runs when the target still looks like the active session."""
    coord = _coordinator()
    coord.storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "audio_url": "https://cdn.example.test/ep_1.mp3"}
    session = coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform=None,
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=0,
        duration=None,
    )
    coord._async_play_via_dlna = AsyncMock(return_value=True)

    assert await coord._async_retry_external_start(
        "media_player.kitchen",
        ExternalPlaybackStatus(state="idle", current_media_id="https://elsewhere.example.test/audio.mp3"),
        session,
        "ep_1",
    ) is False
    assert await coord._async_retry_external_start(
        "media_player.kitchen",
        ExternalPlaybackStatus(state="idle", supported_actions={"Pause"}),
        session,
        "ep_1",
    ) is False
    assert await coord._async_retry_external_start(
        "media_player.kitchen",
        ExternalPlaybackStatus(state="idle", supported_actions={"Play"}),
        session,
        "ep_1",
    ) is True


def test_ha_and_estimated_external_status() -> None:
    """Coordinator builds HA-derived and estimated target status snapshots."""
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    state = SimpleNamespace(
        state="playing",
        attributes={
            "media_position": 10,
            "media_position_updated_at": updated_at,
            "media_duration": 100,
            "media_content_id": "media-id",
        },
        name="Kitchen",
    )
    coord = _coordinator({"media_player.kitchen": state})

    status = coord._ha_status_for_target("media_player.kitchen")

    assert status is not None
    assert status.state == "playing"
    assert status.position >= 10
    assert status.duration == 100
    assert status.progress_source == "ha"
    assert coord._ha_status_for_target("media_player.missing") is None

    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["speed"] = 2.0
    session = coord.storage.data["player"]["external_session"]
    session.update(
        {
            "active": True,
            "position": 10,
            "duration": 25,
            "status_updated_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),
            "supported_actions": ["Stop"],
            "control_source": "ha",
        }
    )

    estimated = coord._estimated_external_status()

    assert estimated is not None
    assert estimated.position == 25
    assert estimated.duration == 25
    assert estimated.supported_actions == {"Stop"}
    assert estimated.progress_source == "estimated"


@pytest.mark.asyncio
async def test_update_external_session_uses_ha_status_and_saves() -> None:
    """External-session polling applies the best available target status."""
    coord = _coordinator()
    coord.storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "audio_url": "https://cdn.example.test/ep_1.mp3"}
    coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform=None,
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=0,
        duration=100,
    )
    coord._async_dlna_status = AsyncMock(return_value=ExternalPlaybackStatus(state="unknown"))
    coord._ha_status_for_target = lambda target: ExternalPlaybackStatus(state="paused", position=12, duration=100, progress_source="ha")

    await coord.async_update_external_session()

    assert coord.storage.data["player"]["state"] == "paused"
    coord.storage.async_save.assert_awaited()
    assert coord.updates


@pytest.mark.asyncio
async def test_mark_feed_played_and_browser_resume_selection() -> None:
    """Coordinator selection helpers only use active feeds and unplayed episodes."""
    coord = _coordinator()
    _seed_library(coord.storage)

    assert coord.active_feed_ids() == {"feed_a", "feed_b"}
    assert coord._normalize_selected_feed_ids("feed_a") == {"feed_a"}
    assert coord._normalize_selected_feed_ids(feed_ids={"feed_b", "feed_disabled"}) == {"feed_b"}
    assert [episode["episode_id"] for episode in coord.active_episodes()] == ["ep_new", "ep_played", "ep_old"]
    assert coord.latest_episode()["episode_id"] == "ep_new"
    assert coord.next_unplayed_episode("feed_a")["episode_id"] == "ep_old"
    assert coord._select_episode_for_output("ep_old", None, "current")["episode_id"] == "ep_old"

    coord.storage.data["player"]["current_episode_id"] = "ep_played"
    assert coord._select_episode_for_output(None, "feed_a", "current")["episode_id"] == "ep_played"
    assert coord._select_episode_for_output(None, "feed_a", "latest")["episode_id"] == "ep_played"
    assert coord._select_episode_for_output(None, "feed_a", "next_unplayed")["episode_id"] == "ep_old"

    count = await coord.async_mark_feed_played("feed_a", True)

    assert count == 2
    assert coord.storage.data["progress"]["ep_old"]["played"] is True
    coord.storage.async_save.assert_awaited()


@pytest.mark.asyncio
async def test_resume_without_episode_does_nothing_or_raises_for_speaker() -> None:
    """Resume handles empty browser queues and reports empty speaker queues."""
    coord = _coordinator()

    await coord.async_resume()

    assert coord.hass.services.calls == []

    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"

    with pytest.raises(HomeAssistantError):
        await coord.async_resume()


@pytest.mark.asyncio
async def test_browser_playback_actions_and_progress_events() -> None:
    """Browser playback actions update storage, save state, and fire public events."""
    coord = _coordinator()
    coord.storage.data["feeds"]["feed_1"] = {"feed_id": "feed_1", "title": "Feed One", "enabled": True}
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "title": "Episode One",
        "published": "2026-01-01T00:00:00+00:00",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "duration_seconds": 100,
    }

    await coord.async_play_episode("ep_1")

    player = coord.storage.data["player"]
    assert player["state"] == "playing"
    assert player["output_mode"] == "browser"
    assert (EVENT_PLAYBACK_STARTED, {"episode_id": "ep_1", "output_mode": "browser"}) in coord.hass.bus.events

    await coord.async_pause()
    await coord.async_stop()
    await coord.async_seek("ep_1", 25)
    await coord.async_save_progress("ep_1", 96, duration=100, playing=True, speed=1.25)
    await coord.async_mark_played("ep_1", False)
    await coord.async_set_speed(1.5, "ep_1")
    await coord.async_mark_current_played(True)
    await coord.async_resume()

    assert (EVENT_PLAYBACK_PAUSED, {"episode_id": "ep_1"}) in coord.hass.bus.events
    assert (EVENT_EPISODE_COMPLETED, {"episode_id": "ep_1"}) in coord.hass.bus.events
    assert coord.storage.data["progress"]["ep_1"]["played"] is True
    assert coord.storage.data["progress"]["ep_1"]["playback_speed"] == 1.5
    assert coord.storage.async_save.await_count >= 7
    assert coord.updates


@pytest.mark.asyncio
async def test_play_latest_and_next_unplayed_delegate_to_browser_playback() -> None:
    """Latest/next helpers select active episodes and play them in the browser."""
    coord = _coordinator()
    _seed_library(coord.storage)

    latest = await coord.async_play_latest()
    next_unplayed = await coord.async_play_next_unplayed("feed_a")

    assert latest["episode_id"] == "ep_new"
    assert next_unplayed["episode_id"] == "ep_old"
    assert coord.storage.data["player"]["current_episode_id"] == "ep_old"

    for progress in coord.storage.data["progress"].values():
        progress["played"] = True

    assert await coord.async_play_next_unplayed() is None


@pytest.mark.asyncio
async def test_media_player_seek_helpers_cover_ha_and_dlna_paths() -> None:
    """Seek helpers use HA media_seek first and DLNA fallback when needed."""
    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.SEEK)
    state = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord.storage.data["feeds"]["feed_1"] = {"feed_id": "feed_1", "enabled": True}
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }

    assert await coord._async_seek_media_player("media_player.kitchen", 12) is True
    assert coord.hass.services.calls[-1][0] == (
        "media_player",
        "media_seek",
        {"entity_id": "media_player.kitchen", "seek_position": 12.0},
    )

    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"
    coord.storage.data["player"]["current_episode_id"] = "ep_1"
    coord._async_seek_media_player = AsyncMock(return_value=False)
    coord._async_seek_via_dlna = AsyncMock(return_value=True)

    await coord.async_seek("ep_1", 42)

    coord._async_seek_media_player.assert_awaited_once_with("media_player.kitchen", 42.0)
    coord._async_seek_via_dlna.assert_awaited_once_with("media_player.kitchen", 42.0)
    assert coord.storage.data["player"]["position"] == 42


class FakeExternalControl:
    """Minimal enhanced DLNA control replacement."""

    def __init__(self, status: ExternalPlaybackStatus | Exception) -> None:
        self.status = status
        self.stopped = False
        self.paused = False
        self.played = False
        self.seek_position = None

    async def async_status(self, description_url: str) -> ExternalPlaybackStatus:
        """Return configured status."""
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    async def async_stop(self, description_url: str) -> None:
        """Record stop."""
        self.stopped = True

    async def async_pause(self, description_url: str) -> None:
        """Record pause."""
        self.paused = True

    async def async_play(self, description_url: str) -> None:
        """Record play."""
        self.played = True

    async def async_seek(self, description_url: str, position: float) -> None:
        """Record seek."""
        self.seek_position = position


@pytest.mark.asyncio
async def test_dlna_description_and_stop_safety_paths() -> None:
    """DLNA stop helper checks description URLs, action support, and media matching."""
    coord = _coordinator()
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }
    coord._set_external_session(
        episode_id="ep_1",
        target="media_player.dlna",
        target_name="DLNA",
        target_platform="dlna_dmr",
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=0,
        duration=None,
    )

    assert coord._dlna_description_url("media_player.dlna") is None
    coord._target_registry_info = lambda target: {"platform": "dlna_dmr", "description_url": "https://example.test/device.xml"}
    assert coord._dlna_description_url("media_player.dlna") == "https://example.test/device.xml"

    coord._external_control = FakeExternalControl(ExternalPlaybackStatus(state="idle"))
    assert await coord._async_stop_via_dlna("media_player.dlna") is True

    coord._external_control = FakeExternalControl(ExternalPlaybackStatus(state="playing", supported_actions={"Pause"}))
    assert await coord._async_stop_via_dlna("media_player.dlna") is False

    coord._external_control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://cdn.example.test/other.mp3",
            supported_actions={"Stop"},
        )
    )
    with pytest.raises(HomeAssistantError):
        await coord._async_stop_via_dlna("media_player.dlna")

    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://cdn.example.test/other.mp3",
            supported_actions={"Stop"},
        )
    )
    coord._external_control = control
    assert await coord._async_stop_via_dlna("media_player.dlna", force=True) is True
    assert control.stopped is True
    assert coord._external_session()["control_source"] == "dlna"


@pytest.mark.asyncio
async def test_dlna_pause_play_and_seek_helpers() -> None:
    """DLNA pause/play/seek helpers handle unsupported, failed, and successful actions."""
    coord = _coordinator()
    coord._target_registry_info = lambda target: {"platform": "dlna_dmr", "description_url": "https://example.test/device.xml"}

    coord._external_control = FakeExternalControl(ExternalPlaybackStatus(state="playing", supported_actions={"Stop"}))
    assert await coord._async_pause_via_dlna("media_player.dlna") is False
    assert await coord._async_play_via_dlna("media_player.dlna") is False
    assert await coord._async_seek_via_dlna("media_player.dlna", 12) is False

    coord._external_control = FakeExternalControl(RuntimeError("offline"))
    assert await coord._async_pause_via_dlna("media_player.dlna") is False
    assert await coord._async_play_via_dlna("media_player.dlna") is False
    assert await coord._async_seek_via_dlna("media_player.dlna", 12) is False

    control = FakeExternalControl(ExternalPlaybackStatus(state="playing", supported_actions={"Pause", "Play", "Seek"}))
    coord._external_control = control

    assert await coord._async_pause_via_dlna("media_player.dlna") is True
    assert await coord._async_play_via_dlna("media_player.dlna") is True
    assert await coord._async_seek_via_dlna("media_player.dlna", 34) is True
    assert control.paused is True
    assert control.played is True
    assert control.seek_position == 34
    assert coord._external_session()["control_source"] == "dlna"


@pytest.mark.asyncio
async def test_play_on_media_player_signed_proxy_metadata_resume_and_previous_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speaker playback builds rich metadata, seeks resume position, and stops old targets."""
    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.SEEK)
    state = SimpleNamespace(state="idle", attributes={"supported_features": features}, name="Kitchen")
    old_state = SimpleNamespace(state="idle", attributes={"supported_features": features}, name="Old Speaker")
    coord = _coordinator({"media_player.kitchen": state, "media_player.old": old_state})
    coord.storage.data["feeds"]["feed_1"] = {
        "feed_id": "feed_1",
        "title": "Feed One",
        "description": "Feed description",
        "artwork_url": "https://cdn.example.test/feed.jpg",
        "enabled": True,
    }
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "title": "Episode One",
        "description": "Episode description",
        "published": "2026-01-01T00:00:00+00:00",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "duration_seconds": 300,
        "artwork_url": "https://cdn.example.test/ep_1.jpg",
    }
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.old"
    coord.async_stop_media_player = AsyncMock()
    coord._ensure_external_polling = lambda: None
    monkeypatch.setattr("custom_components.podcast_player.coordinator.make_signed_speaker_proxy_url", lambda *args: "https://ha.example.test/proxy")
    monkeypatch.setattr("custom_components.podcast_player.coordinator.make_signed_speaker_artwork_proxy_url", lambda *args: "https://ha.example.test/artwork")
    monkeypatch.setattr("custom_components.podcast_player.coordinator.asyncio.sleep", AsyncMock())

    episode = await coord.async_play_on_media_player(
        "media_player.kitchen",
        episode_id="ep_1",
        url_mode="signed_proxy",
        media_content_type="podcast",
        resume_position=15,
    )

    assert episode["episode_id"] == "ep_1"
    coord.async_stop_media_player.assert_awaited_once_with("media_player.old")
    play_call = coord.hass.services.calls[0][0]
    seek_call = coord.hass.services.calls[1][0]
    assert play_call[1] == "play_media"
    payload = play_call[2]
    assert payload["media_content_id"] == "https://ha.example.test/proxy"
    assert payload["media_content_type"] == "podcast"
    assert payload["extra"]["thumb"] == "https://ha.example.test/artwork"
    assert payload["extra"]["original_artwork_url"] == "https://cdn.example.test/ep_1.jpg"
    assert payload["extra"]["metadata"]["description"] == "Episode description"
    assert payload["extra"]["metadata"]["releaseDate"] == "2026-01-01T00:00:00+00:00"
    assert seek_call == (
        "media_player",
        "media_seek",
        {"entity_id": "media_player.kitchen", "seek_position": 15.0},
    )
    assert coord.storage.data["player"]["position"] == 15
    assert coord.storage.data["player"]["target_media_player"] == "media_player.kitchen"
    assert coord.hass.bus.events[-1][1]["url_mode"] == "signed_proxy"


@pytest.mark.asyncio
async def test_play_on_media_player_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speaker playback reports empty selections, bad episodes, URL failures, and service rejection."""
    state = SimpleNamespace(state="idle", attributes={"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})

    with pytest.raises(HomeAssistantError):
        await coord.async_play_on_media_player("media_player.kitchen", episode_id="missing")

    coord.storage.data["episodes"]["bad"] = {"feed_id": "feed_1", "audio_url": "https://cdn.example.test/bad.mp3"}
    with pytest.raises(HomeAssistantError):
        await coord.async_play_on_media_player("media_player.kitchen", episode_id="bad")

    coord.storage.data["episodes"]["no_audio"] = {"episode_id": "no_audio", "feed_id": "feed_1"}
    with pytest.raises(HomeAssistantError):
        await coord.async_play_on_media_player("media_player.kitchen", episode_id="no_audio")

    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }
    monkeypatch.setattr("custom_components.podcast_player.coordinator.make_signed_speaker_proxy_url", lambda *args: None)
    with pytest.raises(HomeAssistantError):
        await coord.async_play_on_media_player("media_player.kitchen", episode_id="ep_1", url_mode="signed_proxy")

    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("speaker rejected"))
    with pytest.raises(HomeAssistantError):
        await coord.async_play_on_media_player("media_player.kitchen", episode_id="ep_1")

    assert coord.storage.data["player"]["speaker_last_error"] == "speaker rejected"


@pytest.mark.asyncio
async def test_speaker_pause_resume_supported_ha_services() -> None:
    """Speaker pause/resume use supported HA media_player services when available."""
    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY)
    state = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord._ensure_external_polling = lambda: None
    coord.storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "feed_id": "feed_1"}
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "ep_1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "ep_1",
            "target_media_player": "media_player.kitchen",
        }
    )

    await coord.async_pause()
    coord.hass.states._states["media_player.kitchen"] = SimpleNamespace(state="paused", attributes={"supported_features": features}, name="Kitchen")
    await coord.async_resume()

    assert coord.hass.services.calls[0][0] == ("media_player", "media_pause", {"entity_id": "media_player.kitchen"})
    assert coord.hass.services.calls[1][0] == ("media_player", "media_play", {"entity_id": "media_player.kitchen"})
    assert coord.storage.data["player"]["state"] == "playing"
    assert coord._external_session()["control_source"] == "ha"


@pytest.mark.asyncio
async def test_stop_media_player_validation_and_error_paths() -> None:
    """Stop target handling reports invalid targets and preserves failed stop errors."""
    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.STOP)
    state = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})

    with pytest.raises(HomeAssistantError):
        await coord.async_stop_media_player("sensor.bad")

    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("stop rejected"))
    coord._async_stop_via_dlna = AsyncMock(side_effect=RuntimeError("dlna failed"))

    with pytest.raises(HomeAssistantError):
        await coord.async_stop_media_player("media_player.kitchen")

    assert "HA media_stop failed: stop rejected" in coord.storage.data["player"]["speaker_last_error"]
    assert "Enhanced DLNA stop failed: dlna failed" in coord.storage.data["player"]["speaker_last_error"]


@pytest.mark.asyncio
async def test_prepare_media_source_playback_missing_and_resume_position() -> None:
    """Media Source preparation reports missing episodes and preserves resume position."""
    state = SimpleNamespace(state="idle", attributes={"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord._ensure_external_polling = lambda: None

    with pytest.raises(HomeAssistantError):
        await coord.async_prepare_media_source_playback(
            episode_id="missing",
            media_player_entity_id="media_player.kitchen",
            media_content_id="https://cdn.example.test/missing.mp3",
            media_content_type="audio/mpeg",
            url_mode="direct",
        )

    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "duration_seconds": 120,
    }
    coord.storage.data["progress"]["ep_1"] = {"episode_id": "ep_1", "position": 33, "played": False, "duration": 120}

    await coord.async_prepare_media_source_playback(
        episode_id="ep_1",
        media_player_entity_id="media_player.kitchen",
        media_content_id="https://cdn.example.test/ep_1.mp3",
        media_content_type="audio/mpeg",
        url_mode="direct",
    )

    assert coord.storage.data["player"]["position"] == 33
    assert coord.storage.data["player"]["external_session"]["position"] == 33


@pytest.mark.asyncio
async def test_coordinator_lifecycle_fetch_and_refresh_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coordinator lifecycle helpers cover success, invalid feed, and refresh branches."""
    coord = _coordinator()
    coord.async_refresh_feeds = AsyncMock()

    assert await coord._async_update_data() == coord.storage.snapshot()
    coord.async_refresh_feeds.assert_awaited_once()

    coord._session = FakeSession(FakeResponse(chunks=[b"<rss />"]))
    assert await coord.async_fetch_feed_text("https://example.test/feed.xml") == (
        "<rss />",
        "https://final.example.test/feed.xml",
    )

    with pytest.raises(PodcastParseError, match="RSS URL"):
        await coord.async_add_feed("ftp://example.test/feed.xml")

    rss_url = "https://example.test/feed.xml"
    feed_id = make_feed_id(rss_url)
    coord.storage.data["feeds"][feed_id] = {"feed_id": feed_id, "rss_url": rss_url, "title": "Existing", "enabled": True}
    coord._async_refresh_single_url = AsyncMock(
        return_value={
            "feed": {"feed_id": feed_id, "rss_url": rss_url, "title": "Existing"},
            "episodes": [],
        }
    )

    assert (await coord.async_add_feed(rss_url))["feed_id"] == feed_id
    assert not any(event[0] == EVENT_FEED_ADDED for event in coord.hass.bus.events)

    coord._async_refresh_existing_feed = AsyncMock()
    await PodcastUpdateCoordinator.async_refresh_feeds(coord, "missing")
    await PodcastUpdateCoordinator.async_refresh_feeds(coord, feed_id)

    coord._async_refresh_existing_feed.assert_awaited_once()

    coord.storage.data["player"]["external_session"]["active"] = True

    async def fake_sleep(seconds: int) -> None:
        coord.storage.data["player"]["external_session"]["active"] = False

    monkeypatch.setattr("custom_components.podcast_player.coordinator.asyncio.sleep", fake_sleep)
    await coord._async_external_poll_loop()

    assert coord._external_poll_task is None


@pytest.mark.asyncio
async def test_external_status_none_and_error_branches() -> None:
    """External status helpers handle unavailable states, bad timestamps, and poll no-ops."""
    coord = _coordinator(
        {
            "media_player.off": SimpleNamespace(state="off", attributes={}, name="Off"),
            "media_player.bad_time": SimpleNamespace(
                state="playing",
                attributes={
                    "media_position": 10,
                    "media_position_updated_at": "bad",
                    "media_duration": 20,
                },
                name="Bad Time",
            ),
        }
    )

    assert coord._ha_status_for_target("media_player.off") is None
    assert coord._ha_status_for_target("media_player.bad_time").position == 10
    assert coord._estimated_external_status() is None

    coord.storage.data["player"]["state"] = "playing"
    session = coord.storage.data["player"]["external_session"]
    session.update({"active": True, "position": 4, "duration": 20, "status_updated_at": "bad"})
    assert coord._estimated_external_status().position == 4

    coord._dlna_description_url = lambda target: "https://example.test/device.xml"
    coord._external_control = FakeExternalControl(RuntimeError("offline"))
    assert await coord._async_dlna_status("media_player.bad_time") is None

    session["active"] = False
    await coord.async_update_external_session()

    session.update({"active": True, "target_media_player": "media_player.missing"})
    coord._async_dlna_status = AsyncMock(return_value=None)
    coord._ha_status_for_target = lambda target: None
    coord._estimated_external_status = lambda: None
    await coord.async_update_external_session()

    coord.storage.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_remaining_speaker_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speaker helpers cover previous-target stop errors, seek failures, and resume seek fallback."""
    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.SEEK)
    state = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord.storage.data["feeds"]["feed_1"] = {"feed_id": "feed_1", "title": "Feed One", "enabled": True}
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "title": "Episode One",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "duration_seconds": 200,
    }
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"
    coord.async_stop_media_player = AsyncMock(side_effect=RuntimeError("stop failed"))

    with pytest.raises(HomeAssistantError):
        await coord.async_play_episode("ep_1")

    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("seek rejected"))
    assert await coord._async_seek_media_player("media_player.kitchen", 12) is False

    coord.storage.data["player"]["output_mode"] = "browser"
    coord.hass.services.calls = []

    async def fake_service_call(domain, service, data, **kwargs) -> None:
        if service == "media_seek":
            raise RuntimeError("seek rejected")
        coord.hass.services.calls.append(((domain, service, data), kwargs))

    coord.hass.services.async_call = fake_service_call
    coord._async_seek_via_dlna = AsyncMock(return_value=False)
    coord._ensure_external_polling = lambda: None
    monkeypatch.setattr("custom_components.podcast_player.coordinator.asyncio.sleep", AsyncMock())

    await coord.async_play_on_media_player("media_player.kitchen", episode_id="ep_1", resume_position=9)

    coord._async_seek_via_dlna.assert_awaited_once_with("media_player.kitchen", 9.0)
    assert coord.storage.data["player"]["position"] == 9

    coord.storage.data["player"]["position"] = "bad"
    assert coord._resume_position_for_episode("ep_1") == 9
    coord.storage.data["progress"]["ep_1"]["position"] = "bad"
    assert coord._resume_position_for_episode("ep_1") == 0


@pytest.mark.asyncio
async def test_speaker_control_error_storage_branches() -> None:
    """Speaker control error helpers store errors or clear stale unavailable targets."""
    coord = _coordinator({"media_player.kitchen": SimpleNamespace(state="off", attributes={}, name="Kitchen")})
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"

    await coord._handle_active_target_control_error("media_player.kitchen", HomeAssistantError("offline"))

    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"] == "offline"

    await coord._handle_active_target_control_error("media_player.other", HomeAssistantError("other error"))

    assert coord.storage.data["player"]["speaker_last_error"] == "other error"
    assert coord._target_is_unavailable_or_missing("sensor.bad") is True


@pytest.mark.asyncio
async def test_update_failure_polling_and_registry_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coordinator update, polling, and registry helpers cover defensive branches."""
    coord = _coordinator()
    coord.async_refresh_feeds = AsyncMock(side_effect=RuntimeError("refresh failed"))

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    coord.storage.data["player"]["external_session"]["active"] = True
    active_task = SimpleNamespace(done=lambda: False)
    coord._external_poll_task = active_task
    coord._ensure_external_polling()

    assert coord._external_poll_task is active_task

    task = asyncio.create_task(asyncio.sleep(60))
    coord._external_poll_task = task
    await coord.async_shutdown()

    assert task.cancelled()
    assert coord._external_poll_task is None

    from homeassistant.helpers import entity_registry as er

    class FakeRegistry:
        def async_get(self, entity_id: str) -> SimpleNamespace:
            return SimpleNamespace(platform="dlna_dmr", supported_features=int(MediaPlayerEntityFeature.PLAY_MEDIA), config_entry_id="entry_1")

    coord = _coordinator({"media_player.dlna": SimpleNamespace(state="idle", attributes={}, name="DLNA")})
    coord.hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: SimpleNamespace(data={"url": "https://example.test/device.xml"})
    )
    monkeypatch.setattr(er, "async_get", lambda hass: FakeRegistry())

    info = coord._target_registry_info("media_player.dlna")
    assert info["platform"] == "dlna_dmr"
    assert info["supported_features"] == int(MediaPlayerEntityFeature.PLAY_MEDIA)
    assert info["description_url"] == "https://example.test/device.xml"

    coord._target_status = lambda entity_id: {}
    coord._target_registry_info = lambda entity_id: {"platform": "demo_platform"}
    assert coord.media_source_target_status("media_player.dlna")["platform"] == "demo_platform"


def test_apply_external_status_idle_and_no_episode_branches() -> None:
    """External status application ignores empty sessions and clears idle sessions."""
    coord = _coordinator()

    coord._apply_external_status(ExternalPlaybackStatus(state="playing", position=5))

    assert coord.storage.data["progress"] == {}

    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }
    coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform=None,
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=0,
        duration=100,
    )

    coord._apply_external_status(ExternalPlaybackStatus(state="idle", position=12, duration=100))

    assert coord.storage.data["progress"]["ep_1"]["position"] == 12
    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord._external_session()["active"] is False


@pytest.mark.asyncio
async def test_external_update_retries_starting_idle_status() -> None:
    """Startup idle reports are normalized to buffering and retried once."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord.storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "duration_seconds": 120,
    }
    coord.storage.data["player"]["state"] = "playing"
    coord._set_external_session(
        episode_id="ep_1",
        target="media_player.kitchen",
        target_name="Kitchen",
        target_platform=None,
        media_content_id="https://cdn.example.test/ep_1.mp3",
        resume_position=7,
        duration=120,
    )
    coord._async_dlna_status = AsyncMock(return_value=ExternalPlaybackStatus(state="unknown"))
    coord._async_retry_external_start = AsyncMock(return_value=True)

    await coord.async_update_external_session()

    coord._async_retry_external_start.assert_awaited_once()
    assert coord._external_session()["transport_state"] == "buffering"
    assert coord.storage.data["progress"]["ep_1"]["position"] == 7
    coord.storage.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_speaker_pause_resume_and_seek_error_fallbacks() -> None:
    """Speaker pause, resume, and seek handle unsupported or unavailable targets."""
    state = SimpleNamespace(state="playing", attributes={"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": state})
    coord.storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "feed_id": "feed_1"}
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "ep_1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"

    with pytest.raises(HomeAssistantError):
        await coord.async_pause()

    assert coord.storage.data["player"]["speaker_last_error"] == "target_pause_unsupported"

    features = int(MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.PAUSE)
    coord.hass.states._states["media_player.kitchen"] = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen")
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("pause rejected"))

    with pytest.raises(HomeAssistantError):
        await coord.async_pause()

    coord.hass.states._states["media_player.kitchen"] = SimpleNamespace(state="paused", attributes={"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}, name="Kitchen")
    coord.hass.services.async_call = AsyncMock()

    with pytest.raises(HomeAssistantError):
        await coord.async_resume()

    coord.hass.states._states["media_player.kitchen"] = SimpleNamespace(state="unavailable", attributes={}, name="Kitchen")
    coord._async_seek_media_player = AsyncMock(return_value=False)
    coord._async_seek_via_dlna = AsyncMock(return_value=False)

    await coord.async_seek("ep_1", 22)

    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert "not available for seek" in coord.storage.data["player"]["speaker_last_error"]


@pytest.mark.asyncio
async def test_stop_and_dlna_validation_branches() -> None:
    """Stop and target validation helpers cover no-target, inactive, and disabled-DLNA paths."""
    coord = _coordinator()

    with pytest.raises(HomeAssistantError):
        await coord.async_stop_media_player()

    coord.storage.data["player"]["external_session"].update({"active": True, "target_media_player": "media_player.kitchen"})
    coord.async_stop_media_player = AsyncMock()
    await coord.async_stop()
    coord.async_stop_media_player.assert_awaited_once_with("media_player.kitchen", force=False)

    idle_state = SimpleNamespace(state="idle", attributes={"supported_features": int(MediaPlayerEntityFeature.PLAY_MEDIA)}, name="Kitchen")
    coord = _coordinator({"media_player.kitchen": idle_state})
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen"
    coord.storage.data["player"]["external_session"].update({"active": True, "target_media_player": "media_player.kitchen"})
    coord._async_stop_via_dlna = AsyncMock(return_value=False)
    coord._async_dlna_status = AsyncMock(return_value=ExternalPlaybackStatus(state="idle"))

    await coord.async_stop_media_player("media_player.kitchen")

    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"] is None

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_control_target("sensor.bad", "seek")
    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_control_target("media_player.missing", "seek")

    coord.hass.states._states["media_player.unavailable"] = SimpleNamespace(state="unavailable", attributes={}, name="Unavailable")
    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_control_target("media_player.unavailable", "seek")

    coord.storage.data["settings"]["enhanced_dlna_controls"] = False
    coord._target_registry_info = lambda target: {"platform": "dlna_dmr", "description_url": "https://example.test/device.xml"}

    assert coord._dlna_description_url("media_player.unavailable") is None
