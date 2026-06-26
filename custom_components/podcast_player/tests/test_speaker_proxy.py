"""Tests for Podcast Player signed proxy helpers."""

import time

from custom_components.podcast_player.speaker_proxy import sign_proxy_token, verify_proxy_token


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
