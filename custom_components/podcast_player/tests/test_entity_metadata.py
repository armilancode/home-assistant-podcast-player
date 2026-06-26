"""Tests for Podcast Player entity metadata defaults."""

from homeassistant.const import EntityCategory

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
    PodcastFeedSensor,
    UnplayedCountSensor,
)


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
        assert getattr(entity_class, "_attr_entity_registry_enabled_default", True) is True


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
        assert entity_class._attr_entity_category == EntityCategory.DIAGNOSTIC
        assert entity_class._attr_entity_registry_enabled_default is False


def test_shortcut_buttons_are_disabled_by_default() -> None:
    """Action buttons that can change playback need explicit user opt-in."""
    for entity_class in (
        PlayLatestButton,
        PlayNextUnplayedButton,
        MarkCurrentPlayedButton,
    ):
        assert entity_class._attr_entity_registry_enabled_default is False


def test_feed_sensors_use_device_entity_naming() -> None:
    """Feed sensors should follow modern HA device entity naming."""
    assert PodcastFeedSensor._attr_has_entity_name is True
