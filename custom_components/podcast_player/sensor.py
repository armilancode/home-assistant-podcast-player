"""Sensors for HA Podcast Player."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PodcastRuntime, PodcastUpdateCoordinator
from .entity import podcast_player_device_info


def _feed_sort_key(feed: dict[str, Any]) -> tuple[str, str]:
    """Return a stable sort key for feed entities."""
    return (str(feed.get("title") or "").casefold(), str(feed.get("feed_id") or ""))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Podcast Player sensors."""
    runtime: PodcastRuntime = entry.runtime_data
    coordinator = runtime.coordinator

    entities: list[SensorEntity] = [
        FeedCountSensor(coordinator),
        UnplayedCountSensor(coordinator),
        LatestEpisodeSensor(coordinator),
        CurrentFeedSensor(coordinator),
        CurrentEpisodeSensor(coordinator),
        CurrentPositionSensor(coordinator),
        CurrentDurationSensor(coordinator),
        CurrentProgressSensor(coordinator),
        PlaybackSpeedSensor(coordinator),
        CurrentOutputSensor(coordinator),
        LatestByFeedSensor(coordinator),
    ]

    known_feed_ids: set[str] = set()

    def _missing_feed_entities() -> list[PodcastFeedSensor]:
        new_entities: list[PodcastFeedSensor] = []
        for feed in sorted(coordinator.storage.enabled_feeds(), key=_feed_sort_key):
            feed_id = feed.get("feed_id")
            if not feed_id or feed_id in known_feed_ids:
                continue
            known_feed_ids.add(feed_id)
            new_entities.append(PodcastFeedSensor(coordinator, feed_id))
        return new_entities

    entities.extend(_missing_feed_entities())
    async_add_entities(entities)

    @callback
    def _async_add_new_feed_entities() -> None:
        """Expose feed sensors for feeds added after setup without requiring a restart."""
        new_entities = _missing_feed_entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_feed_entities))


