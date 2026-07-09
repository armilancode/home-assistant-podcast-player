"""Tests for media-player output target classification."""

from types import SimpleNamespace

from homeassistant.components.media_player import MediaPlayerEntityFeature

from custom_components.podcast_player.const import PLAYER_ENTITY_ID
from custom_components.podcast_player.targets import (
    _feature_enabled,
    _supported_features,
    is_external_media_player_entity_id,
    output_target_status,
    target_name,
)


def _state(entity_id: str = "media_player.kitchen", state: str = "idle", **attrs) -> SimpleNamespace:
    """Return a minimal HA state object."""
    return SimpleNamespace(entity_id=entity_id, state=state, name="Kitchen", attributes=attrs)


def test_target_helper_functions_handle_invalid_values() -> None:
    """Target helper functions are defensive around malformed state data."""
    assert _feature_enabled("bad", object()) is False
    assert _supported_features(_state(supported_features="bad")) == 0
    assert _supported_features(_state(), supported_features="bad") == 0
    assert target_name("media_player.kitchen", _state(friendly_name="Kitchen Speaker")) == "Kitchen Speaker"
    assert target_name("media_player.kitchen", None) == "kitchen"
    assert is_external_media_player_entity_id("media_player.kitchen") is True
    assert is_external_media_player_entity_id(PLAYER_ENTITY_ID) is False
    assert is_external_media_player_entity_id("sensor.kitchen") is False


def test_output_target_rejects_non_external_and_missing_entities() -> None:
    """Only external media_player entities are playable targets."""
    self_status = output_target_status(PLAYER_ENTITY_ID, _state(entity_id=PLAYER_ENTITY_ID))
    missing_status = output_target_status("media_player.missing", None)

    assert self_status["status"] == "unsupported"
    assert self_status["reason"] == "Target must be an external media_player entity."
    assert self_status["capabilities"]["play_media"] is False
    assert self_status["notes"] == ["Not an external media player"]

    assert missing_status["status"] == "unavailable"
    assert missing_status["reason"] == "missing is not currently available in Home Assistant."
    assert missing_status["notes"] == ["Entity not found"]


def test_output_target_without_feature_enum_defaults_to_generic_ready(monkeypatch) -> None:
    """If HA feature enums are unavailable, targets stay generically playable."""
    monkeypatch.setattr("custom_components.podcast_player.targets._media_player_features", lambda: None)

    status = output_target_status("media_player.kitchen", _state())

    assert status["status"] == "ready"
    assert status["playable"] is True
    assert status["capabilities"]["play_media"] is True
    assert status["capabilities"]["seek"] == "none"
    assert status["notes"] == ["Does not report live progress", "Generic HA media player"]


def test_output_target_reports_supported_and_enhanced_dlna_controls() -> None:
    """Target status exposes HA-supported controls and enhanced DLNA fallback controls."""
    features = int(
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.SEEK
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
    )
    supported = output_target_status(
        "media_player.kitchen",
        _state(media_position=12, media_duration=120, supported_features=features),
        supported_features=features,
    )
    enhanced = output_target_status(
        "media_player.dlna",
        _state(),
        platform="dlna_dmr",
        supported_features=int(MediaPlayerEntityFeature.PLAY_MEDIA),
        enhanced_dlna_controls=True,
    )

    assert supported["status"] == "ready"
    assert supported["capabilities"]["progress"] is True
    assert supported["capabilities"]["seek"] == "supported"
    assert supported["capabilities"]["pause"] == "supported"
    assert supported["capabilities"]["resume"] == "supported"
    assert supported["capabilities"]["stop"] == "supported"

    assert enhanced["status"] == "ready"
    assert enhanced["capabilities"]["progress"] is True
    assert enhanced["capabilities"]["seek"] == "best_effort"
    assert enhanced["capabilities"]["pause"] == "best_effort"
    assert enhanced["capabilities"]["resume"] == "best_effort"
    assert enhanced["capabilities"]["stop"] == "best_effort"
    assert enhanced["capabilities"]["raw_avtransport"] is True
    assert enhanced["notes"] == ["Progress can use enhanced DLNA", "Enhanced DLNA controls available"]
