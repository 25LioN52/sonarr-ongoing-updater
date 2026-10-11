#!/usr/bin/env python3
"""Sonarr ongoing updater.

Some trackers publish a running season as ONE torrent and keep updating it:
every new episode replaces the .torrent in the same topic. Sonarr does not
handle that on its own. The updated release is rejected because the earlier
episodes are already on disk ("Existing file on disk is of equal or higher
preference"), and an already imported download is never imported again.

For every *ongoing* season that has aired but missing episodes this service:
  1. runs Sonarr's season search (the same as Interactive Search),
  2. picks a torrent release that contains the missing episodes and whose only
     rejections are of the "already have it on disk" kind; the tracker topic
     the season was already downloaded from always wins, because another
     release of the same season usually has a different dub,
  3. force-grabs it through Sonarr (the same as the grab button in Interactive
     Search), so Sonarr itself tracks the download and imports the new episode,
  4. removes the previous version of that torrent from qBittorrent while
     keeping its files, so the new torrent re-uses them and downloads only
     what is new,
  5. after the import, removes the leftover "import blocked" queue entry from
     Sonarr (the torrent keeps seeding),
  6. optionally sends a Telegram message for every new episode of a season
     that is still airing, whoever grabbed it (Seerr only notifies once a
     whole season is available).

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

__version__ = "1.2.0"

log = logging.getLogger("ongoing-updater")
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    return datetime.now(UTC)


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_dt(value) -> datetime | None:
    """Parse the ISO timestamps Sonarr returns (may end in Z, may have 7 fraction digits)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def lower_keys(data) -> dict:
    """Sonarr history `data` keys have changed case between versions; normalise them."""
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): v for k, v in data.items()}


def fmt_nums(numbers) -> str:
    return ", ".join(f"E{n:02d}" for n in sorted(numbers))


def same_size(a: int, b: int) -> bool:
    # Indexers often report rounded sizes ("5.2 GB"), so allow a small tolerance.
    # A new episode adds far more than 0.5 % to a season pack.
    return abs(a - b) <= max(2 * 1024 * 1024, 0.005 * max(a, b))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on", "так")


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} має бути числом, а зараз: {value!r}")


# Rejections that only mean "you already have (part of) this on disk" or
# "this season pack is not finished yet". These are exactly the ones an
# updatable season torrent runs into, so it is safe to grab despite them.
DEFAULT_OVERRIDABLE = (
    "existing file",              # "Existing file on disk is of equal or higher preference", "... meets cutoff"
    "for existing episode file",  # "Not a Custom Format upgrade for existing episode file(s)"
    "not an upgrade",
    "does not allow upgrades",
    "meets cutoff",
    "full season",                # season pack for a season that is still airing
    "not aired",
    "haven't aired",
    "has not aired",
)

# Never override rejections about the queue, history or blocklist: they
# protect against grabbing the same thing twice or something that failed.
NEVER_OVERRIDE = ("queue", "history", "blocklist", "blacklist")


