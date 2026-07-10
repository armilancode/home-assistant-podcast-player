"""Tests for integration setup helpers and registered service actions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.podcast_player import (
    _as_entity_id_list,
    _feed_id_from_name,
    _feed_ids_from_service,
    _media_player_entity_id_from_service,
    _play_episode_or_output,
    _play_selected_or_output,
    _runtime,
    _single_feed_id,
    _target_contains_media_player,
    _url_mode_from_service,
    async_register_services,
    async_setup,
    async_setup_entry,
    async_unload_entry,
    async_update_options,
)
from custom_components.podcast_player.const import (
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
from custom_components.podcast_player.feed_parser import PodcastParseError
from custom_components.podcast_player.storage import default_data, make_feed_id


class FakeStorage:
    """Minimal storage object for service tests."""

    def __init__(self) -> None:
        self.data = default_data()
        self.data["feeds"] = {
            "feed_1": {"feed_id": "feed_1", "title": "Feed One", "enabled": True},
            "feed_2": {"feed_id": "feed_2", "title": "Second Feed", "enabled": True},
        }
        self.data["episodes"] = {
            "ep_1": {"episode_id": "ep_1", "feed_id": "feed_1", "title": "Episode One"},
            "ep_2": {"episode_id": "ep_2", "feed_id": "feed_2", "title": "Episode Two"},
        }
        self.data["player"]["current_episode_id"] = "ep_1"
        self.async_load = AsyncMock()
        self.async_save = AsyncMock()

    def get_feed(self, feed_id: str | None) -> dict | None:
        """Return a feed by id."""
        return self.data["feeds"].get(feed_id)

    def get_episode(self, episode_id: str | None) -> dict | None:
        """Return an episode by id."""
        return self.data["episodes"].get(episode_id)

    def enabled_feeds(self) -> list[dict]:
        """Return enabled feeds."""
        return [feed for feed in self.data["feeds"].values() if feed.get("enabled", True)]


class FakeCoordinator:
    """Coordinator mock with async service methods."""

    def __init__(self, hass, storage: FakeStorage) -> None:
        self.hass = hass
        self.storage = storage
        self.async_add_feed = AsyncMock(return_value={"feed_id": "feed_3"})
        self.async_remove_feed = AsyncMock(return_value=True)
        self.async_refresh_feeds = AsyncMock()
        self.async_play_episode = AsyncMock()
        self.async_play_on_media_player = AsyncMock(return_value=storage.data["episodes"]["ep_1"])
        self.async_play_latest = AsyncMock(return_value=storage.data["episodes"]["ep_1"])
        self.async_play_next_unplayed = AsyncMock(return_value=storage.data["episodes"]["ep_2"])
        self.async_resume = AsyncMock()
        self.async_pause = AsyncMock()
        self.async_stop = AsyncMock()
        self.async_seek = AsyncMock()
        self.async_save_progress = AsyncMock()
        self.async_mark_played = AsyncMock()
        self.async_mark_current_played = AsyncMock()
        self.async_mark_feed_played = AsyncMock()
        self.async_stop_media_player = AsyncMock()
        self.async_set_speed = AsyncMock()
        self.async_initialize = AsyncMock()
        self.async_shutdown = AsyncMock()


def _install_runtime(hass, *, storage: FakeStorage | None = None, coordinator: FakeCoordinator | None = None) -> SimpleNamespace:
    """Install a fake integration runtime into hass."""
    storage = storage or FakeStorage()
    coordinator = coordinator or FakeCoordinator(hass, storage)
    runtime = SimpleNamespace(storage=storage, coordinator=coordinator)
    hass.data.setdefault(DOMAIN, {})["entry"] = runtime
    return runtime


def _feed_state(hass, entity_id: str = "sensor.feed_one", feed_id: str = "feed_1") -> None:
    """Install a feed sensor state usable as a service target."""
    hass.states.async_set(
        entity_id,
        "1",
        {
            "feed_id": feed_id,
            "podcast_player_entity_type": "feed",
        },
    )


def test_small_service_helpers() -> None:
    """Small service helper functions normalize service input safely."""
    assert _url_mode_from_service({"url_mode": "direct"}) == "direct"
    assert _url_mode_from_service({"url_mode": "direct", "prefer_proxy": True}) == "signed_proxy"
    assert _as_entity_id_list(None) == []
    assert _as_entity_id_list("") == []
    assert _as_entity_id_list("sensor.feed_one") == ["sensor.feed_one"]
    assert _as_entity_id_list(["sensor.feed_one", None, ""]) == ["sensor.feed_one"]
    assert _as_entity_id_list(("sensor.feed_one",)) == ["sensor.feed_one"]
    assert _as_entity_id_list({"sensor.feed_one"}) == ["sensor.feed_one"]
    assert _as_entity_id_list(123) == ["123"]
    assert _target_contains_media_player({"entity_id": ["sensor.feed_one", "media_player.kitchen"]}) is True
    assert _target_contains_media_player({"entity_id": "media_player.podcast_player"}) is False
    assert _media_player_entity_id_from_service({"media_player_entity_id": "media_player.kitchen"}) == "media_player.kitchen"
    assert _media_player_entity_id_from_service({"entity_id": "media_player.kitchen"}) == "media_player.kitchen"
    assert _media_player_entity_id_from_service({}) is None
    assert _single_feed_id([]) is None
    assert _single_feed_id(["feed_1"]) == "feed_1"
    assert _single_feed_id(["feed_1", "feed_2"]) is None

    with pytest.raises(ServiceValidationError):
        _media_player_entity_id_from_service({"entity_id": ["media_player.one", "media_player.two"]})


def test_runtime_and_feed_resolution_helpers(hass) -> None:
    """Feed resolution helpers accept feed IDs, names, and feed sensor targets."""
    runtime = _install_runtime(hass)
    _feed_state(hass)

    assert _runtime(hass) is runtime
    assert _feed_id_from_name(runtime, None) is None
    assert _feed_id_from_name(runtime, "") is None
    assert _feed_id_from_name(runtime, "Feed One") == "feed_1"
    assert _feed_id_from_name(runtime, "Second") == "feed_2"
    assert _feed_ids_from_service(runtime, {"entity_id": "sensor.feed_one"}) == ["feed_1"]
    assert _feed_ids_from_service(runtime, {"entity_id": "media_player.podcast_player"}) == []
    assert _feed_ids_from_service(runtime, {"feed_id": "feed_2"}) == ["feed_2"]
    assert _feed_ids_from_service(runtime, {"feed_id": "all"}) == []
    assert _feed_ids_from_service(runtime, {"feed_name": "Second Feed"}) == ["feed_2"]

    with pytest.raises(HomeAssistantError):
        _runtime(SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        _feed_ids_from_service(runtime, {"entity_id": "sensor.not_feed"})
    with pytest.raises(ServiceValidationError):
        _feed_ids_from_service(runtime, {"feed_id": "missing"})
    with pytest.raises(ServiceValidationError):
        _feed_id_from_name(runtime, "missing")


def test_feed_name_resolution_reports_ambiguous_matches(hass) -> None:
    """Ambiguous feed names raise a useful validation error."""
    storage = FakeStorage()
    storage.data["feeds"]["feed_3"] = {"feed_id": "feed_3", "title": "Feed One Extended", "enabled": True}
    runtime = _install_runtime(hass, storage=storage)

    with pytest.raises(ServiceValidationError):
        _feed_id_from_name(runtime, "Feed")


async def test_async_setup_registers_services_once(hass) -> None:
    """YAML setup path registers Podcast Player services idempotently."""
    assert await async_setup(hass, {}) is True

    registered = len([key for key in hass.services.async_services().get(DOMAIN, {})])
    async_register_services(hass)

    assert len([key for key in hass.services.async_services().get(DOMAIN, {})]) == registered


async def test_registered_services_dispatch_to_coordinator(hass) -> None:
    """Registered services route service data to coordinator methods."""
    async_register_services(hass)
    runtime = _install_runtime(hass)
    coordinator = runtime.coordinator
    _feed_state(hass)

    await hass.services.async_call(DOMAIN, SERVICE_ADD_FEED, {"rss_url": "https://example.test/new.xml"}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_REMOVE_FEED, {"feed_id": "feed_1", "keep_history": False}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH, {"entity_id": "sensor.feed_one"}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_FEEDS, {}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_PLAY_EPISODE, {"episode_id": "ep_1"}, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_PLAY_EPISODE,
        {
            "episode_id": "ep_1",
            "media_player_entity_id": "media_player.kitchen",
            "prefer_proxy": True,
            "media_content_type": "podcast",
            "resume_position": 12,
        },
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_PLAY_CURRENT, {}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_PLAY_LATEST, {"entity_id": "sensor.feed_one"}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_PLAY_NEXT_UNPLAYED, {"entity_id": "sensor.feed_one"}, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_PLAY_NEXT_UNPLAYED,
        {"media_player_entity_id": "media_player.kitchen", "entity_id": "sensor.feed_one"},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_RESUME, {}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_PAUSE, {}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_STOP, {"force": True}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_SEEK, {"episode_id": "ep_1", "position": 42}, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SAVE_PROGRESS,
        {"episode_id": "ep_1", "position": 43, "duration": 100, "playing": True, "speed": 1.25},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_MARK_PLAYED, {"episode_id": "ep_1"}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_MARK_UNPLAYED, {"episode_id": "ep_1"}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_MARK_CURRENT_PLAYED, {"played": False}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_MARK_FEED_PLAYED, {"entity_id": "sensor.feed_one"}, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_PLAY_ON_MEDIA_PLAYER,
        {
            "media_player_entity_id": "media_player.kitchen",
            "entity_id": "sensor.feed_one",
            "episode_mode": "latest",
            "url_mode": "signed_proxy",
            "media_content_type": "podcast",
            "resume_position": 5,
        },
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_STOP_MEDIA_PLAYER,
        {"media_player_entity_id": "media_player.kitchen", "force": True},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_STOP_OUTPUT,
        {"entity_id": "media_player.kitchen", "force": True},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_STOP_OUTPUT, {"force": False}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_SET_SPEED, {"speed": 1.5, "episode_id": "ep_1"}, blocking=True)

    coordinator.async_add_feed.assert_awaited_once_with("https://example.test/new.xml")
    coordinator.async_remove_feed.assert_awaited_once_with("feed_1", False)
    assert coordinator.async_refresh_feeds.await_args_list[0].args == ("feed_1",)
    assert coordinator.async_refresh_feeds.await_args_list[1].args == ()
    coordinator.async_play_episode.assert_any_await("ep_1")
    coordinator.async_play_on_media_player.assert_any_await(
        "media_player.kitchen",
        episode_id="ep_1",
        url_mode="signed_proxy",
        media_content_type="podcast",
        resume_position=12,
    )
    coordinator.async_play_latest.assert_awaited_once_with("feed_1", feed_ids={"feed_1"})
    coordinator.async_play_next_unplayed.assert_awaited_once_with("feed_1", feed_ids={"feed_1"})
    coordinator.async_play_on_media_player.assert_any_await(
        "media_player.kitchen",
        feed_id="feed_1",
        feed_ids={"feed_1"},
        episode_mode="next_unplayed",
        url_mode="direct",
        media_content_type="music",
        resume_position=None,
    )
    coordinator.async_resume.assert_awaited_once()
    coordinator.async_pause.assert_awaited_once()
    coordinator.async_stop.assert_any_await(force=True)
    coordinator.async_seek.assert_awaited_once_with("ep_1", 42.0)
    coordinator.async_save_progress.assert_awaited_once_with("ep_1", 43.0, 100.0, True, 1.25)
    coordinator.async_mark_played.assert_any_await("ep_1", True)
    coordinator.async_mark_played.assert_any_await("ep_1", False)
    coordinator.async_mark_current_played.assert_awaited_once_with(False)
    coordinator.async_mark_feed_played.assert_awaited_once_with("feed_1", True)
    coordinator.async_stop_media_player.assert_any_await("media_player.kitchen", force=True)
    coordinator.async_stop.assert_any_await(force=False)
    coordinator.async_set_speed.assert_awaited_once_with(1.5, "ep_1")


async def test_add_feed_service_converts_parse_errors(hass) -> None:
    """Add-feed service turns parser failures into HomeAssistantError."""
    async_register_services(hass)
    runtime = _install_runtime(hass)
    runtime.coordinator.async_add_feed.side_effect = PodcastParseError("invalid_url", "Invalid feed")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, SERVICE_ADD_FEED, {"rss_url": "https://example.test/bad.xml"}, blocking=True)


async def test_add_feed_service_logs_unexpected_errors(hass) -> None:
    """Unexpected add-feed failures are wrapped for service callers."""
    async_register_services(hass)
    runtime = _install_runtime(hass)
    runtime.coordinator.async_add_feed.side_effect = RuntimeError("boom")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, SERVICE_ADD_FEED, {"rss_url": "https://example.test/bad.xml"}, blocking=True)


async def test_play_helpers_raise_for_wrong_or_empty_selections(hass) -> None:
    """Playback helper paths raise clear errors for wrong targets or empty selections."""
    runtime = _install_runtime(hass)
    runtime.coordinator.async_play_next_unplayed.return_value = None
    runtime.coordinator.async_play_latest.return_value = None

    with pytest.raises(ServiceValidationError):
        await _play_episode_or_output(runtime, "ep_1", {"entity_id": "media_player.kitchen"})

    runtime.storage.data["player"]["current_episode_id"] = None
    with pytest.raises(HomeAssistantError):
        await _play_selected_or_output(runtime, {}, episode_mode="current")


async def test_setup_entry_applies_options_and_schedules_initial_refresh(hass, enable_custom_integrations) -> None:
    """Setup applies entry options, forwards platforms, and schedules initial refresh."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"refresh_interval_minutes": 30})
    entry.add_to_hass(hass)
    storage = FakeStorage()
    created_tasks: list[object] = []

    def record_task(coro):
        created_tasks.append(coro)
        coro.close()
        return object()

    with (
        patch("custom_components.podcast_player.PodcastStorage", return_value=storage),
        patch("custom_components.podcast_player.PodcastUpdateCoordinator", side_effect=lambda hass, entry, storage: FakeCoordinator(hass, storage)),
        patch("custom_components.podcast_player.async_register_api"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)) as forward_setups,
        patch.object(hass, "async_create_task", side_effect=record_task),
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    assert storage.data["settings"]["refresh_interval_minutes"] == 30
    storage.async_load.assert_awaited_once()
    storage.async_save.assert_awaited_once()
    forward_setups.assert_awaited_once_with(entry, PLATFORMS)
    assert created_tasks


