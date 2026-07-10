"""Tests for Podcast Player entity state properties."""

from types import SimpleNamespace

from homeassistant.components.media_player import MediaPlayerState, MediaType

from custom_components.podcast_player import binary_sensor, button, media_player, sensor
from custom_components.podcast_player.binary_sensor import HasUnplayedBinarySensor, IsPlayingBinarySensor
from custom_components.podcast_player.button import (
    MarkCurrentPlayedButton,
    PlayLatestButton,
    PlayNextUnplayedButton,
    RefreshButton,
)
from custom_components.podcast_player.media_player import PodcastPlayerEntity
from custom_components.podcast_player.sensor import (
    CurrentDurationSensor,
    CurrentEpisodeSensor,
    CurrentFeedSensor,
    CurrentOutputSensor,
    CurrentPositionSensor,
    CurrentProgressSensor,
    FeedCountSensor,
    LatestByFeedSensor,
    LatestEpisodeSensor,
    PlaybackSpeedSensor,
    PodcastFeedSensor,
    UnplayedCountSensor,
)
from custom_components.podcast_player.storage import default_data


class FakeStates:
    """Minimal HA states helper."""

    def __init__(self, state: object | None) -> None:
        self._state = state

    def get(self, entity_id: str) -> object | None:
        """Return a configured fake state."""
        return self._state


class FakeStorage:
    """Minimal storage implementation for entity tests."""

    def __init__(self) -> None:
        self.data = default_data()
        self.data["feeds"]["feed_1"] = {
            "feed_id": "feed_1",
            "rss_url": "https://example.test/feed.xml",
            "title": "Example Podcast",
            "author": "Example Host",
            "website": "https://example.test",
            "artwork_url": "https://example.test/feed.jpg",
            "status": "ok",
            "last_refresh": "2026-01-02T00:00:00+00:00",
            "last_success": "2026-01-02T00:00:00+00:00",
            "enabled": True,
        }
        self.data["episodes"]["episode_1"] = {
            "episode_id": "episode_1",
            "feed_id": "feed_1",
            "title": "Episode One",
            "published": "2026-01-02T00:00:00+00:00",
            "duration_seconds": 100,
            "audio_url": "https://example.test/episode.mp3",
            "artwork_url": "https://example.test/episode.jpg",
        }
        self.data["progress"]["episode_1"] = {
            "episode_id": "episode_1",
            "played": False,
            "position": 25,
            "duration": 100,
            "playback_speed": 1.25,
            "last_played_at": "2026-01-02T00:01:00+00:00",
        }
        player = self.data["player"]
        player["state"] = "playing"
        player["current_episode_id"] = "episode_1"
        player["current_feed_id"] = "feed_1"
        player["position"] = 25
        player["duration"] = 100
        player["speed"] = 1.25
        player["output_mode"] = "speaker"
        player["target_media_player"] = "media_player.kitchen"
        player["target_media_player_name"] = "Kitchen"
        player["last_target_media_player"] = "media_player.kitchen"
        player["last_target_media_player_name"] = "Kitchen"
        player["speaker_url_mode"] = "signed_proxy"
        player["speaker_media_content_type"] = "music"
        player["external_session"].update(
            {
                "active": True,
                "target_media_player": "media_player.kitchen",
                "target_media_player_name": "Kitchen",
                "transport_state": "playing",
                "progress_source": "ha",
                "control_source": "ha",
            }
        )

    def get_feed(self, feed_id: str | None) -> dict | None:
        """Return a feed."""
        return self.data["feeds"].get(feed_id)

    def get_episode(self, episode_id: str | None) -> dict | None:
        """Return an episode."""
        return self.data["episodes"].get(episode_id)

    def episodes_for_feed(self, feed_id: str) -> list[dict]:
        """Return feed episodes."""
        return [episode for episode in self.data["episodes"].values() if episode.get("feed_id") == feed_id]

    def enabled_feeds(self) -> list[dict]:
        """Return enabled feeds."""
        return [feed for feed in self.data["feeds"].values() if feed.get("enabled", True)]

    def counts(self) -> dict:
        """Return summary counts."""
        return {
            "total_feeds": 1,
            "enabled_feeds": 1,
            "failed_feeds": 0,
            "total_episodes": 1,
            "unplayed": 1,
            "partially_played": 1,
        }