class Config:
    def __init__(self) -> None:
        self.sonarr_url = (_env("SONARR_URL") or "").rstrip("/")
        self.sonarr_key = _env("SONARR_API_KEY") or ""
        self.qbit_url = (_env("QBIT_URL") or "").rstrip("/")
        self.qbit_user = _env("QBIT_USERNAME") or ""
        self.qbit_pass = _env("QBIT_PASSWORD") or ""
        self.interval = _env_int("INTERVAL_MINUTES", 120)  # searches on the tracker
        self.check_minutes = _env_int("CHECK_MINUTES", 10)  # imports, notifications, cleanup
        self.notify_all_seasons = _env_bool("NOTIFY_ALL_SEASONS", False)
        self.dry_run = _env_bool("DRY_RUN", False)
        self.ongoing_days = _env_int("ONGOING_DAYS", 30)
        self.min_hours = _env_int("MIN_HOURS_SINCE_AIR", 1)
        self.max_searches = _env_int("MAX_SEARCHES_PER_RUN", 10)
        self.remove_old = _env_bool("REMOVE_OLD_TORRENT", True)
        self.cleanup_queue = _env_bool("CLEANUP_QUEUE", True)
        self.guess_by_date = _env_bool("GUESS_BY_PUBLISH_DATE", True)
        # 0 = never switch to a different release (different dub) while the
        # season already has one; N = allow it once an episode is N hours late.
        self.other_release_after_hours = _env_int("OTHER_RELEASE_AFTER_HOURS", 0)
        self.search_timeout = _env_int("SEARCH_TIMEOUT_SECONDS", 300)
        self.grab_wait = _env_int("GRAB_WAIT_SECONDS", 90)
        self.state_file = _env("STATE_FILE", "/data/state.json")
        self.telegram_token = _env("TELEGRAM_BOT_TOKEN") or ""
        self.telegram_chat_ids = [c.strip() for c in (_env("TELEGRAM_CHAT_ID") or "").split(",")
                                  if c.strip()]
        self.telegram_api = (_env("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
        self.seerr_url = (_env("SEERR_URL") or "").rstrip("/")
        self.log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
        extra = _env("EXTRA_OVERRIDABLE_REJECTIONS", "") or ""
        self.overridable = DEFAULT_OVERRIDABLE + tuple(
            part.strip().lower() for part in extra.split(",") if part.strip()
        )


# --------------------------------------------------------------------------- #
# Sonarr API
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    pass


class Sonarr:
    def __init__(self, url: str, api_key: str, search_timeout: int = 300) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.search_timeout = search_timeout

    def _request(self, method: str, path: str, params: dict | None = None,
                 body=None, timeout: int = 60):
        url = f"{self.url}/api/v3/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Api-Key", self.api_key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise ApiError(f"Sonarr {method} /{path}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"Sonarr недоступний ({self.url}): {exc.reason}") from None
        if not raw.strip():
            return None
        return json.loads(raw)

    def _paged(self, path: str, params: dict, page_size: int = 250) -> list:
        records: list = []
        page = 1
        while page <= 100:
            data = self._request("GET", path, {**params, "page": page, "pageSize": page_size})
            batch = (data or {}).get("records") or []
            records.extend(batch)
            total = to_int((data or {}).get("totalRecords"), len(records))
            if not batch or len(records) >= total:
                break
            page += 1
        return records

    def status(self) -> dict:
        return self._request("GET", "system/status") or {}

    def missing(self) -> list:
        return self._paged("wanted/missing", {"monitored": "true", "includeSeries": "true"})

    def series(self, series_id: int) -> dict:
        return self._request("GET", f"series/{series_id}") or {}

    def episodes(self, series_id: int, season: int) -> list:
        return self._request("GET", "episode", {"seriesId": series_id, "seasonNumber": season}) or []

    def history(self, series_id: int, season: int) -> list:
        return self._request("GET", "history/series",
                             {"seriesId": series_id, "seasonNumber": season}) or []

    def history_since(self, since: datetime) -> list:
        return self._request("GET", "history/since", {
            "date": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "includeSeries": "true", "includeEpisode": "true"}) or []

    def queue(self) -> list:
        return self._paged("queue", {"includeUnknownSeriesItems": "false"}, page_size=500)

    def search_season(self, series_id: int, season: int) -> list:
        return self._request("GET", "release", {"seriesId": series_id, "seasonNumber": season},
                             timeout=self.search_timeout) or []

    def grab(self, guid: str, indexer_id: int):
        # Same call the Interactive Search grab button makes; Sonarr does not
        # re-check rejections here, it downloads the cached search result.
        return self._request("POST", "release", body={"guid": guid, "indexerId": indexer_id})

    def remove_queue_item(self, queue_id: int) -> None:
        self._request("DELETE", f"queue/{queue_id}",
                      {"removeFromClient": "false", "blocklist": "false"})


# --------------------------------------------------------------------------- #
# qBittorrent Web API
# --------------------------------------------------------------------------- #

class QBittorrent:
    def __init__(self, url: str, username: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _raw(self, method: str, path: str, params: dict | None = None,
             data: dict | None = None) -> str:
        url = f"{self.url}/api/v2/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        # qBittorrent's CSRF protection wants a Referer matching its address.
        req.add_header("Referer", self.url)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def login(self) -> None:
        text = self._raw("POST", "auth/login",
                         data={"username": self.username, "password": self.password})
        if "ok" not in text.lower():
            raise ApiError("qBittorrent: не вдалося увійти, перевірте QBIT_USERNAME і QBIT_PASSWORD")
        self.logged_in = True

    def _call(self, method: str, path: str, params: dict | None = None,
              data: dict | None = None) -> str:
        try:
            if self.username and not self.logged_in:
                self.login()
            try:
                return self._raw(method, path, params, data)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and self.username:
                    self.login()
                    return self._raw(method, path, params, data)
                raise
        except urllib.error.HTTPError as exc:
            raise ApiError(f"qBittorrent {path}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"qBittorrent недоступний ({self.url}): {exc.reason}") from None

    def version(self) -> str:
        return self._call("GET", "app/version")

    def torrents(self, hashes: list[str] | None = None, category: str | None = None) -> list:
        params: dict = {}
        if hashes:
            params["hashes"] = "|".join(h.lower() for h in hashes)
        if category:
            params["category"] = category
        text = self._call("GET", "torrents/info", params)
        return json.loads(text) if text.strip() else []

    def delete_keep_files(self, torrent_hash: str) -> None:
        self._call("POST", "torrents/delete",
                   data={"hashes": torrent_hash.lower(), "deleteFiles": "false"})


# --------------------------------------------------------------------------- #
# Telegram notifications
# --------------------------------------------------------------------------- #

def _html(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Telegram:
    """Sends "new episode" messages, styled like Seerr's (poster + caption + button)."""

    def __init__(self, api_url: str, token: str, chat_ids: list[str]) -> None:
        self.base = f"{api_url.rstrip('/')}/bot{token}"
        self.chat_ids = chat_ids

    def _post(self, method: str, payload: dict) -> None:
        req = urllib.request.Request(f"{self.base}/{method}", data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise ApiError(f"Telegram {method}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"Telegram недоступний: {exc.reason}") from None

    def send(self, text: str, photo_url: str | None = None,
             button: tuple[str, str] | None = None) -> None:
        markup = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]} if button else None
        for chat_id in self.chat_ids:
            base = {"chat_id": chat_id, "parse_mode": "HTML"}
            with_button = {**base, "reply_markup": markup} if markup else base
            plain = {"text": text, "disable_web_page_preview": True}
            # Poster first; then text only; then without the button (Telegram
            # rejects buttons pointing at addresses it considers invalid).
            attempts = []
            if photo_url:
                attempts.append(("sendPhoto", {**with_button, "photo": photo_url, "caption": text}))
            attempts.append(("sendMessage", {**with_button, **plain}))
            if markup:
                attempts.append(("sendMessage", {**base, **plain}))
            error = None
            for method, payload in attempts:
                try:
                    self._post(method, payload)
                    break
                except ApiError as exc:
                    error = exc
                    log.debug("Telegram %s не вдалося (%s), пробую простіше", method, exc)
            else:
                raise error


def build_episode_message(series: dict, season: int, episodes: list[dict],
                          seerr_url: str) -> tuple[str, str | None, tuple[str, str] | None]:
    title = series.get("title") or "Серіал"
    word = "Нова серія" if len(episodes) == 1 else "Нові серії"
    lines = [f"📺 <b>{word}: {_html(title)}</b>", ""]
    for ep in sorted(episodes, key=lambda e: e.get("episodeNumber") or 0):
        line = f"S{season:02d}E{ep.get('episodeNumber', 0):02d}"
        ep_title = ep.get("title")
        if ep_title and ep_title.upper() != "TBA":
            line += f" — {_html(ep_title)}"
        lines.append(line)
    lines += ["", "Завантажено, з'явиться в Jellyfin після сканування бібліотеки."]
    poster = None
    for image in series.get("images") or []:
        if image.get("coverType") == "poster" and str(image.get("remoteUrl", "")).startswith("http"):
            poster = image["remoteUrl"]
            break
    button = None
    if seerr_url and series.get("tmdbId"):
        button = ("Відкрити в Seerr", f"{seerr_url}/tv/{series['tmdbId']}")
    return "\n".join(lines), poster, button


# --------------------------------------------------------------------------- #
# Working out which episodes a release contains
# --------------------------------------------------------------------------- #

_EP_WORD = (r"\b(?:сер(?:ії|ии|ія|ия|ій|ий)|эпизод(?:ы|ов)?|епізод(?:и|ів)?"
            r"|episodes?|eps?)")
_OF_WORD = r"(?:из|з|із|of)"

_RANGE_PATTERNS = [
    # S01E01-06, S01E01-E06, S01E01E06, S1E1-6 of 10, S01E01-S01E06
    re.compile(r"\bS\d{1,2}\s*E(?P<a>\d{1,4})\s*(?:-\s*(?:S\d{1,2}\s*)?E?|E)(?P<b>\d{1,4})(?!\d)", re.I),
    # Серии: 1-6, серії 1-6, Episodes 1-6, Эпизоды 1–6
    re.compile(_EP_WORD + r"\.?\s*:?\s*(?P<a>\d{1,4})\s*[-–—]\s*(?P<b>\d{1,4})(?!\d)", re.I),
    # 1-6 серії, 1-6 эпизоды
    re.compile(r"(?<![\d.])(?P<a>\d{1,4})\s*[-–—]\s*(?P<b>\d{1,4})\s*" + _EP_WORD, re.I),
    # 1-6 из 10, 1-6 з 10, [1-6 of 10], 1-6 из ??
    re.compile(r"(?<![\d.])(?P<a>\d{1,4})\s*[-–—]\s*(?P<b>\d{1,4})\s*" + _OF_WORD
               + r"\s*(?:\d{1,4}|\?+|x+|х+)(?!\d)", re.I),
]

_SINGLE_PATTERNS = [
    # S01E06 (but not the start of a range)
    re.compile(r"\bS\d{1,2}\s*E(?P<a>\d{1,4})(?!\d)(?!\s*(?:-|E\d))", re.I),
    # Серия 1 из 10, episode 1 of 10
    re.compile(_EP_WORD + r"\.?\s*:?\s*(?P<a>\d{1,4})\s*" + _OF_WORD + r"(?!\w)", re.I),
]


def _looks_like_years(a: int, b: int) -> bool:
    return 1900 <= a <= 2100 and 1900 <= b <= 2100


def parse_title_episodes(title: str) -> set[int]:
    """Episode numbers a release title claims to contain (empty set if unknown)."""
    found: set[int] = set()
    for pattern in _RANGE_PATTERNS:
        for match in pattern.finditer(title):
            a, b = int(match.group("a")), int(match.group("b"))
            if 0 < a <= b and b - a <= 300 and not _looks_like_years(a, b):
                found.update(range(a, b + 1))
    for pattern in _SINGLE_PATTERNS:
        for match in pattern.finditer(title):
            a = int(match.group("a"))
            if 0 < a <= 2000:
                found.add(a)
    return found


def release_episodes(rel: dict) -> tuple[set[int] | None, str | None]:
    """Return (episode numbers, where we learned it) or (None, None) if unknown.

    For a release Sonarr parsed as specific episodes we trust Sonarr. A "full
    season" release is mapped by Sonarr to every episode of the season, aired
    or not, which says nothing about what the torrent really holds, so for
    those we read the title instead.
    """
    if not rel.get("fullSeason"):
        numbers: set[int] = set()
        for key in ("mappedEpisodeNumbers", "episodeNumbers"):
            numbers = {n for n in (rel.get(key) or []) if isinstance(n, int)}
            if numbers:
                break
        if not numbers:
            numbers = {e.get("episodeNumber") for e in (rel.get("mappedEpisodeInfo") or [])
                       if isinstance(e, dict) and isinstance(e.get("episodeNumber"), int)}
        if numbers:
            return numbers, "розбір Sonarr"
    numbers = parse_title_episodes(rel.get("title") or "")
    if numbers:
        return numbers, "назва релізу"
    return None, None


def rejection_texts(rel: dict) -> list[str]:
    texts = []
    for item in rel.get("rejections") or []:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            texts.append(str(item.get("message") or item.get("reason") or item))
    return texts


def is_overridable(reason: str, cfg: Config) -> bool:
    low = reason.lower()
    if any(word in low for word in NEVER_OVERRIDE):
        return False
    return any(pattern in low for pattern in cfg.overridable)


def release_topic_keys(rel: dict) -> set[str]:
    return {k for k in (rel.get("guid"), rel.get("infoUrl"), rel.get("commentUrl")) if k}


def history_topic_keys(data: dict) -> set[str]:
    return {k for k in (data.get("guid"), data.get("nzbinfourl")) if k}


def fingerprint(rel: dict) -> str:
    return f"{rel.get('indexerId')}|{rel.get('guid')}|{to_int(rel.get('size'))}"


def release_affinity(rel: dict, previous: list[dict]) -> tuple[int, str]:
    """How close a release is to what the season was downloaded from before.

    Different releases of the same season often carry different dubs, so the
    topic already on disk always wins.
      3 = the same tracker topic as the latest grab
      2 = the same topic as an earlier grab of this season
      1 = the same release group as an earlier grab
      0 = a different release (or nothing grabbed before)
    `previous` is sorted newest first.
    """
    if not previous:
        return 0, "перша роздача сезону"
    keys = release_topic_keys(rel)
    if previous[0]["keys"] & keys:
        return 3, "та сама роздача"
    if any(p["keys"] & keys for p in previous):
        return 2, "раніше скачана роздача"
    group = (rel.get("releaseGroup") or "").strip().lower()
    if group and any(group == p["group"] for p in previous):
        return 1, f"та сама група ({rel.get('releaseGroup')})"
    return 0, "інша збірка"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        log.warning("Не вдалося прочитати %s (%s), починаю з чистого стану", path, exc)
        state = {}
    state.setdefault("grabs", [])
    state.setdefault("last_search", {})
    return state


def save_state(path: str, state: dict) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# The actual work
# --------------------------------------------------------------------------- #

class Context:
    def __init__(self, cfg: Config, sonarr: Sonarr, qbit: QBittorrent | None,
                 state: dict, poll_seconds: float = 5.0,
                 telegram: Telegram | None = None) -> None:
        self.cfg = cfg
        self.sonarr = sonarr
        self.qbit = qbit
        self.state = state
        self.poll_seconds = poll_seconds
        self.telegram = telegram

    def save(self) -> None:
        if not self.cfg.dry_run:
            save_state(self.cfg.state_file, self.state)

    def detail(self, msg: str, *args) -> None:
        # In a dry run, show every decision so the logs explain what would happen.
        (log.info if self.cfg.dry_run else log.debug)(msg, *args)


def queue_episode_ids(record: dict) -> set[int]:
    ids = set(record.get("episodeIds") or [])
    if isinstance(record.get("episodeId"), int):
        ids.add(record["episodeId"])
    return ids


def is_active_download(record: dict) -> bool:
    """A download that is still in progress (not finished, not failed, not ignored)."""
    if (record.get("status") or "").lower() == "completed":
        return False
    return (record.get("trackedDownloadState") or "").lower() not in ("imported", "failed", "ignored")


def analyse_season(episodes: list, now: datetime, cfg: Config) -> tuple[bool, list[dict]]:
    """Is the season ongoing, and which aired episodes are missing?"""
    aired: list[datetime] = []
    has_future = False
    targets: list[dict] = []
    for ep in episodes:
        air = parse_dt(ep.get("airDateUtc"))
        if air is None or air > now:
            has_future = True
            continue
        aired.append(air)
        if (ep.get("monitored") and not ep.get("hasFile")
                and now - air >= timedelta(hours=cfg.min_hours)
                and isinstance(ep.get("episodeNumber"), int)):
            targets.append({"id": ep["id"], "num": ep["episodeNumber"], "air": air})
    latest = max(aired) if aired else None
    ongoing = has_future or (latest is not None and now - latest <= timedelta(days=cfg.ongoing_days))
    return ongoing, targets


def evaluate_release(ctx: Context, rel: dict, season: int, targets: list[dict],
                     previous: list[dict], known_hashes: set[str]):
    """Return (candidate, None) or (None, reason it was skipped)."""
    if (rel.get("protocol") or "").lower() != "torrent":
        return None, "не торрент"
    rel_season = rel.get("mappedSeasonNumber")
    if rel_season is None:
        rel_season = rel.get("seasonNumber")
    if isinstance(rel_season, int) and rel_season != season:
        return None, f"інший сезон ({rel_season})"
    if rel.get("downloadAllowed") is False:
        return None, "Sonarr не дозволяє його завантажити"
    blocking = [r for r in rejection_texts(rel) if not is_overridable(r, ctx.cfg)]
    if blocking:
        return None, "відхилено: " + "; ".join(blocking)

    info_hash = (rel.get("infoHash") or "").lower()
    if info_hash and info_hash in known_hashes:
        return None, "саме цей торрент уже брали"
    size = to_int(rel.get("size"))
    published = parse_dt(rel.get("publishDate"))
    keys = release_topic_keys(rel)
    same_topic = [p for p in previous if p["keys"] & keys]
    for prev in same_topic:
        if size and prev["size"]:
            unchanged = same_size(size, prev["size"])
        elif published and prev["published"]:
            unchanged = abs(published - prev["published"]) < timedelta(minutes=1)
        else:
            unchanged = True
        if unchanged:
            return None, "ця версія роздачі вже була взята (оновлення ще немає)"
    if fingerprint(rel) in {g.get("fingerprint") for g in ctx.state["grabs"]}:
        return None, "цей реліз уже брав"

    numbers, source = release_episodes(rel)
    if numbers is not None:
        covered = [t for t in targets if t["num"] in numbers]
    elif ctx.cfg.guess_by_date and published:
        # Unknown contents: assume a release published after an episode aired includes it.
        covered = [t for t in targets if t["air"] <= published]
        source = "дата публікації"
    else:
        return None, "не вдалося визначити, які серії всередині"
    if not covered:
        return None, f"немає потрібних серій ({source})"

    affinity, affinity_note = release_affinity(rel, previous)
    if previous and affinity == 0:
        hours = ctx.cfg.other_release_after_hours
        waited = utcnow() - min(t["air"] for t in covered)
        if hours <= 0 or waited < timedelta(hours=hours):
            return None, ("інша збірка (може бути інша озвучка), чекаю оновлення поточної роздачі"
                          + (f" ще {hours - int(waited.total_seconds() // 3600)} год" if hours > 0 else ""))
        affinity_note = f"інша збірка, поточна не оновлювалась {hours}+ год"
    return {"rel": rel, "covered": covered, "source": source,
            "affinity": affinity, "affinity_note": affinity_note}, None


def find_grab_download_id(ctx: Context, series_id: int, season: int, guid: str,
                          since: datetime, exclude: set[str]) -> str | None:
    for record in ctx.sonarr.history(series_id, season):
        if record.get("eventType") != "grabbed":
            continue
        data = lower_keys(record.get("data"))
        if data.get("guid") != guid:
            continue
        when = parse_dt(record.get("date"))
        if when and when < since - timedelta(minutes=2):
            continue
        download_id = (record.get("downloadId") or "").lower()
        if download_id and download_id not in exclude:
            return download_id
    return None


def _same_location(a: dict, b: dict) -> bool:
    if a.get("content_path") and a.get("content_path") == b.get("content_path"):
        return True
    norm = lambda p: (p or "").rstrip("/\\")  # noqa: E731
    return bool(a.get("name")) and a.get("name") == b.get("name") \
        and norm(a.get("save_path")) == norm(b.get("save_path"))


def remove_old_versions(ctx: Context, new_hash: str, label: str) -> bool:
    """Delete (keeping files) other torrents that point at the same files as the new one.

    Returns False if the new torrent is not visible in qBittorrent yet, so the
    caller can try again later.
    """
    found = ctx.qbit.torrents(hashes=[new_hash])
    if not found or found[0].get("state") == "metaDL":
        return False
    new = found[0]
    pool = ctx.qbit.torrents(category=new.get("category") or None)
    old = [t for t in pool
           if (t.get("hash") or "").lower() != new_hash and _same_location(t, new)]
    if not old:
        log.info("%s: у qBittorrent немає старої версії в тій самій папці (%s). "
                 "Якщо роздавальник змінив назву папки, торрент качається повністю.",
                 label, new.get("content_path") or new.get("name"))
        return True
    for torrent in old:
        if ctx.cfg.dry_run:
            log.info("[DRY RUN] %s: прибрав би стару версію торрента «%s», файли лишаються",
                     label, torrent.get("name"))
        else:
            ctx.qbit.delete_keep_files(torrent["hash"])
            log.info("%s: стару версію торрента «%s» прибрано з qBittorrent, файли лишились",
                     label, torrent.get("name"))
    return True


def handle_season(ctx: Context, series_id: int, season: int, title: str,
                  targets: list[dict]) -> None:
    cfg, sonarr = ctx.cfg, ctx.sonarr
    label = f"{title} S{season:02d}"
    log.info("%s: бракує %s, шукаю оновлений реліз…", label, fmt_nums(t["num"] for t in targets))

    previous: list[dict] = []
    known_hashes: set[str] = set()
    for record in sonarr.history(series_id, season):
        if record.get("eventType") != "grabbed":
            continue
        data = lower_keys(record.get("data"))
        for value in (record.get("downloadId"), data.get("torrentinfohash")):
            if value:
                known_hashes.add(str(value).lower())
        previous.append({
            "keys": history_topic_keys(data),
            "size": to_int(data.get("size")),
            "published": parse_dt(data.get("publisheddate")),
            "group": str(data.get("releasegroup") or "").strip().lower(),
            "date": parse_dt(record.get("date")) or datetime.min.replace(tzinfo=UTC),
        })
    previous.sort(key=lambda p: p["date"], reverse=True)

    ctx.state["last_search"][f"{series_id}:{season}"] = utcnow().isoformat()
    releases = sonarr.search_season(series_id, season)

    candidates = []
    for index, rel in enumerate(releases):
        candidate, reason = evaluate_release(ctx, rel, season, targets, previous, known_hashes)
        if candidate:
            candidate["index"] = index
            candidates.append(candidate)
            ctx.detail("  + %s — підходить: %s (%s; %s)", rel.get("title"),
                       fmt_nums(t["num"] for t in candidate["covered"]), candidate["source"],
                       candidate["affinity_note"])
        else:
            ctx.detail("  - %s — %s", rel.get("title"), reason)

    if not candidates:
        log.info("%s: серед %d релізів немає оновлення з потрібними серіями", label, len(releases))
        return

    # The release already on disk first (same dub, its files get re-used), then
    # the one with the most missing episodes, then Sonarr's own preference order.
    best = max(candidates, key=lambda c: (c["affinity"], len(c["covered"]), -c["index"]))
    rel = best["rel"]
    covered = best["covered"]
    log.info("%s: беру «%s» [%s] — містить %s (за: %s; %s)", label, rel.get("title"),
             rel.get("indexer"), fmt_nums(t["num"] for t in covered), best["source"],
             best["affinity_note"])

    if cfg.dry_run:
        log.info("[DRY RUN] %s: реліз не взято, бо увімкнено DRY_RUN", label)
        return

    started = utcnow()
    sonarr.grab(rel["guid"], rel["indexerId"])
    entry = {
        "series_id": series_id,
        "season": season,
        "label": label,
        "title": rel.get("title"),
        "guid": rel.get("guid"),
        "indexer_id": rel.get("indexerId"),
        "fingerprint": fingerprint(rel),
        "target_episode_ids": [t["id"] for t in covered],
        "target_numbers": [t["num"] for t in covered],
        "known_hashes": sorted(known_hashes),
        "grabbed_at": started.isoformat(),
        "download_id": None,
        "old_checked": not (ctx.qbit and cfg.remove_old),
        "queue_done": not cfg.cleanup_queue,
        "done": False,
    }
    ctx.state["grabs"].append(entry)
    ctx.save()
    log.info("%s: Sonarr передав реліз у qBittorrent", label)

    deadline = time.monotonic() + cfg.grab_wait
    while time.monotonic() < deadline:
        entry["download_id"] = find_grab_download_id(
            ctx, series_id, season, entry["guid"], started, known_hashes)
        if entry["download_id"]:
            break
        time.sleep(ctx.poll_seconds)
    if not entry["download_id"]:
        log.info("%s: Sonarr ще не записав граб в історію, перевірю наступного запуску", label)
        ctx.save()
        return

    if not entry["old_checked"]:
        while time.monotonic() < deadline + cfg.grab_wait:
            try:
                if remove_old_versions(ctx, entry["download_id"], label):
                    entry["old_checked"] = True
                    break
            except ApiError as exc:
                log.warning("%s: %s", label, exc)
                break
            time.sleep(ctx.poll_seconds)
    ctx.save()


def season_still_airing(episodes: list, now: datetime) -> bool:
    """True while the season has episodes that have not aired yet (or have no date)."""
    for ep in episodes:
        air = parse_dt(ep.get("airDateUtc"))
        if air is None or air > now:
            return True
    return False


def notify_imports(ctx: Context) -> None:
    """Telegram message for every new episode Sonarr imported in a season that is still airing.

    Covers episodes this service grabbed and episodes Sonarr grabbed itself.
    The season finale and seasons that have finished airing are left to
    Seerr, which announces a season once it is complete.
    """
    state = ctx.state
    now = utcnow()
    since = parse_dt(state.get("notify_since"))
    if since is None:
        # First start: don't announce what was imported before the service existed.
        state["notify_since"] = now.isoformat()
        log.info("Сповіщення: стежу за новими імпортами, починаючи з цього моменту")
        return

    records = ctx.sonarr.history_since(since - timedelta(minutes=5))  # overlap; deduped below
    notified: dict = state.setdefault("notified", {})
    upgrades = set()
    imports = []
    for record in records:
        event = record.get("eventType")
        if event == "episodeFileDeleted":
            if str(lower_keys(record.get("data")).get("reason", "")).lower() == "upgrade":
                upgrades.add(record.get("episodeId"))
        elif event == "downloadFolderImported":
            imports.append(record)

    groups: dict[tuple[int, int], dict] = {}
    for record in imports:
        episode = record.get("episode") or {}
        episode_id = record.get("episodeId") or episode.get("id")
        season = episode.get("seasonNumber")
        if (not isinstance(episode_id, int) or str(episode_id) in notified
                or episode_id in upgrades or not isinstance(season, int) or season == 0):
            continue
        group = groups.setdefault((record.get("seriesId"), season), {
            "series": record.get("series") or {}, "ids": [], "dates": []})
        if episode_id not in group["ids"]:
            group["ids"].append(episode_id)
            group["dates"].append(parse_dt(record.get("date")) or now)

    retry_from = None
    for (series_id, season), group in groups.items():
        series = group["series"] if group["series"].get("title") else ctx.sonarr.series(series_id)
        label = f"{series.get('title') or series_id} S{season:02d}"
        season_episodes = ctx.sonarr.episodes(series_id, season)
        by_id = {e.get("id"): e for e in season_episodes}
        episodes = [by_id[i] for i in group["ids"] if i in by_id]
        if not ctx.cfg.notify_all_seasons and not season_still_airing(season_episodes, now):
            ctx.detail("%s: сезон уже вийшов повністю, про %s сповістить Seerr", label,
                       fmt_nums(e.get("episodeNumber", 0) for e in episodes))
        elif episodes:
            text, poster, button = build_episode_message(series, season, episodes,
                                                         ctx.cfg.seerr_url)
            numbers = fmt_nums(e.get("episodeNumber", 0) for e in episodes)
            if ctx.cfg.dry_run:
                log.info("[DRY RUN] %s: надіслав би сповіщення про %s", label, numbers)
            else:
                try:
                    ctx.telegram.send(text, poster, button)
                except ApiError as exc:
                    log.warning("%s: %s; повторю наступного разу", label, exc)
                    earliest = min(group["dates"])
                    retry_from = earliest if retry_from is None else min(retry_from, earliest)
                    continue
                log.info("%s: надіслано сповіщення в Telegram про %s", label, numbers)
        for episode_id in group["ids"]:
            notified[str(episode_id)] = now.isoformat()

    state["notify_since"] = (retry_from or now).isoformat()
    cutoff = now - timedelta(days=60)
    for key in [k for k, v in notified.items() if (parse_dt(v) or now) < cutoff]:
        del notified[key]


def process_pending(ctx: Context) -> None:
    """Finish earlier grabs: find the download, remove old torrents, clean the queue."""
    pending = [g for g in ctx.state["grabs"] if not g.get("done")]
    now = utcnow()
    queue = None
    for grab in pending:
        label = grab.get("label") or grab.get("title")
        age = now - (parse_dt(grab.get("grabbed_at")) or now)
        episodes_cache: list | None = None

        def episodes() -> list:
            nonlocal episodes_cache
            if episodes_cache is None:
                episodes_cache = ctx.sonarr.episodes(grab["series_id"], grab["season"])
            return episodes_cache

        try:
            if not grab.get("download_id"):
                grab["download_id"] = find_grab_download_id(
                    ctx, grab["series_id"], grab["season"], grab["guid"],
                    parse_dt(grab["grabbed_at"]) or now, set(grab.get("known_hashes") or []))
            download_id = grab.get("download_id")

            if download_id and not grab.get("old_checked"):
                if ctx.qbit and ctx.cfg.remove_old:
                    grab["old_checked"] = remove_old_versions(ctx, download_id, label)
                else:
                    grab["old_checked"] = True

            if download_id and not grab.get("queue_done"):
                if queue is None:
                    queue = ctx.sonarr.queue()
                records = [r for r in queue if (r.get("downloadId") or "").lower() == download_id]
                if not records:
                    if age > timedelta(minutes=10):
                        grab["queue_done"] = True
                else:
                    record = records[0]
                    tracked = (record.get("trackedDownloadState") or "").lower()
                    finished = (record.get("status") or "").lower() == "completed"
                    if tracked == "imported":
                        grab["queue_done"] = True
                    elif finished and tracked in ("importblocked", "importpending"):
                        have = {e["id"] for e in episodes() if e.get("hasFile")}
                        if set(grab["target_episode_ids"]) <= have:
                            if ctx.cfg.dry_run:
                                log.info("[DRY RUN] %s: прибрав би запис із черги Sonarr", label)
                            else:
                                ctx.sonarr.remove_queue_item(record["id"])
                                log.info("%s: нові серії імпортовано (%s), запис «Import blocked» "
                                         "прибрано з черги, торрент далі роздається", label,
                                         fmt_nums(grab.get("target_numbers") or []))
                            grab["queue_done"] = True
        except ApiError as exc:
            log.warning("%s: %s", label, exc)

        if grab.get("old_checked") and grab.get("queue_done"):
            grab["done"] = True
        elif age > timedelta(days=7):
            log.warning("%s: за тиждень не вдалося завершити обробку грабу, припиняю стежити", label)
            grab["done"] = True

    # Keep finished entries for a month: they stop the same release being grabbed twice.
    cutoff = now - timedelta(days=30)
    ctx.state["grabs"] = [g for g in ctx.state["grabs"]
                          if not g.get("done") or (parse_dt(g.get("grabbed_at")) or now) > cutoff]


def run_once(ctx: Context, search: bool = True) -> None:
    """One pass: finish earlier grabs, send notifications, and (if `search`) look for updates."""
    process_pending(ctx)
    if ctx.telegram:
        try:
            notify_imports(ctx)
        except ApiError as exc:
            log.warning("Сповіщення: %s", exc)
    if search:
        search_pass(ctx)
    ctx.save()


def search_pass(ctx: Context) -> None:
    cfg = ctx.cfg
    now = utcnow()
    groups: dict[tuple[int, int], str] = {}
    for ep in ctx.sonarr.missing():
        series_id, season = ep.get("seriesId"), ep.get("seasonNumber")
        if not isinstance(series_id, int) or not isinstance(season, int) or season == 0:
            continue
        groups[(series_id, season)] = (ep.get("series") or {}).get("title") or f"Серіал {series_id}"
    if not groups:
        log.info("Пропущених серій немає")
        return

    active: set[int] = set()
    for record in ctx.sonarr.queue():
        if is_active_download(record):
            active |= queue_episode_ids(record)

    work = []
    for (series_id, season), title in groups.items():
        ongoing, targets = analyse_season(ctx.sonarr.episodes(series_id, season), now, cfg)
        label = f"{title} S{season:02d}"
        if not ongoing:
            ctx.detail("%s: сезон не онгоїнг, пропускаю (це звичайна робота Sonarr)", label)
            continue
        if not targets:
            continue
        if any(t["id"] in active for t in targets):
            log.info("%s: потрібні серії вже качаються, чекаю", label)
            continue
        work.append((series_id, season, title, targets))

    work.sort(key=lambda w: ctx.state["last_search"].get(f"{w[0]}:{w[1]}", ""))
    for series_id, season, title, targets in work[:cfg.max_searches]:
        try:
            handle_season(ctx, series_id, season, title, targets)
        except ApiError as exc:
            log.error("%s S%02d: %s", title, season, exc)
        ctx.save()
    if len(work) > cfg.max_searches:
        log.info("Ще %d сезонів перевірю наступного разу (MAX_SEARCHES_PER_RUN=%d)",
                 len(work) - cfg.max_searches, cfg.max_searches)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Докачує нові серії онгоїнгів з оновлюваних торрентів через Sonarr")
    parser.add_argument("--once", action="store_true", help="один прохід і вихід")
    parser.add_argument("--dry-run", action="store_true",
                        help="лише показати, що було б зроблено (перекриває DRY_RUN)")
    parser.add_argument("--verbose", action="store_true", help="докладні логи")
    args = parser.parse_args(argv)

    cfg = Config()
    if args.dry_run:
        cfg.dry_run = True
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not cfg.sonarr_url or not cfg.sonarr_key:
        log.error("Потрібно задати SONARR_URL і SONARR_API_KEY")
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    sonarr = Sonarr(cfg.sonarr_url, cfg.sonarr_key, cfg.search_timeout)
    qbit = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass) if cfg.qbit_url else None

    log.info("sonarr-ongoing-updater %s%s", __version__, " [DRY RUN]" if cfg.dry_run else "")
    try:
        log.info("Sonarr %s: з'єднання є", sonarr.status().get("version", "?"))
    except ApiError as exc:
        log.error("%s", exc)
    if qbit:
        try:
            log.info("qBittorrent %s: з'єднання є", qbit.version())
        except ApiError as exc:
            log.error("%s", exc)
    else:
        log.warning("QBIT_URL не задано: старі версії торрентів не прибиратимуться автоматично")

    telegram = None
    if cfg.telegram_token and cfg.telegram_chat_ids:
        telegram = Telegram(cfg.telegram_api, cfg.telegram_token, cfg.telegram_chat_ids)
        log.info("Сповіщення в Telegram: увімкнено (%d чат.)", len(cfg.telegram_chat_ids))
    else:
        log.info("Сповіщення в Telegram вимкнено: не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")

    ctx = Context(cfg, sonarr, qbit, load_state(cfg.state_file), telegram=telegram)
    # Imports, notifications and cleanup every CHECK_MINUTES; tracker searches
    # only every INTERVAL_MINUTES so the trackers aren't hammered.
    tick = max(1, min(cfg.check_minutes, cfg.interval))
    last_search: float | None = None
    while True:
        search = last_search is None or time.monotonic() - last_search >= cfg.interval * 60 - 30
        try:
            run_once(ctx, search=search)
        except ApiError as exc:
            log.error("%s", exc)
        except Exception:  # keep the service alive whatever happens in one pass
            log.exception("Непередбачена помилка під час проходу")
        if search:
            last_search = time.monotonic()
            log.info("Наступний пошук оновлень через %d хв", cfg.interval)
        if args.once:
            return 0
        time.sleep(tick * 60)


if __name__ == "__main__":
    sys.exit(main())
