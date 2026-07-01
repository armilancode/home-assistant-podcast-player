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
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, NAME, URL_MODE_DIRECT, URL_MODE_SIGNED_PROXY
from .coordinator import PodcastRuntime
from .speaker_proxy import make_signed_speaker_proxy_path, make_signed_speaker_proxy_url

FEEDS_IDENTIFIER = "feeds"
LATEST_LIMIT = 25
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

        direct_url = episode.get("audio_url")
        if not direct_url:
            raise Unresolvable("Podcast episode has no playable audio URL")

        settings = runtime.storage.data["settings"]
        secret_before = settings.get("speaker_proxy_secret")
        target = item.target_media_player
        target_status = None
        if target is not None:
            target_status = runtime.coordinator.media_source_target_status(str(target))
            if not target_status.get("playable"):
                reason = target_status.get("reason") or f"Target media player is not available: {target}"
                raise Unresolvable(str(reason))

        url, url_mode = _resolve_episode_url_for_target(
            self.hass,
            settings,
            episode_id,
            direct_url,
            str(target) if target is not None else None,
        )

        if target is not None:
            try:
                await runtime.coordinator.async_prepare_media_source_playback(
                    episode_id=episode_id,
                    media_player_entity_id=str(target),
                    media_content_id=url,
                    media_content_type=episode.get("audio_type") or "audio/mpeg",
                    url_mode=url_mode,
                )
            except HomeAssistantError as err:
                raise Unresolvable(str(err)) from err

        if not secret_before and settings.get("speaker_proxy_secret"):
            await runtime.storage.async_save()

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

        if parts[0] == FEEDS_IDENTIFIER and len(parts) == 1:
            return self._feeds(runtime)

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
        children.append(
            self._directory(
                FEEDS_IDENTIFIER,
                "Feeds",
                children_media_class=MediaClass.PODCAST,
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

    def _feeds(self, runtime: PodcastRuntime) -> BrowseMediaSource:
        """Return the feeds directory."""
        children = []
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
            identifier=FEEDS_IDENTIFIER,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PODCAST,
            title="Feeds",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.PODCAST,
            children=children,
        )

    def _feed(self, runtime: PodcastRuntime, feed: dict[str, Any]) -> BrowseMediaSource:
        """Return one feed directory."""
        feed_id = feed["feed_id"]
        children = [
            self._directory(
                f"feed/{feed_id}/{identifier}",
                title,
                thumbnail=feed.get("artwork_url"),
            )
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
        not_shown = 0
        if category == "latest":
            not_shown = max(0, len(episodes) - LATEST_LIMIT)
            episodes = episodes[:LATEST_LIMIT]
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
            not_shown=not_shown,
            children=[
                self._episode(runtime, episode, include_feed_title=feed_id is None)
                for episode in episodes
            ],
        )

    def _directory(
        self,
        identifier: str,
        title: str,
        *,
        thumbnail: str | None = None,
        media_class: MediaClass = MediaClass.DIRECTORY,
        children_media_class: MediaClass = MediaClass.EPISODE,
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
            children_media_class=children_media_class,
        )

    def _episode(
        self,
        runtime: PodcastRuntime,
        episode: dict[str, Any],
        *,
        include_feed_title: bool = False,
    ) -> BrowseMediaSource:
        """Return an episode node."""
        feed = runtime.storage.get_feed(episode.get("feed_id")) or {}
        episode_id = episode["episode_id"]
        progress = runtime.storage.data["progress"].get(episode_id, {})
        title = _episode_display_title(episode, feed, progress, include_feed_title=include_feed_title)
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"episode/{episode_id}",
            media_class=MediaClass.EPISODE,
            media_content_type=MediaType.EPISODE,
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


def _resolve_episode_url_for_target(
    hass: HomeAssistant,
    settings: dict[str, Any],
    episode_id: str,
    direct_url: str,
    target_media_player: str | None,
) -> tuple[str, str]:
    """Return the URL and URL mode that should be handed to Home Assistant."""
    if target_media_player is None:
        return make_signed_speaker_proxy_path(settings, episode_id), URL_MODE_SIGNED_PROXY

    if settings.get("direct_first", True):
        return direct_url, URL_MODE_DIRECT

    proxy_url = make_signed_speaker_proxy_url(hass, settings, episode_id)
    if proxy_url:
        return proxy_url, URL_MODE_SIGNED_PROXY
    return direct_url, URL_MODE_DIRECT


def _episode_display_title(
    episode: dict[str, Any],
    feed: dict[str, Any],
    progress: dict[str, Any],
    *,
    include_feed_title: bool,
) -> str:
    """Return the media browser title for an episode."""
    title = str(episode.get("title") or "Untitled episode")
    feed_title = str(feed.get("title") or "")
    if include_feed_title and feed_title:
        title = f"{title} — {feed_title}"
    duration = _duration_label(progress.get("duration") or episode.get("duration_seconds"))
    if duration:
        title = f"{title} ({duration})"
    return title


def _duration_label(value: Any) -> str | None:
    """Return a compact duration label."""
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def media_source_id_for_episode(episode_id: str) -> str:
    """Return the HA media source URI for an episode."""
    return generate_media_source_id(DOMAIN, f"episode/{episode_id}")
