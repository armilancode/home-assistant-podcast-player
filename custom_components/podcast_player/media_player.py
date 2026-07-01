"""Media player entity for HA Podcast Player."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PodcastRuntime, PodcastUpdateCoordinator
from .entity import podcast_player_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up media player entity."""
    runtime: PodcastRuntime = entry.runtime_data
    async_add_entities([PodcastPlayerEntity(runtime.coordinator)])


class PodcastPlayerEntity(CoordinatorEntity[PodcastUpdateCoordinator], MediaPlayerEntity):
    """Podcast Player media entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_unique_id = "podcast_player_media_player"

    def __init__(self, coordinator: PodcastUpdateCoordinator) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        # Browser audio is controlled by the custom podcast card and custom
        # podcast_player.* services. Do not advertise native HA playback controls
        # here, because the native media popup cannot output browser audio by itself.
        self._attr_supported_features = MediaPlayerEntityFeature(0)
        self._attr_device_info = podcast_player_device_info()

    @property
    def state(self) -> MediaPlayerState:
        """Return native HA media state.

        The real podcast audio output is the custom Lovelace card browser audio
        engine, or a target speaker via podcast_player.play_on_media_player.
        This entity is intentionally exposed as an idle metadata/status entity in
        Home Assistant's generic media-player UI so the native popup/media page
        does not look like a silent playable device. Automation state is exposed
        through dedicated sensors/binary_sensors and extra attributes.
        """
        return MediaPlayerState.IDLE

    @property
    def media_content_id(self) -> str | None:
        """Return current episode id."""
        return self.coordinator.storage.data["player"].get("current_episode_id")

    @property
    def media_content_type(self) -> str | None:
        """Return content type."""
        return MediaType.PODCAST

    @property
    def media_title(self) -> str | None:
        """Return current episode title."""
        episode = self._current_episode()
        return episode.get("title") if episode else None

    @property
    def media_album_name(self) -> str | None:
        """Return current feed title."""
        feed = self._current_feed()
        return feed.get("title") if feed else None

    @property
    def media_artist(self) -> str | None:
        """Return feed author."""
        feed = self._current_feed()
        return feed.get("author") if feed else None

    @property
    def media_duration(self) -> int | None:
        """Return no native duration.

        The HA native media-player popup/card can render a seekable progress
        slider whenever duration/position are present. This entity mirrors the
        podcast browser player state, but it is not itself an audio output
        device. Returning no native duration avoids a misleading silent seek bar.
        Automation-friendly duration is still exposed through dedicated sensors
        and extra attributes.
        """
        return None

    @property
    def media_position(self) -> int | None:
        """Return no native position to avoid a misleading native seek bar."""
        return None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """Return no native position timestamp."""
        return None

    @property
    def media_image_url(self) -> str | None:
        """Return media image."""
        episode = self._current_episode()
        if episode and episode.get("artwork_url"):
            return episode.get("artwork_url")
        feed = self._current_feed()
        return feed.get("artwork_url") if feed else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return safe summary attributes."""
        storage = self.coordinator.storage
        player = storage.data["player"]
        episode = self._current_episode() or {}
        feed = self._current_feed() or {}
        progress = storage.data["progress"].get(player.get("current_episode_id"), {})
        external_session = dict(player.get("external_session") or {})
        return {
            "current_episode_id": player.get("current_episode_id"),
            "current_feed_id": player.get("current_feed_id"),
            "current_feed_title": feed.get("title"),
            "current_episode_title": episode.get("title"),
            "position": player.get("position"),
            "duration": player.get("duration") or episode.get("duration_seconds"),
            "progress_percent": self._progress_percent(player, episode),
            "playback_speed": player.get("speed"),
            "browser_player_state": player.get("state"),
            "native_player_mode": "status_only",
            "output_mode": player.get("output_mode") or "browser",
            "target_media_player": player.get("target_media_player"),
            "target_media_player_name": player.get("target_media_player_name"),
            "last_target_media_player": player.get("last_target_media_player"),
            "last_target_media_player_name": player.get("last_target_media_player_name"),
            "speaker_url_mode": player.get("speaker_url_mode"),
            "speaker_media_content_type": player.get("speaker_media_content_type"),
            "speaker_last_error": player.get("speaker_last_error"),
            "external_session": external_session,
            "played": progress.get("played"),
            "last_played_at": progress.get("last_played_at"),
            "browser_audio_required": True,
            "recommended_controls": "Use custom:podcast-player-card or podcast_player actions for enhanced controls. Home Assistant Media Browser can start playback on supported media_player targets, but native progress and seek depend on the target integration.",
        }

    async def async_media_play(self) -> None:
        """Ignore native play requests.

        The native HA media-player UI has no browser audio engine attached. Use
        custom:podcast-player-card for browser playback or
        podcast_player.play_on_media_player for speaker playback.
        """
        _LOGGER.debug("Ignoring native media_player.play request; use podcast_player card or services")
        return None

    async def async_media_pause(self) -> None:
        """Ignore native pause requests for the state-only entity."""
        _LOGGER.debug("Ignoring native media_player.pause request; use podcast_player card or services")
        return None

    async def async_media_stop(self) -> None:
        """Ignore native stop requests for the state-only entity."""
        _LOGGER.debug("Ignoring native media_player.stop request; use podcast_player card or services")
        return None

    async def async_media_seek(self, position: float) -> None:
        """Ignore native seek requests.

        Seeking is handled by custom:podcast-player-card where the browser audio
        engine exists. The native HA media dialog is state-only for this entity.
        """
        return None

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        """Ignore native play_media requests for this status entity."""
        _LOGGER.debug("Ignoring native media_player.play_media request; use podcast_player.play_episode or play_on_media_player")
        return None

    async def async_media_next_track(self) -> None:
        """Ignore native next-track requests for this status entity."""
        _LOGGER.debug("Ignoring native media_player.next_track request")
        return None

    async def async_media_previous_track(self) -> None:
        """Ignore native previous-track requests for this status entity."""
        _LOGGER.debug("Ignoring native media_player.previous_track request")
        return None

    def _current_episode(self) -> dict[str, Any] | None:
        episode_id = self.coordinator.storage.data["player"].get("current_episode_id")
        return self.coordinator.storage.get_episode(episode_id) if episode_id else None

    def _current_feed(self) -> dict[str, Any] | None:
        feed_id = self.coordinator.storage.data["player"].get("current_feed_id")
        return self.coordinator.storage.get_feed(feed_id) if feed_id else None

    def _adjacent_episode(self, direction: int) -> dict[str, Any] | None:
        current = self.coordinator.storage.data["player"].get("current_episode_id")
        episodes = self.coordinator.active_episodes()
        ids = [ep.get("episode_id") for ep in episodes]
        if current not in ids:
            return episodes[0] if episodes else None
        idx = ids.index(current) + direction
        if 0 <= idx < len(episodes):
            return episodes[idx]
        return None

    def _progress_percent(self, player: dict[str, Any], episode: dict[str, Any]) -> int:
        duration = player.get("duration") or episode.get("duration_seconds") or 0
        position = player.get("position") or 0
        if not duration:
            return 0
        return min(100, max(0, round((position / duration) * 100)))
