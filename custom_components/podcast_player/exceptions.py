"""Translated exception helpers for Podcast Player."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


def translated_error[_ErrorT: HomeAssistantError](
    error_type: type[_ErrorT],
    translation_key: str,
    **translation_placeholders: object,
) -> _ErrorT:
    """Create a Home Assistant exception backed by integration translations."""
    placeholders = {key: str(value) for key, value in translation_placeholders.items()}
    return error_type(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders=placeholders or None,
    )


def exception_message(error: HomeAssistantError) -> str:
    """Return an English message in HA context or a stable key otherwise."""
    try:
        return str(error)
    except HomeAssistantError:
        return error.translation_key or error.__class__.__name__
