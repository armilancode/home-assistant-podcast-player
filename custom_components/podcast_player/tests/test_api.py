"""Tests for Podcast Player websocket/API payload helpers."""

import time
from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.podcast_player.api import (
    PodcastProxyView,
    PodcastSpeakerArtworkProxyView,
    PodcastSpeakerProxyView,
    _audio_proxy_response_headers,
    _episode_artwork_url,
    _generic_audio_probe_response,
    _proxy_episode_artwork,
    _proxy_episode_audio,
    _public_episode,
    _public_feed,
    _public_output_targets,
    _public_settings,
    async_register_api,
    websocket_get_episode,
    websocket_get_library,
)
from custom_components.podcast_player.const import DOMAIN, PLAYER_ENTITY_ID
from custom_components.podcast_player.speaker_proxy import sign_proxy_token
from custom_components.podcast_player.storage import PodcastStorage, default_data


def test_public_episode_includes_media_source_id() -> None:
    """Public episode payloads expose the native HA media-source URI."""
    payload = _public_episode(
        {
            "episode_id": "ep_123",
            "feed_id": "feed_1",
            "title": "Episode",
            "audio_url": "https://example.test/episode.mp3",
        },
        progress={"position": 12, "played": False},
        feed={"title": "Feed One"},
    )

    assert payload["media_source_id"] == f"media-source://{DOMAIN}/episode/ep_123"
    assert payload["audio_url"] == "https://example.test/episode.mp3"
    assert payload["proxy_url"] == "/api/podcast_player/proxy/ep_123"


def test_public_episode_uses_safe_defaults() -> None:
    """Public episode payloads tolerate partial storage records."""
    payload = _public_episode(
        {
            "title": "Episode",
            "duration_seconds": 88,
        },
        progress={"position": "12"},
    )

    assert payload["episode_id"] is None
    assert payload["media_source_id"] is None
    assert payload["duration_seconds"] == 88
    assert payload["played"] is False
    assert payload["position"] == 12
    assert payload["feed_title"] is None


def test_public_feed_contains_frontend_safe_fields() -> None:
    """Public feed payloads expose only frontend-relevant feed fields."""
    payload = _public_feed(
        {
            "feed_id": "feed_1",
            "title": "Feed",
            "description": "Description",
            "author": "Host",
            "website": "https://example.test",
            "artwork_url": "https://example.test/feed.jpg",
            "status": "failed",
            "last_refresh": "2026-01-02T00:00:00+00:00",
            "last_success": "2026-01-01T00:00:00+00:00",
            "last_error": {"code": "timeout"},
            "episode_count": 12,
        }
    )

    assert payload == {
        "feed_id": "feed_1",
        "title": "Feed",
        "description": "Description",
        "author": "Host",
        "website": "https://example.test",
        "artwork_url": "https://example.test/feed.jpg",
        "status": "failed",
        "last_refresh": "2026-01-02T00:00:00+00:00",
        "last_success": "2026-01-01T00:00:00+00:00",
        "last_error": {"code": "timeout"},
        "enabled": True,
        "episode_count": 12,
    }


def test_public_settings_hide_proxy_secret() -> None:
    """Frontend settings must not expose internal proxy secrets."""
    payload = _public_settings(
        {
            "default_playback_speed": 1.0,
            "enhanced_dlna_controls": True,
            "speaker_proxy_secret": "secret-value",
        }
    )

    assert payload == {
        "default_playback_speed": 1.0,
        "enhanced_dlna_controls": True,
    }


class FakeStates:
    """Minimal hass.states replacement for output target payload tests."""

    def __init__(self, states: list[SimpleNamespace]) -> None:
        self._states = states

    def async_all(self, domain: str) -> list[SimpleNamespace]:
        """Return configured fake states for one domain."""
        return self._states if domain == "media_player" else []


class FakeHttp:
    """Minimal hass.http replacement for API registration tests."""

    def __init__(self) -> None:
        self.views: list[object] = []

    def register_view(self, view: object) -> None:
        """Record registered HTTP views."""
        self.views.append(view)


class FakeConnection:
    """Minimal websocket connection replacement."""

    def __init__(self) -> None:
        self.results: list[tuple[int, object]] = []
        self.errors: list[tuple[int, str, str]] = []

    def send_result(self, msg_id: int, result: object) -> None:
        """Record websocket results."""
        self.results.append((msg_id, result))

    def send_error(self, msg_id: int, code: str, message: str) -> None:
        """Record websocket errors."""
        self.errors.append((msg_id, code, message))


