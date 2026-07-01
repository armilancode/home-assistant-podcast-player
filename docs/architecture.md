# Architecture

Podcast Player has two layers:

1. Backend integration under `custom_components/podcast_player/`.
2. Companion Lovelace card under `www/podcast-player-card/`.

The backend must remain useful without the card. The Home Assistant media source surface is `media_source.py`, which exposes podcast feeds and episodes to Home Assistant Media Browser and supported media players. Media Browser playback is still controlled by the selected Home Assistant `media_player`; Podcast Player can prepare shared state for that playback, but native progress and seek support depend on the target integration.

The card is a richer frontend for browsing, browser playback, progress updates, and faster interaction. It uses Podcast Player actions for enhanced external playback control when a target supports it.
