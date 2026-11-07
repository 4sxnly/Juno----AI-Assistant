"""
Spotify integration — plays YOUR music first.

When you say "play <something>", Juno looks in this order:
  1. your playlists        ("play Phonkaphone")
  2. your Liked Songs      ("play Join Me In Death" → the version YOU liked, never a same-named song by
                            someone else; it keeps playing through your Liked Songs afterwards)
  3. artists you listen to ("play Yeat")
  4. close matches in your library (typos, "the", feat. tags…)
  5. a Spotify-wide search, preferring artists you already like.
"X by Y" pins the artist; "playlist", "song" or "artist" in the request narrows what it looks for.

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
          "user-library-read playlist-read-private playlist-read-collaborative user-follow-read")
LIBRARY_SCOPES = ("user-library-read", "playlist-read-private")
FULL_SYNC_SECONDS = 12 * 3600


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


class SpotifyError(RuntimeError):
    pass


class Spotify(Integration):
    id, name, icon, category = "spotify", "Spotify", "♫", "Music"
    aliases = ["spotify"]
    description = ("Plays your music: say “play” and a playlist, song or artist. Your playlists and Liked Songs come "
                   "first. Pause, skip, volume, shuffle, what's playing. Needs your account connected (Premium) "
                   "for most of it.")
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
        return [
            Action("open", "Open Spotify", [f"{OPEN} spotify"], self.open_app),
            Action("liked", "Play my Liked Songs", ["play my (liked songs|likes|liked music|favorites|favourites)",
                                                     "play [my] liked songs", "play [some|my] music",
                                                     "(shuffle|play) my (music|library)",
                                                     "shuffle my liked songs"], self.play_liked,
                   examples=["play my liked songs"]),
            Action("play", "Play a song, playlist or artist",
                   ["play {query} on spotify", "spotify play {query}", "play {query} (in|from|with) spotify",
                    "put on {query} [on spotify]", "(shuffle|listen to) {query}", "play {query}"],
                   self.play, params={"query": "song (optionally 'by artist'), playlist or artist name"},
                   examples=["play join me in death", "play phonkaphone", "play yeat"]),
            Action("resume", "Play / pause Spotify", ["(pause|resume|play|unpause|stop) spotify",
                                                       "spotify (pause|resume|play)"], self.toggle),
            Action("next", "Next song", ["(next|skip) [song|track] on spotify", "spotify (next|skip)"], self.next),
            Action("prev", "Previous song", ["previous [song|track] on spotify", "spotify (previous|back)"],
                   self.prev),
            Action("volume", "Spotify volume", ["(set|put|turn) spotify volume to {amount:num} [percent|%]",
                                                 "spotify volume [to] {amount:num} [percent|%]"], self.volume,
                   params={"amount": "0-100"}),
            Action("shuffle", "Shuffle on / off", ["(turn|switch) shuffle (on|off)", "shuffle (on|off)",
                                                    "(stop|don't) shuffl(e|ing)"], self.shuffle),
            Action("current", "What's playing", ["what's playing [on spotify]", "what song is (this|playing)",
                                                  "what's this song", "who sings this"], self.current),
            Action("sync", "Refresh my library", ["(refresh|sync|update) my (spotify|music) library",
                                                   "(refresh|sync) spotify"], self.resync),
        ]

    def connected(self) -> bool:
        return bool(self.api and self.api.secret(self.id, "refresh_token"))

    def has_library_access(self) -> bool:
        granted = (self.setting("scopes") or "").split()
        return all(s in granted for s in LIBRARY_SCOPES)

    def status(self) -> tuple[str, str]:
        if self.connected():
            if not self.has_library_access():
                return "Connected · reconnect to use your library", "warning"
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
                t = item.get("track") or {}
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

    # ── finding what you meant ───────────────────────────────────────────
    def find(self, query: str) -> dict[str, Any] | None:
        """→ {"kind": track|playlist|artist|liked|album, "uri", "name", "by", "source", ...} or None."""
        text = query.strip()
        whole = norm(text)
        if self.has_library_access() and whole:      # "Love Song" is a title, not a hint
            lib0 = self._load_library()
            hit = next((p for p in lib0.get("playlists", []) if norm(p["name"]) == whole), None)
            if hit:
                return {"kind": "playlist", "uri": hit["uri"], "name": hit["name"], "source": "your playlists"}
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
        m = re.match(r"^(?:some |songs by |music by |stuff by )(.+)$", text, re.I)
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
        lib_artists: dict[str, dict[str, str]] = {}
        for a in lib.get("artists", []):
            lib_artists.setdefault(norm(a["name"]), {"name": a["name"], "uri": a["uri"]})
        for t in tracks:
            for name, aid in zip(t["artists"], t.get("artist_ids", [])):
                if aid:
                    lib_artists.setdefault(norm(name), {"name": name, "uri": f"spotify:artist:{aid}"})

        def artist_ok(t: dict[str, Any]) -> bool:
            return not qa or any(qa == norm(a) or similar(qa, norm(a)) > 0.85 for a in t["artists"])

        # 1. playlists
        if kind_hint in (None, "playlist") and not qa:
            hit = next((p for p in playlists if norm(p["name"]) == q), None)
            if hit:
                return {"kind": "playlist", "uri": hit["uri"], "name": hit["name"], "source": "your playlists"}
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
                return {"kind": "playlist", "uri": best["uri"], "name": best["name"], "source": "your playlists"}
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
        if kind_hint in (None, "artist") and not qa:
            exact = [a for a in artists if norm(a["name"]) == q]
            if exact:
                a = max(exact, key=lambda a: (a.get("popularity", 0)))
                return {"kind": "artist", "uri": a["uri"], "name": a["name"], "source": "Spotify"}
        if kind_hint == "album" and albums:
            a = albums[0]
            return {"kind": "album", "uri": a["uri"], "name": a["name"],
                    "by": ", ".join(x["name"] for x in a.get("artists", [])[:2]), "source": "Spotify"}
        if kind_hint == "playlist" and playlists:
            p = playlists[0]
            return {"kind": "playlist", "uri": p["uri"], "name": p["name"], "source": "Spotify"}
        if tracks:
            def score(t: dict[str, Any]) -> tuple:
                names = [norm(a["name"]) for a in t.get("artists", [])]
                return (norm(t["name"]) == q, any(n in lib_artists for n in names),
                        not qa or qa in names, t.get("popularity", 0))
            t = max(tracks, key=score)
            return {"kind": "track", "uri": t["uri"], "name": t["name"],
                    "by": ", ".join(a["name"] for a in t.get("artists", [])[:2]), "source": "Spotify"}
        if artists and kind_hint in (None, "artist"):
            a = artists[0]
            return {"kind": "artist", "uri": a["uri"], "name": a["name"], "source": "Spotify"}
        if playlists:
            p = playlists[0]
            return {"kind": "playlist", "uri": p["uri"], "name": p["name"], "source": "Spotify"}
        return None

    # ── devices ──────────────────────────────────────────────────────────
    def _device(self, api: IntegrationAPI) -> str:
        """The device to play on — the active one, else this PC's Spotify (opening it if needed)."""
        pc = (os.environ.get("COMPUTERNAME") or "").lower()
        for attempt in range(16):
            devices = (self._call("GET", "/me/player/devices") or {}).get("devices", [])
            usable = [d for d in devices if d.get("id") and not d.get("is_restricted")]
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

    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_uri("spotify:")
        return ActionResult("Opened Spotify.")

    def play(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        query = params.get("query", "").strip().strip("'\"")
        if norm(query) in ("music", "some music", "something", "my music", "anything", "a song", "some songs"):
            return self.play_liked(api, params)
        early = self._need_account(api, query)
        if early:
            return early
        shuffle = True if params.get("_text", "").startswith("shuffle") else None
        if self.has_library_access():
            lib = self._load_library()
            if not lib.get("tracks") or time.time() - lib.get("synced", 0) > 600:
                try:
                    self.sync()
                except SpotifyError as exc:
                    self.api.log.warning("library top-up failed: %s", exc)
        hit = self.find(query)
        if hit is None:
            return ActionResult(f"I couldn't find “{query}” on Spotify.", ok=False)
        if hit["kind"] == "track":
            body: dict[str, Any] = {"uris": [hit["uri"]]}
            if hit.get("context"):
                body = {"context_uri": hit["context"], "offset": {"uri": hit["uri"]}}
            try:
                self._start(api, body, shuffle)
            except SpotifyError:
                if not hit.get("context"):
                    raise
                self._start(api, {"uris": [hit["uri"]]}, shuffle)   # some accounts can't start the collection
        else:
            self._start(api, {"context_uri": hit["uri"]}, shuffle)
        what = {"playlist": f"your playlist {hit['name']}" if hit["source"] == "your playlists"
                else f"the playlist {hit['name']}", "artist": hit["name"], "album": f"the album {hit['name']}"} \
            .get(hit["kind"], hit["name"] + (f" by {hit['by']}" if hit.get("by") else ""))
        where = " from your Liked Songs" if hit["source"] == "your Liked Songs" else ""
        if not self.has_library_access():
            where += " (reconnect Spotify so I can check your library first)"
        return ActionResult(f"Playing {what}{where}.", say=f"Playing {what}.")

    def play_liked(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            api.keys("media_play_pause")
            return ActionResult("Play/pause.", say="")
        lib = self._load_library()
        if not lib.get("user"):
            lib = self.sync()
        shuffle = True if "shuffle" in params.get("_text", "") or "music" in params.get("_text", "") else None
        try:
            self._start(api, {"context_uri": f"spotify:user:{lib['user']}:collection"}, shuffle)
        except SpotifyError:
            uris = [t["uri"] for t in lib.get("tracks", [])[:100]]
            if not uris:
                raise
            self._start(api, {"uris": uris}, shuffle)
        return ActionResult("Playing your Liked Songs" + (" on shuffle." if shuffle else "."), say="Playing your "
                            "liked songs.")

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
        on = " on" in " " + params.get("_text", "") and "off" not in params.get("_text", "") \
            and not params.get("_text", "").startswith(("stop", "don't"))
        self._call("PUT", "/me/player/shuffle", query={"state": "true" if on else "false"})
        return ActionResult(f"Shuffle {'on' if on else 'off'}.", say="")

    def current(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account (Integrations page) and I can tell you.", ok=False)
        now = self._call("GET", "/me/player/currently-playing") or {}
        item = now.get("item")
        if not item:
            return ActionResult("Nothing's playing right now.")
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
