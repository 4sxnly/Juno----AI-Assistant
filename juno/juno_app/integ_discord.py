"""
Discord integration.

Reads your messages from Windows' notification list (the same pop-ups Discord shows), so it needs no login,
no bot and no token — just Discord's desktop notifications switched on. It can:
  • read new messages, grouped by person, or only one person's ("what did Kai say")
  • summarise them with the AI brain
  • open Discord, your DMs, a chat with someone or a server (through Discord's Ctrl+K quick switcher)
  • write a message for you — typed into the box, and only sent if you turn "Send for me" on
  • optionally announce new messages as they arrive (avatar bubble, and read aloud if you like)
"""

from __future__ import annotations

import ctypes
import difflib
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .integrations import OPEN, Action, ActionResult, Field, Integration, IntegrationAPI, send_keys

IS_WINDOWS = sys.platform == "win32"
TITLE_RE = re.compile(r"^(?P<name>.+?)\s+\((?P<ctx>[^()]*)\)\s*$")
ANNOUNCE = ["Off", "Avatar bubble", "Bubble + read aloud"]


@dataclass
class DiscordMessage:
    id: int
    sender: str
    text: str
    server: str = ""
    channel: str = ""
    arrived: float = 0.0          # unix time

    @property
    def is_dm(self) -> bool:
        return not self.server and not self.channel

    @property
    def where(self) -> str:
        if self.is_dm:
            return "DM"
        return " ".join(x for x in (self.server, self.channel) if x)


def parse_title(title: str) -> tuple[str, str, str]:
    """'nyx (#general, Midnight Club)' → ('nyx', 'Midnight Club', '#general'). DMs have no brackets."""
    title = (title or "").strip()
    match = TITLE_RE.match(title)
    if not match:
        return title, "", ""
    parts = [p.strip() for p in match.group("ctx").split(",") if p.strip()]
    channel = next((p for p in parts if p.startswith("#")), "")
    server = ", ".join(p for p in parts if p != channel)
    return match.group("name").strip(), server, channel


def parse_payload(payload: bytes | str) -> list[str]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    # browsers add the site ("discord.com") as an attribution line — not part of the message
    return [t for t in ((x.text or "").strip() for x in root.iter("text") if x.get("placement") != "attribution")
            if t]


def notifications_db() -> Path:
    return Path(os.environ.get("MT_WPN_DB") or (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" /
                                                "Notifications" / "wpndatabase.db"))


def read_messages(after_id: int = 0, limit: int = 60) -> list[DiscordMessage] | None:
    """Discord toasts (desktop app or Discord in a browser), oldest first. None if Windows' list can't be read."""
    db = notifications_db()
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT n.Id, n.ArrivalTime, n.Payload, h.PrimaryId FROM Notification n JOIN NotificationHandler h "
            "ON n.HandlerId = h.RecordId WHERE n.Type='toast' AND n.Id > ? AND (lower(h.PrimaryId) LIKE "
            "'%discord%' OR lower(CAST(n.Payload AS TEXT)) LIKE '%discord.com%') ORDER BY n.Id DESC LIMIT ?",
            (after_id, limit)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    out = []
    for row_id, arrival, payload, handler in reversed(rows):
        texts = parse_payload(payload)
        if not texts:
            continue
        sender, server, channel = parse_title(texts[0])
        body = " ".join(texts[1:]).strip() or "(sent an attachment)"
        when = (int(arrival) / 1e7 - 11644473600) if arrival and int(arrival) > 1e15 else 0.0   # FILETIME
        out.append(DiscordMessage(int(row_id), sender or "Someone", body[:400], server, channel, when))
    return out


def recent_discord_notifications(limit: int = 5) -> list[tuple[str, str]] | None:
    """Older helper kept for user integrations: [(sender, text)], newest first."""
    msgs = read_messages(0, limit)
    return None if msgs is None else [(m.sender, m.text) for m in reversed(msgs)]


def name_matches(wanted: str, sender: str) -> bool:
    a, b = re.sub(r"[^\w]", "", wanted.lower()), re.sub(r"[^\w]", "", sender.lower())
    return bool(a) and (a == b or a in b or (len(a) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() > 0.75))


def ago(ts: float) -> str:
    if not ts:
        return ""
    s = max(0, time.time() - ts)
    return "just now" if s < 60 else f"{int(s // 60)}m ago" if s < 3600 else f"{int(s // 3600)}h ago" \
        if s < 86400 else f"{int(s // 86400)}d ago"


