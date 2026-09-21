# Changelog

## 0.3.0-alpha.3 — 2026-09-21

### Added

- Bundled the companion dashboard card inside the integration so HACS installs and updates the backend and card together.
- Registered the bundled card through Home Assistant's supported frontend module API at a versioned static URL.
- Added a visible backend/card version-mismatch warning with recovery instructions.
- Added privacy-safe frontend registration and migration details to Home Assistant diagnostics.

### Changed

- Fresh installations no longer require copying JavaScript into `www` or creating a Lovelace resource.
- Storage-mode upgrades remove only exact legacy Podcast Player card resources after the bundled module is active.
- YAML-managed Lovelace resources remain untouched and produce a targeted migration reminder.

### Validation

- 236 Python tests and 15 browser lifecycle/session tests pass.
- Ruff, Python compilation, JavaScript syntax validation, and the release-package check pass locally.
- Home Assistant configuration validation passes on the live installation.
- A live upgrade from `0.3.0-alpha.2` removed the exact legacy storage-mode resource, served the bundled card with the expected hash, advertised the versioned module, and rendered the existing dashboard without a card-version warning.

## 0.3.0-alpha.2 — 2026-09-21

### Fixed

- Replaced browser progress service calls with silent websocket checkpoints so background synchronization failures cannot create repeated Home Assistant action toasts or notification vibrations.
- Prevented hidden or suspended mobile WebViews from repeatedly writing progress while keeping one best-effort checkpoint when the page is backgrounded.
- Restored synchronization after the Android Companion app returns to the foreground or reconnects.
- Prevented passive cards and stale clients from rolling newer cross-device progress backward.
- Added a server-authoritative browser playback lease so only one connected browser or Companion app can actively play at a time.
- Made **Take over** transfer playback to the requesting device and stop the previously connected browser player.
- Released browser ownership on pause, eliminating ghost sessions that made every restarted client show **Paused elsewhere**.
- Required lock-screen Play actions to acquire a fresh playback lease before resuming audio.
- Advanced passive-device clocks between authoritative checkpoints without increasing storage writes.

### Changed

- For local browser playback, the status identifies the active client as **Home Assistant app** or **Web browser**. External playback instead shows the selected Home Assistant media player's friendly name and reported state.
- Paused browser playback is intentionally unowned: every client shows **Play**, and the next client to press it becomes the active output.
- Android Companion system media controls are disabled by default to avoid device-specific repeated media-notification haptics. They remain available as an explicit card option.
- Added browser lifecycle and ownership tests to continuous integration.

### Validation

- 226 Python tests and 14 browser lifecycle/session tests pass.
- Ruff, Python compilation, JavaScript syntax validation, Hassfest, and GitHub CI pass.
- Home Assistant configuration validation passes on the live installation.
- Real-device testing passed across a desktop browser and Android Companion app, including background playback, screen lock, progress synchronization, pause, resume, and two-way takeover.

## 0.3.0-alpha.1 — 2026-09-21

- Added an episode-scoped signed Home Assistant proxy fallback when direct podcast streams fail.
- Prevented duplicate fallback attempts from near-simultaneous Android audio errors and rejected playback promises.
- Preserved the resume position while switching playback routes.
- Added clearer network, proxy, codec, and unsupported-format error messages.
- Updated development validation to Home Assistant 2026.9.3.

## 0.3.0-alpha.0

- Imported current working Podcast Player integration and companion card into a dedicated source repository.
- Added HACS repository metadata, CI skeleton, and development docs.
- Added a Media Source MVP for Home Assistant Media Browser playback.
- Hardened media-player output to avoid unsafe Home Assistant internal object control paths.
- Added card picker variants for player, player plus latest episodes, and latest episodes only.
- Replaced synchronous feed parsing with the typed asynchronous `aio-podcast` client.

## 0.2.31

- Fixed a DLNA stop regression where Podcast Player could shut down Home Assistant when stopping an unavailable/off TV target.
