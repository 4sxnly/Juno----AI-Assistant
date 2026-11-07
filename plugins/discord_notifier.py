"""
Discord Heads-Up — Multi-Tool plugin (API v1)

Pops up a themed heads-up card whenever a Discord message arrives: the sender's profile picture and name,
the server/channel, the message, and your own note about that person.

HOW IT WORKS (no login, no token, nothing against Discord's rules)
  Windows keeps every app notification in a small local database
  (%LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db). Discord's desktop app already sends
  a Windows notification for each message (name, text and avatar). This plugin reads new Discord entries
  from that database, read-only, and shows them in Multi-Tool's own style.

NOTES
  Discord doesn't let other apps read the notes you write on profiles — only a "self-bot" logged in with
  your account token could, and Discord bans accounts for that. So notes live here instead: add one to
  anyone from the feed and it shows on every heads-up from them.

REQUIREMENTS
  * Discord desktop app with desktop notifications on (Discord ▸ User Settings ▸ Notifications).
  * Windows notifications for Discord on. Its own banners can be hidden from this page so you only see
    Multi-Tool's version; messages still reach the Windows notification centre (and this plugin).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import customtkinter as ctk

IS_WINDOWS = sys.platform == "win32"
POLL_SECONDS = 1.5
FEED_LIMIT = 40
BANNER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"


def notification_db() -> Path | None:
    override = os.environ.get("MT_WPN_DB")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


@dataclass
class DiscordMessage:
    id: int
    sender: str
    text: str
    server: str = ""
    channel: str = ""
    image_src: str = ""
    arrived: float = field(default_factory=time.time)
    app: str = ""
    avatar: Any = None   # PIL image once loaded

    @property
    def is_dm(self) -> bool:
        return not self.server and not self.channel

    @property
    def where(self) -> str:
        if self.is_dm:
            return "Direct message"
        return " · ".join(x for x in (self.channel, self.server) if x)

    @property
    def key(self) -> str:
        return person_key(self.sender)


def person_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


TITLE_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<ctx>[^()]*)\)\s*$")


def parse_title(title: str) -> tuple[str, str, str]:
    """'nyx (#general, Midnight Club)' → ('nyx', 'Midnight Club', '#general'). DMs have no brackets."""
    title = (title or "").strip()
    match = TITLE_RE.match(title)
    if not match:
        return title, "", ""
    name, ctx = match.group("name").strip(), match.group("ctx")
    parts = [p.strip() for p in ctx.split(",") if p.strip()]
    channel = next((p for p in parts if p.startswith("#")), "")
    server = ", ".join(p for p in parts if p != channel)
    return name, server, channel


def parse_payload(payload: bytes | str) -> tuple[list[str], str]:
    """Toast XML → (texts in order, avatar image src)."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return [], ""
    # browsers add the site ("discord.com") as an attribution line — not part of the message
    texts = [(t.text or "").strip() for t in root.iter("text") if t.get("placement") != "attribution"]
    image = ""
    for img in root.iter("image"):
        src = img.get("src") or ""
        if img.get("placement") == "appLogoOverride" or not image:
            image = src
            if img.get("placement") == "appLogoOverride":
                break
    return [t for t in texts if t], image


def message_from_row(row_id: int, payload: bytes | str, app: str) -> DiscordMessage | None:
    texts, image = parse_payload(payload)
    if not texts:
        return None
    title = texts[0]
    body = "\n".join(texts[1:]) if len(texts) > 1 else ""
    name, server, channel = parse_title(title)
    return DiscordMessage(id=row_id, sender=name or "Someone", text=body, server=server, channel=channel,
                          image_src=image, app=app)


def is_discord(app: str, payload: bytes | str, include_browser: bool) -> bool:
    if "discord" in (app or "").lower():
        return True
    if include_browser:
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else (payload or "")
        return "discord.com" in text.lower()
    return False