# ── typing into Discord (Windows) ───────────────────────────────────────────
def set_clipboard(text: str) -> str | None:
    """Put text on the clipboard; returns what was there before (to restore)."""
    if not IS_WINDOWS:
        raise RuntimeError("needs Windows")
    import win32clipboard  # bundled with Juno (pywin32)
    old = None
    win32clipboard.OpenClipboard()
    try:
        try:
            old = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            old = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
    return old


def focus_discord() -> bool:
    """Bring Discord's window to the front. True if found."""
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _l: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(hwnd, buf, 300)
        title = buf.value
        if title == "Discord" or title.endswith(" - Discord"):
            found.append(hwnd)
            return False
        return True
    user32.EnumWindows(proto(visit), 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)   # tapping Alt lets a background app take focus
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    return True


class Discord(Integration):
    id, name, icon, category = "discord", "Discord", "✉", "Social"
    aliases = ["discord"]
    description = ("Reads your new Discord messages (or one person's), sums them up, opens chats and servers, and "
                   "writes messages for you. Uses Discord's Windows notifications — no login needed. Keep "
                   "Discord's desktop notifications on.")
    fields = [
        Field("announce", "Announce new messages", kind="choice", default="Off", choices=ANNOUNCE,
              help="Juno tells you when a message arrives. Respects Do Not Disturb and mute."),
        Field("dms_only", "Only announce DMs", kind="bool", default=True),
        Field("send_for_me", "Send messages for me", kind="bool", default=False,
              help="Off: Juno types the message into Discord and you press Enter. On: Juno sends it."),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def actions(self) -> list[Action]:
        return [
            Action("open", "Open Discord", [f"{OPEN} discord"], self.open_home),
            Action("dms", "Open my DMs", [f"{OPEN} [my] (discord dms|discord messages|dms|direct messages)",
                                          "go to my dms"], self.open_home),
            Action("chat", "Open a chat with someone",
                   [f"{OPEN} [my] (dm|dms|chat|messages|conversation) with {{name}} [on discord]",
                    "(go to|show me) [my] (dm|dms|chat) with {name}", "(dm|message|text) {name} on discord"],
                   self.open_chat, params={"name": "their Discord name"}, examples=["open my dms with kai"]),
            Action("server", "Open a server or channel",
                   [f"{OPEN} [the] {{server}} (discord server|server on discord)",
                    "(go to|open) {server} on discord", "(go to|open) [the] channel {channel} [on discord]"],
                   self.open_server, params={"server": "server name"}, examples=["open the midnight club server "
                                                                                  "on discord"]),
            Action("send", "Message someone",
                   ["(message|dm|text|tell) {name} on discord (saying|that) {text}",
                    "(message|dm|text) {name} (saying|that) {text}", "tell {name} on discord {text}",
                    "(reply to|answer) {name} [on discord] (saying|with) {text}",
                    "send {name} a [discord] message (saying|that) {text}"],
                   self.send, params={"name": "who", "text": "the message"},
                   examples=["message kai on discord saying be on in 5"]),
            Action("unread", "Read my new messages",
                   ["(any|do i have [any]) (new|unread) [discord] messages", "read [me] my [new] discord messages",
                    "read [me] my [new] messages", "check [my] discord", "who messaged me [on discord]",
                    "what's new on discord", "did anyone message me [on discord]", "any messages"],
                   self.unread, examples=["read my discord messages"]),
            Action("from", "Messages from someone",
                   ["what did {name} (say|send|text) [on discord] [me]",
                    "(any|did i get [any]) messages from {name} [on discord]",
                    "did {name} (message|dm|text) me [on discord]", "read {name}'s (messages|message|dms)",
                    "read [me] [the] messages from {name}"],
                   self.from_person, params={"name": "who"}, examples=["what did kai say"]),
            Action("summary", "Sum up my messages",
                   ["(summarize|summarise|sum up|recap) [my] [recent] discord [messages|chats]",
                    "(summarize|summarise|sum up|recap) my messages", "what did i miss on discord"],
                   self.summary),
            Action("clear", "Mark as read", ["mark [my] discord [messages] [as] read",
                                             "(clear|dismiss) [my] discord (messages|notifications)"], self.mark_read),
        ]

    def status(self) -> tuple[str, str]:
        if not IS_WINDOWS and not os.environ.get("MT_WPN_DB"):
            return "Windows only", "muted"
        if not notifications_db().exists():
            return "Windows notifications not found", "warning"
        mode = self.setting("announce", "Off")
        return ("Ready · announcing new messages" if mode in ANNOUNCE[1:] else "Ready"), "success"

    # ── lifecycle: the optional live watcher ─────────────────────────────
    def on_start(self) -> None:
        if self.setting("announce", "Off") in ANNOUNCE[1:] and (self._watcher is None or not self._watcher.is_alive()):
            self._stop = threading.Event()
            self._watcher = threading.Thread(target=self._watch, args=(self._stop,), daemon=True,
                                             name="discord-watch")
            self._watcher.start()

    def on_stop(self) -> None:
        self._stop.set()
        self._watcher = None

    def on_settings_changed(self) -> None:
        self.on_stop()
        self.on_start()

    def _watch(self, stop: threading.Event) -> None:
        latest = read_messages(0, 1) or []
        seen = latest[-1].id if latest else 0
        while not stop.wait(4.0):
            try:
                fresh = read_messages(seen, 20) or []
            except Exception as exc:  # noqa: BLE001
                self.api.log.warning("Discord watcher: %s", exc)
                continue
            if not fresh:
                continue
            seen = fresh[-1].id
            if self.setting("dms_only", True):
                fresh = [m for m in fresh if m.is_dm]
            if fresh:
                self.api.ctx.call_in_ui(self._announce, fresh)

    def _announce(self, messages: list[DiscordMessage]) -> None:
        core = self.api.core
        mode = self.setting("announce", "Off")
        if mode == "Off" or core.settings.get("dnd"):
            return
        m = messages[-1]
        more = f" (+{len(messages) - 1} more)" if len(messages) > 1 else ""
        where = "" if m.is_dm else f" in {m.server or m.channel}"
        try:
            core.avatar.say(f"✉ {m.sender}{where}: {m.text[:120]}{more}", 8)
        except Exception:
            pass
        core.log_activity(f"Discord · {m.sender}", m.text, "info", self.id)
        if mode == ANNOUNCE[2] and core.can_speak():
            line = f"{m.sender} says {m.text[:140]}" if len(messages) == 1 else \
                f"{len(messages)} new Discord messages. Latest from {m.sender}: {m.text[:100]}"
            core.speak(line)

    # ── reading ──────────────────────────────────────────────────────────
    def _read(self, after: int = 0, limit: int = 60) -> list[DiscordMessage]:
        msgs = read_messages(after, limit)
        if msgs is None:
            raise RuntimeError("I can't read Windows' notification list on this PC. Make sure Discord's desktop "
                               "notifications are on (Discord ▸ Settings ▸ Notifications).")
        return msgs

    @staticmethod
    def _grouped(msgs: list[DiscordMessage]) -> tuple[str, str]:
        """→ (text for the chat, short spoken version)."""
        groups: dict[tuple[str, str], list[DiscordMessage]] = {}
        for m in msgs:
            groups.setdefault((m.sender, m.where), []).append(m)
        lines, spoken = [], []
        for (sender, where), items in groups.items():
            lines.append("  ·  ".join(x for x in (sender, "" if where == "DM" else where, ago(items[-1].arrived)) if x))
            lines += [f"    {m.text}" for m in items[-6:]]
            if len(items) > 6:
                lines.append(f"    …and {len(items) - 6} earlier")
            if len(spoken) < 3:
                said = ". ".join(m.text for m in items[-2:])[:160]
                spoken.append(f"{sender} sent {len(items)} messages: {said}" if len(items) > 1
                              else f"{sender} said {said}")
        if len(groups) > 3:
            spoken.append(f"and {len(groups) - 3} more people")
        return "\n".join(lines), ". ".join(spoken)

    def unread(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        last = int(self.setting("last_read", 0) or 0)
        msgs = self._read(last)
        if not msgs:
            latest = self._read(0, 1)
            tail = f" The last one was from {latest[-1].sender} {ago(latest[-1].arrived)}." if latest else ""
            return ActionResult("No new Discord messages." + tail)
        api.set(self.id, "last_read", msgs[-1].id)
        text, spoken = self._grouped(msgs)
        people = len({m.sender for m in msgs})
        head = f"{len(msgs)} new Discord message{'s' * (len(msgs) != 1)} from {people} " \
               f"{'person' if people == 1 else 'people'} —"
        return ActionResult(head + "\n" + text, say=spoken)

    def from_person(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params.get("name", "").strip()
        msgs = [m for m in self._read(0, 200) if name_matches(name, m.sender)]
        if not msgs:
            return ActionResult(f"Nothing from {name} in your recent Discord notifications.")
        msgs = msgs[-8:]
        who = msgs[-1].sender
        lines = [f"    {m.text}" + (f"   ({ago(m.arrived)})" if m.arrived else "") for m in msgs]
        said = ". ".join(m.text for m in msgs[-3:])[:220]
        return ActionResult(f"Latest from {who}" + ("" if msgs[-1].is_dm else f" · {msgs[-1].where}") + " —\n"
                            + "\n".join(lines), say=f"{who} said: {said}")

    def summary(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        msgs = self._read(0, 40)
        if not msgs:
            return ActionResult("No recent Discord messages to sum up.")
        text, spoken = self._grouped(msgs)
        core = api.core
        if not core._brain_ready():
            return ActionResult("(No AI brain set up, so here's the list.)\n" + text, say=spoken)
        transcript = "\n".join(f"[{m.where}] {m.sender}: {m.text}" for m in msgs)
        manifest = core.local_manifest() if core.settings["provider"] == "local" else None
        reply = core.complete("You summarise someone's recent Discord notifications for them. 2-4 short sentences. "
                              "Group by person, say who wants something or asked a question, skip small talk. "
                              "No preamble.", [{"role": "user", "text": transcript, "images": []}], manifest,
                              max_tokens=220).strip()
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip() or spoken
        return ActionResult(reply)

    def mark_read(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        latest = self._read(0, 1)
        if latest:
            api.set(self.id, "last_read", latest[-1].id)
        return ActionResult("Marked your Discord messages as read.", say="Done.")

    # ── opening + writing ────────────────────────────────────────────────
    def open_home(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_uri("discord://-/channels/@me")
        return ActionResult("Opened your Discord DMs." if "dm" in params.get("_text", "") or "message" in
                            params.get("_text", "") else "Opened Discord.")

    def _switch_to(self, api: IntegrationAPI, query: str) -> None:
        """Open Discord and jump with the Ctrl+K quick switcher ('@name', '*server', '#channel')."""
        if not IS_WINDOWS:
            raise RuntimeError("this needs Windows")
        if not focus_discord():
            api.open_uri("discord://-/channels/@me")
            for _ in range(40):
                time.sleep(0.25)
                if focus_discord():
                    time.sleep(1.5)        # let it finish loading
                    break
            else:
                raise RuntimeError("Discord didn't open in time — open it and try again.")
        time.sleep(0.35)
        send_keys("esc")
        send_keys("ctrl+k")
        time.sleep(0.35)
        old = set_clipboard(query)
        send_keys("ctrl+v")
        time.sleep(0.9)                    # results appear as you type
        send_keys("enter")
        time.sleep(0.6)
        if old is not None:
            try:
                set_clipboard(old)
            except Exception:
                pass

    def open_chat(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params.get("name", "").strip()
        self._switch_to(api, "@" + name)
        return ActionResult(f"Opened your chat with {name}.")

    def open_server(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if params.get("channel"):
            self._switch_to(api, "#" + params["channel"].strip().lstrip("#"))
            return ActionResult(f"Opened #{params['channel'].strip().lstrip('#')}.")
        server = re.sub(r"^the\s+", "", params.get("server", "").strip())
        self._switch_to(api, "*" + server)
        return ActionResult(f"Opened {server} on Discord.")

    def send(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name, text = params.get("name", "").strip(), params.get("text", "").strip()
        if not text:
            return self.open_chat(api, params)
        name = re.sub(r"\s+on discord$", "", name)
        text = text[:1].upper() + text[1:]
        self._switch_to(api, "@" + name)
        time.sleep(0.5)
        old = set_clipboard(text)
        send_keys("ctrl+v")
        time.sleep(0.25)
        sent = bool(self.setting("send_for_me", False))
        if sent:
            send_keys("enter")
            time.sleep(0.3)
        if old is not None:
            try:
                set_clipboard(old)
            except Exception:
                pass
        if sent:
            return ActionResult(f"Sent to {name}: “{text}”", say="Sent.")
        return ActionResult(f"Typed your message to {name} — check it and press Enter to send.\n“{text}”",
                            say="It's typed in. Press enter to send.")
