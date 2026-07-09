"""HA Podcast Player integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import async_register_api
from .const import (
    ALLOWED_SPEEDS,
    CONF_INITIAL_RSS_URL,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_FEED,
    SERVICE_MARK_CURRENT_PLAYED,
    SERVICE_MARK_FEED_PLAYED,
    SERVICE_MARK_PLAYED,
    SERVICE_MARK_UNPLAYED,
    SERVICE_PAUSE,
    SERVICE_PLAY_CURRENT,
    SERVICE_PLAY_EPISODE,
    SERVICE_PLAY_LATEST,
    SERVICE_PLAY_NEXT_UNPLAYED,
    SERVICE_PLAY_ON_MEDIA_PLAYER,
    SERVICE_REFRESH,
    SERVICE_REFRESH_FEEDS,
    SERVICE_REMOVE_FEED,
    SERVICE_RESUME,
    SERVICE_SAVE_PROGRESS,
    SERVICE_SEEK,
    SERVICE_SET_SPEED,
    SERVICE_STOP,
    SERVICE_STOP_MEDIA_PLAYER,
    SERVICE_STOP_OUTPUT,
)
from .coordinator import PodcastRuntime, PodcastUpdateCoordinator
from .feed_parser import PodcastParseError
from .storage import PodcastStorage, make_feed_id, normalize_rss_url

_LOGGER = logging.getLogger(__name__)

INITIAL_FEED_RETRY_ERROR_CODES = {"cannot_connect", "http_error", "redirect_loop", "ssl_error", "timeout"}

# Home Assistant service targets are exposed as data["entity_id"] for these
# custom services. Feed-selecting services use podcast feed sensor entities as
# the target; the output media player remains media_player_entity_id in data.
_SERVICE_TARGET_ENTITY = vol.Any(cv.entity_id, [cv.entity_id], None)

_SERVICE_FEED_FIELDS = {
    vol.Optional("entity_id"): _SERVICE_TARGET_ENTITY,
    vol.Optional("feed_id"): cv.string,
    vol.Optional("feed_name"): cv.string,
}

SERVICE_ADD_FEED_SCHEMA = vol.Schema({vol.Required("rss_url"): cv.string})
SERVICE_REMOVE_FEED_SCHEMA = vol.Schema({vol.Required("feed_id"): cv.string, vol.Optional("keep_history", default=True): cv.boolean})
SERVICE_REFRESH_SCHEMA = vol.Schema({**_SERVICE_FEED_FIELDS})
SERVICE_MARK_CURRENT_PLAYED_SCHEMA = vol.Schema({vol.Optional("played", default=True): cv.boolean})
SERVICE_MARK_FEED_PLAYED_SCHEMA = vol.Schema({**_SERVICE_FEED_FIELDS, vol.Optional("played", default=True): cv.boolean})

_SERVICE_OUTPUT_FIELDS = {
    **_SERVICE_FEED_FIELDS,
    vol.Optional("media_player_entity_id"): cv.entity_id,
    vol.Optional("url_mode", default="direct"): vol.In(["direct", "signed_proxy"]),
    vol.Optional("media_content_type", default="music"): vol.In(["music", "podcast"]),
    vol.Optional("resume_position"): vol.Coerce(float),
    # Backward-compatible alias from v0.2.0-v0.2.2. If true, url_mode becomes signed_proxy.
    vol.Optional("prefer_proxy", default=False): cv.boolean,
}

SERVICE_EPISODE_SCHEMA = vol.Schema({vol.Required("episode_id"): cv.string, **_SERVICE_OUTPUT_FIELDS})
SERVICE_OPTIONAL_FEED_SCHEMA = vol.Schema({**_SERVICE_OUTPUT_FIELDS})
SERVICE_PLAY_CURRENT_SCHEMA = vol.Schema({**_SERVICE_OUTPUT_FIELDS})
SERVICE_PLAY_ON_MEDIA_PLAYER_SCHEMA = vol.Schema({
    **_SERVICE_FEED_FIELDS,
    vol.Required("media_player_entity_id"): cv.entity_id,
    vol.Optional("episode_id"): cv.string,
    vol.Optional("episode_mode", default="current"): vol.In(["current", "next_unplayed", "latest"]),
    vol.Optional("url_mode", default="direct"): vol.In(["direct", "signed_proxy"]),
    vol.Optional("media_content_type", default="music"): vol.In(["music", "podcast"]),
    vol.Optional("resume_position"): vol.Coerce(float),
    vol.Optional("prefer_proxy", default=False): cv.boolean,
})
SERVICE_SEEK_SCHEMA = vol.Schema({vol.Optional("entity_id"): _SERVICE_TARGET_ENTITY, vol.Required("episode_id"): cv.string, vol.Required("position"): vol.Coerce(float)})
SERVICE_SAVE_PROGRESS_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): _SERVICE_TARGET_ENTITY,
        vol.Required("episode_id"): cv.string,
        vol.Required("position"): vol.Coerce(float),
        vol.Optional("duration"): vol.Coerce(float),
        vol.Optional("playing"): cv.boolean,
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.In(ALLOWED_SPEEDS)),
    }
)
SERVICE_STOP_MEDIA_PLAYER_SCHEMA = vol.Schema({
    vol.Optional("entity_id"): _SERVICE_TARGET_ENTITY,
    vol.Optional("media_player_entity_id"): cv.entity_id,
    vol.Optional("force", default=False): cv.boolean,
})
SERVICE_STOP_SCHEMA = vol.Schema({vol.Optional("force", default=False): cv.boolean})
SERVICE_MARK_EPISODE_SCHEMA = vol.Schema({vol.Required("episode_id"): cv.string})
SERVICE_SET_SPEED_SCHEMA = vol.Schema({vol.Optional("entity_id"): _SERVICE_TARGET_ENTITY, vol.Required("speed"): vol.All(vol.Coerce(float), vol.In(ALLOWED_SPEEDS)), vol.Optional("episode_id"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Podcast Player integration."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Podcast Player from a config entry."""
    storage = PodcastStorage(hass)
    await storage.async_load()
    if entry.options:
        storage.data["settings"].update(dict(entry.options))
        await storage.async_save()
    coordinator = PodcastUpdateCoordinator(hass, entry, storage)
    await coordinator.async_initialize()

    runtime = PodcastRuntime(storage=storage, coordinator=coordinator)
    await _async_import_initial_feed(hass, entry, runtime)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    entry.runtime_data = runtime

    async_register_api(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Do an initial non-blocking refresh shortly after startup if feeds exist.
    if storage.enabled_feeds():
        hass.async_create_task(coordinator.async_refresh_feeds())

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def _async_import_initial_feed(hass: HomeAssistant, entry: ConfigEntry, runtime: PodcastRuntime) -> None:
    """Add the optional first feed captured by the config flow."""
    initial_rss_url = entry.data.get(CONF_INITIAL_RSS_URL)
    if not initial_rss_url:
        return

    try:
        normalized_url = normalize_rss_url(initial_rss_url)
    except ValueError as err:
        raise ConfigEntryError(f"Could not add initial podcast feed: {err}") from err

    feed_id = make_feed_id(normalized_url)
    if not runtime.storage.get_feed(feed_id):
        try:
            await runtime.coordinator.async_add_feed(normalized_url)
        except PodcastParseError as err:
            message = f"Could not add initial podcast feed: {err.message}"
            if err.code in INITIAL_FEED_RETRY_ERROR_CODES:
                raise ConfigEntryNotReady(message) from err
            raise ConfigEntryError(message) from err

    data = dict(entry.data)
    data.pop(CONF_INITIAL_RSS_URL, None)
    hass.config_entries.async_update_entry(entry, data=data)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        await runtime.coordinator.async_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def _runtime(hass: HomeAssistant) -> PodcastRuntime:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("Podcast Player is not configured")
    return next(iter(entries.values()))


def _url_mode_from_service(data: dict[str, Any]) -> str:
    """Return URL mode, honoring the legacy prefer_proxy alias."""
    if data.get("prefer_proxy", False):
        return "signed_proxy"
    return data.get("url_mode", "direct")


def _as_entity_id_list(value: Any) -> list[str]:
    """Normalize a HA target entity_id value to a list."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _target_contains_media_player(data: dict[str, Any]) -> bool:
    """Return true if a HA service target contains a real media_player."""
    return any(
        entity_id.startswith("media_player.") and entity_id != "media_player.podcast_player"
        for entity_id in _as_entity_id_list(data.get("entity_id"))
    )


def _raise_media_player_target_help(service_hint: str) -> None:
    """Raise a clear error when an output media player was put in the wrong target field."""
    raise ServiceValidationError(
        f"{service_hint}: action Target selects the podcast feed sensor. "
        "Put the output media player in data.media_player_entity_id instead. "
        "Example: target.entity_id = sensor.example_podcast_feed, "
        "data.media_player_entity_id = media_player.kitchen_speaker."
    )




def _media_player_entity_id_from_service(data: dict[str, Any]) -> str | None:
    """Resolve an output media_player from either data or HA service target.

    media_player_entity_id is the explicit/backward-compatible field.
    entity_id is supplied by Home Assistant when the service is called with
    target.entity_id. Only media_player targets are accepted here.
    """
    explicit = data.get("media_player_entity_id")
    if explicit:
        return explicit

    media_targets = [
        entity_id
        for entity_id in _as_entity_id_list(data.get("entity_id"))
        if entity_id.startswith("media_player.")
    ]
    if not media_targets:
        return None
    if len(media_targets) > 1:
        raise ServiceValidationError("Select only one media_player target for this stop service")
    return media_targets[0]


def _feed_id_from_name(runtime: PodcastRuntime, feed_name: str | None) -> str | None:
    """Resolve a human feed name to a feed_id. Exact match wins; then unique substring."""
    if not feed_name:
        return None
    wanted = feed_name.strip().casefold()
    if not wanted:
        return None

    feeds = runtime.storage.enabled_feeds()
    exact = [feed for feed in feeds if str(feed.get("title") or "").casefold() == wanted]
    if len(exact) == 1:
        return exact[0].get("feed_id")

    partial = [feed for feed in feeds if wanted in str(feed.get("title") or "").casefold()]
    if len(partial) == 1:
        return partial[0].get("feed_id")

    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(str(feed.get("title") or feed.get("feed_id")) for feed in (exact or partial)[:5])
        raise ServiceValidationError(f"Podcast feed name is ambiguous: {feed_name}. Matches: {names}")

    raise ServiceValidationError(f"Podcast feed not found by name: {feed_name}")


def _feed_ids_from_service(runtime: PodcastRuntime, data: dict[str, Any], *, service_hint: str = "Podcast Player service") -> list[str]:
    """Resolve service target/feed fields into feed IDs.

    Priority:
    1. target.entity_id / data.entity_id pointing at Podcast feed sensors
    2. legacy feed_id field
    3. friendly feed_name field
    4. no feed filter
    """
    feed_ids: list[str] = []
    seen: set[str] = set()
    entity_ids = _as_entity_id_list(data.get("entity_id"))
    for entity_id in entity_ids:
        # Older service versions accepted the generic Podcast Player media entity
        # target as a harmless UI dummy. Keep ignoring that one old target instead
        # of breaking saved automations that still contain it. Real feed targets
        # are Podcast Player sensor entities with feed_id attributes.
        if entity_id == "media_player.podcast_player":
            continue
        state = runtime.coordinator.hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}
        feed_id = attrs.get("feed_id")
        entity_type = attrs.get("podcast_player_entity_type")
        if feed_id and entity_type == "feed" and runtime.storage.get_feed(feed_id):
            if feed_id not in seen:
                seen.add(feed_id)
                feed_ids.append(feed_id)
            continue
        if entity_id.startswith("media_player."):
            _raise_media_player_target_help(service_hint)
        raise ServiceValidationError(f"Target {entity_id} is not a Podcast Player feed sensor")

    if feed_ids:
        return feed_ids

    feed_id = data.get("feed_id")
    if feed_id and feed_id != "all":
        if not runtime.storage.get_feed(feed_id):
            raise ServiceValidationError(f"Podcast feed not found: {feed_id}")
        return [feed_id]

    feed_name_id = _feed_id_from_name(runtime, data.get("feed_name"))
    return [feed_name_id] if feed_name_id else []


def _single_feed_id(feed_ids: list[str]) -> str | None:
    """Return a single feed_id where older APIs expect one."""
    if not feed_ids:
        return None
    if len(feed_ids) == 1:
        return feed_ids[0]
    return None


async def _play_episode_or_output(runtime: PodcastRuntime, episode_id: str, data: dict[str, Any]) -> None:
    """Play an episode either in the browser/card or on a media_player target."""
    target = data.get("media_player_entity_id")
    if not target and _target_contains_media_player(data):
        _raise_media_player_target_help("play_episode")
    if target:
        await runtime.coordinator.async_play_on_media_player(
            target,
            episode_id=episode_id,
            url_mode=_url_mode_from_service(data),
            media_content_type=data.get("media_content_type", "music"),
            resume_position=data.get("resume_position"),
        )
        return
    await runtime.coordinator.async_play_episode(episode_id)


async def _play_selected_or_output(
    runtime: PodcastRuntime,
    data: dict[str, Any],
    *,
    episode_mode: str,
) -> None:
    """Select current/latest/next episode and play on browser or target."""
    target = data.get("media_player_entity_id")
    feed_ids = _feed_ids_from_service(runtime, data, service_hint=f"play_{episode_mode}")
    selected_feed_ids = set(feed_ids) if feed_ids else None
    feed_id = _single_feed_id(feed_ids)

    if target:
        await runtime.coordinator.async_play_on_media_player(
            target,
            feed_id=feed_id,
            feed_ids=selected_feed_ids,
            episode_mode=episode_mode,
            url_mode=_url_mode_from_service(data),
            media_content_type=data.get("media_content_type", "music"),
            resume_position=data.get("resume_position"),
        )
        return

    if episode_mode == "latest":
        episode = await runtime.coordinator.async_play_latest(feed_id, feed_ids=selected_feed_ids)
    elif episode_mode == "next_unplayed":
        episode = await runtime.coordinator.async_play_next_unplayed(feed_id, feed_ids=selected_feed_ids)
    else:
        current_id = runtime.storage.data["player"].get("current_episode_id")
        current = runtime.storage.get_episode(current_id) if current_id else None
        if current and (not selected_feed_ids or current.get("feed_id") in selected_feed_ids):
            await runtime.coordinator.async_play_episode(current["episode_id"])
            episode = current
        else:
            episode = await runtime.coordinator.async_play_next_unplayed(feed_id, feed_ids=selected_feed_ids)
            if episode is None:
                episode = await runtime.coordinator.async_play_latest(feed_id, feed_ids=selected_feed_ids)

    if episode is None:
        raise HomeAssistantError("No matching podcast episode found")


def async_register_services(hass: HomeAssistant) -> None:
    """Register services once."""
    if hass.data.get(f"{DOMAIN}_services_registered"):
        return

    async def add_feed(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        try:
            await runtime.coordinator.async_add_feed(call.data["rss_url"])
        except PodcastParseError as err:
            raise HomeAssistantError(err.message) from err
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to add podcast feed")
            raise HomeAssistantError(str(err)) from err

    async def remove_feed(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        removed = await runtime.coordinator.async_remove_feed(call.data["feed_id"], call.data.get("keep_history", True))
        if not removed:
            raise ServiceValidationError(f"Podcast feed not found: {call.data['feed_id']}")

    async def refresh(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        data = dict(call.data)
        feed_ids = _feed_ids_from_service(runtime, data, service_hint="refresh_feeds")
        if feed_ids:
            for feed_id in feed_ids:
                await runtime.coordinator.async_refresh_feeds(feed_id)
        else:
            await runtime.coordinator.async_refresh_feeds()

    async def play_episode(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await _play_episode_or_output(runtime, call.data["episode_id"], dict(call.data))

    async def play_current(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await _play_selected_or_output(runtime, dict(call.data), episode_mode="current")

    async def play_latest(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await _play_selected_or_output(runtime, dict(call.data), episode_mode="latest")

    async def play_next_unplayed(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await _play_selected_or_output(runtime, dict(call.data), episode_mode="next_unplayed")

    async def resume(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_resume()

    async def pause(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_pause()

    async def stop(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_stop(force=call.data.get("force", False))

    async def seek(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_seek(call.data["episode_id"], call.data["position"])

    async def save_progress(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_save_progress(
            call.data["episode_id"],
            call.data["position"],
            call.data.get("duration"),
            call.data.get("playing"),
            call.data.get("speed"),
        )

    async def mark_played(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_mark_played(call.data["episode_id"], True)

    async def mark_unplayed(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_mark_played(call.data["episode_id"], False)

    async def mark_current_played(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_mark_current_played(call.data.get("played", True))

    async def mark_feed_played(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        data = dict(call.data)
        feed_ids = _feed_ids_from_service(runtime, data, service_hint="mark_feed_played")
        if not feed_ids:
            raise ServiceValidationError("Select a Podcast feed target, or provide feed_id/feed_name")
        for feed_id in feed_ids:
            await runtime.coordinator.async_mark_feed_played(feed_id, data.get("played", True))

    async def play_on_media_player(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        data = dict(call.data)
        feed_ids = _feed_ids_from_service(runtime, data, service_hint="play_on_media_player")
        selected_feed_ids = set(feed_ids) if feed_ids else None
        await runtime.coordinator.async_play_on_media_player(
            data["media_player_entity_id"],
            episode_id=data.get("episode_id"),
            feed_id=_single_feed_id(feed_ids),
            feed_ids=selected_feed_ids,
            episode_mode=data.get("episode_mode", "current"),
            url_mode=_url_mode_from_service(data),
            media_content_type=data.get("media_content_type", "music"),
            resume_position=data.get("resume_position"),
        )

    async def stop_media_player(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        data = dict(call.data)
        await runtime.coordinator.async_stop_media_player(_media_player_entity_id_from_service(data), force=data.get("force", False))

    async def stop_output(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        target = _media_player_entity_id_from_service(dict(call.data))
        if target:
            await runtime.coordinator.async_stop_media_player(target, force=call.data.get("force", False))
        else:
            await runtime.coordinator.async_stop(force=call.data.get("force", False))

    async def set_speed(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        await runtime.coordinator.async_set_speed(call.data["speed"], call.data.get("episode_id"))

    hass.services.async_register(DOMAIN, SERVICE_ADD_FEED, add_feed, schema=SERVICE_ADD_FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_FEED, remove_feed, schema=SERVICE_REMOVE_FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh, schema=SERVICE_REFRESH_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_FEEDS, refresh, schema=SERVICE_REFRESH_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_EPISODE, play_episode, schema=SERVICE_EPISODE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_CURRENT, play_current, schema=SERVICE_PLAY_CURRENT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_LATEST, play_latest, schema=SERVICE_OPTIONAL_FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_NEXT_UNPLAYED, play_next_unplayed, schema=SERVICE_OPTIONAL_FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RESUME, resume)
    hass.services.async_register(DOMAIN, SERVICE_PAUSE, pause)
    hass.services.async_register(DOMAIN, SERVICE_STOP, stop, schema=SERVICE_STOP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SEEK, seek, schema=SERVICE_SEEK_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SAVE_PROGRESS, save_progress, schema=SERVICE_SAVE_PROGRESS_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_PLAYED, mark_played, schema=SERVICE_MARK_EPISODE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_UNPLAYED, mark_unplayed, schema=SERVICE_MARK_EPISODE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_CURRENT_PLAYED, mark_current_played, schema=SERVICE_MARK_CURRENT_PLAYED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MARK_FEED_PLAYED, mark_feed_played, schema=SERVICE_MARK_FEED_PLAYED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_ON_MEDIA_PLAYER, play_on_media_player, schema=SERVICE_PLAY_ON_MEDIA_PLAYER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_MEDIA_PLAYER, stop_media_player, schema=SERVICE_STOP_MEDIA_PLAYER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_OUTPUT, stop_output, schema=SERVICE_STOP_MEDIA_PLAYER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_SPEED, set_speed, schema=SERVICE_SET_SPEED_SCHEMA)
    hass.data[f"{DOMAIN}_services_registered"] = True