class NotificationWatcher:
    """Polls the Windows notification database (read-only) for new rows."""

    def __init__(self, db: Path, log: Any):
        self.db = db
        self.log = log
        self.last_id: int | None = None
        self.app_ids: set[str] = set()
        self._copy_dir: Path | None = None

    def _connect(self) -> sqlite3.Connection:
        uri = "file:" + urllib.parse.quote(str(self.db).replace("\\", "/")) + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2)
            con.execute("SELECT 1 FROM Notification LIMIT 1")
            return con
        except sqlite3.Error:
            # Fall back to a private copy (database + its write-ahead log) if Windows holds a lock
            self._copy_dir = self._copy_dir or Path(tempfile.mkdtemp(prefix="mt-wpn-"))
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.db) + suffix)
                if src.exists():
                    shutil.copy2(src, self._copy_dir / ("wpn.db" + suffix))
            return sqlite3.connect(str(self._copy_dir / "wpn.db"), timeout=2)

    def poll(self) -> list[tuple[int, bytes, str]]:
        con = self._connect()
        try:
            if self.last_id is None:
                row = con.execute("SELECT MAX(Id) FROM Notification").fetchone()
                self.last_id = int(row[0] or 0)
                return []
            rows = con.execute(
                "SELECT n.Id, n.Payload, h.PrimaryId FROM Notification n "
                "JOIN NotificationHandler h ON n.HandlerId = h.RecordId "
                "WHERE n.Id > ? AND n.Type = 'toast' ORDER BY n.Id", (self.last_id,)).fetchall()
        finally:
            con.close()
        for row_id, _payload, app in rows:
            self.last_id = max(self.last_id, int(row_id))
            if app:
                self.app_ids.add(app)
        return [(int(r[0]), r[1] or b"", r[2] or "") for r in rows]

    def close(self) -> None:
        if self._copy_dir is not None:
            shutil.rmtree(self._copy_dir, ignore_errors=True)


