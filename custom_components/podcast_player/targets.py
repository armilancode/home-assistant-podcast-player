"""Media-player output target classification helpers."""

from __future__ import annotations

from typing import Any

from .const import PLAYER_ENTITY_ID

UNAVAILABLE_MEDIA_PLAYER_STATES = {"unavailable", "unknown", "off"}


def _feature_enabled(features: int, flag: Any) -> bool:
    """Return true when a Home Assistant media-player feature flag is present."""
    try:
        return bool(int(features or 0) & int(flag))
    except Exception:  # noqa: BLE001
        return False


def _media_player_features() -> Any:
    """Return HA media-player feature enum when available."""
    try:
        from homeassistant.components.media_player import MediaPlayerEntityFeature
    except Exception:  # noqa: BLE001
        return None
    return MediaPlayerEntityFeature


def _supported_features(state: Any, supported_features: int | None = None) -> int:
    """Return supported media-player feature bitmask from a state object."""
    if supported_features is not None:
        try:
            return int(supported_features or 0)
        except (TypeError, ValueError):
            return 0
    attrs = dict(getattr(state, "attributes", {}) or {})
    try:
        return int(attrs.get("supported_features") or 0)
    except (TypeError, ValueError):
        return 0


def target_name(entity_id: str, state: Any | None) -> str:
    """Return a user-facing target name."""
    attrs = dict(getattr(state, "attributes", {}) or {}) if state is not None else {}
    return str(attrs.get("friendly_name") or getattr(state, "name", None) or entity_id.replace("media_player.", ""))


def is_external_media_player_entity_id(entity_id: str) -> bool:
    """Return true for output media-player entities owned by other integrations."""
    return entity_id.startswith("media_player.") and entity_id != PLAYER_ENTITY_ID


def output_target_status(
    entity_id: str,
    state: Any | None,
    platform: str | None = None,
    supported_features: int | None = None,
    enhanced_dlna_controls: bool = False,
) -> dict[str, Any]:
    """Return frontend-safe output status and capabilities for a media player."""
    name = target_name(entity_id, state)
    if not is_external_media_player_entity_id(entity_id):
        return {
            "entity_id": entity_id,
            "name": name,
            "state": None,
            "status": "unsupported",
            "status_label": "Unsupported",
            "playable": False,
            "available": False,
            "reason": "Target must be an external media_player entity.",
            "capabilities": {
                "play_media": False,
                "live_state": False,
                "progress": False,
                "seek": "none",
                "pause": "none",
                "resume": "none",
                "stop": "none",
                "speed": False,
                "artwork": "metadata",
                "limited_controls": True,
                "raw_avtransport": False,
            },
            "notes": ["Not an external media player"],
        }

    if state is None:
        return {
            "entity_id": entity_id,
            "name": name,
            "state": None,
            "status": "unavailable",
            "status_label": "Unavailable",
            "playable": False,
            "available": False,
            "reason": f"{name} is not currently available in Home Assistant.",
            "capabilities": {
                "play_media": False,
                "live_state": False,
                "progress": False,
                "seek": "none",
                "pause": "none",
                "resume": "none",
                "stop": "none",
                "speed": False,
                "artwork": "metadata",
                "limited_controls": True,
                "raw_avtransport": False,
            },
            "notes": ["Entity not found"],
        }

    attrs = dict(getattr(state, "attributes", {}) or {})
    state_value = str(getattr(state, "state", None) or "unknown")
    features = _supported_features(state, supported_features)
    feature_enum = _media_player_features()
    can_seek = False
    can_pause = False
    can_play = False
    can_stop = False
    can_play_media = True
    if feature_enum is not None:
        can_seek = _feature_enabled(features, getattr(feature_enum, "SEEK", 0))
        can_pause = _feature_enabled(features, getattr(feature_enum, "PAUSE", 0))
        can_play = _feature_enabled(features, getattr(feature_enum, "PLAY", 0))
        can_stop = _feature_enabled(features, getattr(feature_enum, "STOP", 0))
        can_play_media = _feature_enabled(features, getattr(feature_enum, "PLAY_MEDIA", 0)) if features else True

    live_state = state_value not in UNAVAILABLE_MEDIA_PLAYER_STATES
    is_dlna = platform == "dlna_dmr"
    ha_progress = live_state and (
        attrs.get("media_position") is not None
        or attrs.get("media_position_updated_at") is not None
        or bool(attrs.get("media_duration"))
    )
    enhanced_dlna = bool(enhanced_dlna_controls and is_dlna and live_state)
    progress = bool(ha_progress or enhanced_dlna)
    status = "ready"
    status_label = "Ready"
    reason = None
    playable = True

    if state_value == "off":
        status = "off"
        status_label = "Off"
        reason = f"{name} is off. Turn it on or choose Browser playback."
        playable = False
    elif state_value in {"unavailable", "unknown"}:
        status = "unavailable"
        status_label = "Unavailable"
        reason = f"{name} is {state_value}. Reconnect it or choose Browser playback."
        playable = False
    elif not can_play_media:
        status = "unsupported"
        status_label = "Unsupported"
        reason = f"{name} does not support Home Assistant play_media."
        playable = False

    seek_mode = "supported" if can_seek and ha_progress else "best_effort" if enhanced_dlna else "none"
    pause_mode = "supported" if can_pause and live_state else "best_effort" if enhanced_dlna else "none"
    resume_mode = "supported" if can_play and live_state else "best_effort" if enhanced_dlna else "none"
    stop_mode = "supported" if can_stop and live_state else "best_effort" if enhanced_dlna else "none"
    limited = not playable or (is_dlna and not progress and not enhanced_dlna)
    notes = [
        "Reports live progress" if ha_progress else "Progress can use enhanced DLNA" if enhanced_dlna else "Does not report live progress",
        "Enhanced DLNA controls available" if enhanced_dlna else "DLNA controls use Home Assistant media_player services" if is_dlna else "Generic HA media player",
    ]
    if reason:
        notes.insert(0, reason)

    return {
        "entity_id": entity_id,
        "name": name,
        "state": state_value,
        "status": status,
        "status_label": status_label,
        "playable": playable,
        "available": playable,
        "reason": reason,
        "capabilities": {
            "play_media": bool(can_play_media),
            "live_state": bool(live_state),
            "progress": bool(progress),
            "seek": seek_mode,
            "pause": pause_mode,
            "resume": resume_mode,
            "stop": stop_mode,
            "speed": False,
            "artwork": "metadata",
            "limited_controls": bool(limited),
            "raw_avtransport": bool(enhanced_dlna),
        },
        "notes": notes,
    }
