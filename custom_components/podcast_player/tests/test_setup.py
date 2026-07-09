"""Tests for Podcast Player setup lifecycle behavior."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.podcast_player import async_setup_entry
from custom_components.podcast_player.const import CONF_INITIAL_RSS_URL, DOMAIN, PLATFORMS
from custom_components.podcast_player.feed_parser import PodcastParseError


async def test_setup_entry_initializes_runtime_and_forwards_platforms(hass, enable_custom_integrations) -> None:
    """Setting up a config entry stores runtime data and forwards platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.podcast_player.PodcastStorage.async_load", AsyncMock()),
        patch("custom_components.podcast_player.async_register_api"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)) as forward_setups,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    assert entry.entry_id in hass.data[DOMAIN]
    assert entry.runtime_data is hass.data[DOMAIN][entry.entry_id]
    forward_setups.assert_awaited_once_with(entry, PLATFORMS)


async def test_setup_entry_imports_initial_feed_once(hass, enable_custom_integrations) -> None:
    """A validated initial feed is imported during setup and then removed from entry data."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INITIAL_RSS_URL: "https://example.test/feed.xml"})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.podcast_player.PodcastStorage.async_load", AsyncMock()),
        patch("custom_components.podcast_player.async_register_api"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
        patch(
            "custom_components.podcast_player.PodcastUpdateCoordinator.async_add_feed",
            AsyncMock(return_value={"feed_id": "feed_1"}),
        ) as add_feed,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    add_feed.assert_awaited_once_with("https://example.test/feed.xml")
    assert CONF_INITIAL_RSS_URL not in entry.data


@pytest.mark.parametrize(
    ("side_effect", "expected_exception"),
    [
        (PodcastParseError("cannot_connect", "Cannot connect"), ConfigEntryNotReady),
        (PodcastParseError("no_audio_enclosures", "No audio"), ConfigEntryError),
    ],
)
async def test_setup_entry_initial_feed_failures_are_setup_errors(
    hass,
    enable_custom_integrations,
    side_effect: PodcastParseError,
    expected_exception: type[Exception],
) -> None:
    """Initial-feed import failures fail setup cleanly instead of being discarded."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INITIAL_RSS_URL: "https://example.test/feed.xml"})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.podcast_player.PodcastStorage.async_load", AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)) as forward_setups,
        patch(
            "custom_components.podcast_player.PodcastUpdateCoordinator.async_add_feed",
            AsyncMock(side_effect=side_effect),
        ),
        pytest.raises(expected_exception),
    ):
        await async_setup_entry(hass, entry)

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert entry.data[CONF_INITIAL_RSS_URL] == "https://example.test/feed.xml"
    forward_setups.assert_not_awaited()
