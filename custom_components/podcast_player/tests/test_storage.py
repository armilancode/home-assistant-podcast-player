"""Regression tests for Podcast Player storage."""

import asyncio

import pytest

from custom_components.podcast_player.storage import (
    PodcastStorage,
    default_data,
    default_external_session,
    make_episode_id,
)


def _episode(episode_id: str, published: str) -> dict:
    return {
        "episode_id": episode_id,
        "title": episode_id,
        "published": published,
    }


def test_trimmed_known_episode_is_not_new_again() -> None:
    """A previously discovered item must remain known after cache trimming."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.data["settings"]["max_episodes_per_feed"] = 1

    old_episode = _episode("old", "2026-01-01T00:00:00+00:00")
    new_episode = _episode("new", "2026-02-01T00:00:00+00:00")

    assert storage.upsert_episodes("feed", [old_episode]) == [
        storage.data["episodes"]["old"]
    ]
    storage.upsert_episodes("feed", [new_episode])

    assert "old" not in storage.data["episodes"]
    assert "old" in storage.data["progress"]

    newly_discovered = storage.upsert_episodes(
        "feed", [old_episode, new_episode]
    )

    assert newly_discovered == []


def test_default_player_has_external_session() -> None:
    """New storage documents include a backend-owned external session."""
    data = default_data()

    assert data["settings"]["enhanced_dlna_controls"] is True
    assert data["player"]["external_session"] == default_external_session()
    assert data["player"]["external_session"]["active"] is False


def test_async_load_persists_new_default_keys() -> None:
    """Loading older storage documents persists newly introduced default keys."""
    class FakeStore:
        def __init__(self) -> None:
            self.saved = None

        async def async_load(self) -> dict:
            data = default_data()
            data["settings"].pop("enhanced_dlna_controls")
            data["player"].pop("external_session")
            return data

        async def async_save(self, data: dict) -> None:
            self.saved = data

    storage = PodcastStorage.__new__(PodcastStorage)
    storage._store = FakeStore()

    asyncio.run(storage.async_load())

    assert storage.data["settings"]["enhanced_dlna_controls"] is True
    assert storage.data["player"]["external_session"] == default_external_session()
    assert storage._store.saved is storage.data


def test_async_load_initializes_empty_storage() -> None:
    """Missing storage creates and saves the default document."""
    class FakeStore:
        def __init__(self) -> None:
            self.saved = None

        async def async_load(self) -> None:
            return None

        async def async_save(self, data: dict) -> None:
            self.saved = data

    storage = PodcastStorage.__new__(PodcastStorage)
    storage._store = FakeStore()

    asyncio.run(storage.async_load())

    assert storage.data["schema_version"] == 1
    assert storage._store.saved is storage.data


def test_async_load_repairs_missing_sections_and_external_session() -> None:
    """Stored documents with missing sections are repaired and saved."""
    class FakeStore:
        def __init__(self) -> None:
            self.saved = None

        async def async_load(self) -> dict:
            return {
                "schema_version": 0,
                "settings": {},
                "player": {"external_session": "bad"},
                "feeds": None,
            }

        async def async_save(self, data: dict) -> None:
            self.saved = data

    storage = PodcastStorage.__new__(PodcastStorage)
    storage._store = FakeStore()

    asyncio.run(storage.async_load())

    assert storage.data["schema_version"] == 1
    assert storage.data["feeds"] == {}
    assert storage.data["episodes"] == {}
    assert storage.data["progress"] == {}
    assert storage.data["player"]["external_session"] == default_external_session()
    assert storage._store.saved is storage.data


def test_async_load_merges_existing_external_session_and_skips_unneeded_save() -> None:
    """Current documents are not rewritten, while partial sessions are upgraded."""
    class CurrentStore:
        def __init__(self) -> None:
            self.saved = None

        async def async_load(self) -> dict:
            return default_data()

        async def async_save(self, data: dict) -> None:
            self.saved = data

    current = PodcastStorage.__new__(PodcastStorage)
    current._store = CurrentStore()

    asyncio.run(current.async_load())

    assert current._store.saved is None

    class PartialSessionStore:
        def __init__(self) -> None:
            self.saved = None

        async def async_load(self) -> dict:
            data = default_data()
            data["player"]["external_session"] = {"active": True}
            return data

        async def async_save(self, data: dict) -> None:
            self.saved = data

    partial = PodcastStorage.__new__(PodcastStorage)
    partial._store = PartialSessionStore()

    asyncio.run(partial.async_load())

    assert partial.data["player"]["external_session"]["active"] is True
    assert "transport_state" in partial.data["player"]["external_session"]
    assert partial._store.saved is partial.data


def test_make_episode_id_uses_stable_fallbacks() -> None:
    """Episode IDs are stable across guid, audio, and fallback identity paths."""
    assert make_episode_id("feed", "guid", None, None, None).startswith("ep_")
    assert make_episode_id("feed", None, "https://example.test/ep.mp3", None, None).startswith("ep_")
    assert make_episode_id("feed", None, None, "Episode", "2026-01-01").startswith("ep_")


def test_storage_feed_lifecycle_and_counts() -> None:
    """Feeds, episodes, progress, and counts update consistently."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()

    feed = {
        "feed_id": "feed_1",
        "rss_url": "https://example.test/feed.xml",
        "title": "Example Podcast",
        "status": "ok",
    }
    assert storage.upsert_feed(feed) is True
    assert storage.upsert_feed({**feed, "author": "Host"}) is False
    assert storage.get_feed("feed_1")["enabled"] is True
    assert storage.enabled_feeds()[0]["feed_id"] == "feed_1"

    episode = {
        "episode_id": "episode_1",
        "feed_id": "feed_1",
        "title": "Episode One",
        "published": "2026-01-01T00:00:00+00:00",
        "duration_seconds": 100,
    }
    assert storage.upsert_episodes("feed_1", [episode])[0]["episode_id"] == "episode_1"
    storage.save_progress("episode_1", 96, duration=100, playing=True, speed=1.25)

    counts = storage.counts()
    assert counts["enabled_feeds"] == 1
    assert counts["total_episodes"] == 1
    assert counts["unplayed"] == 0
    assert storage.data["progress"]["episode_1"]["played"] is True
    assert storage.data["player"]["state"] == "playing"
    assert storage.data["player"]["current_feed_id"] == "feed_1"

    storage.mark_played("episode_1", False)
    assert storage.data["progress"]["episode_1"]["played"] is False
    assert storage.counts()["partially_played"] == 1

    storage.mark_feed_failed("feed_1", "timeout", "Timed out")
    assert storage.get_feed("feed_1")["status"] == "failed"
    assert storage.counts()["failed_feeds"] == 1
    storage.mark_feed_failed("missing", "timeout", "Timed out")

    assert storage.remove_feed("missing") is False
    assert storage.remove_feed("feed_1", keep_history=True) is True
    assert "episode_1" in storage.data["episodes"]
    assert "episode_1" in storage.data["progress"]


