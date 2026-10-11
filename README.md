# sonarr-ongoing-updater

[![Tests and Docker image](https://github.com/25LioN52/sonarr-ongoing-updater/actions/workflows/docker.yml/badge.svg)](https://github.com/25LioN52/sonarr-ongoing-updater/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Downloads new episodes of airing shows when a tracker publishes the whole season as one torrent and updates that torrent with every new episode. Announces each new episode in Telegram through the same bot Seerr uses.

Sonarr cannot do this on its own. It rejects the updated release with "Existing file on disk is of equal or higher preference", because the earlier episodes are already on disk, and it never imports a download a second time. Seerr only announces a season once all of it is available.

## What it does

Every `INTERVAL_MINUTES`, for each season that is still airing and has aired episodes missing:

1. Runs a season search in Sonarr, the same as Interactive Search.
2. Picks a torrent that contains the missing episodes. Only releases whose sole rejections are "file already on disk" or "season not finished yet" qualify. The release the season was already downloaded from always wins (see below).
3. Force-grabs it through Sonarr, the same as clicking download on a rejected release. Sonarr tracks the download and imports the new episode itself.
4. Removes the previous version of the torrent from qBittorrent, keeping the files. The new torrent picks the files up and downloads only the new episode.
5. After the import, removes the "Import blocked" entry from Sonarr's queue. Sonarr marks the old episodes as "not an upgrade"; the torrent keeps seeding.

Every `CHECK_MINUTES` it also sends a Telegram message for each newly imported episode of a season that is still airing (see Notifications).

Example log (log lines and Telegram messages are in Ukrainian):

```
Test Show S01: бракує E06, шукаю оновлений реліз…
Test Show S01: беру «Test Show S01E01-06 of 10 1080p» [Toloka (Prowlarr)] — містить E06 (за: розбір Sonarr; та сама роздача)
Test Show S01: стару версію торрента «Test.Show.S01.1080p» прибрано з qBittorrent, файли лишились
Test Show S01: надіслано сповіщення в Telegram про E06
Test Show S01: нові серії імпортовано (E06), запис «Import blocked» прибрано з черги, торрент далі роздається
```

## Release priority (dubs)

Different releases of the same season usually carry different dubs, so the service sticks to the one the season came from:

1. The same tracker topic as the latest download of this season.
2. Any topic this season was downloaded from before.
3. A release from the same release group, if Sonarr recognised one.

By default it never takes a different release and waits for the current one to be updated. As a fallback for an abandoned topic, set `OTHER_RELEASE_AFTER_HOURS`, for example `72`: a different release is then taken once an episode is more than 72 hours late.

The first download of a new season has nothing to stick to, so Sonarr's own quality and Custom Format rules decide.

## Notifications

A message goes out for every episode Sonarr imports into a season that still has episodes to air, whoever grabbed it: this service, or Sonarr itself (for example the first episodes right after a Seerr request). It contains the poster, the show, episode numbers and titles, and an "Open in Seerr" button.

No message goes out for:

- the season finale and seasons that have already finished airing, because Seerr announces the complete season;
- quality upgrades of an episode you already had;
- anything imported before the service first started.

To get a message for every episode of every season, set `NOTIFY_ALL_SEASONS=true`. Seerr will then announce complete seasons as well.

The message is sent right after Sonarr imports the file. If Jellyfin shows the episode later than that, enable Settings → Connect → **Emby / Jellyfin** in Sonarr to refresh the library on import, or Real-time monitoring in the Jellyfin library settings.

## Before you install

1. **Check it by hand.** In Sonarr, open an airing show, run Interactive Search for the season and grab the updated release despite the red mark. If the new episode gets imported, the service will work too.
2. **qBittorrent: turn off "Keep incomplete torrents in".** Otherwise the new torrent won't find the existing files and downloads the whole season again.
3. **qBittorrent WebUI login** is needed to remove old torrent versions. Without `QBIT_URL` the service still works, but old versions have to be removed by hand.
4. **Telegram details** are in Seerr: Settings → Notifications → Telegram, fields Bot Authentication Token and Chat ID.

## Install

### 1. GitHub repository and automatic image builds

`.github/workflows/docker.yml` runs the tests on every push to `main`, then builds the image for `amd64` and `arm64` and publishes it to GitHub Container Registry as `ghcr.io/<user>/sonarr-ongoing-updater`.

1. Create an empty repository named `sonarr-ongoing-updater` on GitHub. Public is simplest: the image can be pulled without logging in.
2. Push this folder:

   ```bash
   git init -b main
   git add .
   git commit -m "Initial version"
   git remote add origin https://github.com/<user>/sonarr-ongoing-updater.git
   git push -u origin main
   ```

   Pushing a workflow file needs a token with workflow permission: `gh auth login -s workflow`, or a fine-grained token with **Contents** and **Workflows** set to Read and write.
3. In the repository, Settings → Actions → General → Workflow permissions → **Read and write permissions**.
4. Wait for the green check on the **Actions** tab.
5. If `docker pull` asks for a login, open the package on your profile's **Packages** tab → Package settings → Change visibility → Public. For a private package, log in on the server once with a classic token that has `read:packages`:

   ```bash
   echo 'TOKEN' | docker login ghcr.io -u <user> --password-stdin
   ```

Image tags: `latest` follows `main`; `git tag v1.2.0 && git push origin v1.2.0` publishes `1.2.0`; every commit also gets `sha-<short hash>`.

### 2. Run it on the server

Copy the service from `docker-compose.yml` into the compose file that runs `sonarr` and `qbittorrent`:

- replace `your-github-user` in `image` with your GitHub username in lowercase;
- fill in the Sonarr API key (Settings → General → API Key), the qBittorrent login and the Telegram details;
- if your services are named differently, adjust `sonarr` and `qbittorrent` in the URLs. If qBittorrent runs behind gluetun, use `http://gluetun:8080`.

A first pass that only shows what it would do:

```bash
docker compose pull sonarr-ongoing-updater
docker compose run --rm sonarr-ongoing-updater --once --dry-run --verbose
```

The log lists every release found and why it was taken or skipped. When it looks right, set `DRY_RUN: "false"` and start it:

```bash
docker compose up -d sonarr-ongoing-updater
docker logs -f sonarr-ongoing-updater
```

### Updates

A push to GitHub builds a new image by itself. To pull it on the server:

```bash
docker compose pull sonarr-ongoing-updater && docker compose up -d sonarr-ongoing-updater
```

To do that every night, add a line with `crontab -e` on the server, using the folder of your compose file:

```
30 4 * * * cd /path/to/stack && docker compose pull -q sonarr-ongoing-updater && docker compose up -d sonarr-ongoing-updater
```

Watchtower used to be the usual choice for this, but it is no longer maintained, so cron is simpler.

### Without a registry

Docker can build the image straight from the repository. In compose, replace `image:` with:

```yaml
build: https://github.com/<user>/sonarr-ongoing-updater.git#main
```

and update with `docker compose up -d --build sonarr-ongoing-updater`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SONARR_URL`, `SONARR_API_KEY` | required | Sonarr address and API key |
| `QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD` | empty | qBittorrent WebUI, used to remove old torrent versions |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Bot and chat for notifications, the same as in Seerr. Several chats separated by commas |
| `SEERR_URL` | empty | External Seerr address for the "Open in Seerr" button |
| `NOTIFY_ALL_SEASONS` | false | Also announce episodes of seasons that have finished airing |
| `OTHER_RELEASE_AFTER_HOURS` | 0 | 0 = never take a different release. N = take it once an episode is N hours late |
| `INTERVAL_MINUTES` | 120 | How often to search the trackers for updated releases |
| `CHECK_MINUTES` | 10 | How often to check imports, send notifications and clean the queue |
| `DRY_RUN` | false | Only log; grab, delete and send nothing |
| `ONGOING_DAYS` | 30 | A season counts as airing for searches if it has future episodes or its last episode aired within N days |
| `MIN_HOURS_SINCE_AIR` | 1 | Don't search for an episode that aired less than N hours ago |
| `MAX_SEARCHES_PER_RUN` | 10 | Maximum searches per pass, to go easy on the trackers |
| `REMOVE_OLD_TORRENT` | true | Remove the previous torrent version (files stay) |
| `CLEANUP_QUEUE` | true | Remove the "Import blocked" entry once the new episodes are imported |
| `GUESS_BY_PUBLISH_DATE` | true | If the title doesn't say which episodes a release holds, assume it holds every episode aired before it was published |
| `EXTRA_OVERRIDABLE_REJECTIONS` | empty | Extra Sonarr rejection texts to ignore, comma separated |
| `SEARCH_TIMEOUT_SECONDS` | 300 | Timeout for a search through Prowlarr |
| `GRAB_WAIT_SECONDS` | 90 | How long to wait for the torrent to appear in qBittorrent |
| `LOG_LEVEL` | INFO | DEBUG shows every decision |

## What it won't do

- Override rejections for quality, language, size, seeders, the blocklist, the queue or grab history.
- Grab the same version of a release twice. It compares the size with Sonarr's history and its own `state.json`, so nothing happens until the tracker actually updates the release.
- Touch seasons that have finished airing. Sonarr grabs those as full season packs on its own.

## Limitations

- Episode numbers come from Sonarr's parser. For releases Sonarr sees as a full season they are read from the title: `S01E01-06`, `Серии: 1-6 из 10`, `серії 1-8 з 12`, `[1-4 of 8]` and similar. Absolute anime numbering is not recognised in titles.
- If the uploader renames the root folder when updating the torrent, qBittorrent downloads the whole season again. The service then leaves the old version alone and logs a warning.
- Tested against mock Sonarr v4, qBittorrent and Telegram APIs. Run it with `DRY_RUN` on a real server first.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests start local mock Sonarr, qBittorrent and Telegram servers and never touch real ones.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [CHANGELOG.md](CHANGELOG.md) for release history. Report security problems privately, as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
