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


def _supported_features(state: Any) -> int:
    """Return supported media-player feature bitmask from a state object."""
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


def output_target_status(entity_id: str, state: Any | None, platform: str | None = None) -> dict[str, Any]:
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
    features = _supported_features(state)
    feature_enum = _media_player_features()
    can_seek = False
    can_pause = False
    can_stop = False
    can_play_media = True
    if feature_enum is not None:
        can_seek = _feature_enabled(features, getattr(feature_enum, "SEEK", 0))
        can_pause = _feature_enabled(features, getattr(feature_enum, "PAUSE", 0))
        can_stop = _feature_enabled(features, getattr(feature_enum, "STOP", 0))
        can_play_media = _feature_enabled(features, getattr(feature_enum, "PLAY_MEDIA", 0)) if features else True

    live_state = state_value not in UNAVAILABLE_MEDIA_PLAYER_STATES
    progress = live_state and any(k in attrs for k in ("media_position", "media_duration", "media_position_updated_at"))
    is_dlna = platform == "dlna_dmr"
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

    limited = not playable or (is_dlna and not progress)
    notes = [
        "Reports live progress" if progress else "Does not report live progress",
        "DLNA controls use Home Assistant media_player services" if is_dlna else "Generic HA media player",
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
            "seek": "supported" if can_seek and progress else "none",
            "pause": "supported" if can_pause and live_state else "none",
            "stop": "supported" if can_stop and live_state else "none",
            "speed": False,
            "artwork": "metadata",
            "limited_controls": bool(limited),
            "raw_avtransport": False,
        },
        "notes": notes,
    }
