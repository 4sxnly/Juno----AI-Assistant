"""
Spotify integration — plays YOUR music first.

When you say "play <something>", Juno looks in this order:
  1. your vibes            ("play maidcore" → a mix of the artists you said maidcore means)
  2. your playlists        ("play Phonkaphone")
  3. your Liked Songs      ("play Join Me In Death" → the version YOU liked, never a same-named song by
                            someone else; it keeps playing through your Liked Songs afterwards)
  4. artists you listen to ("play Yeat")
  5. close matches in your library (typos, "the", feat. tags…)
  6. a Spotify-wide search — a song TITLE beats an artist you've never heard of
     ("play City of Angels" plays the song), unless Spotify clearly means the artist.
"X by Y" pins the artist; "playlist", "song", "album" or "artist" in the request narrows what it looks for.

Also: queue ("add X to the queue", "play X next"), like / unlike, add to a playlist, repeat, seek, restart,
move playback to another device, and teach it vibes ("when I say maidcore, play Yakui The Maid, Chikoi, Kichi").

Without a connected account Juno can still open Spotify, pause, skip and open a search.
Playback control needs Spotify Premium (Spotify's rule for apps).
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import http.server
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .integrations import OPEN, Action, ActionResult, Field, Integration, IntegrationAPI

REDIRECT = "http://127.0.0.1:8737/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-library-read user-library-modify playlist-read-private playlist-read-collaborative "
          "playlist-modify-private playlist-modify-public user-follow-read")
LIBRARY_SCOPES = ("user-library-read", "playlist-read-private")
FULL_SYNC_SECONDS = 12 * 3600
GENRE_TAIL = re.compile(r"\s+(?:music|songs|vibes?|type beats?|type stuff|stuff|playlist mix|mix)$", re.I)
DEVICE_WORDS = {"phone": "Smartphone", "mobile": "Smartphone", "cell": "Smartphone", "pc": "Computer",
                "computer": "Computer", "laptop": "Computer", "desktop": "Computer", "speaker": "Speaker",
                "tv": "TV", "television": "TV", "tablet": "Tablet", "ipad": "Tablet", "console": "GameConsole",
                "xbox": "GameConsole", "playstation": "GameConsole", "car": "Automobile", "echo": "Speaker",
                "alexa": "Speaker"}


def norm(text: str) -> str:
    """'Join Me In Death (Remastered 2021) - Live' → 'join me in death'."""
    text = (text or "").lower().replace("’", "'").replace("&", " and ")
    text = re.sub(r"\((?:feat|ft|with|prod)[^)]*\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"\s+-\s+(?:remaster|live|radio|single|album|mono|stereo|edit|version|sped|slowed).*$", "", text)
    text = re.sub(r"\((?:[^)]*(?:remaster|version|edit|live|mix|sped|slowed)[^)]*)\)", " ", text)
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"^the\s+", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def split_artists(text: str) -> list[str]:
    """'artists like Yakui The Maid, Chikoi & Kichi' → ['Yakui The Maid', 'Chikoi', 'Kichi']."""
    text = re.sub(r"^(?:(?:artists?|people|stuff|songs|music|things)\s+)?(?:like|by|from|such as)\s+", "",
                  text.strip(), flags=re.I)
    text = re.sub(r"\s+(?:etc\.?|and stuff|and so on|and more|and others|n stuff|n shi|lol|lmao)$", "", text,
                  flags=re.I)
    if re.search(r"[,;/]", text):    # with commas, "and" may be part of a name ("Simon and Garfunkel")
        parts = [re.sub(r"^(?:and|&|n)\s+", "", p.strip(), flags=re.I) for p in re.split(r"\s*[,;/]\s*", text)]
    else:
        parts = re.split(r"\s*(?:\+|\band\b|&|\bn\b)\s*", text, flags=re.I)
    out, seen = [], set()
    for part in parts:
        part = part.strip(" .!?\"'")
        if part and part.lower() not in seen and part.lower() not in ("etc", "more", "others"):
            seen.add(part.lower())
            out.append(part)
    return out


def track_of(item: dict[str, Any]) -> dict[str, Any]:
    """Saved-track / playlist entries call the track 'track' (old) or 'item' (2026 API)."""
    return item.get("track") or item.get("item") or {}


class SpotifyError(RuntimeError):
    pass


class Spotify(Integration):
    id, name, icon, category = "spotify", "Spotify", "♫", "Music"
    aliases = ["spotify"]
    description = ("Plays your music: say “play” and a song, playlist, artist or one of your vibes. Your playlists "
                   "and Liked Songs come first. Queue, like, add to playlists, repeat, skip, shuffle, volume, "
                   "switch devices. Needs your account connected (Premium) for most of it.")
    fields = [Field("client_id", "Spotify app Client ID", help="developer.spotify.com ▸ Dashboard ▸ your app ▸ "
                    "Basic Information. Redirect URI must be http://127.0.0.1:8737/callback and Web API ticked.")]
    REDIRECT = REDIRECT
    SCOPES = SCOPES

    def __init__(self) -> None:
        super().__init__()
        self._access: tuple[str, float] | None = None
        self._library: dict[str, Any] | None = None
        self._sync_lock = threading.Lock()
        self._login_cancel: threading.Event | None = None

    # ── commands ─────────────────────────────────────────────────────────
    def actions(self) -> list[Action]:
        this = "(this|that|the current|current|the) [song|track|one]"
        return [
            Action("open", "Open Spotify", [f"{OPEN} spotify"], self.open_app),
            Action("liked", "Play my Liked Songs", ["play my (liked songs|likes|liked music|favorites|favourites)",
                                                     "play [my] liked songs", "play [some|my] music",
                                                     "(shuffle|play) my (music|library)",
                                                     "shuffle my liked songs"], self.play_liked,
                   examples=["play my liked songs"]),
            Action("play", "Play a song, playlist, artist or vibe",
                   ["play {query} on spotify", "spotify play {query}", "play {query} (in|from|with) spotify",
                    "put on {query} [on spotify]", "(shuffle|listen to) {query}", "play {query}"],
                   self.play, params={"query": "song (optionally 'by artist'), playlist, artist or vibe"},
                   examples=["play join me in death", "play phonkaphone", "play yeat", "play maidcore"]),
            Action("queue", "Add to the queue",
                   ["(add|put|throw|stick) {query} (to|in|on|into) [the|my] (queue|up next)",
                    "queue [up] {query} [on spotify]", "play {query} (next|after this) [on spotify]",
                    "add {query} next"],
                   self.queue, params={"query": "song, artist, playlist or album"},
                   examples=["add city of angels to the queue", "play sky next"]),
            Action("queue_show", "What's in the queue",
                   ["what's (in|on) [my|the] queue", "what's (next|up next|coming up) [on spotify]",
                    "(show|read) [me] [my|the] queue"], self.show_queue),
            Action("vibe_teach", "Teach a vibe",
                   ["when i say {vibe} [i mean|play|that means|put on] {artists}",
                    "(remember|learn|save) [that] {vibe} (means|is) {artists}",
                    "(make|create|add) a vibe (called|named) {vibe} (with|of) {artists}",
                    "{vibe} vibe (is|means) {artists}"],
                   self.teach_vibe, params={"vibe": "a name like maidcore", "artists": "artists, comma separated"},
                   examples=["when i say maidcore play yakui the maid, chikoi, kichi"]),
            Action("vibe_add", "Add an artist to a vibe",
                   ["(add|put) {artists} (to|in|into) [my|the] {vibe} vibe"], self.add_to_vibe,
                   params={"artists": "artist", "vibe": "vibe name"}),
            Action("vibe_forget", "Forget a vibe", ["(forget|remove|delete) [the|my] {vibe} vibe"], self.forget_vibe,
                   params={"vibe": "vibe name"}),
            Action("vibes", "List my vibes", ["what (are|is) my vibes", "(list|show) [me] my vibes",
                                               "what vibes do (i|you) (have|know)"], self.list_vibes),
            Action("like", "Like this song",
                   [f"(like|save|heart) {this}", f"add {this} to [my] (liked songs|likes|favorites|favourites|"
                    "library)", "like (this|it)", "save (this|it) [to my library]"], self.like),
            Action("unlike", "Unlike this song",
                   [f"(unlike|unsave|unheart|dislike) {this}", f"remove {this} from [my] (liked songs|likes|"
                    "favorites|favourites|library)", "unlike (this|it)"], self.unlike),
            Action("add_playlist", "Add to a playlist",
                   [f"(add|put|save) {this} (to|in|on|into) [my] [playlist] {{playlist}} [playlist]",
                    "(add|put|save) (this|that|it) (to|in|on|into) [my] [playlist] {playlist} [playlist]",
                    "(add|put) {query} (to|in|into) [my] [playlist] {playlist} [playlist]"],
                   self.add_to_playlist, params={"playlist": "one of your playlists"},
                   examples=["add this song to phonkaphone"]),
            Action("resume", "Play / pause Spotify", ["(pause|resume|play|unpause|stop) spotify",
                                                       "spotify (pause|resume|play)"], self.toggle),
            Action("next", "Next song", ["(next|skip) [song|track] on spotify", "spotify (next|skip)"], self.next),
            Action("prev", "Previous song", ["previous [song|track] on spotify", "spotify (previous|back)"],
                   self.prev),
            Action("repeat", "Repeat", ["(turn|switch|put) (on|off) repeat", "(turn|switch|put) repeat (on|off)",
                                        "repeat (on|off)", f"(repeat|loop) {this}", "(repeat|loop) (it|this)",
                                        "(stop|don't|no more) (repeating|looping|repeat|loop)",
                                        "(repeat|loop) [the|this] (playlist|album|queue)"], self.repeat),
            Action("seek", "Jump around in the song",
                   ["(skip|go|jump|fast forward|fast-forward) (ahead|forward) [by] {amount:num} [seconds|secs|sec|s]",
                    "(skip|go|jump) (back|backward|backwards) [by] {amount:num} [seconds|secs|sec|s]",
                    "rewind [by] {amount:num} [seconds|secs|sec|s]", "(restart|replay) " + this,
                    "play (this|that|it) (again|from the (start|beginning))", "start (this|the) song over",
                    "(go|skip|jump) to [the] (start|beginning)"], self.seek),
            Action("device", "Play on another device",
                   ["(play|put) (it|this|music|spotify|the music) on [my|the] {device}",
                    "(switch|move|transfer|send) [the] (music|spotify|playback|song) to [my|the] {device}"],
                   self.move_device, params={"device": "phone, PC, speaker, or a device name"}),
            Action("volume", "Spotify volume", ["(set|put|turn) spotify volume to {amount:num} [percent|%]",
                                                 "spotify volume [to] {amount:num} [percent|%]"], self.volume,
                   params={"amount": "0-100"}),
            Action("shuffle", "Shuffle on / off", ["(turn|switch) shuffle (on|off)", "(turn|switch) (on|off) shuffle",
                                                    "shuffle (on|off)", "(stop|don't) shuffl(e|ing)"], self.shuffle),
            Action("current", "What's playing", ["what's playing [on spotify]", "what song is (this|playing)",
                                                  "what's this song", "who sings this", "what is this song",
                                                  "who's this [song] by", "who is this [song] by",
                                                  "what song (is that|was that)"],
                   self.current),
            Action("sync", "Refresh my library", ["(refresh|sync|update) my (spotify|music) library",
                                                   "(refresh|sync) spotify"], self.resync),
        ]

    def connected(self) -> bool:
        return bool(self.api and self.api.secret(self.id, "refresh_token"))

    def granted(self) -> list[str]:
        return (self.setting("scopes") or "").split()

    def has_library_access(self) -> bool:
        return all(s in self.granted() for s in LIBRARY_SCOPES)

    def missing_scopes(self) -> list[str]:
        return [s for s in SCOPES.split() if s not in self.granted()]

    def status(self) -> tuple[str, str]:
        if self.connected():
            if not self.has_library_access():
                return "Connected · reconnect to use your library", "warning"
            if self.missing_scopes():
                return "Connected · reconnect for queue/like/playlist features", "warning"
            lib = self._load_library()
            n, p = len(lib.get("tracks", [])), len(lib.get("playlists", []))
            return (f"Connected · {n} liked songs, {p} playlists" if n or p else "Connected"), "success"
        return ("Ready · account not connected", "muted") if self.setting("client_id") else ("Ready", "success")

    def on_start(self) -> None:
        if self.connected() and self.has_library_access():
            threading.Thread(target=self._safe_sync, daemon=True, name="spotify-sync").start()

    # ── sign-in (PKCE: no secret needed) ─────────────────────────────────
    def connect(self, on_done: Callable[[str | None], None]) -> None:
        client_id = (self.setting("client_id") or "").strip()
        if not client_id:
            on_done("Paste your Spotify Client ID first.")
            return
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}
        reasons = {
            "access_denied": "Spotify said access was denied — you pressed Cancel, or this Spotify account isn't "
                             "listed under User Management on your Spotify developer app.",
            "invalid_client": "Spotify doesn't recognise that Client ID — copy it again from the app's Basic "
                              "Information.",
            "stale": "That sign-in tab came from an earlier attempt — press Connect again and use the new tab.",
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                url = urllib.parse.urlparse(self.path)
                if url.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                if query.get("state") != state:
                    result.setdefault("error", "stale")
                elif "code" in query:
                    result["code"] = query["code"]
                else:
                    result["error"] = query.get("error", "no_code")
                    result["detail"] = query.get("error_description", "")
                if "code" in result:
                    title, text = "Juno is connected to Spotify", "You can close this tab."
                else:
                    title = "Spotify sign-in didn't finish"
                    text = reasons.get(result["error"], f"Spotify said: {result['error']} "
                                                        f"{result.get('detail', '')}".strip())
                html = ("<html><body style='background:#010805;color:#39ff7a;font-family:Consolas,monospace;"
                        "padding:48px'><h2>&gt; " + title + "</h2><p style='color:#b8ffcc;font-size:16px'>"
                        + text.replace("<", "&lt;") + "</p></body></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

        if self._login_cancel is not None:
            self._login_cancel.set()
        cancel = self._login_cancel = threading.Event()

        def work() -> None:
            server = None
            for _ in range(8):
                try:
                    server = http.server.HTTPServer(("127.0.0.1", 8737), Handler)
                    break
                except OSError:
                    time.sleep(0.5)
            if server is None:
                self.api.ctx.call_in_ui(on_done, "Port 8737 is in use by another program — close it and try again.")
                return
            server.timeout = 0.5
            webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
                "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
                "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "show_dialog": "true"}))
            end = time.time() + 300
            while time.time() < end and not cancel.is_set() and "code" not in result and "error" not in result:
                server.handle_request()
            server.server_close()
            if cancel.is_set() and "code" not in result:
                return
            if "code" not in result:
                error = result.get("error")
                message = reasons.get(error, f"Spotify said: {error} {result.get('detail', '')}".strip()) \
                    if error else "Timed out waiting for the Spotify sign-in (5 minutes)."
                self.api.log.warning("Spotify sign-in failed: %s %s", error, result.get("detail", ""))
                self.api.ctx.call_in_ui(on_done, message)
                return
            try:
                self._token({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
                             "client_id": client_id, "code_verifier": verifier})
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                self.api.ctx.call_in_ui(on_done, f"Spotify refused the sign-in ({exc.code}): {body}")
                return
            except Exception as exc:
                self.api.ctx.call_in_ui(on_done, str(exc))
                return
            self._library = None
            self._safe_sync(full=True)
            self.api.ctx.call_in_ui(on_done, None)
        threading.Thread(target=work, daemon=True, name="spotify-login").start()

    def disconnect(self) -> None:
        self.api.set_secret(self.id, "refresh_token", "")
        self._access = None

    def _token(self, form: dict[str, str]) -> str:
        req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                     data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("refresh_token"):
            self.api.set_secret(self.id, "refresh_token", data["refresh_token"])
        if data.get("scope"):
            self.api.set(self.id, "scopes", data["scope"])
        self._access = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return self._access[0]

    def _bearer(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = self.api.secret(self.id, "refresh_token")
        if not refresh:
            raise SpotifyError("Spotify isn't connected — connect it on the Integrations page.")
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh,
                            "client_id": (self.setting("client_id") or "").strip()})

    def _call(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = ("https://api.spotify.com/v1" + path if path.startswith("/") else path) + \
              ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": "Bearer " + self._bearer(), "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 429 and attempt < 2:
                    time.sleep(min(int(exc.headers.get("Retry-After", "1") or 1), 5))
                    continue
                if exc.code == 401 and attempt == 0:
                    self._access = None
                    continue
                reason = ""
                try:
                    reason = json.loads(detail).get("error", {}).get("reason", "")
                except Exception:
                    pass
                if exc.code == 404 and "NO_ACTIVE_DEVICE" in detail + reason:
                    raise SpotifyError("NO_ACTIVE_DEVICE") from None
                if exc.code == 403 and "PREMIUM" in detail.upper():
                    raise SpotifyError("Spotify only lets Premium accounts be controlled by apps.") from None
                if exc.code == 403 and "insufficient" in detail.lower():
                    raise SpotifyError("Juno needs new Spotify permissions — press Disconnect, then Connect "
                                       "again on the Integrations page.") from None
                if exc.code == 403:
                    raise SpotifyError(f"Spotify refused that ({reason or detail[:120]}).") from None
                raise SpotifyError(f"Spotify said {exc.code}: {reason or detail[:120]}") from None
        raise SpotifyError("Spotify is busy — try again in a moment.")

    def _pages(self, path: str, query: dict[str, Any], key: str = "items", stop: Callable[[Any], bool] | None = None,
               limit: int = 5000) -> list[Any]:
        out, url, params = [], path, dict(query)
        while url and len(out) < limit:
            page = self._call("GET", url, query=params)
            params = None
            container = page.get(key, page) if key != "items" else page
            if key != "items" and isinstance(container, dict):
                items = container.get("items", [])
                url = container.get("next")
            else:
                items = page.get("items", [])
                url = page.get("next")
            for item in items:
                if stop and stop(item):
                    return out
                out.append(item)
        return out

    # ── library cache ────────────────────────────────────────────────────
    @property
    def library_file(self) -> Path:
        return self.api.data_dir / "spotify_library.json"

    def _load_library(self) -> dict[str, Any]:
        if self._library is None:
            try:
                self._library = json.loads(self.library_file.read_text(encoding="utf-8"))
            except Exception:
                self._library = {}
        return self._library

    def _save_library(self, lib: dict[str, Any]) -> None:
        self._library = lib
        tmp = self.library_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.library_file)

    def _safe_sync(self, full: bool = False) -> None:
        try:
            self.sync(full)
        except Exception as exc:
            self.api.log.warning("Spotify library sync failed: %s", exc)

    def sync(self, full: bool = False) -> dict[str, Any]:
        """Liked songs, playlists and followed artists → spotify_library.json. Quick top-up unless full."""
        with self._sync_lock:
            lib = dict(self._load_library())
            full = full or not lib.get("tracks") or time.time() - lib.get("full_sync", 0) > FULL_SYNC_SECONDS
            me = self._call("GET", "/me") or {}
            lib["user"] = me.get("id", lib.get("user", ""))
            known = {t["id"] for t in lib.get("tracks", [])} if not full else set()

            def track(item: dict[str, Any]) -> dict[str, Any] | None:
                t = track_of(item)
                if not t.get("id"):
                    return None
                return {"id": t["id"], "uri": t["uri"], "name": t.get("name", ""),
                        "artists": [a.get("name", "") for a in t.get("artists", [])],
                        "artist_ids": [a.get("id", "") for a in t.get("artists", [])], "added": item.get("added_at", "")}
            new = [x for x in (track(i) for i in self._pages("/me/tracks", {"limit": 50},
                                                              stop=lambda i: (i.get("track") or {}).get("id") in known))
                   if x]
            lib["tracks"] = new + ([t for t in lib.get("tracks", []) if t["id"] not in {n["id"] for n in new}]
                                   if not full else [])
            lib["playlists"] = [{"id": p["id"], "uri": p["uri"], "name": p.get("name", ""),
                                 "owner": (p.get("owner") or {}).get("display_name", ""),
                                 "tracks": (p.get("items") if isinstance(p.get("items"), dict) else p.get("tracks") or {}).get("total", 0)}
                                for p in self._pages("/me/playlists", {"limit": 50}, limit=1000) if p and p.get("id")]
            try:
                lib["artists"] = [{"id": a["id"], "uri": a["uri"], "name": a.get("name", "")}
                                  for a in self._pages("/me/following", {"type": "artist", "limit": 50},
                                                       key="artists", limit=1000) if a and a.get("id")]
            except SpotifyError:
                lib.setdefault("artists", [])
            if full:
                lib["full_sync"] = time.time()
            lib["synced"] = time.time()
            self._save_library(lib)
            self.api.log.info("Spotify library: %d liked, %d playlists, %d artists", len(lib["tracks"]),
                              len(lib["playlists"]), len(lib["artists"]))
            return lib


    # ── vibes: your own words for a sound ("maidcore" → these artists) ──────
    def vibes(self) -> list[dict[str, Any]]:
        raw = self.setting("vibes") or []
        return [v for v in raw if isinstance(v, dict) and v.get("name") and v.get("artists")]

    def save_vibes(self, vibes: list[dict[str, Any]]) -> None:
        self.api.set(self.id, "vibes", vibes)

    def set_vibe(self, name: str, artists: list[str], add: bool = False) -> dict[str, Any]:
        name = re.sub(r"\s+", " ", name.strip().strip("\"'“”")).lower()
        vibes = self.vibes()
        current = next((v for v in vibes if norm(v["name"]) == norm(name)), None)
        if current is None:
            current = {"name": name, "artists": []}
            vibes.append(current)
        merged = (current["artists"] if add else []) + artists
        current["artists"] = list(dict.fromkeys(a.strip() for a in merged if a.strip()))
        self.save_vibes(vibes)
        return current

    def find_vibe(self, query: str) -> dict[str, Any] | None:
        q = norm(GENRE_TAIL.sub("", query.strip()))
        q = re.sub(r"^(?:some|sum|my|a|the)\s+", "", q)
        vibes = self.vibes()
        hit = next((v for v in vibes if norm(v["name"]) == q), None)
        if hit is None and len(q) >= 4:
            best = max(vibes, key=lambda v: similar(q, norm(v["name"])), default=None)
            if best is not None and similar(q, norm(best["name"])) >= 0.88:
                hit = best
        return hit

    def _mix(self, artists: list[str], limit: int = 60) -> list[str]:
        """Track URIs for a set of artists: what you've liked by them first, then their songs on Spotify."""
        wanted = {norm(a): a for a in artists}
        lib = self._load_library() if self.has_library_access() else {}
        per_artist: dict[str, list[str]] = {k: [] for k in wanted}
        for t in lib.get("tracks", []):
            for a in t["artists"]:
                if norm(a) in per_artist and t["uri"] not in per_artist[norm(a)]:
                    per_artist[norm(a)].append(t["uri"])
                    break
        for key, name in list(wanted.items())[:10]:
            try:
                found = self._call("GET", "/search", query={"q": f'artist:"{name}"', "type": "track", "limit": 10})
            except SpotifyError as exc:
                self.api.log.info("vibe search for %s failed: %s", name, exc)
                continue
            for t in (found.get("tracks") or {}).get("items") or []:
                if t and any(norm(a.get("name", "")) == key or similar(norm(a.get("name", "")), key) > 0.85
                             for a in t.get("artists", [])) and t["uri"] not in per_artist[key]:
                    per_artist[key].append(t["uri"])
        for uris in per_artist.values():
            random.shuffle(uris)
        out: list[str] = []
        while any(per_artist.values()) and len(out) < limit:     # interleave so no artist hogs the mix
            for key in list(per_artist):
                if per_artist[key]:
                    uri = per_artist[key].pop(0)
                    if uri not in out:
                        out.append(uri)
        random.shuffle(out)
        return out

    def _genre_artists(self, genre: str) -> list[str]:
        try:
            found = self._call("GET", "/search", query={"q": f'genre:"{genre}"', "type": "artist", "limit": 10})
        except SpotifyError:
            return []
        return [a["name"] for a in (found.get("artists") or {}).get("items") or [] if a and a.get("name")][:8]

    # ── finding what you meant ───────────────────────────────────────────
    def find(self, query: str) -> dict[str, Any] | None:
        """→ {"kind": track|playlist|artist|album|mix, "uri", "name", "by", "source", ...} or None."""
        text = query.strip()
        vibe = self.find_vibe(text)
        if vibe is not None:
            return {"kind": "mix", "name": vibe["name"], "artists": vibe["artists"], "source": "your vibes"}
        whole = norm(text)
        if self.has_library_access() and whole:      # "Love Song" is a title, not a hint
            lib0 = self._load_library()
            hit = next((p for p in lib0.get("playlists", []) if norm(p["name"]) == whole), None)
            if hit:
                return self._playlist(hit)
            hit = next((t for t in lib0.get("tracks", []) if norm(t["name"]) == whole), None)
            if hit:
                return self._liked(hit, lib0)
        kind_hint = None
        m = re.match(r"^(?:the |my )?(playlist|song|track|artist|album)\s+(.+)$", text, re.I)
        if m:
            word, text = m.group(1), m.group(2)
        else:
            m = re.match(r"^(?:the |my )?(.+?)\s+(playlist|song|track|album)$", text, re.I)
            if m:
                text, word = m.group(1), m.group(2)
        if m:
            kind_hint = {"song": "track"}.get(word.lower(), word.lower())
        m = re.match(r"^(?:some |sum |songs by |music by |stuff by |something by )(.+)$", text, re.I)
        if m:
            kind_hint, text = "artist", m.group(1)
        title, artist = text, ""
        m = re.match(r"^(.+?)\s+by\s+(.+)$", text, re.I)
        if m and kind_hint != "artist":
            title, artist = m.group(1), m.group(2)
        q, qa = norm(title), norm(artist)
        if not q:
            return None
        lib = self._load_library() if self.has_library_access() else {}
        tracks, playlists = lib.get("tracks", []), lib.get("playlists", [])
        lib_artists = self._library_artists(lib)

        def artist_ok(t: dict[str, Any]) -> bool:
            return not qa or any(qa == norm(a) or similar(qa, norm(a)) > 0.85 for a in t["artists"])

        # 1. playlists
        if kind_hint in (None, "playlist") and not qa:
            hit = next((p for p in playlists if norm(p["name"]) == q), None)
            if hit:
                return self._playlist(hit)
        # 2. liked songs, exact title (+artist)
        if kind_hint in (None, "track"):
            hits = [t for t in tracks if norm(t["name"]) == q and artist_ok(t)]
            if hits:
                return self._liked(hits[0], lib)
        # 3. artists you listen to
        if kind_hint in (None, "artist") and not qa and q in lib_artists:
            a = lib_artists[q]
            return {"kind": "artist", "uri": a["uri"], "name": a["name"], "source": "your artists"}
        # 4. close matches in your library
        if kind_hint in (None, "playlist") and not qa:
            best = max(playlists, key=lambda p: similar(q, norm(p["name"])), default=None)
            if best and similar(q, norm(best["name"])) >= 0.86:
                return self._playlist(best)
        if kind_hint in (None, "track"):
            scored = sorted(((similar(q, norm(t["name"])), t) for t in tracks if artist_ok(t)),
                            key=lambda s: -s[0])
            if scored and scored[0][0] >= 0.88:
                return self._liked(scored[0][1], lib)
            contains = [t for t in tracks if len(q) >= 5 and q in norm(t["name"]) and artist_ok(t)]
            if contains and not qa and len(contains) == 1:
                return self._liked(contains[0], lib)
        # 5. Spotify-wide search
        return self._search(title, artist, kind_hint, lib_artists)

    @staticmethod
    def _library_artists(lib: dict[str, Any]) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for a in lib.get("artists", []):
            out.setdefault(norm(a["name"]), {"name": a["name"], "uri": a["uri"]})
        for t in lib.get("tracks", []):
            for name, aid in zip(t["artists"], t.get("artist_ids", [])):
                if aid:
                    out.setdefault(norm(name), {"name": name, "uri": f"spotify:artist:{aid}"})
        return out

    @staticmethod
    def _playlist(p: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "playlist", "uri": p["uri"], "id": p.get("id"), "name": p["name"], "source": "your playlists"}

    @staticmethod
    def _liked(t: dict[str, Any], lib: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "track", "uri": t["uri"], "name": t["name"], "by": ", ".join(t["artists"][:2]),
                "source": "your Liked Songs", "context": f"spotify:user:{lib.get('user')}:collection"
                if lib.get("user") else None}

    def _search(self, title: str, artist: str, kind_hint: str | None,
                lib_artists: dict[str, dict[str, str]]) -> dict[str, Any] | None:
        q, qa = norm(title), norm(artist)
        if artist:
            text = f'track:"{title}" artist:"{artist}"'
            types = "track"
        else:
            text = title
            types = {"artist": "artist", "track": "track", "playlist": "playlist", "album": "album"}.get(
                kind_hint or "", "track,artist,playlist,album")
        found = self._call("GET", "/search", query={"q": text, "type": types, "limit": 10}) or {}
        tracks = [t for t in (found.get("tracks") or {}).get("items") or [] if t]
        artists = [a for a in (found.get("artists") or {}).get("items") or [] if a]
        playlists = [p for p in (found.get("playlists") or {}).get("items") or [] if p]
        albums = [a for a in (found.get("albums") or {}).get("items") or [] if a]
        if artist and not tracks:      # "X by Y" with a slightly-off title: plain search, then filter by artist
            loose = self._call("GET", "/search", query={"q": f"{title} {artist}", "type": "track", "limit": 10}) or {}
            tracks = [t for t in (loose.get("tracks") or {}).get("items") or [] if t and
                      any(qa in norm(a["name"]) or similar(qa, norm(a["name"])) > 0.8 for a in t.get("artists", []))]

        def as_track(t: dict[str, Any]) -> dict[str, Any]:
            return {"kind": "track", "uri": t["uri"], "name": t["name"],
                    "by": ", ".join(a["name"] for a in t.get("artists", [])[:2]), "source": "Spotify"}

        def as_artist(a: dict[str, Any]) -> dict[str, Any]:
            return {"kind": "artist", "uri": a["uri"], "name": a["name"], "source": "Spotify"}

        if kind_hint == "album" and albums:
            a = albums[0]
            return {"kind": "album", "uri": a["uri"], "id": a.get("id"), "name": a["name"],
                    "by": ", ".join(x["name"] for x in a.get("artists", [])[:2]), "source": "Spotify"}
        if kind_hint == "playlist" and playlists:
            p = playlists[0]
            return {"kind": "playlist", "uri": p["uri"], "id": p.get("id"), "name": p["name"], "source": "Spotify"}
        if kind_hint == "artist" and artists:
            exact = [a for a in artists if norm(a["name"]) == q]
            return as_artist(exact[0] if exact else artists[0])

        def by_names(t: dict[str, Any]) -> list[str]:
            return [norm(a["name"]) for a in t.get("artists", [])]

        exact_titles = [t for t in tracks if norm(t["name"]) == q and (not qa or any(
            qa == n or similar(qa, n) > 0.85 for n in by_names(t)))]
        exact_artists = [a for a in artists if norm(a["name"]) == q] if not qa else []
        if exact_artists:
            a = exact_artists[0]
            # an artist you already listen to, or Spotify's own top results are by them → you meant the artist
            top = tracks[:5]
            theirs = sum(1 for t in top if q in by_names(t))
            if q in lib_artists or not exact_titles or (top and theirs * 2 > len(top)):
                return as_artist(a)
        if exact_titles:
            # a song title wins over an unknown artist; among same-titled songs prefer artists you like,
            # then Spotify's own ranking
            ranked = sorted(enumerate(exact_titles), key=lambda it: (
                not any(n in lib_artists for n in by_names(it[1])), it[0]))
            return as_track(ranked[0][1])
        if tracks:
            ranked = sorted(enumerate(tracks), key=lambda it: (
                not (not qa or qa in by_names(it[1])), not any(n in lib_artists for n in by_names(it[1])),
                -similar(q, norm(it[1]["name"])), it[0]))
            best = ranked[0][1]
            if similar(q, norm(best["name"])) >= 0.6 or qa or not artists:
                return as_track(best)
        if artists and kind_hint in (None, "artist"):
            return as_artist(artists[0])
        if tracks:
            return as_track(tracks[0])
        if playlists:
            p = playlists[0]
            return {"kind": "playlist", "uri": p["uri"], "id": p.get("id"), "name": p["name"], "source": "Spotify"}
        return None

    # ── devices ──────────────────────────────────────────────────────────
    def _devices(self) -> list[dict[str, Any]]:
        devices = (self._call("GET", "/me/player/devices") or {}).get("devices", [])
        return [d for d in devices if d.get("id") and not d.get("is_restricted")]

    def _device(self, api: IntegrationAPI) -> str:
        """The device to play on — the active one, else this PC's Spotify (opening it if needed)."""
        pc = (os.environ.get("COMPUTERNAME") or "").lower()
        for attempt in range(16):
            usable = self._devices()
            if usable:
                pick = next((d for d in usable if d.get("is_active")), None) \
                    or next((d for d in usable if d.get("type") == "Computer" and d.get("name", "").lower() == pc), None) \
                    or next((d for d in usable if d.get("type") == "Computer"), None) or usable[0]
                return pick["id"]
            if attempt == 0:
                api.open_uri("spotify:")      # start the app so it shows up as a device
            time.sleep(0.75)
        raise SpotifyError("Spotify didn't show up as a device — open the Spotify app, then try again.")

    def _start(self, api: IntegrationAPI, body: dict[str, Any] | None, shuffle: bool | None = None) -> None:
        device = self._device(api)
        if shuffle is not None:
            try:
                self._call("PUT", "/me/player/shuffle", query={"state": "true" if shuffle else "false",
                                                              "device_id": device})
            except SpotifyError:
                pass
        try:
            self._call("PUT", "/me/player/play", body, query={"device_id": device})
        except SpotifyError as exc:
            if str(exc) != "NO_ACTIVE_DEVICE":
                raise
            self._call("PUT", "/me/player", {"device_ids": [device], "play": False})
            time.sleep(0.6)
            self._call("PUT", "/me/player/play", body, query={"device_id": device})

    def _play_hit(self, api: IntegrationAPI, hit: dict[str, Any], shuffle: bool | None) -> str:
        """Start playing a find() result; returns how to describe it."""
        if hit["kind"] == "mix":
            uris = self._mix(hit["artists"])
            if not uris:
                raise SpotifyError(f"I couldn't find songs for {hit['name']}.")
            self._start(api, {"uris": uris[:100]}, None)
            names = ", ".join(hit["artists"][:4]) + ("…" if len(hit["artists"]) > 4 else "")
            return f"{hit['name']} — {names}"
        if hit["kind"] == "track":
            if hit.get("context"):
                try:
                    self._start(api, {"context_uri": hit["context"], "offset": {"uri": hit["uri"]}}, shuffle)
                except SpotifyError:
                    # some accounts can't start Liked Songs as a context: play from this song onwards instead
                    tracks = [t["uri"] for t in self._load_library().get("tracks", [])]
                    i = tracks.index(hit["uri"]) if hit["uri"] in tracks else 0
                    self._start(api, {"uris": (tracks[i:] + tracks[:i])[:100] or [hit["uri"]]}, shuffle)
            else:
                self._start(api, {"uris": [hit["uri"]]}, shuffle)
        else:
            self._start(api, {"context_uri": hit["uri"]}, shuffle)
        return {"playlist": f"your playlist {hit['name']}" if hit["source"] == "your playlists"
                else f"the playlist {hit['name']}", "artist": hit["name"], "album": f"the album {hit['name']}"} \
            .get(hit["kind"], hit["name"] + (f" by {hit['by']}" if hit.get("by") else ""))

    # ── actions ──────────────────────────────────────────────────────────
    def _need_account(self, api: IntegrationAPI, query: str = "") -> ActionResult | None:
        if self.connected():
            return None
        if query:
            api.open_uri("spotify:search:" + urllib.parse.quote(query))
            return ActionResult(f"Opened a Spotify search for “{query}”. Connect your Spotify account on the "
                                f"Integrations page and I'll play things directly.", say=f"Here's {query} on Spotify.")
        api.keys("media_play_pause")
        return ActionResult("Play/pause.", say="")

    def _top_up(self) -> None:
        if self.has_library_access():
            lib = self._load_library()
            if not lib.get("tracks") or time.time() - lib.get("synced", 0) > 600:
                try:
                    self.sync()
                except SpotifyError as exc:
                    self.api.log.warning("library top-up failed: %s", exc)

    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_uri("spotify:")
        return ActionResult("Opened Spotify.")

    def play(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        query = params.get("query", "").strip().strip("'\"")
        if norm(query) in ("music", "some music", "something", "my music", "anything", "a song", "some songs",
                           "some tunes", "tunes", "some bangers", "bangers", "a banger", "my songs"):
            return self.play_liked(api, params)
        early = self._need_account(api, query)
        if early:
            return early
        shuffle = True if params.get("_text", "").startswith("shuffle") else None
        self._top_up()
        genre_ish = bool(GENRE_TAIL.search(query) or re.match(r"^(?:some|sum)\s+", query, re.I))
        hit = self.find(query)
        if genre_ish and (hit is None or hit.get("source") == "Spotify"):
            # "play some phonk" / "play maidcore music": a genre, not a title
            genre = re.sub(r"^(?:some|sum)\s+", "", GENRE_TAIL.sub("", query), flags=re.I).strip()
            names = self._genre_artists(genre)
            if names:
                hit = {"kind": "mix", "name": genre, "artists": names, "source": "Spotify genres"}
            elif hit is None and genre != query:
                hit = self.find(genre)
        if hit is None:
            return ActionResult(f"I couldn't find “{query}” on Spotify.", ok=False)
        what = self._play_hit(api, hit, shuffle)
        where = {"your Liked Songs": " from your Liked Songs", "your vibes": " (your vibe, shuffled)",
                 "Spotify genres": " (a mix of the genre)"}.get(hit["source"], "")
        if not self.has_library_access():
            where += " (reconnect Spotify so I can check your library first)"
        return ActionResult(f"Playing {what}{where}.", say=f"Playing {what.split(' — ')[0]}.")

    def play_liked(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            api.keys("media_play_pause")
            return ActionResult("Play/pause.", say="")
        lib = self._load_library()
        if not lib.get("user"):
            lib = self.sync()
        text = params.get("_text", "")
        shuffle = True if "shuffle" in text or "music" in text else None
        try:
            self._start(api, {"context_uri": f"spotify:user:{lib['user']}:collection"}, shuffle)
        except SpotifyError:
            uris = [t["uri"] for t in lib.get("tracks", [])]
            if not uris:
                raise
            if shuffle:
                random.shuffle(uris)
            self._start(api, {"uris": uris[:100]}, shuffle)
        return ActionResult("Playing your Liked Songs" + (" on shuffle." if shuffle else "."), say="Playing your "
                            "liked songs.")

    # queue
    def _queue_uris(self, hit: dict[str, Any]) -> list[str]:
        if hit["kind"] == "track":
            return [hit["uri"]]
        if hit["kind"] == "mix":
            return self._mix(hit["artists"], limit=10)
        if hit["kind"] == "artist":
            liked = [t["uri"] for t in self._load_library().get("tracks", [])
                     if norm(hit["name"]) in [norm(a) for a in t["artists"]]]
            if liked:
                return [random.choice(liked)]
            found = self._call("GET", "/search", query={"q": f'artist:"{hit["name"]}"', "type": "track", "limit": 5})
            items = [t for t in (found.get("tracks") or {}).get("items") or [] if t]
            return [items[0]["uri"]] if items else []
        if hit["kind"] == "playlist" and hit.get("id"):
            page = self._call("GET", f"/playlists/{hit['id']}/items", query={"limit": 25}) or {}
            return [track_of(i)["uri"] for i in page.get("items", []) if track_of(i).get("uri")][:25]
        if hit["kind"] == "album" and hit.get("id"):
            album = self._call("GET", f"/albums/{hit['id']}") or {}
            return [t["uri"] for t in (album.get("tracks") or {}).get("items", []) if t.get("uri")][:25]
        return []

    def queue(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        query = params.get("query", "").strip().strip("'\"")
        if norm(query) in ("this", "it", "that", "this song", "that song"):
            raise LookupError("nothing named")
        if not self.connected():
            return ActionResult("Connect your Spotify account (Integrations page) and I can queue songs.", ok=False)
        self._top_up()
        hit = self.find(query)
        if hit is None:
            return ActionResult(f"I couldn't find “{query}” on Spotify.", ok=False)
        uris = self._queue_uris(hit)
        if not uris:
            return ActionResult(f"I couldn't get songs out of {hit['name']} to queue.", ok=False)
        device = self._device(api)
        try:
            for uri in uris:
                self._call("POST", "/me/player/queue", query={"uri": uri, "device_id": device})
        except SpotifyError as exc:
            if "NO_ACTIVE_DEVICE" not in str(exc) and "404" not in str(exc):
                raise
            what = self._play_hit(api, hit, None)     # nothing was playing: just start it
            return ActionResult(f"Nothing was playing, so I started {what}.")
        if hit["kind"] == "track":
            what = hit["name"] + (f" by {hit['by']}" if hit.get("by") else "")
            return ActionResult(f"Queued {what}.", say=f"Queued {hit['name']}.")
        return ActionResult(f"Queued {len(uris)} song{'s' * (len(uris) != 1)} from {hit['name']}.",
                            say=f"Queued {hit['name']}.")

    def show_queue(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        data = self._call("GET", "/me/player/queue") or {}
        items = [i for i in data.get("queue") or [] if i][:8]
        if not items:
            return ActionResult("Your queue is empty.")
        lines = [f"{n}. {i.get('name', '?')} — " + ", ".join(a["name"] for a in i.get("artists", [])[:2])
                 for n, i in enumerate(items, 1)]
        first = items[0]
        return ActionResult("Up next —\n" + "\n".join(lines),
                            say=f"Next up is {first.get('name')}" + (f", then {items[1].get('name')}"
                                                                      if len(items) > 1 else "") + ".")

    # vibes
    def _artists_from_raw(self, params: dict[str, str]) -> list[str]:
        """Commas get stripped from the matched text, so re-read the artist list from what was typed."""
        artists = params.get("artists", "")
        raw = params.get("_raw", "")
        if artists and raw:
            first = artists.split()[0]
            vibe = (params.get("vibe") or "").split()
            start = 0
            if vibe:
                v = re.search(re.escape(vibe[-1]), raw, re.I)
                start = v.end() if v else 0
            m = re.compile(re.escape(first), re.I).search(raw, start)
            if m:
                tail = raw[m.start():]
                tail = re.sub(r"[\s.!?]*(?:pls|please|thanks|thank you|thx|ty)?[\s.!?]*$", "", tail, flags=re.I)
                parts = split_artists(tail)
                if parts:
                    return parts
        return split_artists(artists)

    def teach_vibe(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params.get("vibe", "").strip()
        name = re.sub(r"^(?:the|a|my)\s+", "", name, flags=re.I)
        artists = self._artists_from_raw(params)
        if not name or not artists:
            return ActionResult("Tell me like: “when I say maidcore, play Yakui The Maid, Chikoi, Kichi”.", ok=False)
        vibe = self.set_vibe(name, artists)
        return ActionResult(f"Got it — “{vibe['name']}” means {', '.join(vibe['artists'])}. Say “play "
                            f"{vibe['name']}” any time.", say=f"Got it. {vibe['name']} it is.")

    def add_to_vibe(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params.get("vibe", "").strip()
        if not self.find_vibe(name):
            raise LookupError("no such vibe")
        vibe = self.set_vibe(self.find_vibe(name)["name"], self._artists_from_raw(params), add=True)
        return ActionResult(f"“{vibe['name']}” is now {', '.join(vibe['artists'])}.")

    def forget_vibe(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        vibe = self.find_vibe(params.get("vibe", ""))
        if vibe is None:
            return ActionResult(f"I don't have a vibe called “{params.get('vibe', '')}”.", ok=False)
        self.save_vibes([v for v in self.vibes() if v["name"] != vibe["name"]])
        return ActionResult(f"Forgot the “{vibe['name']}” vibe.")

    def list_vibes(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        vibes = self.vibes()
        if not vibes:
            return ActionResult("No vibes yet. Teach me one: “when I say maidcore, play Yakui The Maid, Chikoi, "
                                "Kichi”.")
        return ActionResult("Your vibes —\n" + "\n".join(f"{v['name']}: {', '.join(v['artists'])}" for v in vibes),
                            say="Your vibes are " + ", ".join(v["name"] for v in vibes) + ".")

    # the song that's playing
    def _now(self) -> dict[str, Any]:
        now = self._call("GET", "/me/player/currently-playing") or {}
        item = now.get("item")
        if not item:
            raise SpotifyError("Nothing's playing right now.")
        return item

    def _needs(self, scope: str) -> None:
        if scope not in self.granted():
            raise SpotifyError("That needs a new Spotify permission — on the Integrations page press Disconnect, "
                               "then Connect again (once).")

    def like(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        self._needs("user-library-modify")
        item = self._now()
        self._call("PUT", "/me/library", query={"uris": item["uri"]})
        lib = self._load_library()
        if lib and not any(t["id"] == item.get("id") for t in lib.get("tracks", [])):
            lib.setdefault("tracks", []).insert(0, {
                "id": item["id"], "uri": item["uri"], "name": item.get("name", ""),
                "artists": [a["name"] for a in item.get("artists", [])],
                "artist_ids": [a.get("id", "") for a in item.get("artists", [])], "added": ""})
            self._save_library(lib)
        return ActionResult(f"Added {item['name']} to your Liked Songs.", say="Liked.")

    def unlike(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        self._needs("user-library-modify")
        item = self._now()
        self._call("DELETE", "/me/library", query={"uris": item["uri"]})
        lib = self._load_library()
        if lib.get("tracks"):
            lib["tracks"] = [t for t in lib["tracks"] if t["id"] != item.get("id")]
            self._save_library(lib)
        return ActionResult(f"Removed {item['name']} from your Liked Songs.", say="Unliked.")

    def _playlist_named(self, name: str) -> dict[str, Any] | None:
        name = norm(re.sub(r"^(?:my|the)\s+|\s+playlist$", "", name.strip(), flags=re.I))
        playlists = self._load_library().get("playlists", [])
        target = next((p for p in playlists if norm(p["name"]) == name), None)
        if target is None and name:
            best = max(playlists, key=lambda p: similar(name, norm(p["name"])), default=None)
            target = best if best is not None and similar(name, norm(best["name"])) >= 0.8 else None
        return target

    def add_to_playlist(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        target = self._playlist_named(params.get("playlist", ""))
        if target is None and params.get("query"):
            # "add join me IN death to my phonkaphone playlist" — try every place the sentence could split
            words = params.get("_text", "").split()
            for i in range(len(words) - 1, 1, -1):
                if words[i] in ("to", "in", "into", "on"):
                    hit = self._playlist_named(" ".join(words[i + 1:]))
                    if hit is not None:
                        target, params = hit, {**params, "query": " ".join(words[1:i])}
                        break
        if target is None:
            if params.get("query"):
                raise LookupError("not one of your playlists")
            return ActionResult(f"I can't find a playlist of yours called “{params.get('playlist')}”.", ok=False)
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        self._needs("playlist-modify-private")
        if params.get("query") and norm(params["query"]) not in ("this", "that", "it"):
            hit = self.find(params["query"])
            if hit is None or hit["kind"] != "track":
                return ActionResult(f"I couldn't find the song “{params['query']}”.", ok=False)
            uri, title = hit["uri"], hit["name"]
        else:
            item = self._now()
            uri, title = item["uri"], item["name"]
        self._call("POST", f"/playlists/{target['id']}/items", {"uris": [uri]})
        return ActionResult(f"Added {title} to {target['name']}.", say=f"Added to {target['name']}.")

    # playback controls
    def toggle(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return self._need_account(api)
        text = params.get("_text", "")
        state = self._call("GET", "/me/player") or {}
        playing = bool(state.get("is_playing"))
        want_pause = text.startswith(("pause", "stop")) or (playing and not text.startswith(("resume", "play",
                                                                                               "unpause")))
        if want_pause:
            if playing:
                self._call("PUT", "/me/player/pause")
            return ActionResult("Paused Spotify.", say="")
        self._start(api, None)
        return ActionResult("Resumed Spotify.", say="")

    def next(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        self._call("POST", "/me/player/next") if self.connected() else api.keys("media_next")
        return ActionResult("Skipped.", say="")

    def prev(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        self._call("POST", "/me/player/previous") if self.connected() else api.keys("media_prev")
        return ActionResult("Previous track.", say="")

    def repeat(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        text = " " + params.get("_text", "") + " "
        if re.search(r" (off|stop|don't|no more) ", text):
            state, say = "off", "Repeat off."
        elif re.search(r" (playlist|album|queue) ", text):
            state, say = "context", "Repeating this playlist."
        elif " on " in text and "song" not in text and "track" not in text:
            state, say = "context", "Repeat on."
        else:
            state, say = "track", "Repeating this song."
        self._call("PUT", "/me/player/repeat", query={"state": state})
        return ActionResult(say, say="")

    def seek(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        text = params.get("_text", "")
        state = self._call("GET", "/me/player") or {}
        pos = int(state.get("progress_ms") or 0)
        m = re.search(r"\d+(?:\.\d+)?", params.get("amount", ""))
        secs = float(m.group()) if m else 0.0
        if not m:
            target, msg = 0, "From the top."
        elif re.search(r"\b(back|backward|backwards|rewind)\b", text):
            target, msg = max(0, pos - int(secs * 1000)), f"Back {secs:g} seconds."
        else:
            dur = int((state.get("item") or {}).get("duration_ms") or 10 ** 9)
            target, msg = min(dur - 1000, pos + int(secs * 1000)), f"Forward {secs:g} seconds."
        self._call("PUT", "/me/player/seek", query={"position_ms": max(0, target)})
        return ActionResult(msg, say="")

    def move_device(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        want = norm(params.get("device", ""))
        devices = self._devices()
        if not devices:
            return ActionResult("I can't see any Spotify devices — open Spotify on the one you want.", ok=False)
        kind = next((v for k, v in DEVICE_WORDS.items() if re.search(rf"\b{k}\b", want)), None)
        pick = next((d for d in devices if norm(d.get("name", "")) == want), None) \
            or next((d for d in devices if want and want in norm(d.get("name", ""))), None) \
            or (next((d for d in devices if d.get("type") == kind), None) if kind else None)
        if pick is None:
            names = ", ".join(f"{d.get('name')} ({d.get('type', '').lower()})" for d in devices)
            return ActionResult(f"I don't see “{params.get('device')}”. Your devices: {names}.", ok=False)
        self._call("PUT", "/me/player", {"device_ids": [pick["id"]], "play": True})
        return ActionResult(f"Playing on {pick.get('name')}.", say=f"Moved to {pick.get('name')}.")

    def volume(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first — or say “set volume to 40” for the PC volume.",
                                ok=False)
        m = re.search(r"\d+", params.get("amount", ""))
        level = max(0, min(100, int(m.group()))) if m else 50
        self._call("PUT", "/me/player/volume", query={"volume_percent": level})
        return ActionResult(f"Spotify volume {level}%.", say="")

    def shuffle(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        text = " " + params.get("_text", "") + " "
        on = " on " in text and " off " not in text and not re.search(r" (stop|don't) ", text)
        self._call("PUT", "/me/player/shuffle", query={"state": "true" if on else "false"})
        return ActionResult(f"Shuffle {'on' if on else 'off'}.", say="")

    def current(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account (Integrations page) and I can tell you.", ok=False)
        try:
            item = self._now()
        except SpotifyError as exc:
            return ActionResult(str(exc))
        by = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        liked = any(t["id"] == item.get("id") for t in self._load_library().get("tracks", []))
        return ActionResult(f"That's {item['name']}" + (f" by {by}" if by else "") +
                            (" — it's in your Liked Songs." if liked else "."))

    def resync(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first.", ok=False)
        if not self.has_library_access():
            return ActionResult("Press Disconnect, then Connect again on the Integrations page so I can read your "
                                "library.", ok=False)
        lib = self.sync(full=True)
        return ActionResult(f"Library refreshed: {len(lib['tracks'])} liked songs, {len(lib['playlists'])} playlists.")
