"""Binary sensors for HA Podcast Player."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PodcastRuntime, PodcastUpdateCoordinator
from .entity import podcast_player_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Podcast Player binary sensors."""
    runtime: PodcastRuntime = entry.runtime_data
    async_add_entities([IsPlayingBinarySensor(runtime.coordinator), HasUnplayedBinarySensor(runtime.coordinator)])


class PodcastBaseBinarySensor(CoordinatorEntity[PodcastUpdateCoordinator], BinarySensorEntity):
    """Base binary sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PodcastUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = podcast_player_device_info()


class IsPlayingBinarySensor(PodcastBaseBinarySensor):
    """Whether podcast playback is currently marked playing."""

    _attr_name = "Is playing"
    _attr_unique_id = "podcast_player_is_playing"
    _attr_icon = "mdi:play-circle"

    @property
    def is_on(self) -> bool:
        return self.coordinator.storage.data["player"].get("state") == "playing"


class HasUnplayedBinarySensor(PodcastBaseBinarySensor):
    """Whether there are unplayed podcast episodes."""

    _attr_name = "Has unplayed episodes"
    _attr_unique_id = "podcast_player_has_unplayed"
    _attr_icon = "mdi:podcast"

    @property
    def is_on(self) -> bool:
        return self.coordinator.storage.counts()["unplayed"] > 0
