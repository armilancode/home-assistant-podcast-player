"""Media Source support for Podcast Player."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
    generate_media_source_id,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME
from .coordinator import PodcastRuntime
from .speaker_proxy import make_signed_speaker_proxy_url

ROOT_CATEGORIES = {
    "latest": "Latest episodes",
    "unplayed": "Unplayed episodes",
    "in_progress": "In-progress episodes",
    "all": "All episodes",
}


async def async_get_media_source(hass: HomeAssistant) -> "PodcastMediaSource":
    """Set up Podcast Player media source."""
    return PodcastMediaSource(hass)


def _runtime(hass: HomeAssistant) -> PodcastRuntime | None:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    return next(iter(entries.values()))


class PodcastMediaSource(MediaSource):
    """Expose podcasts in Home Assistant Media Browser."""

    name = NAME

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the media source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve an episode to a playable URL."""
        runtime = _runtime(self.hass)
        if runtime is None:
            raise Unresolvable("Podcast Player is not configured")

        parts = _parts(item.identifier)
        if len(parts) < 2 or parts[0] != "episode":
            raise Unresolvable("Podcast media item is not playable")

        episode_id = parts[1]
        episode = runtime.storage.get_episode(episode_id)
        if not episode:
            raise Unresolvable("Podcast episode was not found")

        settings = runtime.storage.data["settings"]
        direct_url = episode.get("audio_url")
        proxy_url = make_signed_speaker_proxy_url(self.hass, settings, episode_id)
        url = direct_url if settings.get("direct_first", True) else proxy_url or direct_url
        if not url:
            raise Unresolvable("Podcast episode has no playable audio URL")

        return PlayMedia(url=url, mime_type=episode.get("audio_type") or "audio/mpeg")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse podcast feeds and episodes."""
        runtime = _runtime(self.hass)
        if runtime is None:
            raise BrowseError("Podcast Player is not configured")

        parts = _parts(item.identifier)
        if not parts:
            return self._root(runtime)

        if parts[0] in ROOT_CATEGORIES:
            return self._episode_list(runtime, parts[0])

        if parts[0] == "feed" and len(parts) >= 2:
            feed_id = parts[1]
            feed = runtime.storage.get_feed(feed_id)
            if not feed or not feed.get("enabled", True):
                raise BrowseError("Podcast feed was not found")
            if len(parts) == 2:
                return self._feed(runtime, feed)
            if len(parts) == 3 and parts[2] in ROOT_CATEGORIES:
                return self._episode_list(runtime, parts[2], feed_id=feed_id, title_prefix=feed.get("title"))

        raise BrowseError("Podcast media source path was not found")

    def _root(self, runtime: PodcastRuntime) -> BrowseMediaSource:
        """Return the media source root."""
        children = [
            self._directory(identifier, title)
            for identifier, title in ROOT_CATEGORIES.items()
        ]
        for feed in sorted(runtime.storage.enabled_feeds(), key=lambda item: str(item.get("title") or "").casefold()):
            feed_id = feed.get("feed_id")
            if not feed_id:
                continue
            children.append(
                self._directory(
                    f"feed/{feed_id}",
                    feed.get("title") or feed_id,
                    thumbnail=feed.get("artwork_url"),
                    media_class=MediaClass.PODCAST,
                )
            )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type=MediaType.APP,
            title=NAME,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    def _feed(self, runtime: PodcastRuntime, feed: dict[str, Any]) -> BrowseMediaSource:
        """Return one feed directory."""
        feed_id = feed["feed_id"]
        children = [
            self._directory(f"feed/{feed_id}/{identifier}", title)
            for identifier, title in ROOT_CATEGORIES.items()
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"feed/{feed_id}",
            media_class=MediaClass.PODCAST,
            media_content_type=MediaType.PODCAST,
            title=feed.get("title") or feed_id,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            thumbnail=feed.get("artwork_url"),
            children=children,
        )

    def _episode_list(
        self,
        runtime: PodcastRuntime,
        category: str,
        *,
        feed_id: str | None = None,
        title_prefix: str | None = None,
    ) -> BrowseMediaSource:
        """Return an episode list."""
        episodes = runtime.coordinator.active_episodes(feed_id)
        progress = runtime.storage.data["progress"]
        if category == "latest":
            episodes = episodes[:25]
        elif category == "unplayed":
            episodes = [episode for episode in episodes if not progress.get(episode.get("episode_id"), {}).get("played")]
        elif category == "in_progress":
            episodes = [
                episode
                for episode in episodes
                if progress.get(episode.get("episode_id"), {}).get("position", 0)
                and not progress.get(episode.get("episode_id"), {}).get("played")
            ]

        title = ROOT_CATEGORIES[category]
        if title_prefix:
            title = f"{title_prefix}: {title}"
        identifier = f"feed/{feed_id}/{category}" if feed_id else category
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.PODCAST,
            media_content_type=MediaType.PODCAST,
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.EPISODE,
            children=[self._episode(runtime, episode) for episode in episodes],
        )

    def _directory(
        self,
        identifier: str,
        title: str,
        *,
        thumbnail: str | None = None,
        media_class: MediaClass = MediaClass.DIRECTORY,
    ) -> BrowseMediaSource:
        """Return a directory node."""
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=media_class,
            media_content_type=MediaType.PODCAST,
            title=title,
            can_play=False,
            can_expand=True,
            thumbnail=thumbnail,
            children_media_class=MediaClass.EPISODE,
        )

    def _episode(self, runtime: PodcastRuntime, episode: dict[str, Any]) -> BrowseMediaSource:
        """Return an episode node."""
        feed = runtime.storage.get_feed(episode.get("feed_id")) or {}
        title = episode.get("title") or "Untitled episode"
        episode_id = episode["episode_id"]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"episode/{episode_id}",
            media_class=MediaClass.EPISODE,
            media_content_type=MediaType.PODCAST,
            title=title,
            can_play=True,
            can_expand=False,
            thumbnail=episode.get("artwork_url") or feed.get("artwork_url"),
        )


def _parts(identifier: str | None) -> list[str]:
    """Return normalized media source identifier path parts."""
    if not identifier:
        return []
    return [part for part in str(identifier).strip("/").split("/") if part]


def media_source_id_for_episode(episode_id: str) -> str:
    """Return the HA media source URI for an episode."""
    return generate_media_source_id(DOMAIN, f"episode/{episode_id}")
