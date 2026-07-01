# Home Assistant Podcast Player

Podcast Player is a Home Assistant custom integration for RSS podcast feeds, playback progress, and playback on Home Assistant media players.

## Current status

Alpha-stage custom integration. It is useful for early validation, but it is not yet ready for broad public installation.

Implemented today:

- RSS feed parsing and local library storage.
- Podcast feed, library, playback, and progress entities.
- Service actions for adding feeds, refreshing feeds, playback, progress, and marking episodes played.
- Media Browser support through Home Assistant media sources.
- Companion Lovelace card under `www/podcast-player-card/`.
- Signed proxy routes for media players that need a Home Assistant URL.

## Playback surfaces

Podcast Player supports two Home Assistant playback surfaces:

- **Media Browser** exposes podcast feeds and episodes to Home Assistant's native media picker. Playback is handed to the selected Home Assistant `media_player`, so native progress and seek controls depend on that target integration.
- **Podcast Player card and actions** provide the richer control path, including backend session tracking and enhanced external playback controls where supported.

Roadmap:

- Broader Media Browser browsing and playback coverage.
- Safer media-player output behavior and clearer playback errors.
- Robust config flows, diagnostics, translations, and automated tests.

## HACS installation

For HACS testing:

1. Add this repository to HACS as a custom repository of type `Integration`.
2. Install **Podcast Player**.
3. Restart Home Assistant.
4. Add the integration from **Settings → Devices & services → Add integration → Podcast Player**.

The companion card is included in this repo during alpha, but HACS integration installs do not automatically install arbitrary `www` files. For manual testing, copy `www/podcast-player-card/podcast-player-card.js` into your Home Assistant `www/podcast-player-card/` directory and add this dashboard resource:

```text
/local/podcast-player-card/podcast-player-card.js
```

## Development

Development should happen in a dedicated checkout of this repository. Runtime files from a Home Assistant configuration directory, such as `.storage`, logs, backups, databases, and secrets, must not be committed.
