"""Tests for translated Podcast Player exceptions."""

import ast
import json
from pathlib import Path
from string import Formatter

from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import Unresolvable
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.podcast_player import FEED_ERROR_TRANSLATION_KEYS
from custom_components.podcast_player.const import DOMAIN
from custom_components.podcast_player.exceptions import exception_message, translated_error

INTEGRATION_PATH = Path(__file__).parents[1]
HOME_ASSISTANT_EXCEPTION_TYPES = (
    BrowseError,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
    Unresolvable,
    UpdateFailed,
)
HOME_ASSISTANT_EXCEPTION_TYPE_NAMES = {exception_type.__name__ for exception_type in HOME_ASSISTANT_EXCEPTION_TYPES}


def _exception_resources() -> dict[str, dict[str, str]]:
    """Return the integration's English exception resources."""
    return json.loads((INTEGRATION_PATH / "strings.json").read_text())["exceptions"]


def _message_placeholders(message: str) -> set[str]:
    """Return placeholder names referenced by a translated message."""
    return {field_name for _, field_name, _, _ in Formatter().parse(message) if field_name is not None}


def test_translated_error_sets_home_assistant_metadata() -> None:
    """The helper builds typed exceptions with normalized string placeholders."""
    error = translated_error(
        ServiceValidationError,
        "feed_not_found",
        feed_id="feed_1",
        attempt=2,
    )
    plain_error = translated_error(HomeAssistantError, "not_configured")

    assert isinstance(error, ServiceValidationError)
    assert error.translation_domain == DOMAIN
    assert error.translation_key == "feed_not_found"
    assert error.translation_placeholders == {
        "feed_id": "feed_1",
        "attempt": "2",
    }
    assert plain_error.translation_placeholders is None


def test_translated_error_supports_every_used_exception_type() -> None:
    """Every Home Assistant exception subclass accepts translation metadata."""
    for error_type in HOME_ASSISTANT_EXCEPTION_TYPES:
        error = translated_error(error_type, "not_configured")

        assert isinstance(error, error_type)
        assert error.translation_domain == DOMAIN
        assert error.translation_key == "not_configured"


def test_exception_message_has_context_free_fallback() -> None:
    """Persisted diagnostics can represent translated errors outside HA context."""
    error = translated_error(HomeAssistantError, "not_configured")
    plain_error = HomeAssistantError("Plain error")

    assert exception_message(plain_error) == "Plain error"
    assert exception_message(error) == "not_configured"


def test_all_home_assistant_exceptions_use_translation_resources() -> None:
    """Every production Home Assistant exception maps to one tested resource."""
    resources = _exception_resources()
    english = json.loads((INTEGRATION_PATH / "translations" / "en.json").read_text())
    used_keys = set(FEED_ERROR_TRANSLATION_KEYS.values()) | {"feed_add_failed"}

    assert resources == english["exceptions"]
    for path in INTEGRATION_PATH.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            assert node.func.id not in HOME_ASSISTANT_EXCEPTION_TYPE_NAMES
            if node.func.id != "translated_error":
                continue
            assert len(node.args) >= 2
            key_node = node.args[1]
            if not isinstance(key_node, ast.Constant):
                assert path.name == "__init__.py"
                assert not node.keywords
                continue
            assert isinstance(key_node.value, str)
            translation_key = key_node.value
            used_keys.add(translation_key)
            expected_placeholders = _message_placeholders(resources[translation_key]["message"])
            actual_placeholders = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
            assert actual_placeholders == expected_placeholders

    assert used_keys == set(resources)


async def test_exception_resources_load_in_home_assistant(hass, enable_custom_integrations) -> None:
    """Home Assistant can load exception messages for the custom integration."""
    translations = await async_get_translations(hass, "en", "exceptions", integrations={DOMAIN})

    assert (
        translations[f"component.{DOMAIN}.exceptions.feed_not_found.message"] == "Podcast feed {feed_id} was not found."
    )
    assert (
        translations[f"component.{DOMAIN}.exceptions.media_player_target_field.message"]
        == "{service}: The action target selects a podcast feed. Set the output media player using media_player_entity_id."
    )