def test_remove_feed_can_delete_history() -> None:
    """Removing a feed can also remove cached episodes and progress."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.upsert_feed({"feed_id": "feed_1", "rss_url": "https://example.test/feed.xml"})
    storage.upsert_episodes("feed_1", [{"episode_id": "episode_1", "feed_id": "feed_1"}])

    assert storage.remove_feed("feed_1", keep_history=False) is True
    assert "episode_1" not in storage.data["episodes"]
    assert "episode_1" not in storage.data["progress"]


def test_trim_keeps_in_progress_old_episode() -> None:
    """Episode trimming preserves old in-progress episodes."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.data["settings"]["max_episodes_per_feed"] = 1

    old_episode = {"episode_id": "old", "feed_id": "feed_1", "published": "2026-01-01T00:00:00+00:00"}
    new_episode = {"episode_id": "new", "feed_id": "feed_1", "published": "2026-02-01T00:00:00+00:00"}
    storage.upsert_episodes("feed_1", [old_episode])
    storage.save_progress("old", 10, duration=100, playing=False)

    storage.upsert_episodes("feed_1", [new_episode])

    assert "old" in storage.data["episodes"]
    assert "new" in storage.data["episodes"]


def test_player_state_and_speed_updates() -> None:
    """Player state and speed helpers update expected fields and validate speed."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    storage.upsert_feed({"feed_id": "feed_1", "rss_url": "https://example.test/feed.xml"})
    storage.upsert_episodes(
        "feed_1",
        [{"episode_id": "episode_1", "feed_id": "feed_1", "duration_seconds": 45}],
    )

    storage.set_player_state("playing", "episode_1")
    assert storage.data["player"]["current_episode_id"] == "episode_1"
    assert storage.data["player"]["duration"] == 45

    storage.set_speed(1.5, "episode_1")
    assert storage.data["settings"]["default_playback_speed"] == 1.5
    assert storage.data["progress"]["episode_1"]["playback_speed"] == 1.5

    with pytest.raises(ValueError, match="Unsupported playback speed"):
        storage.set_speed(9.0)


def test_progress_and_speed_edge_inputs() -> None:
    """Progress ignores invalid optional values and speed can update defaults only."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()

    progress = storage.save_progress("episode_1", 12, duration="bad", playing=None, speed=9.0)

    assert progress["duration"] is None
    assert progress["playback_speed"] == 1.0
    assert storage.data["player"]["state"] == "idle"

    storage.set_speed(1.5)

    assert storage.data["settings"]["default_playback_speed"] == 1.5
    assert storage.data["player"]["speed"] == 1.5


def test_snapshot_is_deep_copy() -> None:
    """Snapshots do not expose mutable storage internals."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    snapshot = storage.snapshot()
    snapshot["settings"]["refresh_interval_minutes"] = 999

    assert storage.data["settings"]["refresh_interval_minutes"] != 999
