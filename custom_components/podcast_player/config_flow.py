"""Config flow for HA Podcast Player."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import DEFAULT_MAX_EPISODES_PER_FEED, DOMAIN, NAME


class PodcastPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Podcast Player."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "PodcastPlayerOptionsFlow":
        """Return the options flow."""
        return PodcastPlayerOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            errors={},
        )


class PodcastPlayerOptionsFlow(config_entries.OptionsFlow):
    """Handle Podcast Player options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Manage integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = {
            "refresh_interval_minutes": 120,
            "max_episodes_per_feed": DEFAULT_MAX_EPISODES_PER_FEED,
            "direct_first": True,
        }
        options.update(dict(self.config_entry.options))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "refresh_interval_minutes",
                        default=options["refresh_interval_minutes"],
                    ): vol.All(vol.Coerce(int), vol.Range(min=15, max=1440)),
                    vol.Optional(
                        "max_episodes_per_feed",
                        default=options["max_episodes_per_feed"],
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=1000)),
                    vol.Optional("direct_first", default=options["direct_first"]): cv.boolean,
                }
            ),
        )
