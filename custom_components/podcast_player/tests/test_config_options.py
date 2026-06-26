"""Tests for Podcast Player config/options helpers."""

from datetime import timedelta

import pytest
import voluptuous as vol

from custom_components.podcast_player.config_flow import (
    NO_FEED_SELECTION,
    URL_MODE_DIRECT,
    URL_MODE_SIGNED_PROXY,
    _validate_optional_rss_url,
    feed_select_options,
    normalize_options,
    options_from_user_input,
)
from custom_components.podcast_player.const import (
    CONF_DEFAULT_PLAYBACK_SPEED,
    CONF_DIRECT_FIRST,
    CONF_MAX_EPISODES_PER_FEED,
    CONF_PLAYED_THRESHOLD,
    CONF_REFRESH_INTERVAL_MINUTES,
    CONF_URL_MODE_PREFERENCE,
    DEFAULT_MAX_EPISODES_PER_FEED,
    DEFAULT_PLAYBACK_SPEED,
    DEFAULT_PLAYED_THRESHOLD,
    DEFAULT_REFRESH_INTERVAL_MINUTES,
)
from custom_components.podcast_player.coordinator import refresh_interval_from_settings
from custom_components.podcast_player.storage import make_feed_id, normalize_rss_url


def test_normalize_options_merges_defaults() -> None:
    """Missing options are filled with stable defaults."""
    options = normalize_options({CONF_REFRESH_INTERVAL_MINUTES: 30})

    assert options == {
        CONF_REFRESH_INTERVAL_MINUTES: 30,
        CONF_MAX_EPISODES_PER_FEED: DEFAULT_MAX_EPISODES_PER_FEED,
        CONF_DEFAULT_PLAYBACK_SPEED: DEFAULT_PLAYBACK_SPEED,
        CONF_PLAYED_THRESHOLD: DEFAULT_PLAYED_THRESHOLD,
        CONF_DIRECT_FIRST: True,
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
        }
    )

    assert options == {
        CONF_REFRESH_INTERVAL_MINUTES: 45,
        CONF_MAX_EPISODES_PER_FEED: 250,
        CONF_DEFAULT_PLAYBACK_SPEED: 1.25,
        CONF_PLAYED_THRESHOLD: 0.9,
        CONF_DIRECT_FIRST: False,
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
