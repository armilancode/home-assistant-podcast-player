"""Constants for HA Podcast Player."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "podcast_player"
NAME = "Podcast Player"
VERSION = "0.3.0-alpha.0"

PLATFORMS = ["media_player", "sensor", "binary_sensor", "button"]

DATA_RUNTIME = "runtime"

STORAGE_KEY = f"{DOMAIN}.library"
STORAGE_VERSION = 1

DEFAULT_REFRESH_INTERVAL = timedelta(hours=2)
DEFAULT_MAX_EPISODES_PER_FEED = 100
DEFAULT_PLAYED_THRESHOLD = 0.95
DEFAULT_PLAYBACK_SPEED = 1.0
DEFAULT_BACK_SECONDS = 15
DEFAULT_FORWARD_SECONDS = 30

PLAYER_ENTITY_ID = "media_player.podcast_player"

EVENT_FEED_ADDED = f"{DOMAIN}_feed_added"
EVENT_FEED_REMOVED = f"{DOMAIN}_feed_removed"
EVENT_NEW_EPISODE = f"{DOMAIN}_new_episode"
EVENT_PLAYBACK_STARTED = f"{DOMAIN}_playback_started"
EVENT_PLAYBACK_PAUSED = f"{DOMAIN}_playback_paused"
EVENT_EPISODE_COMPLETED = f"{DOMAIN}_episode_completed"
EVENT_FEED_REFRESH_FAILED = f"{DOMAIN}_feed_refresh_failed"

SERVICE_ADD_FEED = "add_feed"
SERVICE_REMOVE_FEED = "remove_feed"
SERVICE_REFRESH = "refresh"
SERVICE_REFRESH_FEEDS = "refresh_feeds"
SERVICE_PLAY_EPISODE = "play_episode"
SERVICE_PLAY_CURRENT = "play_current"
SERVICE_PAUSE = "pause"
SERVICE_STOP = "stop"

SERVICE_RESUME = "resume"
SERVICE_PLAY_LATEST = "play_latest"
SERVICE_PLAY_NEXT_UNPLAYED = "play_next_unplayed"
SERVICE_MARK_CURRENT_PLAYED = "mark_current_played"
SERVICE_MARK_FEED_PLAYED = "mark_feed_played"
SERVICE_PLAY_ON_MEDIA_PLAYER = "play_on_media_player"
SERVICE_STOP_MEDIA_PLAYER = "stop_media_player"
SERVICE_STOP_OUTPUT = "stop_output"
SERVICE_SEEK = "seek"
SERVICE_SAVE_PROGRESS = "save_progress"
SERVICE_MARK_PLAYED = "mark_played"
SERVICE_MARK_UNPLAYED = "mark_unplayed"
SERVICE_SET_SPEED = "set_speed"

ALLOWED_SPEEDS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]

CONF_REFRESH_INTERVAL = "refresh_interval"

HTTP_PROXY_URL = "/api/podcast_player/proxy/{episode_id}"
HTTP_SPEAKER_PROXY_URL = "/api/podcast_player/speaker_proxy/{episode_id}"
HTTP_SPEAKER_ARTWORK_PROXY_URL = "/api/podcast_player/speaker_artwork/{episode_id}"
SPEAKER_PROXY_TOKEN_TTL_SECONDS = 24 * 60 * 60

USER_AGENT = f"HA-Podcast-Player/{VERSION} (+Home Assistant)"
