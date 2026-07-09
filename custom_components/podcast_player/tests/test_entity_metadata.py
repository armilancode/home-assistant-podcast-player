"""Tests for Podcast Player entity metadata defaults."""

import inspect
from types import SimpleNamespace

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
from custom_components.podcast_player.entity import podcast_player_device_info
from custom_components.podcast_player.sensor import (
    CurrentDurationSensor,
    CurrentFeedSensor,
    CurrentOutputSensor,
    CurrentPositionSensor,
    CurrentProgressSensor,
    FeedCountSensor,
    LatestByFeedSensor,
    LatestEpisodeSensor,
    PlaybackSpeedSensor,
    PodcastBaseSensor,
    PodcastFeedSensor,
    UnplayedCountSensor,
)


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
        assert "_attr_entity_registry_enabled_default = False" not in _source(entity_class)


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
        source = _source(entity_class)
        assert "_attr_entity_category = EntityCategory.DIAGNOSTIC" in source
        assert "_attr_entity_registry_enabled_default = False" in source


def test_shortcut_buttons_are_disabled_by_default() -> None:
    """Action buttons that can change playback need explicit user opt-in."""
    for entity_class in (
        PlayLatestButton,
        PlayNextUnplayedButton,
        MarkCurrentPlayedButton,
    ):
        assert "_attr_entity_registry_enabled_default = False" in _source(entity_class)


def test_feed_sensors_use_device_entity_naming() -> None:
    """Feed sensors should follow modern HA device entity naming."""
    assert "_attr_has_entity_name = True" in _source(PodcastBaseSensor)
    assert "_attr_has_entity_name = False" not in _source(PodcastFeedSensor)


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
