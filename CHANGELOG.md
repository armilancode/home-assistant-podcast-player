# Changelog

## 0.3.0-alpha.0

- Imported current working Podcast Player integration and companion card into a dedicated source repository.
- Added HACS repository metadata, CI skeleton, and development docs.
- Added a Media Source MVP for Home Assistant Media Browser playback.
- Hardened media-player output to avoid unsafe Home Assistant internal object control paths.
- Added card picker variants for player, player plus latest episodes, and latest episodes only.

## 0.2.31

- Fixed a DLNA stop regression where Podcast Player could shut down Home Assistant when stopping an unavailable/off TV target.
