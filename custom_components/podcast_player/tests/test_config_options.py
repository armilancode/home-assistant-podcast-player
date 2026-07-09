"""Tests for Podcast Player config/options helpers."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.podcast_player.config_flow import (
    NO_FEED_SELECTION,
    URL_MODE_DIRECT,
    URL_MODE_SIGNED_PROXY,
    _validate_optional_rss_url,
    default_options,
    feed_select_options,
    normalize_options,
    options_from_user_input,
)
from custom_components.podcast_player.const import (
    CONF_DEFAULT_PLAYBACK_SPEED,
    CONF_DIRECT_FIRST,
    CONF_ENHANCED_DLNA_CONTROLS,
    CONF_INITIAL_RSS_URL,
    CONF_MAX_EPISODES_PER_FEED,
    CONF_NEW_FEED_URL,
    CONF_PLAYED_THRESHOLD,
    CONF_REFRESH_INTERVAL_MINUTES,
    CONF_REMOVE_FEED_ID,
    CONF_REMOVE_FEED_KEEP_HISTORY,
    CONF_URL_MODE_PREFERENCE,
    DEFAULT_MAX_EPISODES_PER_FEED,
    DEFAULT_PLAYBACK_SPEED,
    DEFAULT_PLAYED_THRESHOLD,
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    DOMAIN,
    NAME,
)
from custom_components.podcast_player.coordinator import refresh_interval_from_settings
from custom_components.podcast_player.feed_parser import PodcastParseError
from custom_components.podcast_player.storage import make_feed_id, normalize_rss_url

USER_INPUT = {
    CONF_REFRESH_INTERVAL_MINUTES: 45,
    CONF_MAX_EPISODES_PER_FEED: 250,
    CONF_DEFAULT_PLAYBACK_SPEED: 1.25,
    CONF_PLAYED_THRESHOLD: 0.9,
    CONF_URL_MODE_PREFERENCE: URL_MODE_SIGNED_PROXY,
    CONF_ENHANCED_DLNA_CONTROLS: False,
}


def _runtime(*, add_feed_side_effect=None, remove_feed_return=True) -> SimpleNamespace:
    """Return a minimal loaded runtime for options flow tests."""
    storage = SimpleNamespace(
        data={
            "feeds": {
                "feed_1": {"feed_id": "feed_1", "title": "Feed One"},
                "feed_2": {"feed_id": "feed_2", "title": "Feed Two"},
            }
        }
    )
    coordinator = SimpleNamespace(
        async_add_feed=AsyncMock(side_effect=add_feed_side_effect),
        async_remove_feed=AsyncMock(return_value=remove_feed_return),
    )
    return SimpleNamespace(storage=storage, coordinator=coordinator)


async def test_user_flow_shows_form(hass, enable_custom_integrations) -> None:
    """The user step shows a setup form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_flow_creates_entry(hass, enable_custom_integrations) -> None:
    """The user step stores normalized options and optional initial feed data."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_INITIAL_RSS_URL: " https://example.test/feed.xml ",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {CONF_INITIAL_RSS_URL: "https://example.test/feed.xml"}
    assert result["options"] == {
        CONF_REFRESH_INTERVAL_MINUTES: 45,
        CONF_MAX_EPISODES_PER_FEED: 250,
        CONF_DEFAULT_PLAYBACK_SPEED: 1.25,
        CONF_PLAYED_THRESHOLD: 0.9,
        CONF_DIRECT_FIRST: False,
        CONF_ENHANCED_DLNA_CONTROLS: False,
    }


async def test_user_flow_rejects_duplicate_entry(hass, enable_custom_integrations) -> None:
    """Only one Podcast Player config entry can be created."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title=NAME).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_user_flow_rejects_invalid_initial_feed_url(hass, enable_custom_integrations) -> None:
    """Invalid optional initial feed URLs are rejected by the setup form schema."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                **USER_INPUT,
                CONF_INITIAL_RSS_URL: "ftp://example.test/feed.xml",
            },
        )

    assert err.value.schema_errors == {CONF_INITIAL_RSS_URL: "RSS URL must start with http:// or https://"}


async def test_options_flow_updates_options_without_loaded_runtime(hass, enable_custom_integrations) -> None:
    """Options can be updated before the integration runtime is loaded."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options=default_options())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_NEW_FEED_URL: "",
            CONF_REMOVE_FEED_ID: NO_FEED_SELECTION,
            CONF_REMOVE_FEED_KEEP_HISTORY: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DIRECT_FIRST] is False
    assert result["data"][CONF_REFRESH_INTERVAL_MINUTES] == 45


async def test_options_flow_adds_and_removes_feeds(hass, enable_custom_integrations) -> None:
    """Options flow can add and remove feeds through the loaded runtime."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options=default_options())
    entry.add_to_hass(hass)
    runtime = _runtime()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_NEW_FEED_URL: "https://example.test/new.xml",
            CONF_REMOVE_FEED_ID: "feed_1",
            CONF_REMOVE_FEED_KEEP_HISTORY: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    runtime.coordinator.async_add_feed.assert_awaited_once_with("https://example.test/new.xml")
    runtime.coordinator.async_remove_feed.assert_awaited_once_with("feed_1", keep_history=False)


@pytest.mark.parametrize(
    ("side_effect", "expected_error"),
    [
        (PodcastParseError("invalid_url", "Invalid URL"), "invalid_url"),
        (PodcastParseError("no_audio_enclosures", "No audio"), "cannot_add_feed"),
        (HomeAssistantError("Cannot add"), "cannot_add_feed"),
        (RuntimeError("Unexpected"), "cannot_add_feed"),
    ],
)
async def test_options_flow_add_feed_errors_are_form_errors(
    hass,
    enable_custom_integrations,
    side_effect: Exception,
    expected_error: str,
) -> None:
    """Add-feed failures keep the options form open with a clean error."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options=default_options())
    entry.add_to_hass(hass)
    runtime = _runtime(add_feed_side_effect=side_effect)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_NEW_FEED_URL: "https://example.test/new.xml",
            CONF_REMOVE_FEED_ID: NO_FEED_SELECTION,
            CONF_REMOVE_FEED_KEEP_HISTORY: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_NEW_FEED_URL: expected_error}


