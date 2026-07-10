"""Diagnostics support for Podcast Player."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_INITIAL_RSS_URL,
    CONF_NEW_FEED_URL,
    CONF_REMOVE_FEED_ID,
)
from .coordinator import PodcastRuntime

TO_REDACT = {
    CONF_INITIAL_RSS_URL,
    CONF_NEW_FEED_URL,
    CONF_REMOVE_FEED_ID,
    "speaker_proxy_secret",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry[PodcastRuntime]) -> dict[str, Any]:
    """Return privacy-conscious diagnostics for a config entry."""
    runtime = entry.runtime_data
    storage = runtime.storage
    player = storage.data["player"]
    external_session = player["external_session"]

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "storage": {
            "schema_version": storage.data["schema_version"],
            "settings": async_redact_data(storage.data["settings"], TO_REDACT),
            "library_counts": storage.counts(),
        },
        "coordinator": {
            "last_update_success": runtime.coordinator.last_update_success,
            "has_update_error": runtime.coordinator.last_exception is not None,
        },
        "playback": {
            "state": player.get("state"),
            "output_mode": player.get("output_mode"),
            "speed": player.get("speed"),
            "speaker_url_mode": player.get("speaker_url_mode"),
            "speaker_media_content_type": player.get("speaker_media_content_type"),
            "has_current_episode": player.get("current_episode_id") is not None,
            "has_output_target": player.get("target_media_player") is not None,
            "has_playback_error": player.get("speaker_last_error") is not None,
            "external_session": {
                "active": external_session.get("active"),
                "transport_state": external_session.get("transport_state"),
                "supported_actions": external_session.get("supported_actions"),
                "control_source": external_session.get("control_source"),
                "progress_source": external_session.get("progress_source"),
                "media_matches_session": external_session.get("media_matches_session"),
                "has_error": external_session.get("last_error") is not None,
            },
        },
    }
