"""
Browser & typing integration.

  "search up m1 abrams on firefox"        → opens Firefox on a search for it
  "open reddit in chrome" / "go to youtube.com on edge"
  "search cats on youtube in firefox"     → YouTube's own search, in Firefox
  "type hello there" / "type gg and press enter"   → types into the window you were last using
  "press ctrl s" / "hit enter" / "new tab" / "close tab" / "refresh" / "go back" / "scroll down"

Typing and key presses go to the window you used before talking to Juno (Juno remembers it), so you can type
the command in Juno's own chat box and the text still lands in your browser, game chat, Notepad…
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .integrations import OPEN, Action, ActionResult, Field, Integration, IntegrationAPI, focus_hwnd, send_keys

IS_WINDOWS = sys.platform == "win32"

BROWSERS: dict[str, dict[str, Any]] = {
    "firefox": {"name": "Firefox", "exe": "firefox.exe", "paths": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                                                                 r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
                                                                 r"%LocalAppData%\Mozilla Firefox\firefox.exe"],
                "aliases": ["firefox", "fire fox", "mozilla", "ff"]},
    "chrome": {"name": "Chrome", "exe": "chrome.exe", "paths": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                                               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                                                               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
               "aliases": ["chrome", "google chrome", "chromium"]},
    "edge": {"name": "Edge", "exe": "msedge.exe",
             "paths": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                       r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
             "aliases": ["edge", "microsoft edge", "ms edge"]},
    "brave": {"name": "Brave", "exe": "brave.exe",
              "paths": [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
                        r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"],
              "aliases": ["brave"]},
    "operagx": {"name": "Opera GX", "exe": "opera.exe", "paths": [r"%LocalAppData%\Programs\Opera GX\opera.exe"],
                "aliases": ["opera gx", "operagx", "gx"]},
    "opera": {"name": "Opera", "exe": "opera.exe", "paths": [r"%LocalAppData%\Programs\Opera\opera.exe",
                                                            r"%ProgramFiles%\Opera\opera.exe"],
              "aliases": ["opera"]},
    "vivaldi": {"name": "Vivaldi", "exe": "vivaldi.exe", "paths": [r"%LocalAppData%\Vivaldi\Application\vivaldi.exe",
                                                                  r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe"],
                "aliases": ["vivaldi"]},
    "zen": {"name": "Zen", "exe": "zen.exe", "paths": [r"%ProgramFiles%\Zen Browser\zen.exe"],
            "aliases": ["zen", "zen browser"]},
    "librewolf": {"name": "LibreWolf", "exe": "librewolf.exe", "paths": [r"%ProgramFiles%\LibreWolf\librewolf.exe"],
                  "aliases": ["librewolf", "libre wolf"]},
}
ENGINES = {"Google": "https://www.google.com/search?q={q}", "DuckDuckGo": "https://duckduckgo.com/?q={q}",
           "Bing": "https://www.bing.com/search?q={q}", "Brave Search": "https://search.brave.com/search?q={q}"}
SITE_SEARCH = {
    "youtube": "https://www.youtube.com/results?search_query={q}", "yt": "https://www.youtube.com/results?search_query={q}",
    "reddit": "https://www.reddit.com/search/?q={q}", "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}", "wiki": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}", "twitter": "https://x.com/search?q={q}", "x": "https://x.com/search?q={q}",
    "tiktok": "https://www.tiktok.com/search?q={q}", "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "google images": "https://www.google.com/search?tbm=isch&q={q}", "images": "https://www.google.com/search?tbm=isch&q={q}",
    "google maps": "https://www.google.com/maps/search/{q}", "maps": "https://www.google.com/maps/search/{q}",
    "roblox": "https://www.roblox.com/discover/?Keyword={q}", "twitch": "https://www.twitch.tv/search?term={q}",
    "pinterest": "https://www.pinterest.com/search/pins/?q={q}", "steam": "https://store.steampowered.com/search/?term={q}",
    "spotify web": "https://open.spotify.com/search/{q}", "genius": "https://genius.com/search?q={q}",
}
SITES = {"youtube": "youtube.com", "reddit": "reddit.com", "gmail": "mail.google.com", "google": "google.com",
         "twitter": "x.com", "x": "x.com", "github": "github.com", "amazon": "amazon.com", "netflix": "netflix.com",
         "twitch": "twitch.tv", "roblox": "roblox.com", "discord": "discord.com/app", "tiktok": "tiktok.com",
         "instagram": "instagram.com", "facebook": "facebook.com", "wikipedia": "wikipedia.org",
         "chatgpt": "chatgpt.com", "claude": "claude.ai", "spotify": "open.spotify.com", "google docs": "docs.google.com",
         "google drive": "drive.google.com", "drive": "drive.google.com", "maps": "maps.google.com",
         "steam": "store.steampowered.com", "pinterest": "pinterest.com", "ebay": "ebay.com"}

KEY_WORDS = {"control": "ctrl", "ctl": "ctrl", "ctrl": "ctrl", "shift": "shift", "alt": "alt", "windows": "win",
             "win": "win", "enter": "enter", "return": "enter", "escape": "esc", "esc": "esc", "tab": "tab",
             "space": "space", "spacebar": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
             "home": "home", "end": "end", "page up": "pageup", "pageup": "pageup", "page down": "pagedown",
             "pagedown": "pagedown", "up": "up", "down": "down", "left": "left", "right": "right",
             "up arrow": "up", "down arrow": "down", "left arrow": "left", "right arrow": "right",
             "print screen": "printscreen"}


# ── Windows plumbing ─────────────────────────────────────────────────────────
class _ForegroundTracker:
    """Remembers the last window you used that isn't Juno, so typing goes there."""

    def __init__(self) -> None:
        self.last = 0
        self.last_title = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not IS_WINDOWS or (self._thread and self._thread.is_alive()):
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="foreground-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        me = os.getpid()
        while not self._stop.wait(0.4):
            try:
                hwnd = user32.GetForegroundWindow()
                if not hwnd or hwnd == self.last:
                    continue
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == me:
                    continue
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if not buf.value or buf.value in ("Program Manager", "Task Switching"):
                    continue
                self.last, self.last_title = hwnd, buf.value
            except Exception:
                pass