class FakeContent:
    """Minimal aiohttp stream content replacement."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        """Yield configured chunks."""
        for chunk in self._chunks:
            yield chunk


class FakeUpstream:
    """Minimal aiohttp upstream response replacement."""

    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks or [])

    async def __aenter__(self) -> "FakeUpstream":
        """Enter response context."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Exit response context."""


class FakeSession:
    """Minimal aiohttp client session replacement."""

    def __init__(
        self,
        *,
        head_response: FakeUpstream | Exception | None = None,
        get_response: FakeUpstream | Exception | None = None,
    ) -> None:
        self.head_response = head_response
        self.get_response = get_response
        self.head_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    async def head(self, url: str, **kwargs):
        """Return configured HEAD response."""
        self.head_calls.append((url, kwargs))
        if isinstance(self.head_response, Exception):
            raise self.head_response
        return self.head_response

    async def get(self, url: str, **kwargs):
        """Return configured GET response."""
        self.get_calls.append((url, kwargs))
        if isinstance(self.get_response, Exception):
            raise self.get_response
        return self.get_response


class FakeStreamResponse:
    """Minimal aiohttp StreamResponse replacement."""

    def __init__(self, *, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.prepared_request = None
        self.chunks: list[bytes] = []
        self.eof = False

    async def prepare(self, request) -> None:
        """Record prepared request."""
        self.prepared_request = request

    async def write(self, chunk: bytes) -> None:
        """Record written chunks."""
        self.chunks.append(chunk)

    async def write_eof(self) -> None:
        """Record EOF."""
        self.eof = True


def _media_state(entity_id: str, state: str, name: str, features: int = 0) -> SimpleNamespace:
    """Build a minimal HA state object."""
    return SimpleNamespace(
        entity_id=entity_id,
        state=state,
        name=name,
        attributes={"friendly_name": name, "supported_features": features},
    )


def _storage() -> PodcastStorage:
    """Return an in-memory storage object."""
    storage = PodcastStorage.__new__(PodcastStorage)
    storage.data = default_data()
    return storage


def _runtime(storage: PodcastStorage) -> SimpleNamespace:
    """Return a minimal runtime object."""
    return SimpleNamespace(storage=storage)


def _hass(runtime: SimpleNamespace | None = None, states: FakeStates | None = None) -> SimpleNamespace:
    """Return a minimal hass object."""
    data = {DOMAIN: {"entry": runtime}} if runtime is not None else {}
    return SimpleNamespace(data=data, states=states or FakeStates([]), http=FakeHttp())


def _request(
    hass: SimpleNamespace,
    *,
    method: str = "GET",
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Return a minimal aiohttp request object for early proxy paths."""
    return SimpleNamespace(app={"hass": hass}, method=method, query=query or {}, headers=headers or {})


def _seed_library(storage: PodcastStorage) -> None:
    """Seed storage with enabled, disabled, and orphaned library content."""
    storage.data["settings"]["speaker_proxy_secret"] = "secret-value"
    storage.data["feeds"] = {
        "feed_1": {
            "feed_id": "feed_1",
            "title": "Feed One",
            "author": "Host One",
            "enabled": True,
            "artwork_url": "https://example.test/feed-one.jpg",
        },
        "feed_2": {
            "feed_id": "feed_2",
            "title": "Feed Two",
            "enabled": False,
        },
        "feed_3": {
            "feed_id": "feed_3",
            "title": "Feed Three",
            "enabled": True,
        },
    }
    storage.data["episodes"] = {
        "ep_new": {
            "episode_id": "ep_new",
            "feed_id": "feed_1",
            "title": "Newest",
            "published": "2026-01-03T00:00:00+00:00",
            "discovered_at": "2026-01-03T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/new.mp3",
            "audio_type": "audio/mpeg",
        },
        "ep_progress": {
            "episode_id": "ep_progress",
            "feed_id": "feed_1",
            "title": "In Progress",
            "published": "2026-01-02T00:00:00+00:00",
            "discovered_at": "2026-01-02T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/progress.mp3",
        },
        "ep_played": {
            "episode_id": "ep_played",
            "feed_id": "feed_1",
            "title": "Played",
            "published": "2026-01-01T00:00:00+00:00",
            "discovered_at": "2026-01-01T01:00:00+00:00",
            "audio_url": "https://cdn.example.test/played.mp3",
        },
        "ep_disabled": {
            "episode_id": "ep_disabled",
            "feed_id": "feed_2",
            "title": "Disabled Feed Episode",
            "published": "2026-01-04T00:00:00+00:00",
        },
        "ep_orphan": {
            "episode_id": "ep_orphan",
            "feed_id": "removed_feed",
            "title": "Orphan Episode",
            "published": "2026-01-05T00:00:00+00:00",
        },
    }
    storage.data["progress"] = {
        "ep_new": {"episode_id": "ep_new", "played": False, "position": 0, "duration": None},
        "ep_progress": {"episode_id": "ep_progress", "played": False, "position": 12, "duration": 120},
        "ep_played": {"episode_id": "ep_played", "played": True, "position": 120, "duration": 120},
        "ep_disabled": {"episode_id": "ep_disabled", "played": False, "position": 0, "duration": None},
        "ep_orphan": {"episode_id": "ep_orphan", "played": False, "position": 0, "duration": None},
    }


def _signed_query(secret: str, episode_id: str) -> dict[str, str]:
    """Return a valid signed proxy query."""
    expires = str(int(time.time()) + 60)
    return {"expires": expires, "token": sign_proxy_token(secret, episode_id, int(expires))}


def test_public_output_targets_exposes_target_statuses() -> None:
    """Output target payloads include frontend-ready target status information."""
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    play_media = int(MediaPlayerEntityFeature.PLAY_MEDIA)
    unsupported_features = 1
    while unsupported_features & play_media:
        unsupported_features <<= 1

    hass = SimpleNamespace(
        states=FakeStates(
            [
                _media_state(PLAYER_ENTITY_ID, "idle", "Podcast Player"),
                _media_state("media_player.ready_speaker", "idle", "Ready Speaker", play_media),
                _media_state("media_player.off_speaker", "off", "Off Speaker", play_media),
                _media_state("media_player.missing_speaker", "unavailable", "Missing Speaker", play_media),
                _media_state("media_player.limited_speaker", "idle", "Limited Speaker", unsupported_features),
                _media_state("media_player.spotify", "playing", "Spotify", play_media),
            ]
        )
    )

    targets = {target["entity_id"]: target for target in _public_output_targets(hass)}

    assert set(targets) == {
        "media_player.ready_speaker",
        "media_player.off_speaker",
        "media_player.missing_speaker",
        "media_player.limited_speaker",
    }
    assert targets["media_player.ready_speaker"]["status"] == "ready"
    assert targets["media_player.ready_speaker"]["playable"] is True
    assert targets["media_player.off_speaker"]["status"] == "off"
    assert targets["media_player.off_speaker"]["playable"] is False
    assert targets["media_player.missing_speaker"]["status"] == "unavailable"
    assert targets["media_player.missing_speaker"]["playable"] is False
    assert targets["media_player.limited_speaker"]["status"] == "unsupported"
    assert targets["media_player.limited_speaker"]["playable"] is False


def test_async_register_api_registers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """API registration is idempotent for websocket commands and HTTP views."""
    commands: list[object] = []
    monkeypatch.setattr("custom_components.podcast_player.api.websocket_api.async_register_command", lambda hass, command: commands.append(command))
    hass = _hass()

    async_register_api(hass)
    async_register_api(hass)

    assert commands == [websocket_get_library, websocket_get_episode]
    assert [type(view) for view in hass.http.views] == [
        PodcastProxyView,
        PodcastSpeakerProxyView,
        PodcastSpeakerArtworkProxyView,
    ]


@pytest.mark.asyncio
async def test_websocket_get_library_filters_active_library() -> None:
    """Library websocket payloads exclude disabled/orphaned content and apply filters."""
    storage = _storage()
    _seed_library(storage)
    connection = FakeConnection()

    await websocket_get_library.__wrapped__(
        _hass(_runtime(storage)),
        connection,
        {"id": 1, "feed_id": "all", "filter": "in_progress", "limit": 10},
    )

    assert connection.errors == []
    msg_id, result = connection.results[0]
    assert msg_id == 1
    assert [episode["episode_id"] for episode in result["episodes"]] == ["ep_progress"]
    assert {feed["feed_id"] for feed in result["feeds"]} == {"feed_1", "feed_3"}
    assert result["feeds"][0]["counts"] == {"episodes": 3, "unplayed": 2, "in_progress": 1}
    assert "speaker_proxy_secret" not in result["settings"]
    assert result["counts"]["total_episodes"] == 3
    assert result["counts"]["unplayed"] == 2
    assert result["output_targets"] == []


@pytest.mark.asyncio
async def test_websocket_get_library_handles_missing_and_disabled_feed() -> None:
    """Library websocket payloads handle missing runtime and disabled feed filters."""
    missing_connection = FakeConnection()
    await websocket_get_library.__wrapped__(_hass(), missing_connection, {"id": 1})

    assert missing_connection.errors == [(1, "not_configured", "Podcast Player is not configured")]

    storage = _storage()
    _seed_library(storage)
    connection = FakeConnection()
    await websocket_get_library.__wrapped__(
        _hass(_runtime(storage)),
        connection,
        {"id": 2, "feed_id": "feed_2", "filter": "all", "limit": 10},
    )

    assert connection.results[0][1]["episodes"] == []


@pytest.mark.asyncio
async def test_websocket_get_episode_handles_success_and_errors() -> None:
    """Episode websocket payloads report success, missing runtime, and unknown episodes."""
    storage = _storage()
    _seed_library(storage)

    connection = FakeConnection()
    await websocket_get_episode.__wrapped__(_hass(_runtime(storage)), connection, {"id": 1, "episode_id": "ep_new"})

    assert connection.errors == []
    assert connection.results[0][1]["episode_id"] == "ep_new"
    assert connection.results[0][1]["feed_title"] == "Feed One"

    missing_runtime = FakeConnection()
    await websocket_get_episode.__wrapped__(_hass(), missing_runtime, {"id": 2, "episode_id": "ep_new"})
    assert missing_runtime.errors == [(2, "not_configured", "Podcast Player is not configured")]

    unknown = FakeConnection()
    await websocket_get_episode.__wrapped__(_hass(_runtime(storage)), unknown, {"id": 3, "episode_id": "missing"})
    assert unknown.errors == [(3, "not_found", "Unknown episode_id")]


def test_audio_proxy_response_headers_defaults() -> None:
    """Audio proxy headers copy safe upstream headers and fill playback defaults."""
    headers = _audio_proxy_response_headers(
        {
            "Content-Length": "123",
            "Content-Range": "bytes 0-122/123",
            "X-Internal": "ignored",
        },
        "audio/aac",
    )

    assert headers == {
        "Content-Length": "123",
        "Content-Range": "bytes 0-122/123",
        "Content-Type": "audio/aac",
        "Accept-Ranges": "bytes",
    }
    assert _generic_audio_probe_response("audio/mpeg").status == 200


@pytest.mark.asyncio
async def test_audio_proxy_early_error_paths() -> None:
    """Audio proxy rejects missing runtime, bad tokens, unknown episodes, and empty audio URLs."""
    assert (await _proxy_episode_audio(_request(_hass()), "ep_1", require_signed_token=False)).status == 404

    storage = _storage()
    storage.data["settings"]["speaker_proxy_secret"] = "secret-value"
    storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "feed_id": "feed_1"}
    hass = _hass(_runtime(storage))

    assert (await _proxy_episode_audio(_request(hass), "ep_1", require_signed_token=True)).status == 403
    assert (await _proxy_episode_audio(_request(hass), "missing", require_signed_token=False)).status == 404
    assert (await _proxy_episode_audio(_request(hass), "ep_1", require_signed_token=False)).status == 404


@pytest.mark.asyncio
async def test_audio_proxy_head_uses_upstream_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio HEAD proxy returns upstream media probe headers when available."""
    storage = _storage()
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "audio_type": "audio/aac",
    }
    hass = _hass(_runtime(storage))
    session = FakeSession(
        head_response=FakeUpstream(
            status=206,
            headers={
                "Content-Type": "audio/aac",
                "Content-Length": "123",
                "Content-Range": "bytes 0-122/123",
                "ETag": "abc",
                "X-Ignored": "ignored",
            },
        )
    )
    monkeypatch.setattr("custom_components.podcast_player.api.async_get_clientsession", lambda hass: session)

    response = await _proxy_episode_audio(_request(hass, method="HEAD"), "ep_1", require_signed_token=False)

    assert response.status == 206
    assert response.headers["Content-Type"] == "audio/aac"
    assert response.headers["Content-Length"] == "123"
    assert response.headers["Content-Range"] == "bytes 0-122/123"
    assert response.headers["ETag"] == "abc"
    assert "X-Ignored" not in response.headers
    assert session.head_calls[0][0] == "https://cdn.example.test/ep_1.mp3"


@pytest.mark.asyncio
async def test_audio_proxy_head_falls_back_for_failed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio HEAD proxy returns conservative success for failed upstream probes."""
    storage = _storage()
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "audio_type": "audio/aac",
    }
    hass = _hass(_runtime(storage))
    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(head_response=aiohttp.ClientError("offline")),
    )

    response = await _proxy_episode_audio(_request(hass, method="HEAD"), "ep_1", require_signed_token=False)

    assert response.status == 200
    assert response.headers["Content-Type"] == "audio/aac"
    assert response.headers["Accept-Ranges"] == "bytes"

    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(head_response=FakeUpstream(status=404)),
    )
    response = await _proxy_episode_audio(_request(hass, method="HEAD"), "ep_1", require_signed_token=False)

    assert response.status == 200


@pytest.mark.asyncio
async def test_audio_proxy_get_streams_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio GET proxy streams upstream chunks and forwards range requests."""
    storage = _storage()
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
        "audio_type": "audio/mpeg",
    }
    hass = _hass(_runtime(storage))
    session = FakeSession(
        get_response=FakeUpstream(
            status=206,
            headers={
                "Content-Type": "audio/mpeg",
                "Accept-Ranges": "bytes",
                "Content-Length": "6",
            },
            chunks=[b"abc", b"def"],
        )
    )
    monkeypatch.setattr("custom_components.podcast_player.api.async_get_clientsession", lambda hass: session)
    monkeypatch.setattr("custom_components.podcast_player.api.web.StreamResponse", FakeStreamResponse)

    response = await _proxy_episode_audio(
        _request(hass, headers={"Range": "bytes=0-5"}),
        "ep_1",
        require_signed_token=False,
    )

    assert response.status == 206
    assert response.headers["Content-Length"] == "6"
    assert response.chunks == [b"abc", b"def"]
    assert response.eof is True
    assert session.get_calls[0][1]["headers"]["Range"] == "bytes=0-5"


