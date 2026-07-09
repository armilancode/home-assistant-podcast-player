"""Config flow for HA Podcast Player."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ALLOWED_SPEEDS,
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
    URL_MODE_DIRECT,
    URL_MODE_SIGNED_PROXY,
)
from .coordinator import PodcastRuntime, async_validate_feed_url
from .feed_parser import PodcastParseError
from .storage import normalize_rss_url

_LOGGER = logging.getLogger(__name__)

NO_FEED_SELECTION = "__none__"
CONNECTIVITY_FEED_ERROR_CODES = {"cannot_connect", "http_error", "redirect_loop", "ssl_error", "timeout"}
URL_MODE_OPTIONS = {
    URL_MODE_DIRECT: "Direct podcast URL",
    URL_MODE_SIGNED_PROXY: "Signed Home Assistant proxy URL",
}


def default_options() -> dict[str, Any]:
    """Return default config entry options."""
    return {
        CONF_REFRESH_INTERVAL_MINUTES: DEFAULT_REFRESH_INTERVAL_MINUTES,
        CONF_MAX_EPISODES_PER_FEED: DEFAULT_MAX_EPISODES_PER_FEED,
        CONF_DEFAULT_PLAYBACK_SPEED: DEFAULT_PLAYBACK_SPEED,
        CONF_PLAYED_THRESHOLD: DEFAULT_PLAYED_THRESHOLD,
        CONF_DIRECT_FIRST: True,
        CONF_ENHANCED_DLNA_CONTROLS: True,
    }


def normalize_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Return supported options merged with defaults."""
    normalized = default_options()
    normalized.update(dict(options or {}))
    return normalized


def _url_mode_from_direct_first(direct_first: bool) -> str:
    """Return the user-facing URL preference."""
    return URL_MODE_DIRECT if direct_first else URL_MODE_SIGNED_PROXY


def _validate_optional_rss_url(value: Any) -> str:
    """Validate an optional RSS URL and return a normalized value."""
    if value in (None, ""):
        return ""
    try:
        return normalize_rss_url(str(value))
    except ValueError as err:
        raise vol.Invalid(str(err)) from err


