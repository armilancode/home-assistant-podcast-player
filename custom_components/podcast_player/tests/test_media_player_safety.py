"""Regression tests for safe media-player target handling."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.podcast_player.const import PLAYER_ENTITY_ID
from custom_components.podcast_player.coordinator import PodcastUpdateCoordinator
from custom_components.podcast_player.external_control import ExternalPlaybackStatus
from custom_components.podcast_player.storage import default_data


class FakeStates:
    """Minimal hass.states replacement."""

    def __init__(self, state: object | dict[str, object | None] | None) -> None:
        self._state = state

    def get(self, entity_id: str) -> object | None:
        """Return configured fake state."""
        if isinstance(self._state, dict):
            return self._state.get(entity_id)
        return self._state


class FakeServices:
    """Minimal hass.services replacement."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    @property
    def called(self) -> bool:
        """Return true if any service was called."""
        return bool(self.calls)

    async def async_call(self, *args, **kwargs) -> None:
        """Record service calls."""
        self.calls.append((args, kwargs))


class FakeBus:
    """Minimal hass.bus replacement."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event_data: dict) -> None:
        """Record fired events."""
        self.events.append((event_type, event_data))


class FakeStorage:
    """Minimal storage object."""

    def __init__(self) -> None:
        self.data = default_data()
        self.saved = False

    async def async_save(self) -> None:
        """Record saves."""
        self.saved = True

    def snapshot(self) -> dict:
        """Return current fake data."""
        return self.data

    def set_player_state(self, state: str, episode_id: str | None = None) -> None:
        """Set fake player state."""
        self.data["player"]["state"] = state
        if episode_id is not None:
            self.data["player"]["current_episode_id"] = episode_id

    def get_progress(self, episode_id: str) -> dict:
        """Return fake progress."""
        return self.data["progress"].setdefault(
            episode_id,
            {"episode_id": episode_id, "played": False, "position": 0, "duration": None, "last_played_at": None},
        )

    def get_episode(self, episode_id: str | None) -> dict | None:
        """Return fake episode."""
        if not episode_id:
            return None
        return self.data["episodes"].get(episode_id)

    def get_feed(self, feed_id: str | None) -> dict | None:
        """Return fake feed."""
        if not feed_id:
            return None
        return self.data["feeds"].get(feed_id)

    def save_progress(self, episode_id: str, position: float, duration=None, playing=None, speed=None) -> dict:
        """Save fake progress."""
        progress = self.get_progress(episode_id)
        progress["position"] = position
        progress["playing"] = playing
        return progress


def _coordinator(state: object | None) -> PodcastUpdateCoordinator:
    coord = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coord.storage = FakeStorage()
    coord.hass = SimpleNamespace(states=FakeStates(state), services=FakeServices(), bus=FakeBus())
    coord._external_control = None
    coord._external_poll_task = None
    coord.async_set_updated_data = lambda data: None
    return coord


class FakeExternalControl:
    """Fake enhanced external control adapter."""

    def __init__(self, status: ExternalPlaybackStatus) -> None:
        self.status = status
        self.stopped = False
        self.seek_position = None
        self.paused = False
        self.played = False

    async def async_status(self, description_url: str) -> ExternalPlaybackStatus:
        """Return configured status."""
        return self.status

    async def async_stop(self, description_url: str) -> None:
        """Record stop."""
        self.stopped = True

    async def async_seek(self, description_url: str, position: float) -> None:
        """Record seek."""
        self.seek_position = position

    async def async_pause(self, description_url: str) -> None:
        """Record pause."""
        self.paused = True
        self.status.state = "paused"

    async def async_play(self, description_url: str) -> None:
        """Record play."""
        self.played = True
        self.status.state = "playing"


def _enable_fake_dlna(coord: PodcastUpdateCoordinator, control: FakeExternalControl) -> None:
    """Enable fake enhanced DLNA control."""
    coord._external_control = control
    coord._dlna_description_url = lambda target: "http://example.test/device.xml"  # type: ignore[method-assign]
    original_target_status = coord._target_status

    def target_status(entity_id: str, state=None):
        status = original_target_status(entity_id, state)
        capabilities = status.setdefault("capabilities", {})
        capabilities["pause"] = "best_effort"
        capabilities["resume"] = "best_effort"
        capabilities["seek"] = "best_effort"
        capabilities["stop"] = "best_effort"
        capabilities["raw_avtransport"] = True
        return status

    coord._target_status = target_status  # type: ignore[method-assign]


def test_playback_target_off_fails_before_service_call() -> None:
    """An off target must fail before Podcast Player calls play_media."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target("media_player.kitchen_speaker")

    assert not coord.hass.services.called


