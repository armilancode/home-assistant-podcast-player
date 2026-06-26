"""Shared entity helpers for Podcast Player."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, NAME, VERSION


def podcast_player_device_info() -> DeviceInfo:
    """Return the integration device info used by all entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, "podcast_player")},
        name=NAME,
        manufacturer="Podcast Player",
        model="RSS Podcast Player",
        sw_version=VERSION,
    )