TRACKER = _ForegroundTracker()


def focus_window(hwnd: int) -> bool:
    return focus_hwnd(hwnd)


def type_text(text: str) -> None:
    """Types any text (emoji and accents too) into the focused window."""
    if not IS_WINDOWS:
        raise RuntimeError("typing needs Windows")
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def press(scan: int, flags: int) -> None:
        down = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, scan, flags, 0, 0)))
        up = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, scan, flags | 0x2, 0, 0)))
        ctypes.windll.user32.SendInput(2, (INPUT * 2)(down, up), ctypes.sizeof(INPUT))

    for ch in text:
        if ch == "\n":
            send_keys("enter")
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):               # surrogate pairs for emoji
            press(int.from_bytes(data[i:i + 2], "little"), 0x4)   # KEYEVENTF_UNICODE
        time.sleep(0.004)


def parse_keys(words: str) -> str:
    """'control plus shift plus s' / 'ctrl s' / 'alt f4' → 'ctrl+shift+s'."""
    text = words.lower().replace("+", " plus ").replace("-", " ")
    text = re.sub(r"\b(?:the|key|keys|button)\b", " ", text)
    parts = [p.strip() for p in re.split(r"\s+(?:plus|and|then)\s+|\s+", text) if p.strip()]
    out, i = [], 0
    while i < len(parts):
        two = " ".join(parts[i:i + 2])
        if two in KEY_WORDS:
            out.append(KEY_WORDS[two])
            i += 2
            continue
        word = parts[i]
        if word in KEY_WORDS:
            out.append(KEY_WORDS[word])
        elif re.fullmatch(r"f\d{1,2}|[a-z0-9]", word):
            out.append(word)
        else:
            raise ValueError(f"I don't know the key “{word}”")
        i += 1
    if not out:
        raise ValueError("which key?")
    return "+".join(out)


