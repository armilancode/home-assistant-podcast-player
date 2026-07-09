"""Tests for Podcast Player signed proxy helpers."""

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from custom_components.podcast_player.speaker_proxy import (
    ensure_proxy_secret,
    local_base_url,
    make_signed_speaker_artwork_proxy_url,
    make_signed_speaker_proxy_path,
    make_signed_speaker_proxy_url,
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
    assert not verify_proxy_token(secret, episode_id, None, token)
    assert not verify_proxy_token(secret, episode_id, "not-an-int", token)
    assert not verify_proxy_token(secret, episode_id, str(expires), None)


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


def test_proxy_secret_is_reused() -> None:
    """Proxy signing secrets are created once and reused."""
    settings = {}
    secret = ensure_proxy_secret(settings)

    assert secret
    assert ensure_proxy_secret(settings) == secret


def test_signed_proxy_urls_use_home_assistant_base_url() -> None:
    """Absolute proxy URLs use the configured HA base URL."""
    hass = SimpleNamespace(config=SimpleNamespace(internal_url="http://ha.example.test:8123", external_url=None))
    settings = {"speaker_proxy_secret": "secret"}

    audio = make_signed_speaker_proxy_url(hass, settings, "ep_123")
    artwork = make_signed_speaker_artwork_proxy_url(hass, settings, "ep_123")

    assert audio.startswith("http://ha.example.test:8123/api/podcast_player/speaker_proxy/ep_123?")
    assert artwork.startswith("http://ha.example.test:8123/api/podcast_player/speaker_artwork/ep_123?")


def test_local_base_url_falls_back_to_external_url(monkeypatch) -> None:
    """The helper falls back to configured external URL when internal URL is empty."""
    from homeassistant.helpers import network

    monkeypatch.setattr(network, "get_url", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no helper url")))
    hass = SimpleNamespace(config=SimpleNamespace(internal_url=None, external_url="https://ha.example.test/"))

    assert local_base_url(hass) == "https://ha.example.test"


def test_local_base_url_retries_helper_signature(monkeypatch) -> None:
    """The helper tolerates Home Assistant get_url signature differences."""
    from homeassistant.helpers import network

    calls = []

    def fake_get_url(hass, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TypeError("old signature")
        return "http://helper.example.test:8123/"

    monkeypatch.setattr(network, "get_url", fake_get_url)
    hass = SimpleNamespace(config=SimpleNamespace(internal_url="http://fallback.example.test", external_url=None))

    assert local_base_url(hass) == "http://helper.example.test:8123"
    assert len(calls) == 2


def test_local_base_url_falls_back_after_empty_helper_urls(monkeypatch) -> None:
    """The helper uses configured URLs when Home Assistant returns no helper URL."""
    from homeassistant.helpers import network

    calls = []

    def fake_get_url(hass, **kwargs):
        calls.append(kwargs)
        return ""

    monkeypatch.setattr(network, "get_url", fake_get_url)
    hass = SimpleNamespace(config=SimpleNamespace(internal_url="http://internal.example.test/", external_url=None))

    assert local_base_url(hass) == "http://internal.example.test"
    assert len(calls) == 4


def test_signed_proxy_url_returns_none_without_base_url() -> None:
    """Absolute proxy URL builders return None when HA has no usable base URL."""
    hass = SimpleNamespace(config=SimpleNamespace(internal_url=None, external_url=None))

    assert make_signed_speaker_proxy_url(hass, {}, "ep_123") is None
    assert make_signed_speaker_artwork_proxy_url(hass, {}, "ep_123") is None