def test_self_media_player_target_fails_before_service_call() -> None:
    """Podcast Player must never send media services to its own status entity."""
    state = SimpleNamespace(state="idle", attributes={}, name="Podcast Player")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target(PLAYER_ENTITY_ID)

    assert not coord.hass.services.called


def test_playback_target_without_play_media_feature_fails_before_service_call() -> None:
    """A target that advertises features without PLAY_MEDIA must fail early."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    play_media = int(MediaPlayerEntityFeature.PLAY_MEDIA)
    unsupported_features = 1
    while unsupported_features & play_media:
        unsupported_features <<= 1
    state = SimpleNamespace(state="idle", attributes={"supported_features": unsupported_features}, name="Limited Player")
    coord = _coordinator(state)

    with pytest.raises(HomeAssistantError):
        coord._validate_media_player_output_target("media_player.limited")

    assert not coord.hass.services.called


def test_playback_metadata_includes_dlna_artist_and_album_fields() -> None:
    """Speaker playback sends podcast metadata in the HA DLNA-compatible metadata object."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["feeds"]["feed-1"] = {
        "feed_id": "feed-1",
        "title": "Example Podcast",
        "description": "Example feed",
    }
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "feed_id": "feed-1",
        "title": "Example Episode",
        "description": "Episode summary",
        "published": "2026-06-29T10:00:00+00:00",
        "audio_url": "https://example.test/episode.mp3",
    }

    asyncio.run(
        coord.async_play_on_media_player(
            "media_player.kitchen_speaker",
            episode_id="episode-1",
        )
    )

    args, kwargs = coord.hass.services.calls[0]
    payload = args[2]
    extra = payload["extra"]
    metadata = extra["metadata"]

    assert kwargs == {"blocking": True}
    assert payload["media_content_id"] == "https://example.test/episode.mp3"
    assert payload["media_content_type"] == "music"
    assert extra["title"] == "Example Episode"
    assert metadata["title"] == "Example Episode"
    assert metadata["artist"] == "Example Podcast"
    assert metadata["albumName"] == "Example Podcast"
    assert metadata["creator"] == "Example Podcast"
    assert metadata["publisher"] == "Example Podcast"
    assert metadata["description"] == "Episode summary"
    assert metadata["subtitle"] == "Episode summary"
    assert metadata["releaseDate"] == "2026-06-29T10:00:00+00:00"


def test_play_on_media_player_creates_sanitized_external_session() -> None:
    """External playback creates backend-owned session state without raw URLs."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["feeds"]["feed-1"] = {"feed_id": "feed-1", "title": "Example Podcast"}
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "feed_id": "feed-1",
        "title": "Example Episode",
        "audio_url": "https://example.test/episode.mp3",
        "duration_seconds": 3600,
    }

    asyncio.run(coord.async_play_on_media_player("media_player.kitchen_speaker", episode_id="episode-1"))

    player = coord.storage.data["player"]
    session = player["external_session"]
    assert player["output_mode"] == "speaker"
    assert session["active"] is True
    assert session["episode_id"] == "episode-1"
    assert session["target_media_player"] == "media_player.kitchen_speaker"
    assert session["position"] == 0
    assert session["duration"] == 3600
    assert session["media_content_id_hash"]
    assert session["media_content_id_hash"] != "https://example.test/episode.mp3"


def test_stop_unavailable_active_target_clears_without_service_call() -> None:
    """Stopping an unavailable active target clears local state without direct internals."""
    state = SimpleNamespace(state="unavailable", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_stop_media_player("media_player.kitchen_speaker"))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.saved
    assert not coord.hass.services.called


def test_stop_missing_inactive_target_clears_without_service_call() -> None:
    """Stopping a missing inactive target is idempotent and must not call media_stop."""
    coord = _coordinator(None)
    coord.storage.data["player"]["last_target_media_player"] = "media_player.old_speaker"

    asyncio.run(coord.async_stop_media_player())

    assert coord.storage.saved
    assert coord.storage.data["player"]["speaker_last_error"] is None
    assert not coord.hass.services.called


def test_stop_idle_active_target_clears_without_service_call() -> None:
    """Stopping an already-idle active target clears local speaker output."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "paused"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_stop_media_player("media_player.kitchen_speaker"))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"] is None
    assert coord.storage.saved
    assert not coord.hass.services.called


