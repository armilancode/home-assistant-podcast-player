"""Signed speaker proxy helpers for HA Podcast Player."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant

from .const import HTTP_SPEAKER_ARTWORK_PROXY_URL, HTTP_SPEAKER_PROXY_URL, SPEAKER_PROXY_TOKEN_TTL_SECONDS


def ensure_proxy_secret(settings: dict) -> str:
    """Ensure a persistent proxy signing secret exists."""
    secret = settings.get("speaker_proxy_secret")
    if not secret:
        secret = secrets.token_urlsafe(32)
        settings["speaker_proxy_secret"] = secret
    return secret


def sign_proxy_token(secret: str, episode_id: str, expires: int) -> str:
    """Return HMAC token for an episode/expires pair."""
    payload = f"{episode_id}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_proxy_token(secret: str, episode_id: str, expires_raw: str | None, token: str | None) -> bool:
    """Verify signed proxy token."""
    if not expires_raw or not token:
        return False
    try:
        expires = int(expires_raw)
    except (TypeError, ValueError):
        return False
    if expires < int(time.time()):
        return False
    expected = sign_proxy_token(secret, episode_id, expires)
    return hmac.compare_digest(expected, token)


def local_base_url(hass: HomeAssistant) -> str | None:
    """Return best base URL for a LAN speaker to fetch from HA.

    Avoid depending on one exact Home Assistant helper signature because this is
    a custom integration that should survive across HA versions.
    """
    try:
        from homeassistant.helpers.network import get_url  # pylint: disable=import-outside-toplevel

        attempts = [
            {"allow_internal": True, "allow_external": True, "prefer_external": False, "allow_ip": True},
            {"allow_internal": True, "allow_external": True, "prefer_external": False},
            {"prefer_external": False},
            {},
        ]
        for kwargs in attempts:
            try:
                url = get_url(hass, **kwargs)
                if url:
                    return str(url).rstrip("/")
            except TypeError:
                continue
            except Exception:  # noqa: BLE001 - fall through to config URLs
                break
    except Exception:  # noqa: BLE001 - helper unavailable
        pass

    internal = getattr(hass.config, "internal_url", None)
    external = getattr(hass.config, "external_url", None)
    return (internal or external or "").rstrip("/") or None


def _signed_proxy_query(settings: dict, episode_id: str) -> str:
    """Return the signed proxy query string for a stored episode."""
    secret = ensure_proxy_secret(settings)
    expires = int(time.time()) + SPEAKER_PROXY_TOKEN_TTL_SECONDS
    token = sign_proxy_token(secret, episode_id, expires)
    return urlencode({"expires": expires, "token": token})


def make_signed_speaker_proxy_path(settings: dict, episode_id: str) -> str:
    """Build a relative signed proxy path for browser-based playback."""
    path = HTTP_SPEAKER_PROXY_URL.format(episode_id=episode_id)
    return f"{path}?{_signed_proxy_query(settings, episode_id)}"


def make_signed_speaker_proxy_url(hass: HomeAssistant, settings: dict, episode_id: str) -> str | None:
    """Build an absolute signed proxy URL for a stored episode."""
    base = local_base_url(hass)
    if not base:
        return None
    return f"{base}{make_signed_speaker_proxy_path(settings, episode_id)}"


def make_signed_speaker_artwork_proxy_url(hass: HomeAssistant, settings: dict, episode_id: str) -> str | None:
    """Build an absolute signed artwork proxy URL for a stored episode.

    DLNA renderers can often play the remote MP3/M4A URL but fail to fetch
    episode artwork from external HTTPS/CDN URLs. This gives them a simple
    Home Assistant LAN URL protected by the same short-lived signature scheme
    as the speaker audio proxy.
    """
    base = local_base_url(hass)
    if not base:
        return None
    secret = ensure_proxy_secret(settings)
    expires = int(time.time()) + SPEAKER_PROXY_TOKEN_TTL_SECONDS
    token = sign_proxy_token(secret, episode_id, expires)
    path = HTTP_SPEAKER_ARTWORK_PROXY_URL.format(episode_id=episode_id)
    return f"{base}{path}?{urlencode({'expires': expires, 'token': token})}"
