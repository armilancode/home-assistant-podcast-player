"""Podcast feed normalization and error helpers."""

from __future__ import annotations

from typing import Any

from aiopodcast import (
    FeedTooLargeError,
    InvalidFeedError,
    NoEpisodesError,
    NoPlayableEpisodesError,
    Podcast,
    PodcastConnectionError,
    PodcastFeedError,
    PodcastHTTPError,
    PodcastRedirectError,
    PodcastSSLError,
    PodcastTimeoutError,
)

from .storage import make_episode_id


class PodcastParseError(Exception):
    """Raised when a feed cannot be fetched or parsed as a podcast."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def podcast_parse_error(error: PodcastFeedError) -> PodcastParseError:
    """Convert dependency exceptions to stable integration error codes."""
    if isinstance(error, NoEpisodesError):
        code = "no_episodes"
    elif isinstance(error, NoPlayableEpisodesError):
        code = "no_audio_enclosures"
    elif isinstance(error, InvalidFeedError):
        code = "parse_error"
    elif isinstance(error, FeedTooLargeError):
        code = "too_large"
    elif isinstance(error, PodcastHTTPError):
        code = "http_error"
    elif isinstance(error, PodcastTimeoutError):
        code = "timeout"
    elif isinstance(error, PodcastSSLError):
        code = "ssl_error"
    elif isinstance(error, PodcastRedirectError):
        code = "redirect_loop"
    elif isinstance(error, PodcastConnectionError):
        code = "cannot_connect"
    else:
        code = "parse_error"
    return PodcastParseError(code, str(error))


def normalize_podcast(podcast: Podcast, feed_id: str) -> dict[str, Any]:
    """Convert typed dependency models to the persisted storage contract."""
    feed = {
        "feed_id": feed_id,
        "rss_url": podcast.source_url,
        "title": podcast.title,
        "description": podcast.description,
        "author": podcast.author,
        "website": podcast.website_url,
        "artwork_url": podcast.artwork_url,
        "status": "ok",
        "last_error": None,
        "episode_count": len(podcast.episodes),
    }

    episodes: list[dict[str, Any]] = []
    for episode in podcast.episodes:
        published = episode.published.isoformat() if episode.published is not None else None
        audio_size = str(episode.enclosure.length) if episode.enclosure.length is not None else None
        episodes.append(
            {
                "episode_id": make_episode_id(
                    feed_id,
                    episode.guid,
                    episode.enclosure.url,
                    episode.title,
                    published,
                ),
                "feed_id": feed_id,
                "guid": episode.guid,
                "title": episode.title,
                "description": episode.description,
                "published": published,
                "duration_seconds": episode.duration_seconds,
                "audio_url": episode.enclosure.url,
                "audio_type": episode.enclosure.mime_type,
                "audio_size": audio_size,
                "artwork_url": episode.artwork_url or podcast.artwork_url,
                "website_url": episode.website_url,
                "explicit": episode.explicit,
                "season": episode.season,
                "episode_number": episode.episode_number,
            }
        )

    return {
        "feed": feed,
        "episodes": episodes,
        "canonical_url": podcast.canonical_url,
    }
