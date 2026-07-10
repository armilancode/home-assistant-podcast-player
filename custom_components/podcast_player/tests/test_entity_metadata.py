"""Tests for Podcast Player entity metadata defaults."""

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.translation import async_get_translations

from custom_components.podcast_player import binary_sensor as binary_sensor_platform
from custom_components.podcast_player import button as button_platform
from custom_components.podcast_player import media_player as media_player_platform
from custom_components.podcast_player import sensor as sensor_platform
from custom_components.podcast_player.binary_sensor import HasUnplayedBinarySensor, IsPlayingBinarySensor
from custom_components.podcast_player.button import (
    MarkCurrentPlayedButton,
    PlayLatestButton,
    PlayNextUnplayedButton,
    RefreshButton,
)
from custom_components.podcast_player.const import DOMAIN
from custom_components.podcast_player.entity import podcast_player_device_info
from custom_components.podcast_player.media_player import PodcastPlayerEntity
from custom_components.podcast_player.sensor import (
    CurrentDurationSensor,
    CurrentEpisodeSensor,
    CurrentFeedSensor,
    CurrentOutputSensor,
    CurrentPositionSensor,
    CurrentProgressSensor,
    FeedCountSensor,
    LatestByFeedSensor,
    LatestEpisodeSensor,
    PlaybackSpeedSensor,
    PodcastFeedSensor,
    UnplayedCountSensor,
)

INTEGRATION_PATH = Path(__file__).parents[1]

TRANSLATED_ENTITIES = {
    "binary_sensor": {
        HasUnplayedBinarySensor: "has_unplayed_episodes",
        IsPlayingBinarySensor: "is_playing",
    },
    "button": {
        MarkCurrentPlayedButton: "mark_current_played",
        PlayLatestButton: "play_latest_episode",
        PlayNextUnplayedButton: "play_next_unplayed",
        RefreshButton: "refresh_feeds",
    },
    "sensor": {
        CurrentDurationSensor: "current_duration",
        CurrentEpisodeSensor: "current_episode",
        CurrentFeedSensor: "current_feed",
        CurrentOutputSensor: "current_output",
        CurrentPositionSensor: "current_position",
        CurrentProgressSensor: "current_progress",
        PodcastFeedSensor: "feed",
        FeedCountSensor: "feeds",
        LatestByFeedSensor: "latest_by_feed",
        LatestEpisodeSensor: "latest_episode",
        PlaybackSpeedSensor: "playback_speed",
        UnplayedCountSensor: "unplayed",
    },
}

CUSTOM_ICON_KEYS = {
    "binary_sensor": {"has_unplayed_episodes"},
    "button": {
        "mark_current_played",
        "play_latest_episode",
        "play_next_unplayed",
        "refresh_feeds",
    },
    "sensor": {
        "current_episode",
        "current_feed",
        "current_output",
        "current_position",
        "current_progress",
        "feed",
        "feeds",
        "latest_by_feed",
        "latest_episode",
        "playback_speed",
        "unplayed",
    },
}


def _source(entity_class: type) -> str:
    """Return class source."""
    return inspect.getsource(entity_class)


class FakeFeedStorage:
    """Minimal storage for feed sensor availability tests."""

    def __init__(self) -> None:
        self.data = {"progress": {}}
        self.feeds = {
            "feed_1": {
                "feed_id": "feed_1",
                "title": "Feed One",
                "enabled": True,
            }
        }

    def get_feed(self, feed_id: str) -> dict | None:
        """Return a fake feed."""
        return self.feeds.get(feed_id)

    def episodes_for_feed(self, feed_id: str) -> list[dict]:
        """Return fake feed episodes."""
        return []


def _entity(entity_class: type):
    """Create an entity with the minimum coordinator context needed for metadata."""
    coordinator = SimpleNamespace(
        last_update_success=True,
        storage=FakeFeedStorage(),
    )
    if entity_class is PodcastFeedSensor:
        return entity_class(coordinator, "feed_1")
    return entity_class(coordinator)


def test_shared_device_info_is_public_and_consistent() -> None:
    """Entity device info must not expose local-development wording."""
    device_info = podcast_player_device_info()

    assert device_info["manufacturer"] == "Podcast Player"
    assert device_info["model"] == "RSS Podcast Player"


def test_primary_entities_are_enabled_by_default() -> None:
    """Core user-facing entities stay visible on new installs."""
    for entity_class in (
        PodcastFeedSensor,
        UnplayedCountSensor,
        LatestEpisodeSensor,
        IsPlayingBinarySensor,
        HasUnplayedBinarySensor,
        RefreshButton,
    ):
        assert _entity(entity_class).entity_registry_enabled_default is True


def test_diagnostic_sensors_are_disabled_by_default() -> None:
    """Technical/redundant sensors should not clutter the default entity list."""
    for entity_class in (
        FeedCountSensor,
        CurrentFeedSensor,
        CurrentPositionSensor,
        CurrentDurationSensor,
        CurrentProgressSensor,
        PlaybackSpeedSensor,
        CurrentOutputSensor,
        LatestByFeedSensor,
    ):
        entity = _entity(entity_class)
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert entity.entity_registry_enabled_default is False


def test_primary_entities_remain_uncategorized() -> None:
    """User-facing playback and library entities are primary device features."""
    for entity_class in (
        PodcastPlayerEntity,
        PodcastFeedSensor,
        UnplayedCountSensor,
        LatestEpisodeSensor,
        CurrentEpisodeSensor,
        IsPlayingBinarySensor,
        HasUnplayedBinarySensor,
        RefreshButton,
        PlayLatestButton,
        PlayNextUnplayedButton,
        MarkCurrentPlayedButton,
    ):
        assert _entity(entity_class).entity_category is None


