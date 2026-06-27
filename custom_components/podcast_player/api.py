"""Websocket commands and HTTP proxy for HA Podcast Player."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    HTTP_PROXY_URL,
    HTTP_SPEAKER_ARTWORK_PROXY_URL,
    HTTP_SPEAKER_PROXY_URL,
    PLAYER_ENTITY_ID,
    USER_AGENT,
)
from .coordinator import PodcastRuntime
from .media_source import media_source_id_for_episode
from .speaker_proxy import ensure_proxy_secret, verify_proxy_token

_LOGGER = logging.getLogger(__name__)

REGISTERED_WS_KEY = f"{DOMAIN}_ws_registered"
REGISTERED_HTTP_KEY = f"{DOMAIN}_http_registered"


def get_runtime(hass: HomeAssistant) -> PodcastRuntime | None:
    """Return the first active Podcast Player runtime."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    return next(iter(entries.values()))


def _public_episode(episode: dict[str, Any], progress: dict[str, Any] | None = None, feed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return frontend-safe episode payload.

    The audio URL is exposed only to authenticated HA frontend clients through websocket.
    It is intentionally not exposed in entity states/events.
    """
    progress = progress or {}
    feed = feed or {}
    episode_id = episode.get("episode_id")
    return {
        "episode_id": episode_id,
        "feed_id": episode.get("feed_id"),
        "feed_title": feed.get("title"),
        "feed_artwork_url": feed.get("artwork_url"),
        "feed_author": feed.get("author"),
        "guid": episode.get("guid"),
        "title": episode.get("title"),
        "description": episode.get("description"),
        "published": episode.get("published"),
        "duration_seconds": progress.get("duration") or episode.get("duration_seconds"),
        "audio_url": episode.get("audio_url"),
        "audio_type": episode.get("audio_type"),
        "media_source_id": media_source_id_for_episode(str(episode_id)) if episode_id else None,
        "artwork_url": episode.get("artwork_url"),
        "website_url": episode.get("website_url"),
        "season": episode.get("season"),
        "episode_number": episode.get("episode_number"),
        "played": bool(progress.get("played", False)),
        "position": int(progress.get("position", 0) or 0),
        "last_played_at": progress.get("last_played_at"),
        "playback_speed": progress.get("playback_speed"),
        "proxy_url": HTTP_PROXY_URL.format(episode_id=episode_id),
    }


def _public_feed(feed: dict[str, Any]) -> dict[str, Any]:
    """Return frontend-safe feed payload."""
    return {
        "feed_id": feed.get("feed_id"),
        "title": feed.get("title"),
        "description": feed.get("description"),
        "author": feed.get("author"),
        "website": feed.get("website"),
        "artwork_url": feed.get("artwork_url"),
        "status": feed.get("status"),
        "last_refresh": feed.get("last_refresh"),
        "last_success": feed.get("last_success"),
        "last_error": feed.get("last_error"),
        "enabled": feed.get("enabled", True),
        "episode_count": feed.get("episode_count"),
    }


def _feature_enabled(features: int, flag: Any) -> bool:
    """Return True if a Home Assistant MediaPlayerEntityFeature flag is present."""
    try:
        return bool(int(features or 0) & int(flag))
    except Exception:  # noqa: BLE001
        return False


def _public_output_targets(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return frontend-safe media-player output target capability info.

    This is intentionally generic. It does not hardcode Bedroom TV; it classifies
    every available HA media_player from registry/state/support flags and adds a
    best-effort capability model for the card and automations.
    """
    try:
        from homeassistant.components.media_player import MediaPlayerEntityFeature
        from homeassistant.helpers import entity_registry as er
    except Exception:  # noqa: BLE001
        MediaPlayerEntityFeature = None  # type: ignore[assignment]
        er = None  # type: ignore[assignment]

    registry = er.async_get(hass) if er is not None else None
    targets: list[dict[str, Any]] = []
    states = getattr(hass, "states", None)
    if states is None:
        return targets

    for state in states.async_all("media_player"):
        entity_id = state.entity_id
        if entity_id == PLAYER_ENTITY_ID:
            continue
        attrs = dict(state.attributes or {})
        friendly = attrs.get("friendly_name") or entity_id.replace("media_player.", "")
        lower_id = entity_id.lower()
        lower_name = str(friendly).lower()

        # Spotify entities are usually account/control integrations, not generic
        # arbitrary MP3/M4A renderers. Hide by default to avoid broken output.
        if "spotify" in lower_id or "spotify" in lower_name:
            continue

        entry = registry.async_get(entity_id) if registry is not None else None
        platform = getattr(entry, "platform", None) if entry else None
        features = int(attrs.get("supported_features") or 0)
        live_state = str(state.state or "unknown") not in {"unknown", "unavailable", "off"}
        progress = live_state and any(k in attrs for k in ("media_position", "media_duration", "media_position_updated_at"))

        can_seek = False
        can_pause = False
        can_stop = False
        can_play_media = True
        if MediaPlayerEntityFeature is not None:
            can_seek = _feature_enabled(features, getattr(MediaPlayerEntityFeature, "SEEK", 0))
            can_pause = _feature_enabled(features, getattr(MediaPlayerEntityFeature, "PAUSE", 0))
            can_stop = _feature_enabled(features, getattr(MediaPlayerEntityFeature, "STOP", 0))
            can_play_media = _feature_enabled(features, getattr(MediaPlayerEntityFeature, "PLAY_MEDIA", 0)) if features else True

        is_dlna = platform == "dlna_dmr"
        limited = not live_state or (is_dlna and not progress)
        seek_mode = "supported" if can_seek and progress else "none"
        stop_mode = "supported" if can_stop else "best_effort"
        artwork_mode = "metadata"

        targets.append(
            {
                "entity_id": entity_id,
                "name": friendly,
                "platform": platform,
                "state": state.state,
                "supported_features": features,
                "capabilities": {
                    "play_media": bool(can_play_media),
                    "live_state": bool(live_state),
                    "progress": bool(progress),
                    "seek": seek_mode,
                    "pause": "supported" if can_pause else ("best_effort" if is_dlna else "none"),
                    "stop": stop_mode,
                    "speed": False,
                    "artwork": artwork_mode,
                    "limited_controls": bool(limited),
                    "raw_avtransport": False,
                },
                "notes": [
                    "Does not report live progress" if not progress else "Reports live progress",
                    "DLNA controls use Home Assistant media_player services" if is_dlna else "Generic HA media player",
                ],
            }
        )

    targets.sort(key=lambda item: str(item.get("name") or item.get("entity_id") or "").lower())
    return targets


@callback
def async_register_api(hass: HomeAssistant) -> None:
    """Register websocket commands and HTTP view once."""
    if not hass.data.get(REGISTERED_WS_KEY):
        websocket_api.async_register_command(hass, websocket_get_library)
        websocket_api.async_register_command(hass, websocket_get_episode)
        hass.data[REGISTERED_WS_KEY] = True

    if not hass.data.get(REGISTERED_HTTP_KEY):
        hass.http.register_view(PodcastProxyView())
        hass.http.register_view(PodcastSpeakerProxyView())
        hass.http.register_view(PodcastSpeakerArtworkProxyView())
        hass.data[REGISTERED_HTTP_KEY] = True


@websocket_api.websocket_command(
    {
        vol.Required("type"): "podcast_player/get_library",
        vol.Optional("feed_id", default="all"): str,
        vol.Optional("filter", default="all"): vol.In(["all", "unplayed", "played", "in_progress"]),
        vol.Optional("limit", default=100): vol.All(int, vol.Range(min=1, max=500)),
    }
)
@websocket_api.async_response
async def websocket_get_library(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Return podcast library to the frontend card."""
    runtime = get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Podcast Player is not configured")
        return

    storage = runtime.storage
    feed_id = msg.get("feed_id", "all")
    filter_name = msg.get("filter", "all")
    limit = msg.get("limit", 100)

    raw_feeds = storage.data["feeds"]
    active_feed_ids = {
        fid
        for fid, feed in raw_feeds.items()
        if feed.get("enabled", True)
    }

    # Removed feeds can intentionally leave cached episodes/progress behind so
    # history can return if the feed is added again. The active library must not
    # show or count those orphaned cached episodes.
    active_episodes = [
        episode
        for episode in storage.data["episodes"].values()
        if episode.get("feed_id") in active_feed_ids
    ]

    feed_episode_counts: dict[str, dict[str, int]] = {}
    for episode in active_episodes:
        fid = episode.get("feed_id")
        if not fid:
            continue
        stats = feed_episode_counts.setdefault(fid, {"episodes": 0, "unplayed": 0, "in_progress": 0})
        stats["episodes"] += 1
        progress = storage.data["progress"].get(episode.get("episode_id"), {})
        if not progress.get("played", False):
            stats["unplayed"] += 1
        if progress.get("position", 0) and not progress.get("played", False):
            stats["in_progress"] += 1

    feeds = []
    for feed in raw_feeds.values():
        if not feed.get("enabled", True):
            continue
        public_feed = _public_feed(feed)
        public_feed["counts"] = feed_episode_counts.get(feed.get("feed_id"), {"episodes": 0, "unplayed": 0, "in_progress": 0})
        feeds.append(public_feed)

    episodes = active_episodes
    if feed_id and feed_id != "all":
        if feed_id not in active_feed_ids:
            episodes = []
        else:
            episodes = [ep for ep in episodes if ep.get("feed_id") == feed_id]

    def include_episode(ep: dict[str, Any]) -> bool:
        progress = storage.data["progress"].get(ep.get("episode_id"), {})
        if filter_name == "unplayed":
            return not progress.get("played", False)
        if filter_name == "played":
            return bool(progress.get("played", False))
        if filter_name == "in_progress":
            return bool(progress.get("position", 0)) and not progress.get("played", False)
        return True

    episodes = [ep for ep in episodes if include_episode(ep)]
    episodes.sort(key=lambda ep: (ep.get("published") or "", ep.get("discovered_at") or ""), reverse=True)
    episodes = episodes[:limit]
    public_episodes = [
        _public_episode(
            ep,
            storage.data["progress"].get(ep.get("episode_id")),
            raw_feeds.get(ep.get("feed_id"), {}),
        )
        for ep in episodes
    ]

    connection.send_result(
        msg["id"],
        {
            "feeds": feeds,
            "episodes": public_episodes,
            "player": storage.data["player"],
            "counts": storage.counts(),
            "settings": storage.data["settings"],
            "output_targets": _public_output_targets(hass),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "podcast_player/get_episode",
        vol.Required("episode_id"): str,
    }
)
@websocket_api.async_response
async def websocket_get_episode(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Return one episode."""
    runtime = get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Podcast Player is not configured")
        return
    episode = runtime.storage.get_episode(msg["episode_id"])
    if not episode:
        connection.send_error(msg["id"], "not_found", "Unknown episode_id")
        return
    progress = runtime.storage.data["progress"].get(msg["episode_id"], {})
    feed = runtime.storage.data["feeds"].get(episode.get("feed_id"), {})
    connection.send_result(msg["id"], _public_episode(episode, progress, feed))


async def _proxy_episode_audio(request: web.Request, episode_id: str, *, require_signed_token: bool) -> web.StreamResponse:
    """Proxy one known episode audio URL."""
    hass: HomeAssistant = request.app["hass"]
    runtime = get_runtime(hass)
    if runtime is None:
        return web.Response(status=404, text="Podcast Player is not configured")

    if require_signed_token:
        secret = ensure_proxy_secret(runtime.storage.data["settings"])
        if not verify_proxy_token(
            secret,
            episode_id,
            request.query.get("expires"),
            request.query.get("token"),
        ):
            return web.Response(status=403, text="Invalid or expired podcast proxy token")

    episode = runtime.storage.get_episode(episode_id)
    if not episode:
        return web.Response(status=404, text="Unknown episode_id")
    audio_url = episode.get("audio_url")
    if not audio_url:
        return web.Response(status=404, text="Episode has no audio URL")

    # Some target speakers probe URLs with HEAD first. Provide enough headers
    # without forcing a remote audio download.
    if request.method == "HEAD":
        return web.Response(
            status=200,
            headers={
                "Content-Type": episode.get("audio_type") or "audio/mpeg",
                "Accept-Ranges": "bytes",
            },
        )

    session = async_get_clientsession(hass)
    headers = {"User-Agent": USER_AGENT}
    if range_header := request.headers.get("Range"):
        headers["Range"] = range_header

    try:
        upstream = await session.get(
            audio_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=45),
            allow_redirects=True,
        )
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Podcast audio proxy failed for episode %s: %s", episode_id, err)
        return web.Response(status=502, text="Upstream podcast audio request failed")

    async with upstream:
        if upstream.status >= 400:
            return web.Response(status=upstream.status, text=f"Upstream returned HTTP {upstream.status}")

        response_headers = {}
        for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
            if value := upstream.headers.get(key):
                response_headers[key] = value
        response_headers.setdefault("Content-Type", episode.get("audio_type") or "audio/mpeg")
        response_headers.setdefault("Accept-Ranges", "bytes")

        response = web.StreamResponse(status=upstream.status, headers=response_headers)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response


def _episode_artwork_url(runtime: PodcastRuntime, episode: dict[str, Any]) -> str | None:
    """Return the preferred artwork URL for an episode."""
    artwork = episode.get("artwork_url")
    if artwork:
        return str(artwork)
    feed = runtime.storage.get_feed(episode.get("feed_id")) or {}
    if feed.get("artwork_url"):
        return str(feed.get("artwork_url"))
    return None


async def _proxy_episode_artwork(request: web.Request, episode_id: str) -> web.StreamResponse:
    """Proxy preferred episode artwork to external speaker/display devices."""
    hass: HomeAssistant = request.app["hass"]
    runtime = get_runtime(hass)
    if runtime is None:
        return web.Response(status=404, text="Podcast Player is not configured")

    secret = ensure_proxy_secret(runtime.storage.data["settings"])
    if not verify_proxy_token(
        secret,
        episode_id,
        request.query.get("expires"),
        request.query.get("token"),
    ):
        return web.Response(status=403, text="Invalid or expired podcast artwork token")

    episode = runtime.storage.get_episode(episode_id)
    if not episode:
        return web.Response(status=404, text="Unknown episode_id")

    artwork_url = _episode_artwork_url(runtime, episode)
    if not artwork_url:
        return web.Response(status=404, text="Episode has no artwork URL")

    # Some DLNA renderers probe artwork with HEAD before rendering it.
    if request.method == "HEAD":
        return web.Response(status=200, headers={"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400"})

    session = async_get_clientsession(hass)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
    try:
        upstream = await session.get(
            artwork_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45, sock_connect=15, sock_read=30),
            allow_redirects=True,
        )
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Podcast artwork proxy failed for episode %s: %s", episode_id, err)
        return web.Response(status=502, text="Upstream podcast artwork request failed")

    async with upstream:
        if upstream.status >= 400:
            return web.Response(status=upstream.status, text=f"Upstream returned HTTP {upstream.status}")

        response_headers = {}
        for key in ("Content-Type", "Content-Length", "ETag", "Last-Modified"):
            if value := upstream.headers.get(key):
                response_headers[key] = value
        response_headers.setdefault("Content-Type", "image/jpeg")
        response_headers.setdefault("Cache-Control", "public, max-age=86400")

        response = web.StreamResponse(status=upstream.status, headers=response_headers)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response