@pytest.mark.asyncio
async def test_audio_proxy_get_reports_upstream_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio GET proxy reports connection and upstream HTTP failures."""
    storage = _storage()
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "audio_url": "https://cdn.example.test/ep_1.mp3",
    }
    hass = _hass(_runtime(storage))
    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(get_response=aiohttp.ClientError("offline")),
    )

    assert (await _proxy_episode_audio(_request(hass), "ep_1", require_signed_token=False)).status == 502

    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(get_response=FakeUpstream(status=503)),
    )

    response = await _proxy_episode_audio(_request(hass), "ep_1", require_signed_token=False)

    assert response.status == 503


@pytest.mark.asyncio
async def test_audio_proxy_views_delegate_to_helper() -> None:
    """HTTP audio proxy views delegate GET and HEAD requests to the helper."""
    request = _request(_hass())

    assert (await PodcastProxyView().get(request, "ep_1")).status == 404
    assert (await PodcastProxyView().head(request, "ep_1")).status == 404
    assert (await PodcastSpeakerProxyView().get(request, "ep_1")).status == 404
    assert (await PodcastSpeakerProxyView().head(request, "ep_1")).status == 404


@pytest.mark.asyncio
async def test_artwork_proxy_early_paths_and_head_success() -> None:
    """Artwork proxy validates signed tokens and returns safe probe headers."""
    assert (await _proxy_episode_artwork(_request(_hass()), "ep_1")).status == 404

    storage = _storage()
    storage.data["settings"]["speaker_proxy_secret"] = "secret-value"
    storage.data["feeds"]["feed_1"] = {
        "feed_id": "feed_1",
        "artwork_url": "https://cdn.example.test/feed.jpg",
    }
    storage.data["episodes"]["ep_1"] = {"episode_id": "ep_1", "feed_id": "feed_1"}
    hass = _hass(_runtime(storage))

    assert (await _proxy_episode_artwork(_request(hass), "ep_1")).status == 403

    query = _signed_query("secret-value", "ep_1")
    assert (await _proxy_episode_artwork(_request(hass, query=query), "missing")).status == 403

    missing_query = _signed_query("secret-value", "missing")
    assert (await _proxy_episode_artwork(_request(hass, query=missing_query), "missing")).status == 404

    storage.data["episodes"]["ep_2"] = {"episode_id": "ep_2", "feed_id": "feed_2"}
    no_artwork_query = _signed_query("secret-value", "ep_2")
    assert (await _proxy_episode_artwork(_request(hass, query=no_artwork_query), "ep_2")).status == 404

    response = await _proxy_episode_artwork(_request(hass, method="HEAD", query=query), "ep_1")
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_artwork_proxy_view_delegates_to_helper() -> None:
    """HTTP artwork proxy view delegates GET and HEAD requests to the helper."""
    request = _request(_hass())

    assert (await PodcastSpeakerArtworkProxyView().get(request, "ep_1")).status == 404
    assert (await PodcastSpeakerArtworkProxyView().head(request, "ep_1")).status == 404


@pytest.mark.asyncio
async def test_artwork_proxy_get_streams_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Artwork GET proxy streams image chunks and copies safe headers."""
    storage = _storage()
    storage.data["settings"]["speaker_proxy_secret"] = "secret-value"
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "artwork_url": "https://cdn.example.test/ep_1.jpg",
    }
    hass = _hass(_runtime(storage))
    session = FakeSession(
        get_response=FakeUpstream(
            headers={
                "Content-Type": "image/png",
                "Content-Length": "6",
                "ETag": "abc",
            },
            chunks=[b"123", b"456"],
        )
    )
    monkeypatch.setattr("custom_components.podcast_player.api.async_get_clientsession", lambda hass: session)
    monkeypatch.setattr("custom_components.podcast_player.api.web.StreamResponse", FakeStreamResponse)

    response = await _proxy_episode_artwork(
        _request(hass, query=_signed_query("secret-value", "ep_1")),
        "ep_1",
    )

    assert response.status == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["Content-Length"] == "6"
    assert response.headers["ETag"] == "abc"
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    assert response.chunks == [b"123", b"456"]
    assert response.eof is True
    assert session.get_calls[0][0] == "https://cdn.example.test/ep_1.jpg"
    assert session.get_calls[0][1]["headers"]["Accept"].startswith("image/")