def test_stop_active_target_without_stop_feature_clears_without_service_call() -> None:
    """A no-stop active target must not be fake-cleared as stopped."""
    state = SimpleNamespace(state="paused", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "paused"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_stop_media_player("media_player.kitchen_speaker"))

    assert coord.storage.data["player"]["state"] == "paused"
    assert coord.storage.data["player"]["output_mode"] == "speaker"
    assert coord.storage.data["player"]["speaker_last_error"]
    assert coord.storage.saved
    assert not coord.hass.services.called


def test_stop_uses_backend_session_target_with_dlna_fallback() -> None:
    """Stopping from another device uses the backend session target even if flat state was lost."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    session = coord.storage.data["player"]["external_session"]
    session.update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
            "target_media_player_name": "Kitchen Speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Stop", "Seek"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_stop_media_player())

    assert control.stopped is True
    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["external_session"]["active"] is False
    assert not coord.hass.services.called


def test_stop_protects_unrelated_dlna_media_without_force() -> None:
    """Safe-stop refuses to stop unrelated target media."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://example.test/other.mp3",
            supported_actions={"Stop"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_stop_media_player())

    assert control.stopped is False
    assert coord.storage.data["player"]["external_session"]["active"] is True


def test_stop_force_allows_unrelated_dlna_media() -> None:
    """Force stop explicitly overrides safe-stop protection."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://example.test/other.mp3",
            supported_actions={"Stop"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_stop_media_player(force=True))

    assert control.stopped is True


def test_stop_active_target_with_stop_feature_calls_media_stop() -> None:
    """A target that advertises STOP receives the supported HA media_stop action."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    features = int(MediaPlayerEntityFeature.STOP)
    state = SimpleNamespace(state="playing", attributes={"supported_features": features}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_stop_media_player("media_player.kitchen_speaker"))

    args, kwargs = coord.hass.services.calls[0]
    assert args == ("media_player", "media_stop", {"entity_id": "media_player.kitchen_speaker"})
    assert kwargs == {"blocking": True}
    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"


def test_pause_off_active_target_clears_without_service_call() -> None:
    """Pausing an off active target clears stale speaker state without media_pause."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_pause())

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_pause_dlna_best_effort_skips_unsupported_ha_pause() -> None:
    """A DLNA target without HA pause support uses enhanced DLNA directly."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Pause", "Play", "Stop", "Seek"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_pause())

    assert control.paused is True
    assert coord.storage.data["player"]["state"] == "paused"
    assert coord.storage.data["player"]["external_session"]["transport_state"] == "paused"
    assert coord.storage.data["player"]["external_session"]["control_source"] == "dlna"
    assert not coord.hass.services.called


def test_pause_dlna_without_pause_action_reports_clean_error() -> None:
    """A DLNA target that cannot pause returns a Podcast Player error without HA validation noise."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Stop"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    with pytest.raises(HomeAssistantError, match="does not support pause"):
        asyncio.run(coord.async_pause())

    assert control.paused is False
    assert coord.storage.data["player"]["state"] == "playing"
    assert coord.storage.data["player"]["output_mode"] == "speaker"
    assert "does not support pause" in coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_resume_off_active_target_clears_without_service_call() -> None:
    """Resuming an off active target clears stale speaker state without media_play."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "paused"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    with pytest.raises(HomeAssistantError):
        asyncio.run(coord.async_resume())

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["player"]["speaker_last_error"]
    assert not coord.hass.services.called