class PodcastProxyView(HomeAssistantView):
    """Authenticated proxy for stored podcast episode audio."""

    url = "/api/podcast_player/proxy/{episode_id}"
    name = "api:podcast_player:proxy"
    requires_auth = True

    async def get(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Proxy one known episode audio URL."""
        return await _proxy_episode_audio(request, episode_id, require_signed_token=False)

    async def head(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Return probe headers for authenticated proxy."""
        return await _proxy_episode_audio(request, episode_id, require_signed_token=False)


class PodcastSpeakerProxyView(HomeAssistantView):
    """Signed no-login proxy for speaker devices.

    External media players cannot normally send Home Assistant auth headers. This
    route is restricted to stored episode IDs and requires a short-lived signed
    token, so it does not become an arbitrary open URL relay.
    """

    url = HTTP_SPEAKER_PROXY_URL
    name = "api:podcast_player:speaker_proxy"
    requires_auth = False

    async def get(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Proxy one known episode audio URL with a signed token."""
        return await _proxy_episode_audio(request, episode_id, require_signed_token=True)

    async def head(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Return probe headers for signed speaker proxy."""
        return await _proxy_episode_audio(request, episode_id, require_signed_token=True)



class PodcastSpeakerArtworkProxyView(HomeAssistantView):
    """Signed no-login proxy for output media player artwork.

    This is intentionally limited to stored episode IDs and short-lived signed
    tokens. It lets DLNA/Chromecast/Sonos-like devices fetch artwork from a
    Home Assistant URL instead of an external podcast CDN that the device may reject.
    """

    url = HTTP_SPEAKER_ARTWORK_PROXY_URL
    name = "api:podcast_player:speaker_artwork"
    requires_auth = False

    async def get(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Proxy one known episode artwork URL with a signed token."""
        return await _proxy_episode_artwork(request, episode_id)

    async def head(self, request: web.Request, episode_id: str) -> web.StreamResponse:
        """Return probe headers for signed speaker artwork."""
        return await _proxy_episode_artwork(request, episode_id)
