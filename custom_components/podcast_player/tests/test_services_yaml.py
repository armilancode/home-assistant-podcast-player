"""Tests for Podcast Player service/action descriptions."""

from pathlib import Path
from typing import Any

import yaml

SERVICES_YAML = Path(__file__).parents[1] / "services.yaml"


def _service_fields(service: dict[str, Any]) -> set[str]:
    """Return documented field names for one service."""
    fields = service.get("fields") or {}
    return set(fields)


def test_services_yaml_parses() -> None:
    """Service descriptions must be valid YAML."""
    services = yaml.safe_load(SERVICES_YAML.read_text())

    assert isinstance(services, dict)
    assert "play_episode" in services
    assert "play_on_media_player" in services


def test_services_yaml_hides_compatibility_only_fields() -> None:
    """Compatibility-only fields stay accepted by schemas without cluttering the UI."""
    services = yaml.safe_load(SERVICES_YAML.read_text())

    for service_name, service in services.items():
        fields = _service_fields(service)
        assert "prefer_proxy" not in fields
        assert "feed_name" not in fields
        if service_name != "remove_feed":
            assert "feed_id" not in fields


def test_services_yaml_has_professional_user_facing_text() -> None:
    """Avoid exposing internal compatibility wording in action descriptions."""
    text = SERVICES_YAML.read_text().casefold()

    for forbidden in (
        "le" + "gacy",
        "backward-" + "compatible",
        "speaker/tv",
        "hacs",
        "al" + "pha",
    ):
        assert forbidden not in text