def test_resume_dlna_best_effort_skips_unsupported_ha_play() -> None:
    """A DLNA target without HA play support uses enhanced DLNA directly."""
    state = SimpleNamespace(state="paused", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "paused"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
            "transport_state": "paused",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="paused",
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Play", "Stop", "Seek"},
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_resume())

    assert control.played is True
    assert coord.storage.data["player"]["state"] == "playing"
    assert coord.storage.data["player"]["external_session"]["transport_state"] == "playing"
    assert coord.storage.data["player"]["external_session"]["control_source"] == "dlna"
    assert not coord.hass.services.called


def test_seek_off_active_target_saves_progress_without_service_call() -> None:
    """Seeking while the active target is off saves progress and avoids media_seek."""
    state = SimpleNamespace(state="off", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"

    asyncio.run(coord.async_seek("episode-1", 42))

    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
    assert coord.storage.data["progress"]["episode-1"]["position"] == 42
    assert not coord.hass.services.called


def test_external_session_poll_saves_dlna_progress() -> None:
    """Backend polling saves DLNA progress for all cards/devices."""
    state = SimpleNamespace(state="playing", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="playing",
            transport_state="PLAYING",
            position=88,
            duration=3600,
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Stop", "Seek", "Pause"},
            progress_source="dlna",
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_update_external_session())

    session = coord.storage.data["player"]["external_session"]
    assert session["position"] == 88
    assert session["duration"] == 3600
    assert session["progress_source"] == "dlna"
    assert coord.storage.data["progress"]["episode-1"]["position"] == 88


def test_external_session_startup_idle_dlna_retries_play_without_clearing() -> None:
    """A fresh DLNA session may report idle before it starts; keep it active and retry Play."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
            "target_media_player_name": "Kitchen Speaker",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "position": 0,
            "duration": 3600,
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="idle",
            transport_state="STOPPED",
            position=0,
            duration=3600,
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Play", "Seek"},
            progress_source="dlna",
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_update_external_session())

    session = coord.storage.data["player"]["external_session"]
    assert control.played is True
    assert session["active"] is True
    assert session["transport_state"] == "buffering"
    assert session["target_media_player"] == "media_player.kitchen_speaker"
    assert coord.storage.data["player"]["state"] == "playing"
    assert coord.storage.data["player"]["output_mode"] == "speaker"
    assert coord.storage.data["player"]["target_media_player"] == "media_player.kitchen_speaker"


def test_external_session_idle_after_startup_grace_clears_session() -> None:
    """An idle external target is only treated as ended after the startup grace window."""
    state = SimpleNamespace(state="idle", attributes={}, name="Kitchen Speaker")
    coord = _coordinator(state)
    coord.storage.data["episodes"]["episode-1"] = {
        "episode_id": "episode-1",
        "audio_url": "https://example.test/episode.mp3",
    }
    coord.storage.data["player"]["state"] = "playing"
    coord.storage.data["player"]["current_episode_id"] = "episode-1"
    coord.storage.data["player"]["output_mode"] = "speaker"
    coord.storage.data["player"]["target_media_player"] = "media_player.kitchen_speaker"
    coord.storage.data["player"]["external_session"].update(
        {
            "active": True,
            "episode_id": "episode-1",
            "target_media_player": "media_player.kitchen_speaker",
            "target_media_player_name": "Kitchen Speaker",
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
            "duration": 3600,
        }
    )
    control = FakeExternalControl(
        ExternalPlaybackStatus(
            state="idle",
            transport_state="STOPPED",
            position=0,
            duration=3600,
            current_media_id="https://example.test/episode.mp3",
            supported_actions={"Play", "Seek"},
            progress_source="dlna",
            control_source="dlna",
        )
    )
    _enable_fake_dlna(coord, control)

    asyncio.run(coord.async_update_external_session())

    assert control.played is False
    assert coord.storage.data["player"]["external_session"]["active"] is False
    assert coord.storage.data["player"]["state"] == "idle"
    assert coord.storage.data["player"]["output_mode"] == "browser"
