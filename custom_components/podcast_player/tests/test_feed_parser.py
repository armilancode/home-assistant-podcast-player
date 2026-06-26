"""Tests for Podcast Player RSS parsing."""

from custom_components.podcast_player.feed_parser import parse_podcast_feed

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
