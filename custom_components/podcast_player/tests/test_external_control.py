"""Tests for external media-player control helpers."""

from unittest.mock import AsyncMock

import pytest

from custom_components.podcast_player.external_control import (
    DlnaAvTransportController,
    ExternalPlaybackStatus,
    current_media_matches_session,
    seconds_from_upnp_time,
    state_from_transport,
    upnp_time_from_seconds,
)


def test_upnp_time_conversion() -> None:
    """DLNA time helpers convert supported values safely."""
    assert seconds_from_upnp_time("01:02:03") == 3723
    assert seconds_from_upnp_time("00:00:05.000") == 5
    assert seconds_from_upnp_time("NOT_IMPLEMENTED") is None
    assert seconds_from_upnp_time("NOT_IMPLEMENTED_") is None
    assert seconds_from_upnp_time("") is None
    assert seconds_from_upnp_time("01:02") is None
    assert seconds_from_upnp_time("bad:value:here") is None
    assert seconds_from_upnp_time("-01:00:00") == 0
    assert upnp_time_from_seconds(3723) == "01:02:03"
    assert upnp_time_from_seconds(-5) == "00:00:00"


def test_transport_state_mapping() -> None:
    """DLNA transport states map to app playback states."""
    assert state_from_transport("PLAYING") == "playing"
    assert state_from_transport("PAUSED_PLAYBACK") == "paused"
    assert state_from_transport("TRANSITIONING") == "buffering"
    assert state_from_transport("STOPPED") == "idle"
    assert state_from_transport("NO_MEDIA_PRESENT") == "idle"
    assert state_from_transport(None) == "unknown"
    assert state_from_transport("something_else") == "unknown"


def test_current_media_matches_session_without_leaking_tokens() -> None:
    """Safe-stop matching accepts direct and signed proxy URLs."""
    assert current_media_matches_session("https://example.test/audio.mp3", "https://example.test/audio.mp3", "ep_1") is True
    assert current_media_matches_session("https://ha.test/api/podcast_player/speaker_proxy/ep_1?token=secret", None, "ep_1") is True
    assert current_media_matches_session("https://example.test/other.mp3", "https://example.test/audio.mp3", "ep_1") is False
    assert current_media_matches_session("https://example.test/other.mp3", None, None) is False
    assert current_media_matches_session(None, "https://example.test/audio.mp3", "ep_1") is None


def test_external_playback_status_properties() -> None:
    """External status convenience properties reflect supported actions."""
    status = ExternalPlaybackStatus(state="playing", supported_actions={"Stop", "Pause", "Seek"})

    assert status.is_active is True
    assert status.can_stop is True
    assert status.can_pause is True
    assert status.can_seek is True
    assert ExternalPlaybackStatus(state="idle").is_active is False
    assert ExternalPlaybackStatus(state="playing").can_stop is False


class FakeAction:
    """Minimal UPnP action replacement."""

    def __init__(self, result: dict | Exception | None = None) -> None:
        self.result = result or {}
        self.calls: list[dict] = []

    async def async_call(self, **kwargs):
        """Record action call and return the configured result."""
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeService:
    """Minimal UPnP service replacement."""

    def __init__(self, actions: dict[str, FakeAction]) -> None:
        self.actions = actions

    def action(self, name: str) -> FakeAction:
        """Return a fake action by name."""
        return self.actions[name]


@pytest.mark.asyncio
async def test_dlna_status_reads_transport_position_and_actions() -> None:
    """DLNA status combines transport, position, and supported action data."""
    service = FakeService(
        {
            "GetTransportInfo": FakeAction({"CurrentTransportState": "PLAYING"}),
            "GetPositionInfo": FakeAction(
                {
                    "RelTime": "00:01:05",
                    "TrackDuration": "01:00:00",
                    "TrackURI": "https://example.test/audio.mp3",
                }
            ),
            "GetCurrentTransportActions": FakeAction({"Actions": "Stop, Pause, Seek"}),
        }
    )
    controller = DlnaAvTransportController()
    controller._async_service = AsyncMock(return_value=service)

    status = await controller.async_status("https://example.test/device.xml")

    assert status.state == "playing"
    assert status.transport_state == "PLAYING"
    assert status.position == 65
    assert status.duration == 3600
    assert status.current_media_id == "https://example.test/audio.mp3"
    assert status.supported_actions == {"Stop", "Pause", "Seek"}
    assert status.progress_source == "dlna"
    assert status.control_source == "dlna"


@pytest.mark.asyncio
async def test_dlna_status_tolerates_partial_failures_and_rejects_empty_status() -> None:
    """DLNA status ignores individual action failures but rejects fully empty status."""
    service = FakeService(
        {
            "GetTransportInfo": FakeAction(RuntimeError("transport failed")),
            "GetPositionInfo": FakeAction({"RelTime": "00:00:12"}),
            "GetCurrentTransportActions": FakeAction(RuntimeError("actions failed")),
        }
    )
    controller = DlnaAvTransportController()
    controller._async_service = AsyncMock(return_value=service)

    status = await controller.async_status("https://example.test/device.xml")

    assert status.state == "unknown"
    assert status.position == 12
    assert status.supported_actions == set()

    empty_service = FakeService(
        {
            "GetTransportInfo": FakeAction(RuntimeError("transport failed")),
            "GetPositionInfo": FakeAction(RuntimeError("position failed")),
            "GetCurrentTransportActions": FakeAction(RuntimeError("actions failed")),
        }
    )
    controller._async_service = AsyncMock(return_value=empty_service)

    with pytest.raises(RuntimeError, match="transport status"):
        await controller.async_status("https://example.test/device.xml")


@pytest.mark.asyncio
async def test_dlna_control_actions_call_upnp_service() -> None:
    """DLNA control methods send the expected AVTransport action calls."""
    service = FakeService(
        {
            "Stop": FakeAction(),
            "Pause": FakeAction(),
            "Play": FakeAction(),
            "Seek": FakeAction(),
        }
    )
    controller = DlnaAvTransportController()
    controller._async_service = AsyncMock(return_value=service)

    await controller.async_stop("https://example.test/device.xml")
    await controller.async_pause("https://example.test/device.xml")
    await controller.async_play("https://example.test/device.xml")
    await controller.async_seek("https://example.test/device.xml", 65)

    assert service.actions["Stop"].calls == [{"InstanceID": 0}]
    assert service.actions["Pause"].calls == [{"InstanceID": 0}]
    assert service.actions["Play"].calls == [{"InstanceID": 0, "Speed": "1"}]
    assert service.actions["Seek"].calls == [{"InstanceID": 0, "Unit": "REL_TIME", "Target": "00:01:05"}]
