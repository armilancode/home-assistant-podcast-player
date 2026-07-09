"""Tests for Podcast Player RSS parsing."""

import pytest

from custom_components.podcast_player.feed_parser import (
    PodcastParseError,
    _as_text,
    _duration_to_seconds,
    _get_first,
    _image_from_obj,
    _is_audio_enclosure,
    _parse_datetime,
    _pick_audio,
    parse_podcast_feed,
)

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Example Podcast</title>
    <link>https://example.com/podcast</link>
    <itunes:author>Example Host</itunes:author>
    <itunes:image href="https://example.com/feed.jpg" />
    <item>
      <guid>episode-1</guid>
      <title>Episode One</title>
      <pubDate>Thu, 25 Jun 2026 10:00:00 +0000</pubDate>
      <itunes:duration>1:02:03</itunes:duration>
      <enclosure url="/audio/episode-1.mp3" type="audio/mpeg" length="12345" />
    </item>
  </channel>
</rss>
"""


def test_parse_podcast_feed_normalizes_feed_and_episode() -> None:
    """RSS feeds are normalized into stable feed and episode payloads."""
    parsed = parse_podcast_feed(SAMPLE_RSS, "https://example.com/feed.xml", "feed_abc")

    assert parsed["feed"]["title"] == "Example Podcast"
    assert parsed["feed"]["author"] == "Example Host"
    assert len(parsed["episodes"]) == 1

    episode = parsed["episodes"][0]
    assert episode["feed_id"] == "feed_abc"
    assert episode["guid"] == "episode-1"
    assert episode["title"] == "Episode One"
    assert episode["duration_seconds"] == 3723
    assert episode["audio_url"] == "https://example.com/audio/episode-1.mp3"
    assert episode["audio_type"] == "audio/mpeg"


def test_small_feed_parser_helpers() -> None:
    """Small parser helpers normalize empty values and common podcast formats."""
    assert _get_first({"a": "", "b": "value"}, "a", "b") == "value"
    assert _get_first({"a": None}, "a", default="fallback") == "fallback"
    assert _as_text(None) is None
    assert _as_text("  title  ") == "title"
    assert _as_text(123) == "123"

    assert _parse_datetime(None) is None
    assert _parse_datetime((2026, 1, 2, 3, 4, 5, 0, 0, 0)) == "2026-01-02T03:04:05+00:00"
    assert _parse_datetime((2026, 99, 2, 3, 4, 5, 0, 0, 0)) is None
    assert _parse_datetime("Thu, 25 Jun 2026 10:00:00 +0000") == "2026-06-25T10:00:00+00:00"
    assert _parse_datetime("Thu, 25 Jun 2026 10:00:00") == "2026-06-25T10:00:00+00:00"
    assert _parse_datetime("not-a-date") is None
    assert _parse_datetime(12345) is None

    assert _duration_to_seconds(None) is None
    assert _duration_to_seconds("") is None
    assert _duration_to_seconds("   ") is None
    assert _duration_to_seconds(12.9) == 12
    assert _duration_to_seconds("90") == 90
    assert _duration_to_seconds("01:02:03") == 3723
    assert _duration_to_seconds("02:03") == 123
    assert _duration_to_seconds("bad") is None
    assert _duration_to_seconds("1:2:3:4") is None


def test_image_helpers_read_common_feedparser_shapes() -> None:
    """Image helper accepts the common image structures emitted by feedparser."""
    assert _image_from_obj(None) is None
    assert _image_from_obj({"image": {"href": "https://example.test/image.jpg"}}) == "https://example.test/image.jpg"
    assert _image_from_obj({"image": {"url": "https://example.test/image.jpg"}}) == "https://example.test/image.jpg"
    assert _image_from_obj({"images": [{"url": "https://example.test/images.jpg"}]}) == "https://example.test/images.jpg"
    assert _image_from_obj({"media_thumbnail": [{"url": "https://example.test/thumb.jpg"}]}) == "https://example.test/thumb.jpg"
    assert _image_from_obj({"itunes_image": "https://example.test/itunes.jpg"}) == "https://example.test/itunes.jpg"
    assert _image_from_obj({"images": ["bad"], "media_thumbnail": ["bad"]}) is None
    assert _image_from_obj(
        {
            "image": {"href": ""},
            "images": [{"href": ""}, {"url": "https://example.test/second.jpg"}],
        }
    ) == "https://example.test/second.jpg"
    assert _image_from_obj(
        {
            "media_thumbnail": [{"url": ""}],
            "image_href": "https://example.test/fallback.jpg",
        }
    ) == "https://example.test/fallback.jpg"


def test_audio_pick_helpers_accept_mime_types_and_extensions() -> None:
    """Audio helpers accept podcast audio MIME types, audio prefixes, and known extensions."""
    assert _is_audio_enclosure({}) is False
    assert _is_audio_enclosure({"url": "https://example.test/file.mp3"}) is True
    assert _is_audio_enclosure({"href": "https://example.test/file.bin", "type": "audio/custom"}) is True
    assert _is_audio_enclosure({"href": "https://example.test/file.bin", "type": "application/octet-stream"}) is False

    assert _pick_audio(
        {
            "media_content": [
                {
                    "url": "/episode.m4a?download=1",
                    "type": "application/octet-stream",
                    "fileSize": "456",
                }
            ]
        },
        "https://example.test/feed.xml",
    ) == {
        "audio_url": "https://example.test/episode.m4a?download=1",
        "audio_type": "application/octet-stream",
        "audio_size": "456",
    }
    assert _pick_audio(
        {
            "links": [
                {"href": "https://example.test/page", "rel": "related"},
                {"href": "/audio.ogg", "rel": "alternate", "type": "audio/ogg", "length": "123"},
            ]
        },
        "https://example.test/feed.xml",
    )["audio_url"] == "https://example.test/audio.ogg"
    assert _pick_audio({"enclosures": [{"href": ""}], "links": "bad"}, "https://example.test/feed.xml") is None
    assert _pick_audio(
        {
            "enclosures": "bad",
            "media_content": "bad",
            "links": [None, {"href": "/clip.wav", "rel": "", "length": "321"}],
        },
        "https://example.test/feed.xml",
    )["audio_url"] == "https://example.test/clip.wav"


def test_parse_podcast_feed_uses_fallbacks_and_skips_bad_entries() -> None:
    """Parser handles fallback feed/episode fields and skips entries without audio."""
    raw = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <itunes:title>Fallback Podcast</itunes:title>
        <description>Feed description</description>
        <publisher>Publisher Name</publisher>
        <image><url>https://example.test/feed.jpg</url></image>
        <item>
          <title>No Audio</title>
        </item>
        <item>
          <title></title>
          <updated>Thu, 25 Jun 2026 10:00:00 +0000</updated>
          <description>Episode description</description>
          <itunes:season>2</itunes:season>
          <itunes:episode>7</itunes:episode>
          <itunes:explicit>no</itunes:explicit>
          <link>https://example.test/episode-page</link>
          <enclosure url="https://cdn.example.test/episode.aac" type="audio/aac" length="789" />
        </item>
      </channel>
    </rss>
    """

    parsed = parse_podcast_feed(raw, "https://example.test/feed.xml", "feed_1")

    assert parsed["feed"]["title"] == "Fallback Podcast"
    assert parsed["feed"]["description"] == "Feed description"
    assert parsed["feed"]["author"] == "Publisher Name"
    assert parsed["feed"]["episode_count"] == 1
    episode = parsed["episodes"][0]
    assert episode["title"] == "Untitled episode"
    assert episode["description"] == "Episode description"
    assert episode["published"] == "2026-06-25T10:00:00+00:00"
    assert episode["season"] == "2"
    assert episode["episode_number"] == "7"
    assert episode["explicit"] is None
    assert episode["website_url"] == "https://example.test/episode-page"


def test_parse_podcast_feed_reports_invalid_feed_shapes() -> None:
    """Parser raises user-facing parse errors for empty or unplayable feeds."""
    with pytest.raises(PodcastParseError) as no_entries:
        parse_podcast_feed("<rss><channel><title>Empty</title></channel></rss>", "https://example.test/feed.xml", "feed_1")
    assert no_entries.value.code == "no_episodes"

    with pytest.raises(PodcastParseError) as no_audio:
        parse_podcast_feed(
            "<rss><channel><title>No Audio</title><item><title>Episode</title></item></channel></rss>",
            "https://example.test/feed.xml",
            "feed_1",
        )
    assert no_audio.value.code == "no_audio_enclosures"

    with pytest.raises(PodcastParseError) as parse_error:
        parse_podcast_feed("not xml <", "https://example.test/feed.xml", "feed_1")
    assert parse_error.value.code == "parse_error"
