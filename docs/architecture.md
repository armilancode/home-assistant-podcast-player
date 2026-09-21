# Architecture

Podcast Player has two layers:

1. Backend integration under `custom_components/podcast_player/`.
2. Bundled companion Lovelace card under `custom_components/podcast_player/frontend/`.

The backend must remain useful without the card. The Home Assistant media source surface is `media_source.py`, which exposes podcast feeds and episodes to Home Assistant Media Browser and supported media players. Media Browser playback is still controlled by the selected Home Assistant `media_player`; Podcast Player can prepare shared state for that playback, but native progress and seek support depend on the target integration.

Podcast feed retrieval and parsing use `aio-podcast` with Home Assistant's shared `aiohttp` session. The dependency performs incremental asynchronous parsing and returns typed models, which the integration converts to its stable storage format.

The card is a richer frontend for browsing, browser playback, progress updates, and faster interaction. It uses Podcast Player actions for enhanced external playback control when a target supports it.

The integration serves the card from a versioned static URL and advertises it with Home Assistant's supported extra-module frontend API. A fresh install therefore needs no `www` copy or Lovelace resource. On upgrades, an exact legacy Podcast Player resource is removed through the Lovelace resource collection only in storage mode; YAML resources remain user-managed.

## Browser playback sessions

Browser audio is produced locally by either a web browser or a Home Assistant Companion WebView. The backend stores one server-authoritative lease for the frontend that is currently producing audio.

- Starting or taking over playback creates a new episode-scoped session id.
- Checkpoints from any other session id are rejected.
- A state-change observer tells a connected previous owner to stop when the lease moves.
- Pausing saves progress and clears the lease. Paused playback has shared state but no device owner.
- A subsequent Play action claims a new lease before local audio starts, including Play from system media controls.
- Passive cards display the authoritative position and extrapolate the visible clock between checkpoints without writing progress every second.

The lease is intentionally not durable ownership. Persisting it while paused would strand playback on a browser session that no longer exists after a tab, browser, or Companion app restart.
