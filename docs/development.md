# Development workflow

Use a dedicated checkout of this repository for development.

## Rules

- Keep runtime files, `.storage`, databases, logs, backups, and secrets out of Git.
- Prefer small commits with a clear reason.

## Manual testing

Use a separate Home Assistant development configuration for manual testing. Copy only the integration and card files needed for the test run, and never copy runtime state back into the repository.
