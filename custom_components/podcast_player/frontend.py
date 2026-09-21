"""Bundled frontend card registration for Podcast Player."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components import frontend
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.const import CONF_ID, CONF_URL
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "podcast-player-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / CARD_FILENAME
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"
CARD_MODULE_URL = f"{CARD_URL_PATH}?v={VERSION}"

FRONTEND_STATUS_KEY = f"{DOMAIN}_frontend_status"
STATIC_REGISTERED_KEY = f"{DOMAIN}_frontend_static_registered"

LEGACY_CARD_PATHS = {
    "/local/podcast-player-card/podcast-player-card.js",
    "/local/podcast-player-card.js",
    "/hacsfiles/home-assistant-podcast-player/podcast-player-card.js",
}


def _is_legacy_card_url(url: Any) -> bool:
    """Return whether a Lovelace resource is an older manual card URL."""
    if not isinstance(url, str):
        return False
    return urlsplit(url).path in LEGACY_CARD_PATHS


def _is_managed_module_url(url: str) -> bool:
    """Return whether an extra module URL belongs to this integration."""
    return urlsplit(url).path == CARD_URL_PATH


async def _async_migrate_legacy_resources(hass: HomeAssistant) -> tuple[int, bool]:
    """Remove obsolete storage-mode resources after the bundled card is active.

    YAML resources remain user-managed. Loading the bundled module alongside an
    older YAML entry is safe because custom-element registration is idempotent.
    """
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        return 0, False

    resources = lovelace_data.resources
    await resources.async_get_info()
    legacy_items = [
        item
        for item in resources.async_items()
        if _is_legacy_card_url(item.get(CONF_URL))
    ]
    if not legacy_items:
        return 0, False

    if lovelace_data.resource_mode != MODE_STORAGE:
        _LOGGER.warning(
            "Podcast Player loaded its bundled card, but a legacy card resource "
            "is still present in YAML. Remove the old %s resource from the "
            "Lovelace YAML configuration",
            legacy_items[0].get(CONF_URL),
        )
        return 0, True

    removed = 0
    for item in legacy_items:
        item_id = item.get(CONF_ID)
        if not item_id:
            continue
        try:
            await resources.async_delete_item(item_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Podcast Player could not remove legacy Lovelace resource %s",
                item.get(CONF_URL),
            )
            continue
        removed += 1
        _LOGGER.info(
            "Podcast Player migrated legacy Lovelace resource %s to bundled card %s",
            item.get(CONF_URL),
            CARD_MODULE_URL,
        )
    return removed, False


async def async_setup_card_frontend(hass: HomeAssistant) -> dict[str, Any]:
    """Serve and load the bundled card through supported frontend APIs."""
    status: dict[str, Any] = {
        "available": False,
        "version": VERSION,
        "module_url": CARD_MODULE_URL,
        "legacy_resources_removed": 0,
        "legacy_yaml_resource_present": False,
    }

    if frontend.DOMAIN not in hass.config.components or DATA_EXTRA_MODULE_URL not in hass.data:
        _LOGGER.warning(
            "Podcast Player backend loaded without the Home Assistant frontend; "
            "the bundled dashboard card was not registered"
        )
        hass.data[FRONTEND_STATUS_KEY] = status
        return status

    if not hass.data.get(STATIC_REGISTERED_KEY):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(CARD_PATH), cache_headers=True)]
        )
        hass.data[STATIC_REGISTERED_KEY] = True

    module_manager = hass.data[DATA_EXTRA_MODULE_URL]
    for existing_url in tuple(module_manager.urls):
        if _is_managed_module_url(existing_url) and existing_url != CARD_MODULE_URL:
            frontend.remove_extra_js_url(hass, existing_url)
    if CARD_MODULE_URL not in module_manager.urls:
        frontend.add_extra_js_url(hass, CARD_MODULE_URL)

    removed, yaml_present = await _async_migrate_legacy_resources(hass)
    status.update(
        {
            "available": True,
            "legacy_resources_removed": removed,
            "legacy_yaml_resource_present": yaml_present,
        }
    )
    hass.data[FRONTEND_STATUS_KEY] = status
    return status


def async_unload_card_frontend(hass: HomeAssistant) -> None:
    """Stop advertising the card module when the integration unloads."""
    module_manager = hass.data.get(DATA_EXTRA_MODULE_URL)
    if module_manager is not None and CARD_MODULE_URL in module_manager.urls:
        frontend.remove_extra_js_url(hass, CARD_MODULE_URL)
    if status := hass.data.get(FRONTEND_STATUS_KEY):
        status["available"] = False
