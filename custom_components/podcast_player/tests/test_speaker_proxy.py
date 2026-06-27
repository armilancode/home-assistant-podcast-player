"""Tests for Podcast Player signed proxy helpers."""

import time
from urllib.parse import parse_qs, urlparse

from custom_components.podcast_player.speaker_proxy import (
    make_signed_speaker_proxy_path,
    sign_proxy_token,
    verify_proxy_token,
)


def test_proxy_token_verification() -> None:
    """Valid tokens verify and tampered tokens fail."""
    secret = "secret"
    episode_id = "ep_123"
    expires = int(time.time()) + 60
    token = sign_proxy_token(secret, episode_id, expires)

    assert verify_proxy_token(secret, episode_id, str(expires), token)
    assert not verify_proxy_token(secret, "other", str(expires), token)
    assert not verify_proxy_token(secret, episode_id, str(expires), "bad")
    assert not verify_proxy_token(secret, episode_id, str(int(time.time()) - 1), token)


def test_signed_proxy_path_contains_verifiable_token() -> None:
    """Relative signed proxy paths are usable by browser-based playback."""
    settings = {}
    path = make_signed_speaker_proxy_path(settings, "ep_123")

    parsed = urlparse(path)
    query = parse_qs(parsed.query)

    assert parsed.path == "/api/podcast_player/speaker_proxy/ep_123"
    assert verify_proxy_token(
        settings["speaker_proxy_secret"],
        "ep_123",
        query["expires"][0],
        query["token"][0],
    )