@pytest.mark.asyncio
async def test_artwork_proxy_get_reports_upstream_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Artwork GET proxy reports connection and upstream HTTP failures."""
    storage = _storage()
    storage.data["settings"]["speaker_proxy_secret"] = "secret-value"
    storage.data["episodes"]["ep_1"] = {
        "episode_id": "ep_1",
        "feed_id": "feed_1",
        "artwork_url": "https://cdn.example.test/ep_1.jpg",
    }
    hass = _hass(_runtime(storage))
    query = _signed_query("secret-value", "ep_1")
    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(get_response=aiohttp.ClientError("offline")),
    )

    assert (await _proxy_episode_artwork(_request(hass, query=query), "ep_1")).status == 502

    monkeypatch.setattr(
        "custom_components.podcast_player.api.async_get_clientsession",
        lambda hass: FakeSession(get_response=FakeUpstream(status=404)),
    )

    response = await _proxy_episode_artwork(_request(hass, query=query), "ep_1")

    assert response.status == 404


def test_episode_artwork_url_prefers_episode_then_feed() -> None:
    """Artwork helper prefers episode artwork and falls back to feed artwork."""
    storage = _storage()
    storage.data["feeds"]["feed_1"] = {"feed_id": "feed_1", "artwork_url": "https://cdn.example.test/feed.jpg"}
    runtime = _runtime(storage)

    assert _episode_artwork_url(runtime, {"artwork_url": "https://cdn.example.test/episode.jpg"}) == "https://cdn.example.test/episode.jpg"
    assert _episode_artwork_url(runtime, {"feed_id": "feed_1"}) == "https://cdn.example.test/feed.jpg"
    assert _episode_artwork_url(runtime, {"feed_id": "missing"}) is None
