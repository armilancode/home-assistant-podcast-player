"""Tests for external media-player control helpers."""

from custom_components.podcast_player.external_control import (
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
    assert upnp_time_from_seconds(3723) == "01:02:03"


def test_transport_state_mapping() -> None:
    """DLNA transport states map to app playback states."""
    assert state_from_transport("PLAYING") == "playing"
    assert state_from_transport("PAUSED_PLAYBACK") == "paused"
    assert state_from_transport("STOPPED") == "idle"
    assert state_from_transport("NO_MEDIA_PRESENT") == "idle"
    assert state_from_transport("something_else") == "unknown"


def test_current_media_matches_session_without_leaking_tokens() -> None:
    """Safe-stop matching accepts direct and signed proxy URLs."""
    assert current_media_matches_session("https://example.test/audio.mp3", "https://example.test/audio.mp3", "ep_1") is True
    assert current_media_matches_session("https://ha.test/api/podcast_player/speaker_proxy/ep_1?token=secret", None, "ep_1") is True
    assert current_media_matches_session("https://example.test/other.mp3", "https://example.test/audio.mp3", "ep_1") is False
    assert current_media_matches_session(None, "https://example.test/audio.mp3", "ep_1") is None
