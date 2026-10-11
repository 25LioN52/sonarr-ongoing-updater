# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-10-11

First version published on GitHub.

### Added

- Season search for airing seasons with missing episodes, and a force-grab through Sonarr of the updated season torrent.
- Release priority that sticks to the tracker topic, earlier topics and release group the season came from. `OTHER_RELEASE_AFTER_HOURS` allows a different release once an episode is late.
- Removal of the previous torrent version from qBittorrent, keeping its files.
- Removal of the "Import blocked" queue entry after the new episodes are imported.
- A Telegram message with poster and "Open in Seerr" button for every new episode of an airing season.
- Docker image for `amd64` and `arm64`, published to GitHub Container Registry.

[Unreleased]: https://github.com/25LioN52/sonarr-ongoing-updater/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/25LioN52/sonarr-ongoing-updater/releases/tag/v1.2.0