async def test_options_flow_remove_missing_feed_is_form_error(hass, enable_custom_integrations) -> None:
    """Remove-feed failures keep the options form open with a clean error."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options=default_options())
    entry.add_to_hass(hass)
    runtime = _runtime(remove_feed_return=False)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_NEW_FEED_URL: "",
            CONF_REMOVE_FEED_ID: "feed_1",
            CONF_REMOVE_FEED_KEEP_HISTORY: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_REMOVE_FEED_ID: "feed_not_found"}


async def test_options_flow_feed_changes_require_loaded_runtime(hass, enable_custom_integrations) -> None:
    """Adding or removing feeds requires a loaded runtime."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options=default_options())
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            CONF_NEW_FEED_URL: "https://example.test/new.xml",
            CONF_REMOVE_FEED_ID: NO_FEED_SELECTION,
            CONF_REMOVE_FEED_KEEP_HISTORY: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "not_loaded"}


def test_normalize_options_merges_defaults() -> None:
    """Missing options are filled with stable defaults."""
    options = normalize_options({CONF_REFRESH_INTERVAL_MINUTES: 30})

    assert options == {
        CONF_REFRESH_INTERVAL_MINUTES: 30,
        CONF_MAX_EPISODES_PER_FEED: DEFAULT_MAX_EPISODES_PER_FEED,
        CONF_DEFAULT_PLAYBACK_SPEED: DEFAULT_PLAYBACK_SPEED,
        CONF_PLAYED_THRESHOLD: DEFAULT_PLAYED_THRESHOLD,
        CONF_DIRECT_FIRST: True,
        CONF_ENHANCED_DLNA_CONTROLS: True,
    }


def test_options_from_user_input_maps_url_mode() -> None:
    """The user-facing URL mode is stored as the existing direct_first setting."""
    options = options_from_user_input(
        {
            CONF_REFRESH_INTERVAL_MINUTES: 45,
            CONF_MAX_EPISODES_PER_FEED: 250,
            CONF_DEFAULT_PLAYBACK_SPEED: 1.25,
            CONF_PLAYED_THRESHOLD: 0.9,
            CONF_URL_MODE_PREFERENCE: URL_MODE_SIGNED_PROXY,
            CONF_ENHANCED_DLNA_CONTROLS: False,
        }
    )

    assert options == {
        CONF_REFRESH_INTERVAL_MINUTES: 45,
        CONF_MAX_EPISODES_PER_FEED: 250,
        CONF_DEFAULT_PLAYBACK_SPEED: 1.25,
        CONF_PLAYED_THRESHOLD: 0.9,
        CONF_DIRECT_FIRST: False,
        CONF_ENHANCED_DLNA_CONTROLS: False,
    }


def test_feed_select_options_are_sorted_and_include_noop() -> None:
    """Remove-feed choices are deterministic and include a safe no-op option."""
    choices = feed_select_options(
        [
            {"feed_id": "feed_b", "title": "Beta"},
            {"feed_id": "feed_a", "title": "Alpha"},
            {"title": "Missing ID"},
        ]
    )

    assert list(choices.items()) == [
        (NO_FEED_SELECTION, "Do not remove a feed"),
        ("feed_a", "Alpha"),
        ("feed_b", "Beta"),
    ]


def test_optional_rss_url_validation() -> None:
    """RSS URL validation accepts empty values and normalizes HTTP URLs."""
    assert _validate_optional_rss_url("") == ""
    assert _validate_optional_rss_url(" https://example.test/feed.xml ") == "https://example.test/feed.xml"

    with pytest.raises(vol.Invalid):
        _validate_optional_rss_url("ftp://example.test/feed.xml")


def test_rss_url_normalization_keeps_feed_ids_stable() -> None:
    """The shared RSS normalizer produces stable feed IDs across entry points."""
    normalized = normalize_rss_url(" HTTPS://Example.test/podcast.xml?token=ABC#ignored ")

    assert normalized == "https://example.test/podcast.xml?token=ABC#ignored"
    assert make_feed_id(normalized) == make_feed_id("https://example.test/podcast.xml?token=ABC#ignored")


def test_refresh_interval_from_settings_is_clamped() -> None:
    """Coordinator refresh intervals use safe bounds from options."""
    assert refresh_interval_from_settings({CONF_REFRESH_INTERVAL_MINUTES: 5}) == timedelta(minutes=15)
    assert refresh_interval_from_settings({CONF_REFRESH_INTERVAL_MINUTES: 60}) == timedelta(minutes=60)
    assert refresh_interval_from_settings({CONF_REFRESH_INTERVAL_MINUTES: 9999}) == timedelta(minutes=1440)
    assert refresh_interval_from_settings({CONF_REFRESH_INTERVAL_MINUTES: "bad"}) == timedelta(
        minutes=DEFAULT_REFRESH_INTERVAL_MINUTES
    )


def test_direct_url_mode_constant_remains_public_option() -> None:
    """The direct URL mode is available as a form option."""
    assert URL_MODE_DIRECT == "direct"