def load_avatar(src: str, cache: Path) -> Any:
    """Toast avatar → PIL image. Discord writes it to a temp file that may vanish, so we keep a copy."""
    if not src:
        return None
    try:
        from PIL import Image
        import io
        if src.lower().startswith(("http://", "https://")):
            req = urllib.request.Request(src, headers={"User-Agent": "MultiTool"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read(4 * 1024 * 1024)
        else:
            path = src
            if src.lower().startswith("file:"):
                path = urllib.request.url2pathname(urllib.parse.urlparse(src).path)
                if re.match(r"^[\\/][A-Za-z]:", path):
                    path = path[1:]
            data = Path(path).read_bytes()
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGBA")
        img.thumbnail((128, 128))
        cache.parent.mkdir(parents=True, exist_ok=True)
        img.save(cache, "PNG")
        return img
    except Exception:
        try:
            from PIL import Image
            if cache.exists():
                return Image.open(cache).convert("RGBA")
        except Exception:
            pass
        return None


def set_banner(app_id: str, show: bool) -> None:
    """Windows' own "Show notification banners" switch for one app (Settings ▸ System ▸ Notifications)."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, BANNER_KEY + "\\" + app_id) as key:
        if show:
            try:
                winreg.DeleteValue(key, "ShowBanner")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, "ShowBanner", 0, winreg.REG_DWORD, 0)


def banner_hidden(app_id: str) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BANNER_KEY + "\\" + app_id) as key:
            value, _ = winreg.QueryValueEx(key, "ShowBanner")
            return int(value) == 0
    except Exception:
        return False


DEFAULTS = {"enabled": True, "dms_only": False, "include_browser": False, "show_text": True,
            "duration": 7, "discord_app_ids": []}


class Plugin:
    meta_title = "Discord Heads-Up"
    meta_description = ("Heads-up pop-ups for Discord messages — profile picture, name, server and message — plus "
                        "your own notes on people. Reads Windows' notification list; no Discord login needed.")
    meta_icon = "✉"
    meta_order = 30
    meta_api_version = 1

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.settings = {k: ctx.get_setting(k, v) for k, v in DEFAULTS.items()}
        self.notes_file = ctx.data_dir / "notes.json"
        self.avatar_dir = ctx.data_dir / "avatars"
        self.notes: dict[str, dict[str, str]] = self._load_notes()
        self.feed: list[DiscordMessage] = []
        self.state = "starting"
        self.error = ""
        self._stop = threading.Event()
        self._page: Any = None
        self._built = False
        self._thumbs: list[Any] = []
        self._feed_after: Any = None
        db = notification_db()
        self.watcher = NotificationWatcher(db, ctx.log) if db is not None else None
        if not IS_WINDOWS and not os.environ.get("MT_WPN_DB"):
            self.state = "unsupported"
        elif self.watcher is None or not self.watcher.db.exists():
            self.state = "missing"
        else:
            threading.Thread(target=self._run, daemon=True, name="discord-watch").start()

    # ── settings / notes ─────────────────────────────────────────────────
    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.ctx.set_setting(key, value)

    def _load_notes(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.notes_file.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("note")}
        except Exception:
            return {}

    def _save_notes(self) -> None:
        tmp = self.notes_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.notes, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.notes_file)

    def note_for(self, sender: str) -> str:
        return (self.notes.get(person_key(sender)) or {}).get("note", "")

    def set_note(self, sender: str, note: str) -> None:
        key = person_key(sender)
        note = note.strip()[:300]
        if note:
            self.notes[key] = {"name": sender, "note": note}
        else:
            self.notes.pop(key, None)
        self._save_notes()
        self.ctx.set_status(f"Note for {sender} saved" if note else f"Note for {sender} removed", "success")

    # ── watching ─────────────────────────────────────────────────────────
    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                rows = self.watcher.poll()
                failures = 0
                if self.state != "watching":
                    self.state, self.error = "watching", ""
                    self.ctx.call_in_ui(self._refresh_status)
                for row_id, payload, app in rows:
                    if not is_discord(app, payload, bool(self.settings["include_browser"])):
                        continue
                    msg = message_from_row(row_id, payload, app)
                    if msg is None:
                        continue
                    if "discord" in app.lower() and app not in self.settings["discord_app_ids"]:
                        self.ctx.call_in_ui(self.set_setting, "discord_app_ids",
                                            sorted(set(self.settings["discord_app_ids"]) | {app}))
                    slug = re.sub(r"[^a-z0-9]+", "-", msg.key) or "someone"
                    msg.avatar = load_avatar(msg.image_src, self.avatar_dir / f"{slug[:60]}.png")
                    self.ctx.call_in_ui(self._on_message, msg)
            except Exception as exc:
                failures += 1
                if failures in (1, 10) or failures % 100 == 0:
                    self.ctx.log.warning("Discord watcher: %s", exc)
                self.state, self.error = "error", str(exc)
                self.ctx.call_in_ui(self._refresh_status)
            self._stop.wait(POLL_SECONDS if failures < 5 else 10)

    def _on_message(self, msg: DiscordMessage) -> None:
        self.feed.insert(0, msg)
        del self.feed[FEED_LIMIT:]
        if self.settings["enabled"] and not (self.settings["dms_only"] and not msg.is_dm):
            self.show(msg)
        self._schedule_feed_refresh()

    def show(self, msg: DiscordMessage) -> None:
        body = msg.text if self.settings["show_text"] else "sent you a message"
        if not hasattr(self.ctx, "notify"):  # core older than 0.4 — restart Multi-Tool to finish updating
            self.ctx.set_status(f"Discord · {msg.sender}: {body}")
            return
        self.ctx.notify(msg.sender, body or "(attachment)", subtitle=msg.where, image=msg.avatar,
                        note=self.note_for(msg.sender), on_click=self.open_discord,
                        duration=float(self.settings["duration"] or 7), icon=msg.sender[:1])

    @staticmethod
    def open_windows_settings() -> None:
        try:
            if IS_WINDOWS:
                os.startfile("ms-settings:notifications")  # type: ignore[attr-defined]
        except Exception:
            pass

    @staticmethod
    def open_discord() -> None:
        try:
            if IS_WINDOWS:
                os.startfile("discord://-/channels/@me")  # type: ignore[attr-defined]
        except Exception:
            pass

    def test(self) -> None:
        sample = DiscordMessage(id=0, sender="Multi-Tool", text="This is how Discord messages will pop up — "
                                "with their avatar, where it was sent and your note about them.",
                                server="Preview", channel="#heads-up")
        sample.avatar = None
        self.show(sample)

    # ── Windows banner switch ────────────────────────────────────────────
    def app_ids(self) -> list[str]:
        known = set(self.settings.get("discord_app_ids") or [])
        if self.watcher is not None:
            known |= {a for a in self.watcher.app_ids if "discord" in a.lower()}
        return sorted(known)

    def toggle_windows_banners(self) -> None:
        ids = self.app_ids()
        if not ids or not IS_WINDOWS:
            self.ctx.set_status("Get one Discord message first so Multi-Tool learns Discord's Windows ID.", "warning")
            return
        hide = not all(banner_hidden(a) for a in ids)
        try:
            for app in ids:
                set_banner(app, show=not hide)
        except Exception as exc:
            self.ctx.set_status(f"Couldn't change Windows' notification setting: {exc}", "error")
            return
        self.ctx.set_status("Windows' own Discord pop-ups are hidden (still in the notification centre)."
                            if hide else "Windows' own Discord pop-ups are back.", "success")
        self._rebuild()

    # ── lifecycle ────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self._stop.set()
        if self.watcher is not None:
            self.watcher.close()

    def on_show(self) -> None:
        self._refresh_status()

    # ── page ─────────────────────────────────────────────────────────────
    def get_frame(self, master: Any) -> Any:
        self._page = ctk.CTkScrollableFrame(master, fg_color="transparent")
        self._page.grid_columnconfigure(0, weight=1)
        self._build()
        return self._page

    def _rebuild(self) -> None:
        if self._built:
            self.ctx.app.after(10, self._build)

    def _build(self) -> None:
        t = self.ctx.theme
        page = self._page
        for child in page.winfo_children():
            child.destroy()

        top = t.tag(ctk.CTkFrame(page), "card")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14), padx=(0, 8))
        top.grid_columnconfigure(0, weight=1)
        self.status_label = t.tag(ctk.CTkLabel(top, text="", anchor="w", justify="left", wraplength=720), "text")
        self.status_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        switches = t.tag(ctk.CTkFrame(top, fg_color="transparent"), "transparent")
        switches.grid(row=1, column=0, sticky="ew", padx=16)
        for i, (key, text) in enumerate((("enabled", "Heads-up pop-ups"), ("dms_only", "Direct messages only"),
                                         ("show_text", "Show message text"),
                                         ("include_browser", "Also Discord in the browser"))):
            var = ctk.BooleanVar(value=bool(self.settings[key]))
            ctk.CTkSwitch(switches, text=text, variable=var,
                          command=lambda k=key, v=var: self.set_setting(k, v.get())).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=3)
        bar = t.tag(ctk.CTkFrame(top, fg_color="transparent"), "transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 14))
        ctk.CTkButton(bar, text="Test heads-up", width=130, command=self.test).pack(side="left")
        ids = self.app_ids()
        hidden = bool(ids) and IS_WINDOWS and all(banner_hidden(a) for a in ids)
        t.tag(ctk.CTkButton(bar, text="Show Windows' Discord pop-ups" if hidden else "Hide Windows' Discord pop-ups",
                            width=230, command=self.toggle_windows_banners,
                            state="normal" if ids else "disabled"), "secondary").pack(side="left", padx=8)
        t.tag(ctk.CTkButton(bar, text="Windows notification settings", width=210,
                            command=self.open_windows_settings), "ghost").pack(side="left")

        feed = t.tag(ctk.CTkFrame(page), "card")
        feed.grid(row=1, column=0, sticky="ew", pady=(0, 14), padx=(0, 8))
        feed.grid_columnconfigure(0, weight=1)
        t.tag(ctk.CTkLabel(feed, text="Recent messages", anchor="w"), "title", font_size=15).grid(
            row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        t.tag(ctk.CTkLabel(feed, anchor="w", justify="left", wraplength=720,
                           text="Add a note to anyone — it appears on every pop-up from them. "
                                "Notes stay on this PC."), "muted").grid(row=1, column=0, sticky="ew", padx=16)
        self.feed_box = t.tag(ctk.CTkFrame(feed, fg_color="transparent"), "transparent")
        self.feed_box.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 14))
        self.feed_box.grid_columnconfigure(1, weight=1)
        self._fill_feed()

        people = t.tag(ctk.CTkFrame(page), "card")
        people.grid(row=2, column=0, sticky="ew", pady=(0, 14), padx=(0, 8))
        people.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(people, text="Your notes", anchor="w"), "title", font_size=15).grid(
            row=0, column=0, columnspan=3, sticky="ew", padx=16, pady=(14, 6))
        if not self.notes:
            t.tag(ctk.CTkLabel(people, text="No notes yet.", anchor="w"), "muted").grid(
                row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 14))
        for i, (key, entry) in enumerate(sorted(self.notes.items()), start=1):
            t.tag(ctk.CTkLabel(people, text=entry.get("name", key), anchor="w", width=160,
                               font=t.font(13, "bold")), "text").grid(row=i, column=0, sticky="w", padx=(16, 8), pady=3)
            box = ctk.CTkEntry(people)
            box.insert(0, entry.get("note", ""))
            box.grid(row=i, column=1, sticky="ew", pady=3)
            box.bind("<Return>", lambda _e, n=entry.get("name", key), b=box: (self.set_note(n, b.get()), self._rebuild()))
            t.tag(ctk.CTkButton(people, text="Delete", width=70,
                                command=lambda n=entry.get("name", key): (self.set_note(n, ""), self._rebuild())),
                  "ghost").grid(row=i, column=2, sticky="e", padx=(8, 16), pady=3)
        if self.notes:
            ctk.CTkLabel(people, text="", height=6).grid(row=len(self.notes) + 1, column=0)

        self._built = True
        t.apply(page)
        self._refresh_status()

    def _fill_feed(self) -> None:
        t = self.ctx.theme
        box = self.feed_box
        for child in box.winfo_children():
            child.destroy()
        self._thumbs = []
        if not self.feed:
            t.tag(ctk.CTkLabel(box, text="Nothing yet — new Discord messages show up here.", anchor="w"),
                  "muted").grid(row=0, column=0, columnspan=3, sticky="w", padx=4)
            return
        for row, msg in enumerate(self.feed[:15]):
            image = None
            if msg.avatar is not None:
                try:
                    round_img = self._round(msg.avatar, 40)
                    image = ctk.CTkImage(light_image=round_img, dark_image=round_img, size=(40, 40))
                    self._thumbs.append(image)
                except Exception:
                    image = None
            t.tag(ctk.CTkLabel(box, text="" if image else msg.sender[:1].upper(), image=image, width=44, height=44,
                               font=t.font(16, "bold")), "accent").grid(row=row * 2, column=0, rowspan=2,
                                                                         sticky="n", padx=(4, 10), pady=(6, 0))
            head = f"{msg.sender}    {msg.where}    {time.strftime('%H:%M', time.localtime(msg.arrived))}"
            t.tag(ctk.CTkLabel(box, text=head, anchor="w", font=t.font(13, "bold")), "text").grid(
                row=row * 2, column=1, sticky="ew", pady=(6, 0))
            t.tag(ctk.CTkLabel(box, text=(msg.text or "(attachment)")[:240], anchor="w", justify="left",
                               wraplength=480), "muted").grid(row=row * 2 + 1, column=1, sticky="ew")
            note = ctk.CTkEntry(box, width=220, placeholder_text="add a note…")
            existing = self.note_for(msg.sender)
            if existing:
                note.insert(0, existing)
            note.grid(row=row * 2, column=2, rowspan=2, sticky="e", padx=(8, 4))
            note.bind("<Return>", lambda _e, s=msg.sender, n=note: (self.set_note(s, n.get()), self._rebuild()))
        t.apply(box)

    @staticmethod
    def _round(img: Any, size: int) -> Any:
        from PIL import Image, ImageDraw, ImageOps
        img = ImageOps.fit(img.convert("RGBA"), (size * 3, size * 3))
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, img.size[0] - 1, img.size[1] - 1), fill=255)
        img.putalpha(mask)
        return img.resize((size, size))

    def _schedule_feed_refresh(self) -> None:
        if not self._built or self._feed_after is not None:
            return

        def run() -> None:
            self._feed_after = None
            try:
                self._fill_feed()
            except Exception:
                pass
        self._feed_after = self.ctx.app.after(300, run)

    def _refresh_status(self) -> None:
        if not self._built:
            return
        texts = {
            "starting": ("Starting…", "muted"),
            "watching": ("● Watching for Discord messages. Keep Discord's desktop notifications switched on.",
                         "success"),
            "missing": ("Windows' notification list wasn't found on this PC — make sure notifications are on in "
                        "Windows Settings ▸ System ▸ Notifications.", "warning"),
            "unsupported": ("Discord Heads-Up works on Windows only.", "warning"),
            "error": (f"Couldn't read Windows' notification list: {self.error}", "danger"),
        }
        text, role = texts.get(self.state, ("", "muted"))
        try:
            self.status_label.configure(text=text)
            self.ctx.theme.tag(self.status_label, role)
            self.ctx.theme.apply(self.status_label)
        except Exception:
            pass
