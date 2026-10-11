"""Tests for ongoing_updater against mock Sonarr, qBittorrent and Telegram servers.

Run: python3 -m unittest discover -s tests -v
"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ongoing_updater.py"
spec = importlib.util.spec_from_file_location("ongoing_updater", SCRIPT)
ou = importlib.util.module_from_spec(spec)
sys.modules["ongoing_updater"] = ou
spec.loader.exec_module(ou)

NOW = ou.utcnow()
TOPIC_A = "https://toloka.example/t100"
POSTER = "https://image.tmdb.org/t/p/original/poster.jpg"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.1234567Z")  # 7 fraction digits, like .NET


class World:
    def __init__(self):
        self.series = {"id": 1, "title": "Test Show", "tmdbId": 12345,
                       "images": [{"coverType": "poster", "remoteUrl": POSTER}]}
        self.episodes = []
        self.history = []
        self.releases = []
        self.queue = []
        self.torrents = []
        self.grabbed = []
        self.searches = 0
        self.deleted_queue = []
        self.deleted_torrents = []
        self.on_grab = None
        self.telegram = []
        self.photo_fails = False
        self.button_fails = False
        self.imports = []  # history records served by /history/since


def record_import(world, episode_ids, date=None, upgrade=False):
    """Simulate Sonarr importing episode files (what /history/since returns)."""
    when = iso(date or ou.utcnow())
    for eid in episode_ids:
        ep = next(e for e in world.episodes if e["id"] == eid)
        ep["hasFile"] = True
        if upgrade:
            world.imports.append({"eventType": "episodeFileDeleted", "episodeId": eid,
                                  "seriesId": 1, "date": when, "data": {"Reason": "Upgrade"}})
        world.imports.append({"eventType": "downloadFolderImported", "episodeId": eid,
                              "seriesId": 1, "date": when, "data": {},
                              "episode": {k: ep[k] for k in ("id", "seasonNumber",
                                                             "episodeNumber", "title")},
                              "series": world.series})


def make_episodes(files_upto, aired_upto, total=10, last_air=timedelta(days=1)):
    eps = []
    for n in range(1, total + 1):
        if n <= aired_upto:
            air = NOW - last_air - timedelta(weeks=aired_upto - n)
        else:
            air = NOW + timedelta(weeks=n - aired_upto)
        eps.append({"id": 100 + n, "seriesId": 1, "seasonNumber": 1, "episodeNumber": n,
                    "title": f"Episode {n}" if n != 7 else "TBA",
                    "airDateUtc": iso(air), "monitored": True, "hasFile": n <= files_upto})
    return eps


def make_handlers(world):
    class Json(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _parse(self):
            u = urllib.parse.urlparse(self.path)
            return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    class Sonarr(Json):
        def _auth(self):
            if self.headers.get("X-Api-Key") != "key":
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            path, q = self._parse()
            if path == "/api/v3/system/status":
                return self._send(200, {"version": "4.0.15.2941"})
            if path == "/api/v3/series/1":
                return self._send(200, world.series)
            if path == "/api/v3/wanted/missing":
                recs = [{**e, "series": {"title": world.series["title"]}} for e in world.episodes
                        if e["monitored"] and not e["hasFile"]
                        and ou.parse_dt(e["airDateUtc"]) <= ou.utcnow()]
                return self._send(200, {"page": 1, "totalRecords": len(recs), "records": recs})
            if path == "/api/v3/episode":
                return self._send(200, [e for e in world.episodes
                                        if e["seasonNumber"] == int(q["seasonNumber"])])
            if path == "/api/v3/history/series":
                return self._send(200, world.history)
            if path == "/api/v3/history/since":
                since = ou.parse_dt(q["date"])
                return self._send(200, [r for r in world.imports
                                        if ou.parse_dt(r["date"]) >= since])
            if path == "/api/v3/queue":
                return self._send(200, {"page": 1, "totalRecords": len(world.queue),
                                        "records": world.queue})
            if path == "/api/v3/release":
                world.searches += 1
                return self._send(200, world.releases)
            self._send(404)

        def do_POST(self):
            if not self._auth():
                return
            path, _ = self._parse()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if path == "/api/v3/release":
                world.grabbed.append(body)
                if world.on_grab:
                    world.on_grab(world, body)
                return self._send(200, body)
            self._send(404)

        def do_DELETE(self):
            if not self._auth():
                return
            path, q = self._parse()
            if path.startswith("/api/v3/queue/"):
                qid = int(path.rsplit("/", 1)[1])
                world.deleted_queue.append((qid, q))
                rec = next(r for r in world.queue if r["id"] == qid)
                world.queue = [r for r in world.queue if r["downloadId"] != rec["downloadId"]]
                return self._send(200)
            self._send(404)

    class QBit(Json):
        def _text(self, code, text="", cookie=None):
            body = text.encode()
            self.send_response(code)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return "SID=secret" in (self.headers.get("Cookie") or "")

        def do_GET(self):
            path, q = self._parse()
            if not self._authed():
                return self._text(403, "Forbidden")
            if path == "/api/v2/app/version":
                return self._text(200, "v5.0.4")
            if path == "/api/v2/torrents/info":
                ts = world.torrents
                if "hashes" in q:
                    hs = q["hashes"].split("|")
                    ts = [t for t in ts if t["hash"] in hs]
                if "category" in q:
                    ts = [t for t in ts if t["category"] == q["category"]]
                return self._text(200, json.dumps(ts))
            self._text(404)

        def do_POST(self):
            path, _ = self._parse()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(
                self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
            if path == "/api/v2/auth/login":
                if form.get("username") == "admin" and form.get("password") == "pw":
                    return self._text(200, "Ok.", cookie="SID=secret; HttpOnly; path=/")
                return self._text(200, "Fails.")
            if not self._authed():
                return self._text(403, "Forbidden")
            if path == "/api/v2/torrents/delete":
                world.deleted_torrents.append((form["hashes"], form["deleteFiles"]))
                world.torrents = [t for t in world.torrents if t["hash"] != form["hashes"]]
                return self._text(200)
            self._text(404)

    class Telegram(Json):
        def do_POST(self):
            path, _ = self._parse()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = path.rsplit("/", 1)[1]
            if not path.startswith("/botTOKEN/"):
                return self._send(401, {"ok": False})
            if method == "sendPhoto" and world.photo_fails:
                return self._send(400, {"ok": False, "description": "wrong file identifier"})
            if world.button_fails and "reply_markup" in body:
                return self._send(400, {"ok": False, "description": "BUTTON_URL_INVALID"})
            world.telegram.append((method, body))
            self._send(200, {"ok": True})

    return Sonarr, QBit, Telegram


OLD_TORRENT = {"hash": "aaaa1111", "name": "Test.Show.S01.1080p", "save_path": "/downloads/tv/",
               "content_path": "/downloads/tv/Test.Show.S01.1080p", "category": "tv-sonarr",
               "state": "uploading"}
OTHER_TORRENT = {"hash": "cccc3333", "name": "Other.Show.S02", "save_path": "/downloads/tv/",
                 "content_path": "/downloads/tv/Other.Show.S02", "category": "tv-sonarr",
                 "state": "uploading"}


def old_grab_history(group="UkrDub"):
    return [{"eventType": "grabbed", "downloadId": "AAAA1111", "episodeId": 101,
             "date": iso(NOW - timedelta(days=8)),
             "data": {"Guid": TOPIC_A, "NzbInfoUrl": TOPIC_A, "Size": "5000000000",
                      "PublishedDate": iso(NOW - timedelta(days=8)), "ReleaseGroup": group,
                      "TorrentInfoHash": "aaaa1111", "Indexer": "Toloka (Prowlarr)"}}]


def release(title, guid=TOPIC_A, size=6_000_000_000, eps=None, full=False,
            rejections=("Existing file on disk is of equal or higher preference: WEBDL-1080p v1",),
            protocol="torrent", published=timedelta(hours=12), indexer_id=5, group=None):
    return {"guid": guid, "infoUrl": guid, "indexerId": indexer_id, "indexer": "Toloka (Prowlarr)",
            "title": title, "fullSeason": full, "seasonNumber": 1, "releaseGroup": group,
            "episodeNumbers": list(eps or []), "protocol": protocol, "size": size,
            "publishDate": iso(NOW - published), "rejected": bool(rejections),
            "rejections": list(rejections), "downloadAllowed": True}


def default_on_grab(world, body):
    world.history.append({"eventType": "grabbed", "downloadId": "BBBB2222", "episodeId": 101,
                          "date": iso(ou.utcnow()),
                          "data": {"Guid": body["guid"], "Size": "6000000000"}})
    world.torrents.append({**OLD_TORRENT, "hash": "bbbb2222", "state": "checkingUP"})
    for ep in world.episodes:
        world.queue.append({"id": 900 + ep["episodeNumber"], "seriesId": 1,
                            "episodeId": ep["id"], "downloadId": "BBBB2222",
                            "status": "downloading", "trackedDownloadState": "downloading",
                            "trackedDownloadStatus": "ok"})


class Base(unittest.TestCase):
    def setUp(self):
        self.world = World()
        sh, qh, th = make_handlers(self.world)
        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (sh, qh, th)]
        for srv in self.servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        s, q, t = (srv.server_port for srv in self.servers)
        self.tmp = tempfile.mkdtemp()
        os.environ.update({
            "SONARR_URL": f"http://127.0.0.1:{s}", "SONARR_API_KEY": "key",
            "QBIT_URL": f"http://127.0.0.1:{q}", "QBIT_USERNAME": "admin", "QBIT_PASSWORD": "pw",
            "TELEGRAM_API_URL": f"http://127.0.0.1:{t}", "TELEGRAM_BOT_TOKEN": "TOKEN",
            "TELEGRAM_CHAT_ID": "111", "SEERR_URL": "https://seerr.example",
            "STATE_FILE": os.path.join(self.tmp, "state.json"), "GRAB_WAIT_SECONDS": "3",
        })
        for name in ("DRY_RUN", "OTHER_RELEASE_AFTER_HOURS", "NOTIFY_ALL_SEASONS"):
            os.environ.pop(name, None)

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def ctx(self, dry_run=False):
        cfg = ou.Config()
        cfg.dry_run = dry_run
        return ou.Context(cfg, ou.Sonarr(cfg.sonarr_url, cfg.sonarr_key),
                          ou.QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass),
                          ou.load_state(cfg.state_file), poll_seconds=0.05,
                          telegram=ou.Telegram(cfg.telegram_api, cfg.telegram_token,
                                               cfg.telegram_chat_ids))


class UpdatedTorrentFlow(Base):
    def setUp(self):
        super().setUp()
        w = self.world
        w.episodes = make_episodes(files_upto=5, aired_upto=6)
        w.history = old_grab_history()
        w.torrents = [copy.deepcopy(OLD_TORRENT), copy.deepcopy(OTHER_TORRENT)]
        w.releases = [
            release("Other group S01E01-06 1080p", guid="https://x/t200", eps=range(1, 7),
                    rejections=("Not enough seeders: 0 (minimum 1)",
                                "Existing file on disk is of equal or higher preference: WEBDL-1080p v1")),
            release("Old group S01E01-05 1080p", guid="https://x/t300", eps=range(1, 6)),
            release("Usenet S01E06", guid="nzb1", eps=[6], protocol="usenet", rejections=()),
            release("Queued S01E01-06", guid="https://x/t400", eps=range(1, 7),
                    rejections=("Release in queue already meets cutoff: WEBDL-1080p",)),
            release("Test Show S01E01-06 of 10 1080p", eps=range(1, 7)),
        ]
        w.on_grab = default_on_grab

    def test_full_cycle(self):
        w = self.world
        ctx = self.ctx()
        ou.run_once(ctx)
        self.assertEqual(w.grabbed, [{"guid": TOPIC_A, "indexerId": 5}])
        self.assertEqual(w.deleted_torrents, [("aaaa1111", "false")])
        self.assertEqual([t["hash"] for t in w.torrents], ["cccc3333", "bbbb2222"])
        grab = ctx.state["grabs"][0]
        self.assertEqual(grab["download_id"], "bbbb2222")
        self.assertEqual(grab["target_numbers"], [6])
        self.assertTrue(grab["old_checked"])
        self.assertFalse(grab["queue_done"])
        with open(ctx.cfg.state_file) as fh:
            self.assertEqual(len(json.load(fh)["grabs"]), 1)

        # While downloading: no new search, no notification yet.
        searches = w.searches
        ou.run_once(self.ctx())
        self.assertEqual(w.searches, searches)
        self.assertEqual(len(w.grabbed), 1)
        self.assertEqual(w.telegram, [])

        # Sonarr imported E06; old episodes are "not an upgrade" -> import blocked.
        record_import(w, [106])
        for r in w.queue:
            r.update(status="completed", trackedDownloadState="importBlocked",
                     trackedDownloadStatus="warning")
        ctx2 = self.ctx()
        ou.run_once(ctx2)
        self.assertEqual(len(w.deleted_queue), 1)
        self.assertEqual(w.deleted_queue[0][1], {"removeFromClient": "false", "blocklist": "false"})
        self.assertEqual(w.queue, [])
        self.assertTrue(ctx2.state["grabs"][0]["done"])
        self.assertEqual(len(w.grabbed), 1)
        self.assertEqual(w.searches, searches)

        # One Telegram message with poster, episode title and a Seerr button.
        self.assertEqual(len(w.telegram), 1)
        method, body = w.telegram[0]
        self.assertEqual(method, "sendPhoto")
        self.assertEqual(body["chat_id"], "111")
        self.assertEqual(body["photo"], POSTER)
        self.assertIn("Нова серія: Test Show", body["caption"])
        self.assertIn("S01E06 — Episode 6", body["caption"])
        self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://seerr.example/tv/12345")

        # Nothing repeats on later passes.
        ou.run_once(self.ctx())
        self.assertEqual(len(w.telegram), 1)

    def test_dry_run_changes_nothing(self):
        w = self.world
        ctx = self.ctx(dry_run=True)
        with self.assertLogs("ongoing-updater", "INFO") as logs:
            ou.run_once(ctx)
        self.assertEqual(w.grabbed, [])
        self.assertEqual(w.deleted_torrents, [])
        self.assertFalse(os.path.exists(ctx.cfg.state_file))
        text = "\n".join(logs.output)
        self.assertIn("беру «Test Show S01E01-06 of 10 1080p»", text)
        self.assertIn("та сама роздача", text)
        self.assertIn("Not enough seeders", text)
        self.assertIn("Release in queue", text)

    def test_tracker_not_updated_yet(self):
        w = self.world
        w.releases = [release("Test Show S01E01-05 of 10", size=5_000_000_000, eps=range(1, 6)),
                      release("Test Show S01 1080p", size=5_010_000_000, full=True)]
        ou.run_once(self.ctx())
        self.assertEqual(w.grabbed, [])
        self.assertEqual(w.searches, 1)

    def test_skip_when_already_downloading(self):
        w = self.world
        w.queue = [{"id": 1, "episodeId": 106, "downloadId": "ZZZ", "status": "downloading",
                    "trackedDownloadState": "downloading"}]
        ou.run_once(self.ctx())
        self.assertEqual(w.searches, 0)

    def test_old_season_left_to_sonarr(self):
        w = self.world
        w.episodes = make_episodes(files_upto=9, aired_upto=10, last_air=timedelta(days=400))
        ou.run_once(self.ctx())
        self.assertEqual(w.searches, 0)


class ReleasePriority(Base):
    """Different releases of a season usually have different dubs."""

    def setUp(self):
        super().setUp()
        w = self.world
        w.episodes = make_episodes(files_upto=5, aired_upto=7)  # E06 and E07 missing
        w.history = old_grab_history(group="UkrDub")
        w.torrents = [copy.deepcopy(OLD_TORRENT)]
        w.on_grab = default_on_grab
        self.other = release("Other dub S01E01-07 1080p", guid="https://x/t900", eps=range(1, 8),
                             group="OtherDub")

    def test_same_topic_wins_even_with_fewer_episodes(self):
        w = self.world
        w.releases = [self.other, release("Test Show S01E01-06 of 10", eps=range(1, 7))]
        ou.run_once(self.ctx())
        self.assertEqual([g["guid"] for g in w.grabbed], [TOPIC_A])

    def test_other_release_never_taken_by_default(self):
        w = self.world
        w.releases = [self.other]
        with self.assertLogs("ongoing-updater", "INFO"):
            ou.run_once(self.ctx(dry_run=True))
        ou.run_once(self.ctx())
        self.assertEqual(w.grabbed, [])

    def test_other_release_after_timeout(self):
        w = self.world
        w.releases = [self.other]
        os.environ["OTHER_RELEASE_AFTER_HOURS"] = "48"  # E06 is ~8 days late
        ou.run_once(self.ctx())
        self.assertEqual([g["guid"] for g in w.grabbed], ["https://x/t900"])

    def test_other_release_timeout_not_reached(self):
        w = self.world
        w.episodes = make_episodes(files_upto=5, aired_upto=6)  # only E06, aired 1 day ago
        w.releases = [self.other]
        os.environ["OTHER_RELEASE_AFTER_HOURS"] = "48"
        ou.run_once(self.ctx())
        self.assertEqual(w.grabbed, [])

    def test_same_release_group_allowed(self):
        w = self.world
        w.releases = [release("UkrDub new topic S01E01-07", guid="https://x/t777",
                              eps=range(1, 8), group="UkrDub")]
        ou.run_once(self.ctx())
        self.assertEqual([g["guid"] for g in w.grabbed], ["https://x/t777"])


class FullSeasonTitles(Base):
    def setUp(self):
        super().setUp()
        self.world.on_grab = default_on_grab
        self.world.torrents = [copy.deepcopy(OLD_TORRENT)]

    def test_unparseable_pack_uses_publish_date(self):
        w = self.world
        w.episodes = make_episodes(files_upto=5, aired_upto=6)
        w.history = old_grab_history()
        w.releases = [release("Test Show Season 1 1080p", full=True)]
        ou.run_once(self.ctx())
        self.assertEqual(len(w.grabbed), 1)

    def test_unparseable_pack_published_before_episode(self):
        w = self.world
        w.episodes = make_episodes(files_upto=5, aired_upto=6)
        w.history = old_grab_history()
        w.releases = [release("Test Show Season 1 1080p", full=True, published=timedelta(days=2))]
        ou.run_once(self.ctx())
        self.assertEqual(w.grabbed, [])

    def test_fresh_request_of_ongoing_season(self):
        w = self.world
        w.torrents = []
        w.episodes = make_episodes(files_upto=0, aired_upto=3)
        w.releases = [release("Тест / Test Show / Сезон: 1 / Серии: 1-3 из 10 [2026, WEB-DL 1080p]",
                              guid="https://x/t500", full=True, rejections=("Full season pack",))]
        ctx = self.ctx()
        ou.run_once(ctx)
        self.assertEqual(w.grabbed, [{"guid": "https://x/t500", "indexerId": 5}])
        self.assertEqual(ctx.state["grabs"][0]["target_numbers"], [1, 2, 3])
        self.assertEqual(w.deleted_torrents, [])

        # Import happens; poster fails -> falls back to a text message listing all three.
        record_import(w, [101, 102, 103])
        w.photo_fails = True
        ou.run_once(self.ctx())
        self.assertEqual(len(w.telegram), 1)
        method, body = w.telegram[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Нові серії: Test Show", body["text"])
        for n in (1, 2, 3):
            self.assertIn(f"S01E0{n} — Episode {n}", body["text"])


class Parsing(unittest.TestCase):
    def test_titles(self):
        cases = {
            "Test Show S01E01-06 of 10 1080p": set(range(1, 7)),
            "Шоу / Show / Сезон: 1 / Серии: 1-6 из 10 [2026, WEB-DL 1080p]": set(range(1, 7)),
            "Шоу (Сезон 1, серії 1-8 з 12) / Show (2026) WEB-DL 1080p": set(range(1, 9)),
            "Шоу (Сезон 1, 1-8 серії) 2026": set(range(1, 9)),
            "Show.S01E05.1080p.WEB": {5},
            "Show S01E01-E04 1080p": set(range(1, 5)),
            "Show S01E01-S01E04": set(range(1, 5)),
            "Шоу [1-4 из 8] 2026": set(range(1, 5)),
            "Шоу [1-4 of ??]": set(range(1, 5)),
            "Шоу, Серия 3 из 10": {3},
            "Show S01 1080p [2025-2026]": set(),
            "Show S01 2025-2026 серии": set(),
            "Show S01E01-1080p": set(),
            "Show Season 1 Complete": set(),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(ou.parse_title_episodes(title), expected)

    def test_rejections(self):
        cfg = ou.Config()
        ok = ["Existing file on disk is of equal or higher preference: WEBDL-1080p v1",
              "Existing file on disk has a equal or higher Custom Format score: 0",
              "Existing file meets cutoff: WEBDL-1080p",
              "Not a Custom Format upgrade for existing episode file(s)",
              "Full season pack",
              "Existing file on disk and Quality Profile 'HD' does not allow upgrades"]
        bad = ["Release in queue already meets cutoff: WEBDL-1080p",
               "Grab event in history already meets cutoff: WEBDL-1080p",
               "Release is blocklisted", "Not enough seeders: 0 (minimum 1)",
               "Unknown Series", "Language is not wanted in profile"]
        for r in ok:
            self.assertTrue(ou.is_overridable(r, cfg), r)
        for r in bad:
            self.assertFalse(ou.is_overridable(r, cfg), r)

    def test_parse_dt(self):
        self.assertIsNotNone(ou.parse_dt("2026-10-09T12:34:56.1234567Z"))
        self.assertIsNotNone(ou.parse_dt("2026-10-09T12:34:56Z"))
        self.assertIsNone(ou.parse_dt(None))

    def test_message_escapes_html(self):
        text, poster, button = ou.build_episode_message(
            {"title": "Tom & Jerry <3"}, 2, [{"episodeNumber": 4, "title": "A < B"}], "")
        self.assertIn("Tom &amp; Jerry &lt;3", text)
        self.assertIn("S02E04 — A &lt; B", text)
        self.assertIsNone(poster)
        self.assertIsNone(button)


class Notifications(Base):
    """Which imports produce a Telegram message."""

    def setUp(self):
        super().setUp()
        self.world.episodes = make_episodes(files_upto=5, aired_upto=6)  # E07-E10 still ahead

    def baseline(self):
        ou.run_once(self.ctx(), search=False)  # first start: remember "now", announce nothing

    def test_import_before_first_start_is_not_announced(self):
        record_import(self.world, [106], date=NOW - timedelta(hours=1))
        self.baseline()
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(self.world.telegram, [])

    def test_episode_grabbed_by_sonarr_itself_is_announced(self):
        self.baseline()
        record_import(self.world, [106])
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(len(self.world.telegram), 1)
        self.assertIn("S01E06 — Episode 6", self.world.telegram[0][1]["caption"])
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(len(self.world.telegram), 1)  # never twice

    def test_finished_season_left_to_seerr(self):
        self.world.episodes = make_episodes(files_upto=9, aired_upto=10)
        self.baseline()
        record_import(self.world, [110])  # the finale
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(self.world.telegram, [])

    def test_finished_season_with_notify_all(self):
        os.environ["NOTIFY_ALL_SEASONS"] = "true"
        self.world.episodes = make_episodes(files_upto=9, aired_upto=10)
        self.baseline()
        record_import(self.world, [110])
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(len(self.world.telegram), 1)

    def test_upgrade_is_not_announced(self):
        self.baseline()
        record_import(self.world, [105], upgrade=True)
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(self.world.telegram, [])

    def test_telegram_down_retries_later(self):
        self.baseline()
        record_import(self.world, [106])
        os.environ["TELEGRAM_BOT_TOKEN"] = "WRONG"
        with self.assertLogs("ongoing-updater", "WARNING"):
            ou.run_once(self.ctx(), search=False)
        self.assertEqual(self.world.telegram, [])
        os.environ["TELEGRAM_BOT_TOKEN"] = "TOKEN"
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(len(self.world.telegram), 1)

    def test_check_without_search_does_not_hit_tracker(self):
        self.baseline()
        ou.run_once(self.ctx(), search=False)
        self.assertEqual(self.world.searches, 0)


class TelegramFallback(Base):
    def test_invalid_button_url_falls_back_to_plain_text(self):
        self.world.button_fails = True
        self.ctx().telegram.send("hi", "https://img/x.jpg", ("Відкрити", "http://localhost:5055/tv/1"))
        self.assertEqual(len(self.world.telegram), 1)
        method, body = self.world.telegram[0]
        self.assertEqual(method, "sendMessage")
        self.assertNotIn("reply_markup", body)

    def test_all_attempts_fail_raises(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "WRONG"
        with self.assertRaises(ou.ApiError):
            self.ctx().telegram.send("hi")


class Cli(Base):
    def test_main_once_dry_run(self):
        self.world.episodes = make_episodes(files_upto=5, aired_upto=6)
        self.world.releases = [release("Test Show S01E01-06 of 10 1080p", eps=range(1, 7))]
        with self.assertLogs("ongoing-updater", "INFO"):
            rc = ou.main(["--once", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.world.grabbed, [])
        self.assertEqual(self.world.searches, 1)

    def test_missing_config(self):
        os.environ["SONARR_API_KEY"] = ""
        with self.assertLogs("ongoing-updater", "ERROR"):
            self.assertEqual(ou.main(["--once"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
