"""Tests for coordinator feed, session, and selection helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.podcast_player.const import (
    EVENT_FEED_ADDED,
    EVENT_FEED_REFRESH_FAILED,
    EVENT_NEW_EPISODE,
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
    with pytest.raises(UpdateFailed, match="boom"):
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
    coord._set_external_session(
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

    with pytest.raises(HomeAssistantError, match="No podcast episode"):
        await coord.async_resume()
