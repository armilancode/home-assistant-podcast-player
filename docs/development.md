# Development workflow

Use a dedicated checkout of this repository for development.

## Rules

- Keep runtime files, `.storage`, databases, logs, backups, and secrets out of Git.
- Prefer small commits with a clear reason.

## Manual testing

Use a separate Home Assistant development configuration for manual testing. Copy only the integration and card files needed for the test run, and never copy runtime state back into the repository.

## Validation

Run the same checks required by continuous integration before publishing:

```text
ruff check .
python -m compileall custom_components/podcast_player
node --check custom_components/podcast_player/frontend/podcast-player-card.js
node --test tests/*.test.js
pytest
```

For a release, also validate the Home Assistant configuration with the candidate integration, restart Home Assistant, and complete a short two-client browser playback check:

1. Start playback on one client and confirm the other offers **Take over**.
2. Take over and confirm the first connected client stops.
3. Pause and confirm both clients show **Play**, not **Take over**.
4. Resume from the other client and confirm the shared position is retained.

The version in `custom_components/podcast_player/const.py`, `manifest.json`, the bundled card header, changelog heading, and release tag must describe the same release.
