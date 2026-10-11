# Contributing

Bug reports, fixes and support for more tracker title formats are welcome.

## Reporting a bug

Open an issue with the **Bug report** form. The most useful thing to attach is a dry-run log with debug output, which lists every release Sonarr returned and why each was taken or skipped:

```bash
docker compose run --rm sonarr-ongoing-updater --once --dry-run --verbose
```

Remove API keys, passwords, bot tokens and private tracker links from the log before posting it.

## Development

The service uses only the Python standard library, so all you need is Python 3 (the image uses 3.12).

```bash
git clone https://github.com/25LioN52/sonarr-ongoing-updater.git
cd sonarr-ongoing-updater
python3 -m unittest discover -s tests -v
```

The tests start local mock Sonarr, qBittorrent and Telegram servers and never contact real ones.

To try a change against your own Sonarr, run the script from source with the variables from the Settings table in the README, and keep `--dry-run` on:

```bash
SONARR_URL=http://localhost:8989 SONARR_API_KEY=... STATE_FILE=./data/state.json \
  python3 ongoing_updater.py --once --dry-run --verbose
```

`data/` and `.env` are git-ignored, so local state and secrets stay out of commits.

## Guidelines

- **Standard library only.** The image is plain `python:3.12-alpine` with one file copied in. Please don't add third-party dependencies.
- **One file.** `ongoing_updater.py` is the whole service, and the Dockerfile copies only that file.
- **Test every behaviour change** in `tests/test_updater.py`. The mock servers read their responses from the `World` class, so a new scenario usually means adding releases or episodes there.
- **Log lines, CLI help and Telegram messages are in Ukrainian.** Keep new ones in Ukrainian. Code, comments and docs are in English.
- **Document new settings** in the Settings table in `README.md`, and in `docker-compose.yml` if a typical install needs them.
- **Note user-visible changes** under Unreleased in `CHANGELOG.md`.

## Pull requests

1. Branch off `main`.
2. Check that `python3 -m unittest discover -s tests -v` passes. CI runs the same tests and builds the image for every pull request.
3. Open the pull request and fill in the checklist.

## Releases

Bump `__version__` in `ongoing_updater.py`, move the Unreleased entries in `CHANGELOG.md` under the new version, commit, and push a tag:

```bash
git tag v1.3.0
git push origin v1.3.0
```

The workflow then publishes `ghcr.io/25lion52/sonarr-ongoing-updater:1.3.0`.
