"""Tests for podcast dependency model normalization."""

from datetime import UTC, datetime

import pytest
from aiopodcast import (
    FeedTooLargeError,
    InvalidFeedError,
    NoEpisodesError,
    NoPlayableEpisodesError,
    Podcast,
    PodcastConnectionError,
    PodcastEnclosure,
    PodcastEpisode,
    PodcastFeedError,
    PodcastHTTPError,
    PodcastRedirectError,
    PodcastSSLError,
    PodcastTimeoutError,
)

from custom_components.podcast_player.feed_parser import normalize_podcast, podcast_parse_error
from custom_components.podcast_player.storage import make_episode_id


def test_normalize_podcast_preserves_storage_contract() -> None:
    """Typed podcast models are converted without changing persisted field shapes."""
    published = datetime(2026, 6, 25, 10, tzinfo=UTC)
    podcast = Podcast(
        source_url="https://example.test/feed.xml",
        canonical_url="https://cdn.example.test/feed.xml",
        title="Example Podcast",
        description="Feed description",
        author="Example Host",
        website_url="https://example.test/podcast",
        artwork_url="https://example.test/feed.jpg",
        episodes=(
            PodcastEpisode(
                title="Episode One",
                enclosure=PodcastEnclosure(
                    "https://cdn.example.test/episode-1.mp3",
                    mime_type="audio/mpeg",
                    length=12345,
                ),
                guid="episode-1",
                description="Episode description",
                published=published,
                duration_seconds=3723,
                artwork_url="https://example.test/episode.jpg",
                website_url="https://example.test/episodes/1",
                explicit="no",
                season="2",
                episode_number="7",
            ),
            PodcastEpisode(
                title="Episode Two",
                enclosure=PodcastEnclosure("https://cdn.example.test/episode-2.ogg"),
            ),
        ),
    )

    normalized = normalize_podcast(podcast, "feed_abc")

    assert normalized["feed"] == {
        "feed_id": "feed_abc",
        "rss_url": "https://example.test/feed.xml",
        "title": "Example Podcast",
        "description": "Feed description",
        "author": "Example Host",
        "website": "https://example.test/podcast",
        "artwork_url": "https://example.test/feed.jpg",
        "status": "ok",
        "last_error": None,
        "episode_count": 2,
    }
    assert normalized["canonical_url"] == "https://cdn.example.test/feed.xml"

    first = normalized["episodes"][0]
    assert first == {
        "episode_id": make_episode_id(
            "feed_abc",
            "episode-1",
            "https://cdn.example.test/episode-1.mp3",
            "Episode One",
            "2026-06-25T10:00:00+00:00",
        ),
        "feed_id": "feed_abc",
        "guid": "episode-1",
        "title": "Episode One",
        "description": "Episode description",
        "published": "2026-06-25T10:00:00+00:00",
        "duration_seconds": 3723,
        "audio_url": "https://cdn.example.test/episode-1.mp3",
        "audio_type": "audio/mpeg",
        "audio_size": "12345",
        "artwork_url": "https://example.test/episode.jpg",
        "website_url": "https://example.test/episodes/1",
        "explicit": "no",
        "season": "2",
        "episode_number": "7",
    }

    second = normalized["episodes"][1]
    assert second["published"] is None
    assert second["audio_size"] is None
    assert second["artwork_url"] == "https://example.test/feed.jpg"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NoEpisodesError("No episodes"), "no_episodes"),
        (NoPlayableEpisodesError("No playable episodes"), "no_audio_enclosures"),
        (InvalidFeedError("Invalid XML"), "parse_error"),
        (FeedTooLargeError(1024), "too_large"),
        (PodcastHTTPError(503), "http_error"),
        (PodcastTimeoutError("Timed out"), "timeout"),
        (PodcastSSLError("TLS failed"), "ssl_error"),
        (PodcastRedirectError("Too many redirects"), "redirect_loop"),
        (PodcastConnectionError("Offline"), "cannot_connect"),
        (PodcastFeedError("Unknown feed failure"), "parse_error"),
    ],
)
def test_podcast_parse_error_maps_dependency_failures(error: PodcastFeedError, code: str) -> None:
    """Dependency exceptions retain stable codes used by flows, events, and storage."""
    mapped = podcast_parse_error(error)

    assert mapped.code == code
    assert mapped.message == str(error)
    assert str(mapped) == str(error)
