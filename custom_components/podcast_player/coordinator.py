"""Coordinator for HA Podcast Player."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_REFRESH_INTERVAL_MINUTES,
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    EVENT_EPISODE_COMPLETED,
    EVENT_FEED_ADDED,
    EVENT_FEED_REFRESH_FAILED,
    EVENT_NEW_EPISODE,
    EVENT_PLAYBACK_PAUSED,
    EVENT_PLAYBACK_STARTED,
    USER_AGENT,
)
from .external_control import (
    DlnaAvTransportController,
    ExternalPlaybackStatus,
    current_media_matches_session,
)
from .feed_parser import PodcastParseError, parse_podcast_feed
from .speaker_proxy import make_signed_speaker_artwork_proxy_url, make_signed_speaker_proxy_url
from .storage import PodcastStorage, default_external_session, make_feed_id, normalize_rss_url, stable_hash, utcnow_iso
from .targets import UNAVAILABLE_MEDIA_PLAYER_STATES, is_external_media_player_entity_id, output_target_status

_LOGGER = logging.getLogger(__name__)

MAX_FEED_BODY_BYTES = 10 * 1024 * 1024
FEED_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_PARALLEL_REFRESHES = 4
ACTIVE_MEDIA_PLAYER_STATES = {"playing", "paused", "buffering"}
EXTERNAL_POLL_SECONDS = 5
EXTERNAL_STARTUP_GRACE_SECONDS = 30


def refresh_interval_from_settings(settings: dict[str, Any]) -> timedelta:
    """Return a safe refresh interval from persisted settings."""
    try:
        minutes = int(settings.get(CONF_REFRESH_INTERVAL_MINUTES, DEFAULT_REFRESH_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_REFRESH_INTERVAL_MINUTES
    minutes = min(1440, max(15, minutes))
    return timedelta(minutes=minutes)


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
            update_interval=refresh_interval_from_settings(storage.data["settings"]),
            config_entry=entry,
        )
        self.storage = storage
        self._session = async_get_clientsession(hass)
        self._refresh_lock = asyncio.Lock()
        self._refresh_sem = asyncio.Semaphore(MAX_PARALLEL_REFRESHES)
        self._external_control = DlnaAvTransportController()
        self._external_poll_task: asyncio.Task[None] | None = None

    async def async_initialize(self) -> None:
        """Initialize coordinator data without forcing a network refresh."""
        self.async_set_updated_data(self.storage.snapshot())
        self._ensure_external_polling()

    async def async_shutdown(self) -> None:
        """Stop background tasks owned by the coordinator."""
        if self._external_poll_task and not self._external_poll_task.done():
            self._external_poll_task.cancel()
            try:
                await self._external_poll_task
            except asyncio.CancelledError:
                pass
        self._external_poll_task = None

    def _ensure_external_polling(self) -> None:
        """Ensure external session polling runs when HA can schedule tasks."""
        if self._external_poll_task and not self._external_poll_task.done():
            return
        session = self._external_session()
        if not session.get("active"):
            return
        create_task = getattr(self.hass, "async_create_task", None)
        if create_task is None:
            return
        self._external_poll_task = create_task(self._async_external_poll_loop())

    async def _async_external_poll_loop(self) -> None:
        """Poll active external playback sessions."""
        try:
            while self._external_session().get("active"):
                await asyncio.sleep(EXTERNAL_POLL_SECONDS)
                if self._external_session().get("active"):
                    await self.async_update_external_session()
        finally:
            self._external_poll_task = None

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
        try:
            rss_url = normalize_rss_url(rss_url)
        except ValueError as err:
            raise PodcastParseError("invalid_url", str(err)) from err
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

    def _external_session(self) -> dict[str, Any]:
        """Return the current external playback session, adding missing defaults."""
        player = self.storage.data["player"]
        session = player.get("external_session")
        if not isinstance(session, dict):
            session = default_external_session()
            player["external_session"] = session
            return session
        defaults = default_external_session()
        defaults.update(session)
        player["external_session"] = defaults
        return defaults

    def _enhanced_dlna_controls_enabled(self) -> bool:
        """Return whether enhanced DLNA controls are enabled."""
        return bool(self.storage.data["settings"].get("enhanced_dlna_controls", True))

    def _target_registry_info(self, entity_id: str) -> dict[str, Any]:
        """Return generic registry/config-entry information for a media player."""
        attrs = dict(getattr(self.hass.states.get(entity_id), "attributes", {}) or {})
        info: dict[str, Any] = {
            "platform": None,
            "supported_features": attrs.get("supported_features"),
            "description_url": None,
        }
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            entry = registry.async_get(entity_id)
        except Exception:  # noqa: BLE001
            entry = None

        if entry is not None:
            info["platform"] = getattr(entry, "platform", None)
            info["supported_features"] = attrs.get("supported_features") or getattr(entry, "supported_features", 0)
            config_entry_id = getattr(entry, "config_entry_id", None)
            if info["platform"] == "dlna_dmr" and config_entry_id:
                try:
                    config_entry = self.hass.config_entries.async_get_entry(config_entry_id)
                    data = getattr(config_entry, "data", {}) or {}
                    info["description_url"] = data.get("url")
                except Exception:  # noqa: BLE001
                    info["description_url"] = None
        return info

    def _target_status(self, entity_id: str, state: Any | None = None) -> dict[str, Any]:
        """Return target status using HA state plus registry-supported features."""
        state = state if state is not None else self.hass.states.get(entity_id)
        info = self._target_registry_info(entity_id)
        return output_target_status(
            entity_id,
            state,
            info.get("platform"),
            supported_features=info.get("supported_features"),
            enhanced_dlna_controls=self._enhanced_dlna_controls_enabled(),
        )

    def media_source_target_status(self, entity_id: str) -> dict[str, Any]:
        """Return target status for Media Browser playback resolution."""
        status = self._target_status(entity_id)
        if "platform" not in status:
            status["platform"] = self._target_registry_info(entity_id).get("platform")
        return status

    def _target_control_mode(self, entity_id: str, state: Any | None, capability: str) -> str:
        """Return the advertised control mode for an external target."""
        status = self._target_status(entity_id, state)
        return str(status.get("capabilities", {}).get(capability) or "none")

    def _set_external_session(
        self,
        *,
        episode_id: str,
        target: str,
        target_name: str,
        target_platform: str | None,
        media_content_id: str,
        resume_position: int,
        duration: int | None,
    ) -> dict[str, Any]:
        """Create/update the backend-owned external playback session."""
        now = utcnow_iso()
        session = default_external_session()
        session.update(
            {
                "active": True,
                "session_id": uuid4().hex,
                "episode_id": episode_id,
                "target_media_player": target,
                "target_media_player_name": target_name,
                "target_platform": target_platform,
                "started_at": now,
                "updated_at": now,
                "resume_position": int(resume_position or 0),
                "position": int(resume_position or 0),
                "duration": duration,
                "transport_state": "playing",
                "supported_actions": [],
                "control_source": "ha",
                "progress_source": "saved" if resume_position else "unavailable",
                "status_updated_at": now,
                "media_content_id_hash": stable_hash(media_content_id, 32),
                "media_matches_session": True,
                "last_error": None,
            }
        )
        self.storage.data["player"]["external_session"] = session
        return session

    def _clear_external_session(self, reason: str | None = None) -> None:
        """Mark the external session inactive without leaking implementation data."""
        existing = self._external_session()
        ended = default_external_session()
        for key in (
            "session_id",
            "episode_id",
            "target_media_player",
            "target_media_player_name",
            "target_platform",
            "started_at",
            "position",
            "duration",
            "control_source",
            "progress_source",
            "media_content_id_hash",
            "media_matches_session",
        ):
            ended[key] = existing.get(key)
        ended["active"] = False
        ended["transport_state"] = "idle"
        ended["ended_at"] = utcnow_iso()
        ended["updated_at"] = ended["ended_at"]
        ended["status_updated_at"] = ended["ended_at"]
        ended["last_error"] = reason
        ended["supported_actions"] = existing.get("supported_actions") or []
        self.storage.data["player"]["external_session"] = ended

    def _expected_media_id_for_session(self, session: dict[str, Any]) -> str | None:
        """Return the expected direct episode URL for safe-stop matching."""
        episode_id = session.get("episode_id")
        episode = self.storage.get_episode(episode_id) if episode_id else None
        return episode.get("audio_url") if episode else None

    def _external_session_age_seconds(self, session: dict[str, Any]) -> float | None:
        """Return the age of an external session in seconds."""
        try:
            started = datetime.fromisoformat(str(session.get("started_at"))).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
        return max(0, (datetime.now(timezone.utc) - started).total_seconds())

    def _external_session_in_startup_grace(self, session: dict[str, Any]) -> bool:
        """Return true while a new external session may still be loading."""
        age = self._external_session_age_seconds(session)
        return age is not None and age <= EXTERNAL_STARTUP_GRACE_SECONDS

    def _external_status_matches_session(
        self,
        status: ExternalPlaybackStatus,
        session: dict[str, Any],
        episode_id: str | None,
    ) -> bool | None:
        """Return whether an external status appears to belong to the active session."""
        return current_media_matches_session(
            status.current_media_id,
            self._expected_media_id_for_session(session),
            episode_id,
        )

    def _external_status_is_starting(
        self,
        status: ExternalPlaybackStatus,
        session: dict[str, Any],
        episode_id: str | None,
    ) -> bool:
        """Return true for a transient idle report during external startup."""
        if status.state != "idle" or not session.get("active"):
            return False
        if not self._external_session_in_startup_grace(session):
            return False
        media_match = self._external_status_matches_session(status, session, episode_id)
        return media_match is not False

    def _starting_external_status(
        self,
        status: ExternalPlaybackStatus,
        session: dict[str, Any],
    ) -> ExternalPlaybackStatus:
        """Return a normalized buffering status for an external session that is starting."""
        return ExternalPlaybackStatus(
            state="buffering",
            transport_state="starting",
            position=session.get("position") or status.position,
            duration=status.duration or session.get("duration"),
            current_media_id=status.current_media_id,
            supported_actions=status.supported_actions,
            progress_source=status.progress_source or session.get("progress_source") or "unavailable",
            control_source=status.control_source or session.get("control_source") or "ha",
        )

    async def _async_retry_external_start(
        self,
        target: str,
        status: ExternalPlaybackStatus,
        session: dict[str, Any],
        episode_id: str | None,
    ) -> bool:
        """Best-effort Play retry for DLNA renderers that load media without starting it."""
        media_match = self._external_status_matches_session(status, session, episode_id)
        if media_match is False:
            return False
        if status.supported_actions and "Play" not in status.supported_actions:
            return False
        return await self._async_play_via_dlna(target)

    def _apply_external_status(self, status: ExternalPlaybackStatus) -> None:
        """Persist external target status/progress."""
        player = self.storage.data["player"]
        session = self._external_session()
        episode_id = session.get("episode_id") or player.get("current_episode_id")
        if not episode_id:
            return

        now = utcnow_iso()
        duration = status.duration or session.get("duration") or player.get("duration")
        position = status.position if status.position is not None else session.get("position") or player.get("position") or 0
        session.update(
            {
                "position": int(position or 0),
                "duration": duration,
                "transport_state": status.state,
                "supported_actions": sorted(status.supported_actions or []),
                "control_source": status.control_source,
                "progress_source": status.progress_source,
                "status_updated_at": now,
                "updated_at": now,
                "last_error": None,
            }
        )
        media_match = self._external_status_matches_session(status, session, episode_id)
        if media_match is not None:
            session["media_matches_session"] = media_match

        if status.state in {"playing", "paused", "buffering"}:
            self.storage.save_progress(
                episode_id,
                int(position or 0),
                duration,
                playing=status.state in {"playing", "buffering"},
                speed=player.get("speed"),
            )
            player["output_mode"] = "speaker"
            player["target_media_player"] = session.get("target_media_player")
            player["target_media_player_name"] = session.get("target_media_player_name")
        elif status.state == "idle":
            self.storage.save_progress(episode_id, int(position or 0), duration, playing=False, speed=player.get("speed"))
            self.storage.set_player_state("idle")
            self._set_browser_output(player)
            self._clear_external_session()

    def _ha_status_for_target(self, target: str) -> ExternalPlaybackStatus | None:
        """Build an external status snapshot from HA media_player attributes."""
        state = self.hass.states.get(target)
        if state is None:
            return None
        attrs = dict(getattr(state, "attributes", {}) or {})
        state_value = str(getattr(state, "state", "") or "")
        progress_available = (
            attrs.get("media_position") is not None
            or attrs.get("media_position_updated_at") is not None
            or bool(attrs.get("media_duration"))
        )
        if state_value not in ACTIVE_MEDIA_PLAYER_STATES and state_value != "idle":
            return None
        position: float | int | None = attrs.get("media_position")
        if position is not None and state_value == "playing" and attrs.get("media_position_updated_at"):
            try:
                updated = datetime.fromisoformat(str(attrs["media_position_updated_at"])).astimezone(timezone.utc)
                position = float(position) + max(0, (datetime.now(timezone.utc) - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        return ExternalPlaybackStatus(
            state="idle" if state_value == "idle" else state_value,
            transport_state=state_value,
            position=int(position) if position is not None else None,
            duration=int(attrs["media_duration"]) if attrs.get("media_duration") else None,
            current_media_id=attrs.get("media_content_id"),
            supported_actions=None,
            progress_source="ha" if progress_available else "unavailable",
            control_source="ha",
        )

    def _estimated_external_status(self) -> ExternalPlaybackStatus | None:
        """Return estimated progress for active targets that expose no live progress."""
        player = self.storage.data["player"]
        session = self._external_session()
        if not session.get("active") or player.get("state") != "playing":
            return None
        try:
            updated_raw = session.get("status_updated_at") or player.get("updated_at")
            updated = datetime.fromisoformat(str(updated_raw)).astimezone(timezone.utc)
            elapsed = max(0, (datetime.now(timezone.utc) - updated).total_seconds())
        except (TypeError, ValueError):
            elapsed = 0
        speed = float(player.get("speed") or 1.0)
        position = int(float(session.get("position") or player.get("position") or 0) + elapsed * speed)
        duration = session.get("duration") or player.get("duration")
        if duration:
            position = min(position, int(duration))
        return ExternalPlaybackStatus(
            state="playing",
            transport_state="estimated",
            position=position,
            duration=duration,
            supported_actions=set(session.get("supported_actions") or []),
            progress_source="estimated",
            control_source=str(session.get("control_source") or "ha"),
        )

    async def _async_dlna_status(self, target: str) -> ExternalPlaybackStatus | None:
        """Read status directly from a DLNA DMR target when available."""
        description_url = self._dlna_description_url(target)
        if not description_url:
            return None
        try:
            return await self._external_control.async_status(description_url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Podcast Player enhanced DLNA status failed target=%s: %s", target, err)
            return None

    async def async_update_external_session(self) -> None:
        """Poll and persist active external playback status."""
        session = self._external_session()
        target = session.get("target_media_player")
        if not session.get("active") or not target:
            return

        dlna_status = await self._async_dlna_status(str(target))
        status = dlna_status if dlna_status is not None and dlna_status.state != "unknown" else None
        ha_status = self._ha_status_for_target(str(target))
        if status is None and ha_status is not None and (ha_status.progress_source == "ha" or ha_status.state == "idle"):
            status = ha_status
        if status is None:
            status = self._estimated_external_status()
        if status is None:
            return
        episode_id = session.get("episode_id") or self.storage.data["player"].get("current_episode_id")
        if self._external_status_is_starting(status, session, episode_id):
            await self._async_retry_external_start(str(target), status, session, episode_id)
            status = self._starting_external_status(status, session)
        self._apply_external_status(status)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())

    async def async_play_episode(self, episode_id: str) -> None:
        """Set current episode and state to browser playback.

        If speaker output was active, stop that target first so a browser play
        command never leaves an orphan TV/speaker session running.
        """
        if not self.storage.get_episode(episode_id):
            raise ValueError("Unknown episode_id")
        player = self.storage.data["player"]
        session = self._external_session()
        previous_target = (
            player.get("target_media_player")
            if player.get("output_mode") == "speaker"
            else session.get("target_media_player") if session.get("active") else None
        )
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
            paused = False
            tried_dlna = False
            try:
                target_state = self._validate_media_player_control_target(target, "pause")
                pause_mode = self._target_control_mode(target, target_state, "pause")
                if pause_mode == "supported":
                    await self.hass.services.async_call(
                        "media_player",
                        "media_pause",
                        {"entity_id": target},
                        blocking=True,
                    )
                    paused = True
                elif pause_mode == "best_effort":
                    tried_dlna = True
                    paused = await self._async_pause_via_dlna(target)
                    if not paused:
                        raise HomeAssistantError(f"Target media player does not support pause: {target}")
                else:
                    raise HomeAssistantError(f"Target media player does not support pause: {target}")
            except HomeAssistantError as err:
                if not paused and not tried_dlna:
                    paused = await self._async_pause_via_dlna(target)
                if not paused:
                    await self._handle_active_target_control_error(target, err)
                    raise
            except Exception as err:  # noqa: BLE001
                if not await self._async_pause_via_dlna(target):
                    message = f"Target media player rejected pause: {err}"
                    await self._store_speaker_error(message)
                    raise HomeAssistantError(message) from err
            if paused:
                if self._external_session().get("control_source") != "dlna":
                    self._external_session()["control_source"] = "ha"
        self.storage.set_player_state("paused")
        if player.get("output_mode") == "speaker":
            self._external_session()["transport_state"] = "paused"
            self._external_session()["updated_at"] = utcnow_iso()
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self.hass.bus.async_fire(EVENT_PLAYBACK_PAUSED, {"episode_id": episode_id})

    async def async_stop(self, force: bool = False) -> None:
        """Stop playback or target speaker when applicable."""
        player = self.storage.data["player"]
        session = self._external_session()
        target = player.get("target_media_player") if player.get("output_mode") == "speaker" else None
        if not target and session.get("active"):
            target = session.get("target_media_player")
        if target:
            await self.async_stop_media_player(str(target), force=force)
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
            if not seek_sent:
                seek_sent = await self._async_seek_via_dlna(target, float(position))
            if not seek_sent and self._target_is_unavailable_or_missing(target):
                await self._clear_active_speaker_target(
                    target,
                    f"Target media player is not available for seek: {target}",
                )
                player = self.storage.data["player"]
            elif seek_sent:
                session = self._external_session()
                session["position"] = int(float(position or 0))
                session["progress_source"] = "dlna" if session.get("control_source") == "dlna" else session.get("progress_source")
                session["updated_at"] = utcnow_iso()
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
            resumed = False
            tried_dlna = False
            try:
                target_state = self._validate_media_player_control_target(target, "resume")
                resume_mode = self._target_control_mode(target, target_state, "resume")
                if resume_mode == "supported":
                    await self.hass.services.async_call(
                        "media_player",
                        "media_play",
                        {"entity_id": target},
                        blocking=True,
                    )
                    resumed = True
                elif resume_mode == "best_effort":
                    tried_dlna = True
                    resumed = await self._async_play_via_dlna(target)
                    if not resumed:
                        raise HomeAssistantError(f"Target media player does not support resume: {target}")
                else:
                    raise HomeAssistantError(f"Target media player does not support resume: {target}")
            except HomeAssistantError as err:
                if not resumed and not tried_dlna:
                    resumed = await self._async_play_via_dlna(target)
                if not resumed:
                    await self._handle_active_target_control_error(target, err)
                    raise
            except Exception as err:  # noqa: BLE001
                if not await self._async_play_via_dlna(target):
                    message = f"Target media player rejected resume: {err}"
                    await self._store_speaker_error(message)
                    raise HomeAssistantError(message) from err
            if resumed:
                if self._external_session().get("control_source") != "dlna":
                    self._external_session()["control_source"] = "ha"
            self.storage.set_player_state("playing", episode_id)
            session = self._external_session()
            session["transport_state"] = "playing"
            session["updated_at"] = utcnow_iso()
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            self._ensure_external_polling()
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
        target_info = self._target_registry_info(media_player_entity_id)
        player = self.storage.data["player"]
        previous_target = player.get("target_media_player") if player.get("output_mode") == "speaker" else None
        if previous_target and previous_target != media_player_entity_id:
            await self.async_stop_media_player(str(previous_target))

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
        description = episode.get("description") or feed.get("description")
        published = episode.get("published")
        metadata: dict[str, Any] = {
            "title": title,
            "artist": podcast_name,
            "albumName": podcast_name,
            "album": podcast_name,
            "creator": podcast_name,
            "publisher": podcast_name,
        }
        if description:
            metadata["description"] = description
            metadata["subtitle"] = description
        if published:
            metadata["releaseDate"] = published
        extra: dict[str, Any] = {
            "title": title,
            "artist": podcast_name,
            "album": podcast_name,
            "creator": podcast_name,
            "metadata": metadata,
        }
        if artwork:
            extra["thumb"] = artwork
            extra["thumbnail"] = artwork
            extra["album_art"] = artwork
            extra["entity_picture"] = artwork
            extra["artwork_url"] = artwork
            metadata["album_art_uri"] = artwork
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
                    if not await self._async_seek_via_dlna(media_player_entity_id, float(resume_seconds)):
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
        self._set_external_session(
            episode_id=episode_id,
            target=media_player_entity_id,
            target_name=target_state.name,
            target_platform=target_info.get("platform"),
            media_content_id=url,
            resume_position=resume_seconds,
            duration=self.storage.get_progress(episode_id).get("duration") or episode.get("duration_seconds"),
        )
        if target_info.get("platform") == "dlna_dmr":
            await self._async_play_via_dlna(media_player_entity_id)
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self._ensure_external_polling()
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

    async def async_prepare_media_source_playback(
        self,
        *,
        episode_id: str,
        media_player_entity_id: str,
        media_content_id: str,
        media_content_type: str,
        url_mode: str,
    ) -> None:
        """Register an external session started through Home Assistant Media Browser.

        Media Source resolution returns a playable URL to Home Assistant. The
        selected target media_player still receives HA's native play_media call,
        but preparing the session here lets Podcast Player services and cards
        observe, stop, seek, and poll the target after playback starts.
        """
        episode = self.storage.get_episode(episode_id)
        if not episode:
            raise HomeAssistantError("Podcast episode was not found")

        target_state = self._validate_media_player_output_target(media_player_entity_id)
        target_info = self._target_registry_info(media_player_entity_id)
        resume_seconds = self._resume_position_for_episode(episode_id)

        self.storage.set_player_state("playing", episode_id)
        player = self.storage.data["player"]
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
        session = self._set_external_session(
            episode_id=episode_id,
            target=media_player_entity_id,
            target_name=target_state.name,
            target_platform=target_info.get("platform"),
            media_content_id=media_content_id,
            resume_position=resume_seconds,
            duration=self.storage.get_progress(episode_id).get("duration") or episode.get("duration_seconds"),
        )
        session["transport_state"] = "buffering"
        session["progress_source"] = "unavailable"
        session["control_source"] = "ha"
        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        self._ensure_external_polling()

    async def async_stop_media_player(self, media_player_entity_id: str | None = None, force: bool = False) -> None:
        """Stop a target media player used for podcast speaker output."""
        player = self.storage.data["player"]
        session = self._external_session()
        target = media_player_entity_id or session.get("target_media_player") or player.get("target_media_player") or player.get("last_target_media_player")
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

        state_value = str(state.state) if state is not None else None
        target_is_active = state_value in ACTIVE_MEDIA_PLAYER_STATES
        target_status = self._target_status(target, state)
        stop_mode = str(target_status["capabilities"].get("stop") or "none")
        if target_is_active and stop_mode not in {"supported", "best_effort"}:
            errors.append(f"Target media player does not support stop: {target}")
        if target_is_active and stop_mode == "supported":
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

        if not stopped:
            try:
                stopped = await self._async_stop_via_dlna(target, force=force)
            except HomeAssistantError as err:
                errors.append(str(err))
                player["last_target_media_player"] = target
                player["last_target_media_player_name"] = target_name
                player["speaker_last_error"] = "; ".join(errors)
                self._external_session()["last_error"] = player["speaker_last_error"]
                await self.storage.async_save()
                self.async_set_updated_data(self.storage.snapshot())
                raise
            except Exception as err:  # noqa: BLE001
                errors.append(f"Enhanced DLNA stop failed: {err}")
                _LOGGER.debug("Podcast Player enhanced DLNA stop failed for %s: %s", target, err)

        if not stopped and (state is None or not target_is_active):
            dlna_status = await self._async_dlna_status(target)
            if dlna_status is None or not dlna_status.is_active:
                stopped = True
                _LOGGER.info("Podcast Player clearing inactive target=%s state=%r", target, state_value)

        player["last_target_media_player"] = target
        player["last_target_media_player_name"] = target_name
        player["speaker_last_error"] = None if stopped else "; ".join(errors)

        if stopped:
            # Clear output only after a real stop path succeeded.
            if target == player.get("target_media_player") or target == session.get("target_media_player"):
                self.storage.set_player_state("idle")
                player["output_mode"] = "browser"
                player["target_media_player"] = None
                player["target_media_player_name"] = None
                player["speaker_url_mode"] = None
                player["speaker_media_content_type"] = None
                self._clear_external_session()
            await self.storage.async_save()
            self.async_set_updated_data(self.storage.snapshot())
            return

        await self.storage.async_save()
        self.async_set_updated_data(self.storage.snapshot())
        raise HomeAssistantError(player["speaker_last_error"] or f"Could not stop {target}")

    def _dlna_description_url(self, target: str) -> str | None:
        """Return a DLNA DMR description URL for a target when available."""
        if not self._enhanced_dlna_controls_enabled():
            return None
        info = self._target_registry_info(target)
        if info.get("platform") != "dlna_dmr":
            return None
        description_url = info.get("description_url")
        return str(description_url) if description_url else None

    async def _async_stop_via_dlna(self, target: str, *, force: bool = False) -> bool:
        """Stop a DLNA target through AVTransport when safe and available."""
        description_url = self._dlna_description_url(target)
        if not description_url:
            return False

        status = await self._external_control.async_status(description_url)
        session = self._external_session()
        if not status.is_active:
            return True
        if status.supported_actions and "Stop" not in status.supported_actions:
            return False

        media_match = current_media_matches_session(
            status.current_media_id,
            self._expected_media_id_for_session(session),
            session.get("episode_id"),
        )
        if media_match is False and not force:
            raise HomeAssistantError("Target is no longer playing this podcast session. Use force stop to stop it anyway.")
        if media_match is not None:
            session["media_matches_session"] = media_match

        await self._external_control.async_stop(description_url)
        session["control_source"] = "dlna"
        session["supported_actions"] = sorted(status.supported_actions or [])
        return True

    async def _async_pause_via_dlna(self, target: str) -> bool:
        """Pause a DLNA target through AVTransport when available."""
        description_url = self._dlna_description_url(target)
        if not description_url:
            return False
        try:
            status = await self._external_control.async_status(description_url)
            if status.supported_actions and "Pause" not in status.supported_actions:
                return False
            await self._external_control.async_pause(description_url)
        except Exception:  # noqa: BLE001
            return False
        session = self._external_session()
        session["control_source"] = "dlna"
        session["supported_actions"] = sorted(status.supported_actions or [])
        return True

    async def _async_play_via_dlna(self, target: str) -> bool:
        """Resume a DLNA target through AVTransport when available."""
        description_url = self._dlna_description_url(target)
        if not description_url:
            return False
        try:
            status = await self._external_control.async_status(description_url)
            if status.supported_actions and "Play" not in status.supported_actions:
                return False
            await self._external_control.async_play(description_url)
        except Exception:  # noqa: BLE001
            return False
        session = self._external_session()
        session["control_source"] = "dlna"
        session["supported_actions"] = sorted(status.supported_actions or [])
        return True

    async def _async_seek_via_dlna(self, target: str, position: float) -> bool:
        """Seek a DLNA target through AVTransport when available."""
        description_url = self._dlna_description_url(target)
        if not description_url:
            return False
        try:
            status = await self._external_control.async_status(description_url)
            if status.supported_actions and "Seek" not in status.supported_actions:
                return False
            await self._external_control.async_seek(description_url, position)
        except Exception:  # noqa: BLE001
            return False
        session = self._external_session()
        session["control_source"] = "dlna"
        session["supported_actions"] = sorted(status.supported_actions or [])
        return True

    def _validate_media_player_output_target(self, entity_id: str) -> Any:
        """Validate that a media_player target is safe to use for podcast output."""
        if not self._is_external_media_player_entity_id(entity_id):
            raise HomeAssistantError("Target must be an external media_player entity")
        target_state = self.hass.states.get(entity_id)
        status = self._target_status(entity_id, target_state)
        if not status["playable"]:
            raise HomeAssistantError(str(status["reason"] or f"Target media player is not available: {entity_id}"))
        return target_state

    def _is_external_media_player_entity_id(self, entity_id: str) -> bool:
        """Return true for real output media_player entities."""
        return is_external_media_player_entity_id(entity_id)

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
        self._clear_external_session()

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