def test_shortcut_buttons_are_disabled_by_default() -> None:
    """Action buttons that can change playback need explicit user opt-in."""
    for entity_class in (
        PlayLatestButton,
        PlayNextUnplayedButton,
        MarkCurrentPlayedButton,
    ):
        assert _entity(entity_class).entity_registry_enabled_default is False


def test_feed_sensors_use_device_entity_naming() -> None:
    """Feed sensors should follow modern HA device entity naming."""
    storage = FakeFeedStorage()
    coordinator = SimpleNamespace(last_update_success=True, storage=storage)
    sensor = PodcastFeedSensor(coordinator, "feed_1")

    assert sensor.has_entity_name is True
    assert sensor.translation_key == "feed"
    assert sensor.translation_placeholders == {"feed_title": "Feed One"}


def test_entity_names_are_translation_backed() -> None:
    """Every non-primary entity name has matching core and custom translations."""
    strings = json.loads((INTEGRATION_PATH / "strings.json").read_text())
    english = json.loads((INTEGRATION_PATH / "translations" / "en.json").read_text())

    assert strings["entity"] == english["entity"]
    for domain, entities in TRANSLATED_ENTITIES.items():
        assert set(strings["entity"][domain]) == set(entities.values())
        for entity_class, translation_key in entities.items():
            assert _entity(entity_class).translation_key == translation_key
            assert "_attr_name =" not in _source(entity_class)
            assert strings["entity"][domain][translation_key]["name"]

    media_player = _entity(PodcastPlayerEntity)
    assert media_player.name is None
    assert media_player.translation_key is None


def test_custom_icons_use_icon_translations() -> None:
    """Custom icons are translation-backed and reference valid entity keys."""
    strings = json.loads((INTEGRATION_PATH / "strings.json").read_text())
    icons = json.loads((INTEGRATION_PATH / "icons.json").read_text())

    assert set(icons["entity"]) == set(CUSTOM_ICON_KEYS)
    for domain, expected_keys in CUSTOM_ICON_KEYS.items():
        assert set(icons["entity"][domain]) == expected_keys
        assert expected_keys <= set(strings["entity"][domain])

    for entities in TRANSLATED_ENTITIES.values():
        for entity_class in entities:
            assert _entity(entity_class).icon is None
            assert "_attr_icon =" not in _source(entity_class)
    assert _entity(PodcastPlayerEntity).icon is None
    assert icons["entity"]["sensor"]["current_output"]["state"] == {
        "browser": "mdi:web",
        "speaker": "mdi:speaker",
    }


async def test_entity_resources_load_in_home_assistant(hass, enable_custom_integrations) -> None:
    """Home Assistant can load entity translations and icons at runtime."""
    translations = await async_get_translations(hass, "en", "entity", integrations={DOMAIN})
    icons = await async_get_icons(hass, "entity", integrations={DOMAIN})

    assert translations[f"component.{DOMAIN}.entity.sensor.feed.name"] == "Feed {feed_title}"
    assert translations[f"component.{DOMAIN}.entity.sensor.current_output.state.browser"] == "Browser"
    assert icons[DOMAIN]["sensor"]["feed"]["default"] == "mdi:podcast"
    assert icons[DOMAIN]["sensor"]["current_output"]["state"]["speaker"] == "mdi:speaker"


def test_entity_device_and_state_classes_are_semantic() -> None:
    """Entities use only classes whose Home Assistant semantics match their data."""
    assert _entity(IsPlayingBinarySensor).device_class is BinarySensorDeviceClass.RUNNING
    assert _entity(CurrentPositionSensor).device_class is SensorDeviceClass.DURATION
    assert _entity(CurrentPositionSensor).native_unit_of_measurement is UnitOfTime.SECONDS
    assert _entity(CurrentDurationSensor).device_class is SensorDeviceClass.DURATION
    assert _entity(CurrentDurationSensor).native_unit_of_measurement is UnitOfTime.SECONDS
    assert _entity(CurrentOutputSensor).device_class is SensorDeviceClass.ENUM
    assert _entity(CurrentOutputSensor).options == ["browser", "speaker"]

    for entity_class in (PodcastFeedSensor, FeedCountSensor, UnplayedCountSensor):
        assert _entity(entity_class).state_class is SensorStateClass.MEASUREMENT

    for entity_class in (
        LatestEpisodeSensor,
        CurrentFeedSensor,
        CurrentEpisodeSensor,
        CurrentPositionSensor,
        CurrentDurationSensor,
        CurrentProgressSensor,
        PlaybackSpeedSensor,
        CurrentOutputSensor,
        LatestByFeedSensor,
    ):
        assert _entity(entity_class).state_class is None


def test_platform_parallel_updates_are_explicit() -> None:
    """All entity platforms must declare their parallel update behavior."""
    assert binary_sensor_platform.PARALLEL_UPDATES == 0
    assert button_platform.PARALLEL_UPDATES == 1
    assert media_player_platform.PARALLEL_UPDATES == 0
    assert sensor_platform.PARALLEL_UPDATES == 0


def test_feed_sensor_availability_includes_coordinator_and_feed_state() -> None:
    """Feed sensors must go unavailable on coordinator failure or missing feed data."""
    storage = FakeFeedStorage()
    coordinator = SimpleNamespace(last_update_success=True, storage=storage)
    sensor = PodcastFeedSensor(coordinator, "feed_1")

    assert sensor.available is True
    assert sensor.native_value == 0

    coordinator.last_update_success = False
    assert sensor.available is False
    assert sensor.native_value is None

    coordinator.last_update_success = True
    storage.feeds["feed_1"]["enabled"] = False
    assert sensor.available is False

    storage.feeds.pop("feed_1")
    assert sensor.available is False
