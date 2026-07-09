"""Tests for feed refresh availability logging."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.podcast_player.coordinator import PodcastUpdateCoordinator
from custom_components.podcast_player.feed_parser import PodcastParseError
from custom_components.podcast_player.storage import PodcastStorage, default_data


class FakeBus:
    """Minimal event bus for coordinator tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event_data: dict) -> None:
        """Record fired events."""
        self.events.append((event_type, event_data))


def _coordinator(*, feed_status: str = "ok") -> tuple[PodcastUpdateCoordinator, dict]:
    """Return a coordinator shell with one feed."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    feed = {
        "feed_id": "feed_1",
        "rss_url": "https://example.test/feed.xml",
        "title": "Example Podcast",
        "status": feed_status,
        "enabled": True,
    }
    if feed_status == "failed":
        feed["last_error"] = {"code": "timeout", "message": "Old failure"}
    storage.data["feeds"]["feed_1"] = feed
    storage.async_save = AsyncMock()

    coordinator = PodcastUpdateCoordinator.__new__(PodcastUpdateCoordinator)
    coordinator.storage = storage
    coordinator.hass = SimpleNamespace(bus=FakeBus())
    coordinator.async_set_updated_data = lambda data: None
    return coordinator, feed


@pytest.mark.asyncio
async def test_feed_refresh_logs_unavailable_once(caplog) -> None:
    """Feed refresh failures are logged once while the feed remains failed."""
    coordinator, feed = _coordinator()
    coordinator._async_refresh_single_url = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            PodcastParseError("timeout", "Feed server timed out."),
            PodcastParseError("timeout", "Feed server timed out."),
        ]
    )

    await coordinator._async_refresh_existing_feed(feed)
    await coordinator._async_refresh_existing_feed(feed)

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("Podcast feed Example Podcast is unavailable: Feed server timed out.") == 1
    assert feed["status"] == "failed"


@pytest.mark.asyncio
async def test_feed_refresh_logs_recovery(caplog) -> None:
    """A previously failed feed logs when it refreshes successfully again."""
    coordinator, feed = _coordinator(feed_status="failed")
    coordinator._async_refresh_single_url = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "feed": {
                "feed_id": "feed_1",
                "rss_url": "https://example.test/feed.xml",
                "title": "Example Podcast",
                "status": "ok",
                "last_error": None,
            },
            "episodes": [],
            "canonical_url": "https://example.test/feed.xml",
        }
    )

    await coordinator._async_refresh_existing_feed(feed)

    messages = [record.getMessage() for record in caplog.records]
    assert "Podcast feed Example Podcast is back online" in messages
    assert coordinator.storage.data["feeds"]["feed_1"]["status"] == "ok"