class Browser(Integration):
    id, name, icon, category = "browser", "Browser & typing", "⌨", "Web"
    aliases = ["firefox", "chrome", "edge", "brave", "opera", "browser"]
    description = ("Search or open sites in a specific browser (“search up m1 abrams on firefox”), and type or press "
                   "keys in the window you were using (“type gg and press enter”, “new tab”, “press ctrl s”).")
    fields = [Field("engine", "Search with", kind="choice", default="Google", choices=list(ENGINES)),
              Field("default_browser", "Default browser", kind="choice",
                    default="Windows default", choices=["Windows default", *[b["name"] for b in BROWSERS.values()]])]

    def actions(self) -> list[Action]:
        return [
            Action("search", "Search in a browser",
                   ["(search|look|google) [up] {query} (on|in|with|using) {browser}",
                    "google {query} (on|in|with|using) {browser}",
                    "(search|google) {browser} for {query}", "{browser} search [for] {query}",
                    "(open|use) {browser} (and|to) (search|look up|google) [for] {query}",
                    "search up {query}", "look up {query} (online|on the internet|on the web)"],
                   self.search, params={"query": "what to search", "browser": "firefox, chrome, edge…"},
                   examples=["search up m1 abrams on firefox"]),
            Action("site", "Open a site in a browser",
                   ["(open|go to|visit|pull up|bring up) {site} (on|in|with|using) {browser}",
                    "(open|use) {browser} (and|to) (go to|open|visit) {site}"],
                   self.open_site, params={"site": "site name or address", "browser": "which browser"},
                   examples=["open youtube in chrome"]),
            Action("type", "Type text", ["type [out|in] {text} and (press|hit) enter", "type [out|in] {text}"],
                   self.type, params={"text": "what to type"}, examples=["type gg and press enter"]),
            Action("press", "Press keys", ["(press|hit|tap) {keys}", "(do|use) [the] (shortcut|hotkey) {keys}"],
                   self.press, params={"keys": "like ctrl s, alt tab, enter"}, examples=["press ctrl s"]),
            Action("tab_new", "New tab", ["(open|make) [a] new tab", "new tab"], self.keys("ctrl+t", "New tab.")),
            Action("tab_close", "Close tab", ["close [this|the|that] tab"], self.keys("ctrl+w", "Closed the tab.")),
            Action("tab_next", "Next tab", ["(next|switch) tab", "go to the next tab"],
                   self.keys("ctrl+tab", "Next tab.")),
            Action("tab_reopen", "Reopen closed tab", ["(reopen|restore|undo close) [the] [last] [closed] tab",
                                                       "bring back [that|the] tab"],
                   self.keys("ctrl+shift+t", "Reopened it.")),
            Action("refresh", "Refresh page", ["(refresh|reload) [the|this] [page|tab]"],
                   self.keys("f5", "Refreshed.")),
            Action("back", "Go back", ["go back [a page]", "(previous|last) page", "back a page"],
                   self.keys("alt+left", "Back.")),
            Action("forward", "Go forward", ["go forward [a page]", "next page", "forward a page"],
                   self.keys("alt+right", "Forward.")),
            Action("scroll", "Scroll", ["scroll (down|up)", "scroll [to the] (top|bottom)", "page (down|up)"],
                   self.scroll),
        ]

    def status(self) -> tuple[str, str]:
        found = [b["name"] for k, b in BROWSERS.items() if self.find_browser(k)]
        return ("Found " + ", ".join(found[:5]) if found else "Ready"), "success"

    def on_start(self) -> None:
        TRACKER.start()

    def on_stop(self) -> None:
        TRACKER.stop()

    # ── finding browsers ─────────────────────────────────────────────────
    @staticmethod
    def browser_key(spoken: str) -> str | None:
        spoken = re.sub(r"\b(?:the|my|a|browser|web browser)\b", " ", spoken.lower()).strip()
        spoken = re.sub(r"\s+", " ", spoken)
        for key, info in sorted(BROWSERS.items(), key=lambda kv: -max(len(a) for a in kv[1]["aliases"])):
            if spoken in info["aliases"] or spoken == key:
                return key
        return None

    def find_browser(self, key: str) -> str | None:
        info = BROWSERS[key]
        for raw in info["paths"]:
            path = os.path.expandvars(raw)
            if "%" not in path and Path(path).is_file():
                return path
        if IS_WINDOWS:
            try:
                import winreg
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\"
                                            + info["exe"]) as k:
                            value = winreg.QueryValue(k, None)
                            if value and Path(value.strip('"')).is_file() and \
                                    (key != "opera" or "GX" not in value) and (key != "operagx" or "GX" in value):
                                return value.strip('"')
                    except OSError:
                        continue
            except Exception:
                pass
        if self.api is not None:
            lnk = self.api.apps.find(info["name"])
            if lnk is not None:
                return str(lnk)
        return None

    def _open_in(self, api: IntegrationAPI, browser: str | None, url: str) -> str:
        """Open url in the named browser (or the chosen/Windows default one). Returns the browser's name."""
        if browser is None:
            choice = self.setting("default_browser", "Windows default")
            browser = next((k for k, b in BROWSERS.items() if b["name"] == choice), None)
            if browser is None:
                api.open_url(url)
                return "your browser"
        exe = self.find_browser(browser)
        name = BROWSERS[browser]["name"]
        if exe is None:
            raise RuntimeError(f"I couldn't find {name} on this PC.")
        if exe.lower().endswith(".lnk"):
            os.startfile(exe, arguments=f'"{url}"')     # type: ignore[attr-defined]
        else:
            api.run([exe, url])
        return name

    def _search_url(self, query: str) -> str:
        """'cats on youtube' → YouTube's search; anything else → the chosen search engine."""
        m = re.match(r"^(.+?)\s+(?:on|in)\s+(" + "|".join(sorted(map(re.escape, SITE_SEARCH), key=len,
                                                                     reverse=True)) + r")$", query, re.I)
        if m:
            return SITE_SEARCH[m.group(2).lower()].format(q=urllib.parse.quote_plus(m.group(1)))
        engine = ENGINES.get(self.setting("engine", "Google") or "Google", ENGINES["Google"])
        return engine.format(q=urllib.parse.quote_plus(query))

    # ── actions ──────────────────────────────────────────────────────────
    def search(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        query = params.get("query", "").strip()
        spoken = params.get("browser")
        if not spoken:        # "search up m1 abrams on firefox"
            m = re.match(r"^(.+?)\s+(?:on|in|with|using)\s+(?:my\s+|the\s+)?([\w ]+?)(?:\s+browser)?$", query, re.I)
            if m and self.browser_key(m.group(2)):
                query, spoken = m.group(1), m.group(2)
        browser = self.browser_key(spoken) if spoken else None
        if spoken and browser is None:     # "cats on youtube in chrome" split too early
            m = re.match(r"^(.+?)\s+(?:on|in|with|using)\s+(.+)$", spoken)
            if m and self.browser_key(m.group(2)):
                query, spoken = f"{query} on {m.group(1)}", m.group(2)
                browser = self.browser_key(spoken)
        if spoken and browser is None:
            if spoken.lower() in SITE_SEARCH:      # "search cats on reddit"
                url = SITE_SEARCH[spoken.lower()].format(q=urllib.parse.quote_plus(query))
                where = self._open_in(api, None, url)
                return ActionResult(f"Searched {spoken.title()} for “{query}”.", say="Here you go.")
            raise LookupError("not a browser")
        where = self._open_in(api, browser, self._search_url(query))
        return ActionResult(f"Searching “{query}” in {where}.", say="Here you go.")

    def open_site(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        browser = self.browser_key(params.get("browser", ""))
        if browser is None:
            raise LookupError("not a browser")
        site = params.get("site", "").strip().lower().replace(" dot ", ".")
        site = SITES.get(site, site)
        if not re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", site):
            url = self._search_url(params.get("site", ""))
        else:
            url = site if "://" in site else "https://" + site
        name = self._open_in(api, browser, url)
        return ActionResult(f"Opened {params.get('site')} in {name}.", say="")

    def _target(self) -> str:
        if focus_window(TRACKER.last):
            return TRACKER.last_title
        return ""

    def type(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        text = params.get("text", "")
        raw = params.get("_raw", "")
        m = re.search(r"\btype(?:\s+out|\s+in)?\s+(.+?)(?:\s+and\s+(?:press|hit)\s+enter[\s.!]*)?\s*$", raw,
                      re.I | re.S)
        if m and m.group(1).lower().startswith(text.split()[0].lower() if text else ""):
            text = m.group(1)       # keep the capitals and punctuation you typed
        text = text.strip().strip("\"“”")
        enter = bool(re.search(r"and (press|hit) enter\s*$", params.get("_text", "")))
        where = self._target()
        type_text(text)
        if enter:
            send_keys("enter")
        return ActionResult(f"Typed “{text}”" + (f" into {where[:40]}" if where else "") +
                            (" and pressed Enter." if enter else "."), say="")

    def press(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        try:
            combo = parse_keys(params.get("keys", ""))
        except ValueError:
            raise LookupError("not keys") from None
        self._target()
        send_keys(combo)
        return ActionResult(f"Pressed {combo.replace('+', ' + ')}.", say="")

    def keys(self, combo: str, message: str):  # noqa: ANN201
        def run(api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
            self._target()
            send_keys(combo)
            return ActionResult(message, say="")
        return run

    def scroll(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        text = params.get("_text", "")
        combo = "ctrl+home" if "top" in text else "ctrl+end" if "bottom" in text else \
            "pageup" if "up" in text else "pagedown"
        self._target()
        send_keys(combo)
        return ActionResult("Scrolled.", say="")