class FakeCoordinator:
    """Coordinator shell for entity tests."""

    def __init__(self) -> None:
        self.storage = FakeStorage()
        state = SimpleNamespace(
            state="playing",
            attributes={
                "media_position": 25,
                "media_duration": 100,
                "media_position_updated_at": "2026-01-02T00:01:00+00:00",
            },
        )
        self.hass = SimpleNamespace(states=FakeStates(state))
        self.last_update_success = True
        self.listeners = []
        self.refresh_count = 0
        self.play_latest_count = 0
        self.play_next_count = 0
        self.mark_current_count = 0

    def async_add_listener(self, listener):
        """Record a listener and return an unsubscribe callback."""
        self.listeners.append(listener)
        return lambda: None

    async def async_refresh_feeds(self) -> None:
        """Record refresh calls."""
        self.refresh_count += 1

    async def async_play_latest(self) -> None:
        """Record play-latest calls."""
        self.play_latest_count += 1

    async def async_play_next_unplayed(self) -> None:
        """Record play-next calls."""
        self.play_next_count += 1

    async def async_mark_current_played(self, played: bool = True) -> None:
        """Record mark-current calls."""
        self.mark_current_count += int(played)

    def latest_episode(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> dict | None:
        """Return the latest episode."""
        return self.storage.data["episodes"]["episode_1"]

    def active_episodes(self, feed_id: str | None = None, *, feed_ids: set[str] | None = None) -> list[dict]:
        """Return active episodes."""
        return list(self.storage.data["episodes"].values())


class FakeEntry:
    """Minimal config entry for platform setup tests."""

    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.runtime_data = SimpleNamespace(coordinator=coordinator)
        self.unloads = []

    def async_on_unload(self, unsubscribe) -> None:
        """Record unload callbacks."""
        self.unloads.append(unsubscribe)


def test_sensor_entities_report_library_and_playback_state() -> None:
    """Sensor entities expose expected values and attributes from storage."""
    coordinator = FakeCoordinator()

    feed_sensor = PodcastFeedSensor(coordinator, "feed_1")
    assert feed_sensor.translation_key == "feed"
    assert feed_sensor.translation_placeholders == {"feed_title": "Example Podcast"}
    assert feed_sensor.available is True
    assert feed_sensor.native_value == 1
    assert feed_sensor.entity_picture == "https://example.test/feed.jpg"
    assert feed_sensor.extra_state_attributes["latest_episode_id"] == "episode_1"

    assert FeedCountSensor(coordinator).native_value == 1
    assert FeedCountSensor(coordinator).extra_state_attributes["last_refresh"] == "2026-01-02T00:00:00+00:00"
    assert UnplayedCountSensor(coordinator).native_value == 1
    assert UnplayedCountSensor(coordinator).extra_state_attributes["partially_played"] == 1
    assert LatestEpisodeSensor(coordinator).native_value == "Episode One"
    assert LatestEpisodeSensor(coordinator).extra_state_attributes["feed_id"] == "feed_1"
    assert CurrentFeedSensor(coordinator).native_value == "Example Podcast"
    assert CurrentFeedSensor(coordinator).extra_state_attributes["author"] == "Example Host"
    assert CurrentEpisodeSensor(coordinator).native_value == "Episode One"
    assert CurrentEpisodeSensor(coordinator).extra_state_attributes["position"] == 25
    assert CurrentPositionSensor(coordinator).native_value == 25
    assert CurrentDurationSensor(coordinator).native_value == 100
    assert CurrentProgressSensor(coordinator).native_value == 25
    assert PlaybackSpeedSensor(coordinator).native_value == 1.25
    assert CurrentOutputSensor(coordinator).native_value == "speaker"
    assert CurrentOutputSensor(coordinator).extra_state_attributes["target_reports_progress"] is True
    assert LatestByFeedSensor(coordinator).native_value == 1
    assert LatestByFeedSensor(coordinator).extra_state_attributes["items"][0]["episode_id"] == "episode_1"


async def test_platform_setup_and_button_presses() -> None:
    """Platform setup creates entities and buttons call coordinator actions."""
    coordinator = FakeCoordinator()
    entry = FakeEntry(coordinator)
    coordinator.storage.data["feeds"]["missing_id"] = {"title": "Missing ID", "enabled": True}

    added = []
    await sensor.async_setup_entry(None, entry, added.extend)
    assert any(isinstance(entity, PodcastFeedSensor) for entity in added)
    assert entry.unloads

    coordinator.storage.data["feeds"]["feed_2"] = {
        "feed_id": "feed_2",
        "rss_url": "https://example.test/feed-2.xml",
        "title": "Second Feed",
        "enabled": True,
    }
    feed_entity_count = sum(isinstance(entity, PodcastFeedSensor) for entity in added)
    coordinator.listeners[0]()
    coordinator.listeners[0]()
    assert sum(isinstance(entity, PodcastFeedSensor) for entity in added) == feed_entity_count + 1

    binary_added = []
    await binary_sensor.async_setup_entry(None, entry, binary_added.extend)
    assert any(isinstance(entity, IsPlayingBinarySensor) for entity in binary_added)
    assert any(isinstance(entity, HasUnplayedBinarySensor) for entity in binary_added)

    button_added = []
    await button.async_setup_entry(None, entry, button_added.extend)
    assert {type(entity) for entity in button_added} == {
        RefreshButton,
        PlayLatestButton,
        PlayNextUnplayedButton,
        MarkCurrentPlayedButton,
    }
    for entity in button_added:
        await entity.async_press()

    assert coordinator.refresh_count == 1
    assert coordinator.play_latest_count == 1
    assert coordinator.play_next_count == 1
    assert coordinator.mark_current_count == 1


def test_binary_sensors_report_state() -> None:
    """Binary sensors expose playback and library state."""
    coordinator = FakeCoordinator()

    assert IsPlayingBinarySensor(coordinator).is_on is True
    assert HasUnplayedBinarySensor(coordinator).is_on is True


def test_sensor_edge_states_report_empty_values() -> None:
    """Sensors handle missing feeds, no current episode, and empty latest summaries."""
    coordinator = FakeCoordinator()
    coordinator.storage.data["player"]["current_episode_id"] = None
    coordinator.storage.data["player"]["current_feed_id"] = None
    coordinator.storage.data["player"]["position"] = 0
    coordinator.storage.data["player"]["duration"] = None
    coordinator.latest_episode = lambda *args, **kwargs: None

    missing_feed = PodcastFeedSensor(coordinator, "missing")
    assert missing_feed.available is False
    assert missing_feed.native_value is None

    assert LatestEpisodeSensor(coordinator).native_value is None
    assert LatestEpisodeSensor(coordinator).extra_state_attributes == {}
    assert CurrentFeedSensor(coordinator).native_value is None
    assert CurrentEpisodeSensor(coordinator).native_value is None
    assert CurrentEpisodeSensor(coordinator).extra_state_attributes["played"] is None
    assert CurrentDurationSensor(coordinator).native_value is None
    assert CurrentProgressSensor(coordinator).native_value == 0
    assert LatestByFeedSensor(coordinator).native_value == 0
    assert LatestByFeedSensor(coordinator).extra_state_attributes == {"items": []}


async def test_media_player_entity_reports_status_and_ignores_native_controls() -> None:
    """The media player entity is a status entity and ignores native controls."""
    coordinator = FakeCoordinator()
    entity = PodcastPlayerEntity(coordinator)

    assert entity.state is MediaPlayerState.IDLE
    assert entity.media_content_id == "episode_1"
    assert entity.media_content_type is MediaType.PODCAST
    assert entity.media_title == "Episode One"
    assert entity.media_album_name == "Example Podcast"
    assert entity.media_artist == "Example Host"
    assert entity.media_duration is None
    assert entity.media_position is None
    assert entity.media_position_updated_at is None
    assert entity.media_image_url == "https://example.test/episode.jpg"
    assert entity.extra_state_attributes["progress_percent"] == 25
    assert entity.extra_state_attributes["target_media_player"] == "media_player.kitchen"
    assert entity._adjacent_episode(1) is None

    assert await entity.async_media_play() is None
    assert await entity.async_media_pause() is None
    assert await entity.async_media_stop() is None
    assert await entity.async_media_seek(30) is None
    assert await entity.async_play_media("music", "episode_1") is None
    assert await entity.async_media_next_track() is None
    assert await entity.async_media_previous_track() is None


def test_media_player_entity_fallback_artwork_and_adjacent_episode() -> None:
    """Media player status helpers handle missing artwork and adjacent episode lookup."""
    coordinator = FakeCoordinator()
    entity = PodcastPlayerEntity(coordinator)

    coordinator.storage.data["episodes"]["episode_1"].pop("artwork_url")
    assert entity.media_image_url == "https://example.test/feed.jpg"

    coordinator.storage.data["episodes"]["episode_2"] = {
        "episode_id": "episode_2",
        "feed_id": "feed_1",
        "title": "Episode Two",
        "published": "2026-01-03T00:00:00+00:00",
        "duration_seconds": 0,
    }
    assert entity._adjacent_episode(1)["episode_id"] == "episode_2"

    coordinator.storage.data["player"]["current_episode_id"] = "missing"
    assert entity._adjacent_episode(1)["episode_id"] == "episode_1"
    assert entity._progress_percent({"position": 10, "duration": 0}, {}) == 0


async def test_media_player_platform_setup() -> None:
    """Media player platform setup creates the status entity."""
    coordinator = FakeCoordinator()
    entry = FakeEntry(coordinator)
    added = []

    await media_player.async_setup_entry(None, entry, added.extend)

    assert len(added) == 1
    assert isinstance(added[0], PodcastPlayerEntity)