class PodcastBaseSensor(CoordinatorEntity[PodcastUpdateCoordinator], SensorEntity):
    """Base podcast sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PodcastUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = podcast_player_device_info()

    @property
    def _player(self) -> dict[str, Any]:
        return self.coordinator.storage.data["player"]

    def _current_episode(self) -> dict[str, Any] | None:
        episode_id = self._player.get("current_episode_id")
        return self.coordinator.storage.get_episode(episode_id) if episode_id else None

    def _current_feed(self) -> dict[str, Any] | None:
        feed_id = self._player.get("current_feed_id")
        return self.coordinator.storage.get_feed(feed_id) if feed_id else None

    def _progress(self) -> dict[str, Any]:
        episode_id = self._player.get("current_episode_id")
        if not episode_id:
            return {}
        return self.coordinator.storage.data["progress"].get(episode_id, {})


class PodcastFeedSensor(PodcastBaseSensor):
    """Automation-targetable sensor representing one podcast feed.

    These entities are intentionally named ``Feed <title>`` so Home
    Assistant creates readable entity IDs like
    ``sensor.podcast_player_feed_the_joe_rogan_experience``. Services can then use the
    normal ``target.entity_id`` dropdown to select the podcast, while the output
    media player remains a normal data field named ``media_player_entity_id``.
    """

    _attr_icon = "mdi:podcast"

    def __init__(self, coordinator: PodcastUpdateCoordinator, feed_id: str) -> None:
        super().__init__(coordinator)
        self.feed_id = feed_id
        self._attr_unique_id = f"podcast_player_feed_{feed_id}"

    @property
    def name(self) -> str:
        feed = self._feed() or {}
        return f"Feed {feed.get('title') or self.feed_id}"

    @property
    def available(self) -> bool:
        feed = self._feed()
        return bool(feed and feed.get("enabled", True))

    @property
    def native_value(self) -> int | None:
        if not self.available:
            return None
        return self._feed_counts()["unplayed_count"]

    @property
    def entity_picture(self) -> str | None:
        feed = self._feed() or {}
        return feed.get("artwork_url")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        feed = self._feed() or {}
        latest = self.coordinator.latest_episode(feed_id=self.feed_id)
        counts = self._feed_counts()
        return {
            "podcast_player_entity_type": "feed",
            "feed_id": self.feed_id,
            "title": feed.get("title"),
            "url": feed.get("rss_url"),
            "rss_url": feed.get("rss_url"),
            "image": feed.get("artwork_url"),
            "artwork_url": feed.get("artwork_url"),
            "author": feed.get("author"),
            "website": feed.get("website"),
            "status": feed.get("status"),
            "last_refresh": feed.get("last_refresh"),
            "last_success": feed.get("last_success"),
            "last_error": feed.get("last_error"),
            "episode_count": counts["episode_count"],
            "unplayed_count": counts["unplayed_count"],
            "in_progress_count": counts["in_progress_count"],
            "latest_episode_id": latest.get("episode_id") if latest else None,
            "latest_episode_title": latest.get("title") if latest else None,
            "latest_episode_published": latest.get("published") if latest else None,
            "latest_episode_artwork_url": (latest.get("artwork_url") if latest else None) or feed.get("artwork_url"),
        }

    def _feed(self) -> dict[str, Any] | None:
        return self.coordinator.storage.get_feed(self.feed_id)

    def _feed_counts(self) -> dict[str, int]:
        episodes = self.coordinator.storage.episodes_for_feed(self.feed_id)
        progress = self.coordinator.storage.data["progress"]
        unplayed = 0
        in_progress = 0
        for episode in episodes:
            episode_id = episode.get("episode_id")
            item_progress = progress.get(episode_id, {})
            if not item_progress.get("played", False):
                unplayed += 1
            if item_progress.get("position", 0) > 0 and not item_progress.get("played", False):
                in_progress += 1
        return {
            "episode_count": len(episodes),
            "unplayed_count": unplayed,
            "in_progress_count": in_progress,
        }


class FeedCountSensor(PodcastBaseSensor):
    """Feed count sensor."""

    _attr_name = "Feeds"
    _attr_unique_id = "podcast_player_feeds"
    _attr_icon = "mdi:rss"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> int:
        return self.coordinator.storage.counts()["enabled_feeds"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counts = self.coordinator.storage.counts()
        return {
            "total_feeds": counts["total_feeds"],
            "enabled_feeds": counts["enabled_feeds"],
            "failed_feeds": counts["failed_feeds"],
            "last_refresh": self._last_refresh(),
        }

    def _last_refresh(self) -> str | None:
        feeds = self.coordinator.storage.data["feeds"].values()
        values = [feed.get("last_refresh") for feed in feeds if feed.get("last_refresh")]
        return max(values) if values else None


class UnplayedCountSensor(PodcastBaseSensor):
    """Unplayed episodes sensor."""

    _attr_name = "Unplayed"
    _attr_unique_id = "podcast_player_unplayed"
    _attr_icon = "mdi:podcast"

    @property
    def native_value(self) -> int:
        return self.coordinator.storage.counts()["unplayed"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counts = self.coordinator.storage.counts()
        return {
            "unplayed_total": counts["unplayed"],
            "partially_played": counts["partially_played"],
            "total_episodes": counts["total_episodes"],
        }


class LatestEpisodeSensor(PodcastBaseSensor):
    """Latest episode sensor."""

    _attr_name = "Latest episode"
    _attr_unique_id = "podcast_player_latest_episode"
    _attr_icon = "mdi:playlist-music"

    @property
    def native_value(self) -> str | None:
        episode = self.coordinator.latest_episode()
        return episode.get("title") if episode else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        episode = self.coordinator.latest_episode()
        if not episode:
            return {}
        feed = self.coordinator.storage.get_feed(episode.get("feed_id")) or {}
        progress = self.coordinator.storage.data["progress"].get(episode.get("episode_id"), {})
        return {
            "feed_title": feed.get("title"),
            "feed_id": episode.get("feed_id"),
            "episode_id": episode.get("episode_id"),
            "published": episode.get("published"),
            "duration": episode.get("duration_seconds"),
            "artwork_url": episode.get("artwork_url") or feed.get("artwork_url"),
            "is_played": progress.get("played", False),
        }


class CurrentFeedSensor(PodcastBaseSensor):
    """Current feed sensor."""

    _attr_name = "Current feed"
    _attr_unique_id = "podcast_player_current_feed"
    _attr_icon = "mdi:rss-box"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        feed = self._current_feed()
        return feed.get("title") if feed else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        feed = self._current_feed() or {}
        return {"feed_id": feed.get("feed_id"), "author": feed.get("author"), "artwork_url": feed.get("artwork_url")}


class CurrentEpisodeSensor(PodcastBaseSensor):
    """Current episode sensor."""

    _attr_name = "Current episode"
    _attr_unique_id = "podcast_player_current_episode"
    _attr_icon = "mdi:playlist-play"

    @property
    def native_value(self) -> str | None:
        episode = self._current_episode()
        return episode.get("title") if episode else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        episode = self._current_episode() or {}
        feed = self._current_feed() or {}
        progress = self._progress()
        return {
            "episode_id": episode.get("episode_id"),
            "feed_id": episode.get("feed_id"),
            "feed_title": feed.get("title"),
            "published": episode.get("published"),
            "position": self._player.get("position"),
            "duration": self._player.get("duration") or episode.get("duration_seconds"),
            "played": progress.get("played"),
            "playback_speed": self._player.get("speed"),
            "artwork_url": episode.get("artwork_url") or feed.get("artwork_url"),
        }


class CurrentPositionSensor(PodcastBaseSensor):
    """Current playback position sensor."""

    _attr_name = "Current position"
    _attr_unique_id = "podcast_player_current_position"
    _attr_icon = "mdi:progress-clock"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    @property
    def native_value(self) -> int:
        return int(self._player.get("position") or 0)


class CurrentDurationSensor(PodcastBaseSensor):
    """Current playback duration sensor."""

    _attr_name = "Current duration"
    _attr_unique_id = "podcast_player_current_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    @property
    def native_value(self) -> int | None:
        episode = self._current_episode() or {}
        value = self._player.get("duration") or episode.get("duration_seconds")
        return int(value) if value else None


class CurrentProgressSensor(PodcastBaseSensor):
    """Current playback progress percentage."""

    _attr_name = "Current progress"
    _attr_unique_id = "podcast_player_current_progress"
    _attr_icon = "mdi:percent"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int:
        episode = self._current_episode() or {}
        duration = self._player.get("duration") or episode.get("duration_seconds") or 0
        position = self._player.get("position") or 0
        if not duration:
            return 0
        return min(100, max(0, round((position / duration) * 100)))


class PlaybackSpeedSensor(PodcastBaseSensor):
    """Playback speed sensor."""

    _attr_name = "Playback speed"
    _attr_unique_id = "podcast_player_playback_speed"
    _attr_icon = "mdi:speedometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> float:
        return float(self._player.get("speed") or self.coordinator.storage.data["settings"].get("default_playback_speed", 1.0))


class CurrentOutputSensor(PodcastBaseSensor):
    """Current podcast output mode/target."""

    _attr_name = "Current output"
    _attr_unique_id = "podcast_player_current_output"
    _attr_icon = "mdi:speaker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str:
        return self._player.get("output_mode") or "browser"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        target = self._player.get("target_media_player")
        target_state = self.coordinator.hass.states.get(target) if target else None
        attrs = target_state.attributes if target_state else {}
        target_state_value = target_state.state if target_state else None
        reports_live_state = bool(target_state_value and target_state_value not in ("unknown", "unavailable"))
        reports_progress = bool(
            reports_live_state
            and (
                attrs.get("media_position") is not None
                or attrs.get("media_duration") is not None
                or attrs.get("media_position_updated_at") is not None
            )
        )
        return {
            "target_media_player": target,
            "target_media_player_name": self._player.get("target_media_player_name"),
            "target_state": target_state_value,
            "target_reports_live_state": reports_live_state,
            "target_reports_progress": reports_progress,
            "external_limited_controls": bool(target and not reports_live_state),
            "speaker_url_mode": self._player.get("speaker_url_mode"),
            "speaker_media_content_type": self._player.get("speaker_media_content_type"),
            "speaker_last_error": self._player.get("speaker_last_error"),
        }


class LatestByFeedSensor(PodcastBaseSensor):
    """Latest episode by feed summary sensor."""

    _attr_name = "Latest by feed"
    _attr_unique_id = "podcast_player_latest_by_feed"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> int:
        return len(self._latest_items())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"items": self._latest_items()}

    def _latest_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for feed in self.coordinator.storage.enabled_feeds():
            episode = self.coordinator.latest_episode(feed.get("feed_id"))
            if not episode:
                continue
            items.append(
                {
                    "feed_id": feed.get("feed_id"),
                    "feed_title": feed.get("title"),
                    "episode_id": episode.get("episode_id"),
                    "episode_title": episode.get("title"),
                    "published": episode.get("published"),
                    "duration": episode.get("duration_seconds"),
                    "artwork_url": episode.get("artwork_url") or feed.get("artwork_url"),
                }
            )
        return items
