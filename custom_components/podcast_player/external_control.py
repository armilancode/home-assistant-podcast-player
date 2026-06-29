"""External media-player control helpers.

The coordinator uses Home Assistant media_player services first. This module
contains the isolated best-effort fallback for DLNA DMR targets that expose
AVTransport actions but do not surface all controls through HA state/services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

DLNA_AV_TRANSPORT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"


@dataclass(slots=True)
class ExternalPlaybackStatus:
    """Sanitized target playback status."""

    state: str
    transport_state: str | None = None
    position: int | None = None
    duration: int | None = None
    current_media_id: str | None = None
    supported_actions: set[str] | None = None
    progress_source: str = "unavailable"
    control_source: str = "unavailable"

    @property
    def is_active(self) -> bool:
        """Return true when target transport is still actively controllable."""
        return self.state in {"playing", "paused", "buffering"}

    @property
    def can_stop(self) -> bool:
        """Return true when target reports a stop action."""
        return bool(self.supported_actions and "Stop" in self.supported_actions)

    @property
    def can_pause(self) -> bool:
        """Return true when target reports a pause action."""
        return bool(self.supported_actions and "Pause" in self.supported_actions)

    @property
    def can_seek(self) -> bool:
        """Return true when target reports a seek action."""
        return bool(self.supported_actions and "Seek" in self.supported_actions)


def seconds_from_upnp_time(value: Any) -> int | None:
    """Convert a DLNA time value to seconds."""
    text = str(value or "").strip()
    if not text or text in {"NOT_IMPLEMENTED", "NOT_IMPLEMENTED_"}:
        return None
    try:
        parts = text.split(".")[0].split(":")
        if len(parts) != 3:
            return None
        hours, minutes, seconds = (int(part) for part in parts)
        return max(0, int(timedelta(hours=hours, minutes=minutes, seconds=seconds).total_seconds()))
    except (TypeError, ValueError):
        return None


def upnp_time_from_seconds(position: float | int) -> str:
    """Return a DLNA REL_TIME target string."""
    total = max(0, int(float(position or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def state_from_transport(transport_state: str | None) -> str:
    """Map DLNA transport states to app state names."""
    value = str(transport_state or "").upper()
    if value == "PLAYING":
        return "playing"
    if value == "PAUSED_PLAYBACK":
        return "paused"
    if value == "TRANSITIONING":
        return "buffering"
    if value in {"STOPPED", "NO_MEDIA_PRESENT"}:
        return "idle"
    return "unknown"


def current_media_matches_session(current_media_id: str | None, expected_media_id: str | None, episode_id: str | None) -> bool | None:
    """Return whether a target media URI still appears to be this podcast session.

    None means the target did not report enough data to decide safely.
    """
    current = str(current_media_id or "").strip()
    if not current:
        return None

    expected = str(expected_media_id or "").strip()
    if expected and current == expected:
        return True

    if episode_id:
        # Signed speaker proxy URLs include this stable path segment. Do not
        # compare the token itself and do not persist raw URLs in entity state.
        proxy_path = f"/api/podcast_player/speaker_proxy/{episode_id}"
        if proxy_path in current:
            return True

    return False


class DlnaAvTransportController:
    """Best-effort async UPnP AVTransport client."""

    def __init__(self, timeout: int = 5) -> None:
        """Initialize the controller."""
        self.timeout = timeout

    async def _async_service(self, description_url: str) -> Any:
        """Create an AVTransport service client from a DMR description URL."""
        from async_upnp_client.aiohttp import AiohttpRequester
        from async_upnp_client.client_factory import UpnpFactory

        requester = AiohttpRequester(timeout=self.timeout)
        factory = UpnpFactory(requester, non_strict=True)
        device = await factory.async_create_device(description_url)
        return device.service(DLNA_AV_TRANSPORT_SERVICE)

    async def async_status(self, description_url: str) -> ExternalPlaybackStatus:
        """Return target transport status."""
        service = await self._async_service(description_url)
        transport_info: dict[str, Any] = {}
        position_info: dict[str, Any] = {}
        actions_info: dict[str, Any] = {}

        try:
            transport_info = await service.action("GetTransportInfo").async_call(InstanceID=0)
        except Exception:  # noqa: BLE001 - individual status calls are best effort
            transport_info = {}
        try:
            position_info = await service.action("GetPositionInfo").async_call(InstanceID=0)
        except Exception:  # noqa: BLE001
            position_info = {}
        try:
            actions_info = await service.action("GetCurrentTransportActions").async_call(InstanceID=0)
        except Exception:  # noqa: BLE001
            actions_info = {}

        if not transport_info and not position_info and not actions_info:
            raise RuntimeError("DLNA target did not return transport status")

        actions = {
            action.strip()
            for action in str(actions_info.get("Actions") or "").split(",")
            if action.strip()
        }
        transport_state = transport_info.get("CurrentTransportState")
        position = seconds_from_upnp_time(position_info.get("RelTime"))
        duration = seconds_from_upnp_time(position_info.get("TrackDuration"))
        return ExternalPlaybackStatus(
            state=state_from_transport(transport_state),
            transport_state=str(transport_state) if transport_state else None,
            position=position,
            duration=duration,
            current_media_id=position_info.get("TrackURI"),
            supported_actions=actions,
            progress_source="dlna" if position is not None else "unavailable",
            control_source="dlna",
        )

    async def async_stop(self, description_url: str) -> None:
        """Send AVTransport Stop."""
        service = await self._async_service(description_url)
        await service.action("Stop").async_call(InstanceID=0)

    async def async_pause(self, description_url: str) -> None:
        """Send AVTransport Pause."""
        service = await self._async_service(description_url)
        await service.action("Pause").async_call(InstanceID=0)

    async def async_play(self, description_url: str) -> None:
        """Send AVTransport Play."""
        service = await self._async_service(description_url)
        await service.action("Play").async_call(InstanceID=0, Speed="1")

    async def async_seek(self, description_url: str, position: float | int) -> None:
        """Send AVTransport absolute time seek."""
        service = await self._async_service(description_url)
        await service.action("Seek").async_call(
            InstanceID=0,
            Unit="REL_TIME",
            Target=upnp_time_from_seconds(position),
        )
