"""Coordinator for HA Podcast Player."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from yarl import URL

from .const import (
    DEFAULT_REFRESH_INTERVAL,
    EVENT_EPISODE_COMPLETED,
    EVENT_FEED_ADDED,
    EVENT_FEED_REFRESH_FAILED,
    EVENT_NEW_EPISODE,
    EVENT_PLAYBACK_PAUSED,
    EVENT_PLAYBACK_STARTED,
    PLAYER_ENTITY_ID,
    USER_AGENT,
)
from .feed_parser import PodcastParseError, parse_podcast_feed
from .speaker_proxy import make_signed_speaker_artwork_proxy_url, make_signed_speaker_proxy_url
from .storage import PodcastStorage, make_feed_id, utcnow_iso

_LOGGER = logging.getLogger(__name__)

MAX_FEED_BODY_BYTES = 10 * 1024 * 1024
FEED_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_PARALLEL_REFRESHES = 4
UNAVAILABLE_MEDIA_PLAYER_STATES = {"unavailable", "unknown", "off"}


@dataclass
class PodcastRuntime:
    """Runtime data for the integration."""

    storage: PodcastStorage
    coordinator: "PodcastUpdateCoordinator"


class PodcastUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and coordinate podcast data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, storage: PodcastStorage) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Podcast Player",
            update_interval=DEFAULT_REFRESH_INTERVAL,
            config_entry=entry,
        )
        self.storage = storage
        self._session = async_get_clientsession(hass)
        self._refresh_lock = asyncio.Lock()
        self._refresh_sem = asyncio.Semaphore(MAX_PARALLEL_REFRESHES)

    async def async_initialize(self) -> None:
        """Initialize coordinator data without forcing a network refresh."""
        self.async_set_updated_data(self.storage.snapshot())

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic refresh all feeds."""
        try:
            await self.async_refresh_feeds()
        except Exception as err:  # noqa: BLE001 - coordinator must keep HA alive
            raise UpdateFailed(str(err)) from err
        return self.storage.snapshot()

    async def async_fetch_feed_text(self, rss_url: str) -> tuple[str, str]:
        """Fetch raw feed text and return text plus final URL."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
        async with self._session.get(rss_url, headers=headers, timeout=FEED_FETCH_TIMEOUT, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise PodcastParseError("http_error", f"Feed server returned HTTP {resp.status}.")
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_FEED_BODY_BYTES:
                    raise PodcastParseError("too_large", "Feed is larger than the 10 MB safety limit.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = resp.charset or "utf-8"
            return raw.decode(encoding, errors="replace"), str(resp.url)

    async def async_add_feed(self, rss_url: str) -> dict[str, Any]:
        """Add and immediately refresh a feed."""
        rss_url = str(URL(rss_url.strip()))
        if not rss_url.lower().startswith(("http://", "https://")):
            raise PodcastParseError("invalid_url", "RSS URL must start with http:// or https://")
        feed_id = make_feed_id(rss_url)
        result = await self._async_refresh_single_url(feed_id, rss_url)
        was_new = self.storage.upsert_feed(result["feed"])
        new_episodes = self.storage.upsert_episodes(feed_id, result["episodes"])
        self.storage.data["feeds"][feed_id]["last_refresh"] = utcnow_iso()
        self.storage.data["feeds"][feed_id]["last_success"] = utcnow_iso()
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        if was_new:
            self.hass.bus.async_fire(EVENT_FEED_ADDED, {"feed_id": feed_id, "title": result["feed"].get("title")})
        self._fire_new_episode_events(new_episodes)
        return self.storage.data["feeds"][feed_id]

    async def async_remove_feed(self, feed_id: str, keep_history: bool = True) -> bool:
        """Remove a feed."""
        removed = self.storage.remove_feed(feed_id, keep_history=keep_history)
        if removed:
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            self.hass.bus.async_fire("podcast_player_feed_removed", {"feed_id": feed_id})
        return removed

    async def async_refresh_feeds(self, feed_id: str | None = None) -> None:
        """Refresh all enabled feeds or one feed."""
        async with self._refresh_lock:
            feeds = self.storage.enabled_feeds()
            if feed_id:
                feed = self.storage.get_feed(feed_id)
                feeds = [feed] if feed else []

            tasks = [self._async_refresh_existing_feed(feed) for feed in feeds if feed]
            if tasks:
                await asyncio.gather(*tasks)
                await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())

    async def _async_refresh_existing_feed(self, feed: dict[str, Any]) -> None:
        """Refresh one already stored feed."""
        feed_id = feed["feed_id"]
        rss_url = feed["rss_url"]
        try:
            result = await self._async_refresh_single_url(feed_id, rss_url)
            result["feed"]["last_refresh"] = utcnow_iso()
            result["feed"]["last_success"] = utcnow_iso()
            result["feed"]["canonical_url"] = result.get("canonical_url")
            self.storage.upsert_feed(result["feed"])
            new_episodes = self.storage.upsert_episodes(feed_id, result["episodes"])
            self._fire_new_episode_events(new_episodes)
        except PodcastParseError as err:
            self.storage.mark_feed_failed(feed_id, err.code, err.message)
            self.hass.bus.async_fire(
                EVENT_FEED_REFRESH_FAILED,
                {"feed_id": feed_id, "code": err.code, "message": err.message},
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.storage.mark_feed_failed(feed_id, "cannot_connect", str(err))
            self.hass.bus.async_fire(
                EVENT_FEED_REFRESH_FAILED,
                {"feed_id": feed_id, "code": "cannot_connect", "message": str(err)},
            )

    async def _async_refresh_single_url(self, feed_id: str, rss_url: str) -> dict[str, Any]:
        """Fetch and parse a feed URL."""
        async with self._refresh_sem:
            try:
                raw_text, final_url = await self.async_fetch_feed_text(rss_url)
            except asyncio.TimeoutError as err:
                raise PodcastParseError("timeout", "Feed server timed out.") from err
            except aiohttp.ClientSSLError as err:
                raise PodcastParseError("ssl_error", str(err)) from err
            except aiohttp.TooManyRedirects as err:
                raise PodcastParseError("redirect_loop", "Feed redirects too many times.") from err
            except aiohttp.ClientError as err:
                raise PodcastParseError("cannot_connect", str(err)) from err

            parsed = await self.hass.async_add_executor_job(parse_podcast_feed, raw_text, rss_url, feed_id)
            parsed["canonical_url"] = final_url
            return parsed

    def _fire_new_episode_events(self, new_episodes: list[dict[str, Any]]) -> None:
        """Fire events for new episodes."""
        for episode in new_episodes:
            feed = self.storage.get_feed(episode.get("feed_id")) or {}
            self.hass.bus.async_fire(
                EVENT_NEW_EPISODE,
                {
                    "feed_id": episode.get("feed_id"),
                    "feed_title": feed.get("title"),
                    "episode_id": episode.get("episode_id"),
                    "title": episode.get("title"),
                    "published": episode.get("published"),
                },
            )

    async def async_play_episode(self, episode_id: str) -> None:
        """Set current episode and state to browser playback.

        If speaker output was active, stop that target first so a browser play
        command never leaves an orphan TV/speaker session running.
        """
        if not self.storage.get_episode(episode_id):
            raise ValueError("Unknown episode_id")
        player = self.storage.data["player"]
        previous_target = player.get("target_media_player") if player.get("output_mode") == "speaker" else None
        if previous_target:
            try:
                await self.async_stop_media_player(previous_target)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Podcast Player could not stop previous speaker %s before browser playback: %s", previous_target, err)
                raise HomeAssistantError(f"Could not stop previous speaker target before browser playback: {err}") from err

        self.storage.set_player_state("playing", episode_id)
        player = self.storage.data["player"]
        self._set_browser_output(player)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self.hass.bus.async_fire(EVENT_PLAYBACK_STARTED, {"episode_id": episode_id, "output_mode": "browser"})

    async def async_pause(self) -> None:
        """Pause current episode or target speaker when applicable."""
        player = self.storage.data["player"]
        episode_id = player.get("current_episode_id")
        if player.get("output_mode") == "speaker" and player.get("target_media_player"):
            target = str(player.get("target_media_player"))
            try:
                self._validate_media_player_control_target(target, "pause")
                await self.hass.services.async_call(
                    "media_player",
                    "media_pause",
                    {"entity_id": target},
                    blocking=True,
                )
            except HomeAssistantError as err:
                await self._handle_active_target_control_error(target, err)
                raise
            except Exception as err:  # noqa: BLE001
                message = f"Target media player rejected pause: {err}"
                await self._store_speaker_error(message)
                raise HomeAssistantError(message) from err
        self.storage.set_player_state("paused")
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self.hass.bus.async_fire(EVENT_PLAYBACK_PAUSED, {"episode_id": episode_id})

    async def async_stop(self) -> None:
        """Stop playback or target speaker when applicable."""
        player = self.storage.data["player"]
        if player.get("output_mode") == "speaker" and player.get("target_media_player"):
            await self.async_stop_media_player(player.get("target_media_player"))
            return
        self.storage.set_player_state("idle")
        self._set_browser_output(self.storage.data["player"])
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def _async_seek_media_player(self, entity_id: str, position: float) -> bool:
        """Best-effort seek for an external media_player using HA's public service API."""
        seek_position = float(position)
        try:
            self._validate_media_player_control_target(entity_id, "seek")
        except HomeAssistantError as err:
            _LOGGER.debug("Podcast Player skipped media_seek target=%s position=%s: %s", entity_id, seek_position, err)
            return False
        try:
            await self.hass.services.async_call(
                "media_player",
                "media_seek",
                {"entity_id": entity_id, "seek_position": seek_position},
                blocking=True,
            )
            _LOGGER.debug("Podcast Player requested HA media_seek target=%s position=%s", entity_id, seek_position)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Podcast Player HA media_seek failed target=%s position=%s: %s", entity_id, seek_position, err)
        return False

    async def async_seek(self, episode_id: str, position: float) -> None:
        """Seek current episode or speaker target when applicable."""
        player = self.storage.data["player"]
        if (
            player.get("output_mode") == "speaker"
            and player.get("target_media_player")
            and player.get("current_episode_id") == episode_id
        ):
            target = str(player.get("target_media_player"))
            seek_sent = await self._async_seek_media_player(target, float(position))
            if not seek_sent and self._target_is_unavailable_or_missing(target):
                await self._clear_active_speaker_target(
                    target,
                    f"Target media player is not available for seek: {target}",
                )
                player = self.storage.data["player"]
        self.storage.save_progress(episode_id, position, playing=player.get("state") == "playing")
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def async_save_progress(
        self,
        episode_id: str,
        position: float,
        duration: float | None = None,
        playing: bool | None = None,
        speed: float | None = None,
    ) -> None:
        """Save progress."""
        before = self.storage.get_progress(episode_id).get("played", False)
        progress = self.storage.save_progress(episode_id, position, duration, playing, speed)
        after = progress.get("played", False)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        if after and not before:
            self.hass.bus.async_fire(EVENT_EPISODE_COMPLETED, {"episode_id": episode_id})

    async def async_mark_played(self, episode_id: str, played: bool) -> None:
        """Mark episode played/unplayed."""
        self.storage.mark_played(episode_id, played)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def async_set_speed(self, speed: float, episode_id: str | None = None) -> None:
        """Set speed."""
        self.storage.set_speed(speed, episode_id)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def async_resume(self) -> None:
        """Resume current podcast output.

        Browser output is handled by the custom card. Speaker output can be
        resumed through the target media_player when the integration knows one.
        """
        player = self.storage.data["player"]
        episode_id = player.get("current_episode_id")
        if not episode_id:
            episode = self.next_unplayed_episode() or self.latest_episode()
            episode_id = episode.get("episode_id") if episode else None

        if player.get("output_mode") == "speaker" and player.get("target_media_player"):
            target = str(player.get("target_media_player"))
            if not episode_id:
                raise HomeAssistantError("No podcast episode available to resume")
            try:
                self._validate_media_player_control_target(target, "resume")
                await self.hass.services.async_call(
                    "media_player",
                    "media_play",
                    {"entity_id": target},
                    blocking=True,
                )
            except HomeAssistantError as err:
                await self._handle_active_target_control_error(target, err)
                raise
            except Exception as err:  # noqa: BLE001
                message = f"Target media player rejected resume: {err}"
                await self._store_speaker_error(message)
                raise HomeAssistantError(message) from err
            self.storage.set_player_state("playing", episode_id)
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            return

        if episode_id:
            await self.async_play_episode(episode_id)

    async def async_play_latest(self, feed_id: str | None = None, feed_ids: set[str] | None = None) -> dict[str, Any] | None:
        """Select/play latest episode, optionally from one or more feeds."""
        episode = self.latest_episode(feed_id, feed_ids=feed_ids)
        if not episode:
            return None
        await self.async_play_episode(episode["episode_id"])
        return episode

    async def async_play_next_unplayed(self, feed_id: str | None = None, feed_ids: set[str] | None = None) -> dict[str, Any] | None:
        """Select/play next unplayed episode, optionally from one or more feeds."""
        episode = self.next_unplayed_episode(feed_id, feed_ids=feed_ids)
        if not episode:
            return None
        await self.async_play_episode(episode["episode_id"])
        return episode

    async def async_mark_current_played(self, played: bool = True) -> None:
        """Mark the current episode played/unplayed."""
        episode_id = self.storage.data["player"].get("current_episode_id")
        if not episode_id:
            return
        await self.async_mark_played(episode_id, played)

    async def async_mark_feed_played(self, feed_id: str, played: bool = True) -> int:
        """Mark all active episodes in a feed as played/unplayed."""
        count = 0
        for episode in self.active_episodes(feed_id):
            self.storage.mark_played(episode["episode_id"], played)
            count += 1
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        return count


    def _resume_position_for_episode(self, episode_id: str, explicit_position: float | int | None = None) -> int:
        """Return the best known resume position for an episode.

        Browser playback can report an exact currentTime while speaker outputs
        may only have the last saved progress. Keep this target-agnostic: use an
        explicit position from the card/automation first, then stored progress,
        then the current player snapshot if it matches the same episode.
        """
        candidates: list[Any] = [explicit_position]
        progress = self.storage.get_progress(episode_id)
        candidates.append(progress.get("position"))
        player = self.storage.data.get("player", {})
        if player.get("current_episode_id") == episode_id:
            candidates.append(player.get("position"))
        for value in candidates:
            try:
                pos = int(float(value or 0))
            except (TypeError, ValueError):
                continue
            if pos > 0:
                return pos
        return 0

    async def async_play_on_media_player(
        self,
        media_player_entity_id: str,
        episode_id: str | None = None,
        feed_id: str | None = None,
        feed_ids: set[str] | None = None,
        episode_mode: str = "current",
        url_mode: str = "direct",
        media_content_type: str = "music",
        resume_position: float | int | None = None,
    ) -> dict[str, Any] | None:
        """Send an episode to a real HA media_player entity/speaker."""
        target_state = self._validate_media_player_output_target(media_player_entity_id)

        episode = self._select_episode_for_output(episode_id, feed_id, episode_mode, feed_ids=feed_ids)
        if not episode:
            raise HomeAssistantError("No podcast episode available for the requested selection")

        episode_id = episode.get("episode_id")
        if not episode_id:
            raise HomeAssistantError("Selected episode has no episode_id")

        resume_seconds = self._resume_position_for_episode(episode_id, resume_position)

        if url_mode == "signed_proxy":
            url = make_signed_speaker_proxy_url(self.hass, self.storage.data["settings"], episode_id)
            if not url:
                raise HomeAssistantError("Could not build a Home Assistant speaker proxy URL. Configure HA internal/external URL.")
        else:
            url = episode.get("audio_url")

        if not url:
            raise HomeAssistantError("Selected episode has no playable audio URL")

        feed = self.storage.get_feed(episode.get("feed_id")) or {}
        title = episode.get("title") or "Podcast episode"
        podcast_name = feed.get("title") or feed.get("author") or "Podcast Player"
        original_artwork = episode.get("artwork_url") or feed.get("artwork_url")
        proxied_artwork = make_signed_speaker_artwork_proxy_url(
            self.hass, self.storage.data["settings"], episode_id
        ) if original_artwork else None
        artwork = proxied_artwork or original_artwork
        extra: dict[str, Any] = {
            "title": title,
            "artist": podcast_name,
            "album": podcast_name,
            "creator": podcast_name,
        }
        if artwork:
            extra["thumb"] = artwork
            extra["thumbnail"] = artwork
            extra["album_art"] = artwork
            extra["entity_picture"] = artwork
            extra["artwork_url"] = artwork
            if original_artwork and original_artwork != artwork:
                extra["original_artwork_url"] = original_artwork
        try:
            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": media_player_entity_id,
                    "media_content_id": url,
                    "media_content_type": media_content_type,
                    "extra": extra,
                },
                blocking=True,
            )
            if resume_seconds > 2:
                try:
                    await asyncio.sleep(0.6)
                    await self.hass.services.async_call(
                        "media_player",
                        "media_seek",
                        {"entity_id": media_player_entity_id, "seek_position": float(resume_seconds)},
                        blocking=True,
                    )
                    _LOGGER.debug(
                        "Podcast Player requested media_seek target=%s episode=%s position=%s",
                        media_player_entity_id,
                        episode_id,
                        resume_seconds,
                    )
                except Exception as seek_err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Podcast Player media_seek not supported/failed target=%s episode=%s position=%s: %s",
                        media_player_entity_id,
                        episode_id,
                        resume_seconds,
                        seek_err,
                    )
        except Exception as err:  # noqa: BLE001
            self.storage.data["player"]["speaker_last_error"] = str(err)
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            raise HomeAssistantError(f"Target media player rejected podcast audio: {err}") from err

        self.storage.set_player_state("playing", episode_id)
        player = self.storage.data["player"]
        # Preserve the known resume point while the target takes over. Limited
        # external devices may not report live progress, but source switching
        # should never reset the last known position to 0.
        if resume_seconds > 0:
            player["position"] = resume_seconds
            self.storage.get_progress(episode_id)["position"] = resume_seconds
        player["output_mode"] = "speaker"
        player["target_media_player"] = media_player_entity_id
        player["target_media_player_name"] = target_state.name
        player["last_target_media_player"] = media_player_entity_id
        player["last_target_media_player_name"] = target_state.name
        player["speaker_url_mode"] = url_mode
        player["speaker_media_content_type"] = media_content_type
        player["speaker_last_error"] = None
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self.hass.bus.async_fire(
            EVENT_PLAYBACK_STARTED,
            {
                "episode_id": episode_id,
                "target_media_player": media_player_entity_id,
                "output_mode": "speaker",
                "url_mode": url_mode,
            },
        )
        return episode

    async def async_stop_media_player(self, media_player_entity_id: str | None = None) -> None:
        """Stop a target media player used for podcast speaker output."""
        player = self.storage.data["player"]
        target = media_player_entity_id or player.get("target_media_player") or player.get("last_target_media_player")
        if not target:
            _LOGGER.warning("Podcast Player stop requested, but no target media player is known")
            return
        target = str(target)
        if not self._is_external_media_player_entity_id(target):
            message = f"Target must be an external media_player entity: {target}"
            await self._store_speaker_error(message)
            raise HomeAssistantError(message)

        state = self.hass.states.get(target)
        target_name = state.name if state is not None else target
        _LOGGER.debug(
            "Podcast Player stop requested target=%s name=%r current_output=%r active_target=%r last_target=%r ha_state=%r",
            target,
            target_name,
            player.get("output_mode"),
            player.get("target_media_player"),
            player.get("last_target_media_player"),
            state.state if state is not None else None,
        )

        stopped = False
        errors: list[str] = []

        if state is None or str(state.state) in {"unavailable", "unknown", "off"}:
            message = f"Target media player is not available for stopping: {target}"
            errors.append(message)
            if target == player.get("target_media_player"):
                stopped = True
                _LOGGER.info("Podcast Player clearing unavailable active target=%s", target)
        if not stopped:
            if state is None:
                player["last_target_media_player"] = target
                player["last_target_media_player_name"] = target_name
                player["speaker_last_error"] = "; ".join(errors)
                await self.storage.async_save()
                self.async_set_updated_data(self.storage.snapshot())
                raise HomeAssistantError(player["speaker_last_error"])
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "media_stop",
                    {"entity_id": target},
                    blocking=True,
                )
                stopped = True
                _LOGGER.debug("Podcast Player HA media_stop succeeded for %s", target)
            except Exception as err:  # noqa: BLE001
                errors.append(f"HA media_stop failed: {err}")
                _LOGGER.warning("Podcast Player HA media_stop failed for %s: %s", target, err)

        player["last_target_media_player"] = target
        player["last_target_media_player_name"] = target_name
        player["speaker_last_error"] = None if stopped else "; ".join(errors)

        if stopped:
            # Clear output only after a real stop path succeeded.
            if target == player.get("target_media_player"):
                self.storage.set_player_state("idle")
                player["output_mode"] = "browser"
                player["target_media_player"] = None
                player["target_media_player_name"] = None
                player["speaker_url_mode"] = None
                player["speaker_media_content_type"] = None
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            return

        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        raise HomeAssistantError(player["speaker_last_error"] or f"Could not stop {target}")

    def _validate_media_player_output_target(self, entity_id: str) -> Any:
        """Validate that a media_player target is safe to use for podcast output."""
        target_state = self._validate_media_player_control_target(entity_id, "playback")

        try:
            from homeassistant.components.media_player import MediaPlayerEntityFeature

            features = int(target_state.attributes.get("supported_features") or 0)
            if features and not features & int(MediaPlayerEntityFeature.PLAY_MEDIA):
                raise HomeAssistantError(f"Target media player does not support play_media: {entity_id}")
        except HomeAssistantError:
            raise
        except Exception:  # noqa: BLE001
            # If HA changes feature internals, keep behavior permissive and let
            # media_player.play_media return the authoritative service error.
            pass

        return target_state

    def _is_external_media_player_entity_id(self, entity_id: str) -> bool:
        """Return true for real output media_player entities."""
        return entity_id.startswith("media_player.") and entity_id != PLAYER_ENTITY_ID

    def _validate_media_player_control_target(self, entity_id: str, action: str) -> Any:
        """Validate a target before calling a Home Assistant media_player service."""
        if not self._is_external_media_player_entity_id(entity_id):
            raise HomeAssistantError("Target must be an external media_player entity")

        target_state = self.hass.states.get(entity_id)
        if target_state is None:
            raise HomeAssistantError(f"Target media player not found: {entity_id}")

        state = str(target_state.state)
        if state in UNAVAILABLE_MEDIA_PLAYER_STATES:
            raise HomeAssistantError(f"Target media player is not available for {action}: {entity_id} is {state}")

        return target_state

    def _target_is_unavailable_or_missing(self, entity_id: str) -> bool:
        """Return true if a target should not receive media_player service calls."""
        if not self._is_external_media_player_entity_id(entity_id):
            return True
        target_state = self.hass.states.get(entity_id)
        return target_state is None or str(target_state.state) in UNAVAILABLE_MEDIA_PLAYER_STATES

    async def _handle_active_target_control_error(self, target: str, err: HomeAssistantError) -> None:
        """Clear stale active speaker state when a known target is gone."""
        if target == self.storage.data["player"].get("target_media_player") and self._target_is_unavailable_or_missing(target):
            await self._clear_active_speaker_target(target, str(err))
            return
        await self._store_speaker_error(str(err))

    async def _clear_active_speaker_target(self, target: str, reason: str) -> None:
        """Clear local active-speaker state without calling an unavailable target."""
        player = self.storage.data["player"]
        target_state = self.hass.states.get(target)
        target_name = target_state.name if target_state is not None else target
        player["last_target_media_player"] = target
        player["last_target_media_player_name"] = target_name
        self.storage.set_player_state("idle")
        self._set_browser_output(player)
        player["speaker_last_error"] = reason
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def _store_speaker_error(self, message: str) -> None:
        """Persist the last speaker-control error."""
        self.storage.data["player"]["speaker_last_error"] = message
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    def _select_episode_for_output(
        self,
        episode_id: str | None,
        feed_id: str | None,
        episode_mode: str,
        *,
        feed_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Select an episode for speaker output."""
        if episode_id:
            return self.storage.get_episode(episode_id)
        selected_feed_ids = self._normalize_selected_feed_ids(feed_id, feed_ids)
        if episode_mode == "current":
            current_id = self.storage.data["player"].get("current_episode_id")
            current = self.storage.get_episode(current_id) if current_id else None
            if current and (not selected_feed_ids or current.get("feed_id") in selected_feed_ids):
                return current
            return self.next_unplayed_episode(feed_id, feed_ids=selected_feed_ids) or self.latest_episode(feed_id, feed_ids=selected_feed_ids)
        if episode_mode == "latest":
            return self.latest_episode(feed_id, feed_ids=selected_feed_ids)
        return self.next_unplayed_episode(feed_id, feed_ids=selected_feed_ids)

    def _set_browser_output(self, player: dict[str, Any]) -> None:
        """Mark the logical output as browser/local card playback."""
        player["output_mode"] = "browser"
        player["target_media_player"] = None
        player["target_media_player_name"] = None
        player["speaker_url_mode"] = None
        player["speaker_media_content_type"] = None
        player["speaker_last_error"] = None

    def active_feed_ids(self) -> set[str]:
        """Return active/enabled feed IDs."""
        return {feed["feed_id"] for feed in self.storage.enabled_feeds() if feed.get("feed_id")}

    def _normalize_selected_feed_ids(self, feed_id: str | None = None, feed_ids: set[str] | None = None) -> set[str]:
        """Return active feed IDs selected by service input."""
        active_feed_ids = self.active_feed_ids()
        if feed_ids:
            return {fid for fid in feed_ids if fid in active_feed_ids}
        if feed_id and feed_id != "all" and feed_id in active_feed_ids:
            return {feed_id}
        return set()

    def active_episodes(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Return episodes belonging to active feeds only."""
        active_feed_ids = self.active_feed_ids()
        selected_feed_ids = self._normalize_selected_feed_ids(feed_id, feed_ids)
        allowed_feed_ids = selected_feed_ids or active_feed_ids
        episodes = [ep for ep in self.storage.data["episodes"].values() if ep.get("feed_id") in allowed_feed_ids]
        episodes.sort(key=lambda ep: (ep.get("published") or "", ep.get("discovered_at") or ""), reverse=True)
        return episodes

    def latest_episode(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> dict[str, Any] | None:
        """Return latest active episode by published date."""
        episodes = self.active_episodes(feed_id, feed_ids=feed_ids)
        return episodes[0] if episodes else None

    def next_unplayed_episode(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> dict[str, Any] | None:
        """Return newest unplayed active episode."""
        for episode in self.active_episodes(feed_id, feed_ids=feed_ids):
            progress = self.storage.data["progress"].get(episode.get("episode_id"), {})
            if not progress.get("played", False):
                return episode
        return None