async def test_setup_entry_initial_feed_invalid_url_is_config_entry_error(hass, enable_custom_integrations) -> None:
    """Invalid stored initial feed URLs fail setup as config entry errors."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INITIAL_RSS_URL: "ftp://example.test/feed.xml"})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.podcast_player.PodcastStorage.async_load", AsyncMock()),
        pytest.raises(ConfigEntryError),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_initial_feed_existing_feed_is_not_imported(hass, enable_custom_integrations) -> None:
    """Initial-feed import is skipped when the feed already exists in storage."""
    rss_url = "https://example.test/feed.xml"
    feed_id = make_feed_id(rss_url)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INITIAL_RSS_URL: rss_url})
    entry.add_to_hass(hass)
    storage = FakeStorage()
    storage.data["feeds"][feed_id] = {"feed_id": feed_id, "title": "Existing", "enabled": True}
    coordinator = FakeCoordinator(hass, storage)

    with (
        patch("custom_components.podcast_player.PodcastStorage", return_value=storage),
        patch("custom_components.podcast_player.PodcastUpdateCoordinator", return_value=coordinator),
        patch("custom_components.podcast_player.async_register_api"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        assert await async_setup_entry(hass, entry) is True

    coordinator.async_add_feed.assert_not_awaited()
    assert CONF_INITIAL_RSS_URL not in entry.data


@pytest.mark.parametrize(
    ("parse_error", "expected_exception"),
    [
        (PodcastParseError("timeout", "Timed out"), ConfigEntryNotReady),
        (PodcastParseError("no_audio_enclosures", "No playable audio"), ConfigEntryError),
    ],
)
async def test_setup_entry_initial_feed_error_classes(
    hass,
    enable_custom_integrations,
    parse_error: PodcastParseError,
    expected_exception: type[Exception],
) -> None:
    """Initial-feed parser errors are classified as retryable or permanent."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INITIAL_RSS_URL: "https://example.test/feed.xml"})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.podcast_player.PodcastStorage.async_load", AsyncMock()),
        patch("custom_components.podcast_player.PodcastUpdateCoordinator.async_add_feed", AsyncMock(side_effect=parse_error)),
        pytest.raises(expected_exception),
    ):
        await async_setup_entry(hass, entry)


async def test_update_options_and_unload_entry(hass) -> None:
    """Options updates reload the entry and unload shuts down runtime/platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = _install_runtime(hass)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_entry:
        await async_update_options(hass, entry)
    reload_entry.assert_awaited_once_with(entry.entry_id)

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)) as unload_platforms:
        assert await async_unload_entry(hass, SimpleNamespace(entry_id="entry")) is True

    runtime.coordinator.async_shutdown.assert_awaited_once()
    unload_platforms.assert_awaited_once()
    assert "entry" not in hass.data.get(DOMAIN, {})


async def test_unload_entry_keeps_runtime_when_platform_unload_fails(hass) -> None:
    """Failed platform unload leaves runtime data in place."""
    _install_runtime(hass)

    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert await async_unload_entry(hass, SimpleNamespace(entry_id="entry")) is False

    assert "entry" in hass.data[DOMAIN]
