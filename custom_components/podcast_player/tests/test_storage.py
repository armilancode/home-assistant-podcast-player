"""Regression tests for Podcast Player storage."""

from custom_components.podcast_player.storage import PodcastStorage, default_data


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