def options_from_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return persistable options from a config/options form submission."""
    return {
        CONF_REFRESH_INTERVAL_MINUTES: int(user_input[CONF_REFRESH_INTERVAL_MINUTES]),
        CONF_MAX_EPISODES_PER_FEED: int(user_input[CONF_MAX_EPISODES_PER_FEED]),
        CONF_DEFAULT_PLAYBACK_SPEED: float(user_input[CONF_DEFAULT_PLAYBACK_SPEED]),
        CONF_PLAYED_THRESHOLD: float(user_input[CONF_PLAYED_THRESHOLD]),
        CONF_DIRECT_FIRST: user_input[CONF_URL_MODE_PREFERENCE] == URL_MODE_DIRECT,
        CONF_ENHANCED_DLNA_CONTROLS: bool(user_input[CONF_ENHANCED_DLNA_CONTROLS]),
    }


def feed_select_options(feeds: list[dict[str, Any]]) -> dict[str, str]:
    """Return remove-feed choices sorted by display title."""
    choices = {NO_FEED_SELECTION: "Do not remove a feed"}
    for feed in sorted(feeds, key=lambda item: str(item.get("title") or item.get("feed_id") or "").casefold()):
        feed_id = feed.get("feed_id")
        if feed_id:
            choices[str(feed_id)] = str(feed.get("title") or feed_id)
    return choices


def _config_flow_feed_error(err: PodcastParseError) -> str:
    """Map feed probe failures to config flow translation keys."""
    if err.code == "invalid_url":
        return "invalid_url"
    if err.code in CONNECTIVITY_FEED_ERROR_CODES:
        return "cannot_connect"
    return "cannot_add_feed"


def _settings_schema(options: dict[str, Any], *, include_initial_feed: bool = False, feeds: list[dict[str, Any]] | None = None) -> vol.Schema:
    """Return the shared settings schema."""
    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_REFRESH_INTERVAL_MINUTES,
            default=options[CONF_REFRESH_INTERVAL_MINUTES],
        ): vol.All(vol.Coerce(int), vol.Range(min=15, max=1440)),
        vol.Optional(
            CONF_MAX_EPISODES_PER_FEED,
            default=options[CONF_MAX_EPISODES_PER_FEED],
        ): vol.All(vol.Coerce(int), vol.Range(min=10, max=1000)),
        vol.Optional(
            CONF_DEFAULT_PLAYBACK_SPEED,
            default=options[CONF_DEFAULT_PLAYBACK_SPEED],
        ): vol.All(vol.Coerce(float), vol.In(ALLOWED_SPEEDS)),
        vol.Optional(
            CONF_PLAYED_THRESHOLD,
            default=options[CONF_PLAYED_THRESHOLD],
        ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
        vol.Optional(
            CONF_URL_MODE_PREFERENCE,
            default=_url_mode_from_direct_first(bool(options[CONF_DIRECT_FIRST])),
        ): vol.In(URL_MODE_OPTIONS),
        vol.Optional(
            CONF_ENHANCED_DLNA_CONTROLS,
            default=bool(options[CONF_ENHANCED_DLNA_CONTROLS]),
        ): cv.boolean,
    }

    if include_initial_feed:
        schema[vol.Optional(CONF_INITIAL_RSS_URL, default="")] = _validate_optional_rss_url
    else:
        schema[vol.Optional(CONF_NEW_FEED_URL, default="")] = _validate_optional_rss_url
        schema[vol.Optional(CONF_REMOVE_FEED_ID, default=NO_FEED_SELECTION)] = vol.In(feed_select_options(feeds or []))
        schema[vol.Optional(CONF_REMOVE_FEED_KEEP_HISTORY, default=True)] = cv.boolean

    return vol.Schema(schema)


def _runtime_for_entry(hass: Any, config_entry: ConfigEntry) -> PodcastRuntime | None:
    """Return runtime data for an options flow if the entry is loaded."""
    entries = hass.data.get(DOMAIN, {})
    runtime = entries.get(config_entry.entry_id)
    if runtime is not None:
        return runtime
    return next(iter(entries.values()), None) if entries else None


class PodcastPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Podcast Player."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "PodcastPlayerOptionsFlow":
        """Return the options flow."""
        return PodcastPlayerOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}

        if user_input is not None:
            options = options_from_user_input(user_input)
            data = {}
            if user_input.get(CONF_INITIAL_RSS_URL):
                try:
                    await async_validate_feed_url(self.hass, user_input[CONF_INITIAL_RSS_URL])
                except PodcastParseError as err:
                    errors[CONF_INITIAL_RSS_URL] = _config_flow_feed_error(err)
                except Exception:  # noqa: BLE001 - config flow must return a clean form error
                    _LOGGER.exception("Unexpected error while validating initial podcast feed")
                    errors[CONF_INITIAL_RSS_URL] = "cannot_add_feed"
                else:
                    data[CONF_INITIAL_RSS_URL] = user_input[CONF_INITIAL_RSS_URL]
            if not errors:
                return self.async_create_entry(title=NAME, data=data, options=options)

        return self.async_show_form(
            step_id="user",
            data_schema=_settings_schema(default_options(), include_initial_feed=True),
            errors=errors,
        )


class PodcastPlayerOptionsFlow(config_entries.OptionsFlow):
    """Handle Podcast Player options."""

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Manage integration options."""
        runtime = _runtime_for_entry(self.hass, self.config_entry)
        feeds = list(runtime.storage.data["feeds"].values()) if runtime is not None else []
        errors: dict[str, str] = {}

        if user_input is not None:
            options = options_from_user_input(user_input)
            new_feed_url = user_input.get(CONF_NEW_FEED_URL)
            remove_feed_id = user_input.get(CONF_REMOVE_FEED_ID)
            remove_requested = remove_feed_id not in (None, NO_FEED_SELECTION)

            if runtime is None and (new_feed_url or remove_requested):
                errors["base"] = "not_loaded"

            if runtime is not None and new_feed_url:
                try:
                    await runtime.coordinator.async_add_feed(new_feed_url)
                except PodcastParseError as err:
                    errors[CONF_NEW_FEED_URL] = err.code if err.code in {"invalid_url"} else "cannot_add_feed"
                except HomeAssistantError:
                    errors[CONF_NEW_FEED_URL] = "cannot_add_feed"
                except Exception:  # noqa: BLE001 - config flow must return a clean form error
                    _LOGGER.exception("Unexpected error while adding podcast feed from options")
                    errors[CONF_NEW_FEED_URL] = "cannot_add_feed"

            if runtime is not None and remove_requested:
                removed = await runtime.coordinator.async_remove_feed(
                    remove_feed_id,
                    keep_history=bool(user_input.get(CONF_REMOVE_FEED_KEEP_HISTORY, True)),
                )
                if not removed:
                    errors[CONF_REMOVE_FEED_ID] = "feed_not_found"

            if not errors:
                return self.async_create_entry(title="", data=options)

            feeds = list(runtime.storage.data["feeds"].values()) if runtime is not None else feeds

        options = normalize_options(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(options, feeds=feeds),
            errors=errors,
        )
