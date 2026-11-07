"""
Juno — your desktop assistant.

The main window is a green-phosphor command center: sidebar navigation, a live dashboard around Juno's
avatar, the chat, and pages for integrations, phrases, avatar, brain & voice, memory and settings.
Mini mode hides the window and keeps Juno's avatar on the desktop with a small chat panel.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import math
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable

import customtkinter as ctk

from . import base as B
from . import engine as E
from .base import (APP_NAME, IS_FROZEN, IS_WINDOWS, AppServices, SettingsStore, Telemetry, ThemeManager,
                   ToastManager, UpdateManager, _DnDBase)
from .core import JUNO_DEFAULTS, LISTEN_MODES, JunoContext, JunoCore
from .integrations import (TEMPLATE_PACK, TEMPLATE_PY, Field, PackIntegration, Spotify, example_of,
                           load_python_integrations, normalize)
from .orb import ORB_STYLES, OrbRenderer

APP_VERSION = "1.0.0"
B.APP_VERSION = APP_VERSION

NAV = [
    ("home", "Command Center", "◈"),
    ("chat", "Chat", "❯"),
    ("integrations", "Integrations", "⌘"),
    ("phrases", "Phrases", "❝"),
    ("avatar", "Avatar", "◉"),
    ("brain", "Brain & Voice", "◎"),
    ("personality", "Personality", "☰"),
    ("memory", "Memory", "▤"),
    ("activity", "Activity", "≡"),
    ("permissions", "Permissions", "⛨"),
    ("settings", "Settings", "⚙"),
]
LEVEL_ROLE = {"ok": "success", "info": "accent", "warn": "warning", "error": "danger"}
LEVEL_TAG = {"ok": "DONE", "info": "INFO", "warn": "WARN", "error": "FAIL"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ════════════════════════════════════════════════════════════════════════════
#  Updates for Juno's own files
# ════════════════════════════════════════════════════════════════════════════

class JunoUpdater(UpdateManager):
    RELEASE_TAG_RE = re.compile(r"^juno-build-(\d+)$")
    INSTALLER_PREFIX = "juno"
    INSTALLER_PATHS = ("juno/requirements.txt", "juno/launcher.py", "juno/build/", "juno/assets/",
                       ".github/workflows/build-juno.yml", "build/fetch_engines.ps1")

    def _map(self, path: str) -> tuple[Path, str] | None:
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts) or "\\" in path or ":" in path:
            return None
        if parts[:2] == ["juno", "juno_app"] and len(parts) >= 3 and "__pycache__" not in parts \
                and path.endswith(".py"):
            return self.paths.app_dir.joinpath("core", "juno_app", *parts[2:]), "core"
        if parts[:2] == ["juno", "integrations"] and len(parts) == 3:
            return self.paths.plugins_dir / parts[2], "plugin"
        if parts[:3] == ["juno", "extras", "characters"] and len(parts) == 4 and path.endswith(".mtchar"):
            return self.paths.app_dir / "extras" / "characters" / parts[3], "character"
        return None


# ════════════════════════════════════════════════════════════════════════════
#  Small drawn widgets
# ════════════════════════════════════════════════════════════════════════════

class Dot(tk.Canvas):
    def __init__(self, master: Any, color: str, size: int = 10):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self._mt_role = "transparent"
        self.size = size
        self.set(color)

    def set(self, color: str) -> None:
        self.delete("all")
        s = self.size
        self.create_oval(1, 1, s - 1, s - 1, fill=color, outline="")


class Ring(tk.Canvas):
    """Circular gauge (CPU / RAM / disk)."""

    def __init__(self, master: Any, label: str, palette: dict[str, Any], size: int = 92):
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0)
        self._mt_role = "transparent"
        self.label, self.p, self.size = label, palette, size
        self.value: float | None = None
        self.draw(None)

    def draw(self, value: float | None) -> None:
        self.value = value
        self.delete("all")
        s, p, w = self.size, self.p, 7
        box = (w, w, s - w, s - w)
        self.create_oval(*box, outline=p["dim"], width=w)
        if value is not None:
            color = p["danger"] if value >= 90 else p["warning"] if value >= 75 else p["accent"]
            self.create_arc(*box, start=90, extent=-3.6 * max(1.0, value), style="arc", outline=color, width=w)
        self.create_text(s / 2, s / 2 - 8, text=self.label, fill=p["text_muted"], font=(p["mono_font"], 9, "bold"))
        self.create_text(s / 2, s / 2 + 9, text="--" if value is None else f"{value:.0f}%", fill=p["title"],
                         font=(p["mono_font"], 13, "bold"))


class Wave(tk.Canvas):
    """Animated voice waveform (sidebar / talk button)."""

    def __init__(self, master: Any, palette: dict[str, Any], width: int, height: int, bars: int = 34):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0)
        self._mt_role = "transparent"
        self.p, self.w, self.h, self.n = palette, width, height, bars
        self.levels = [0.1] * bars
        self.items = []
        gap = width / bars
        for i in range(bars):
            x = gap * i + gap / 2
            self.items.append(self.create_line(x, height / 2, x, height / 2, fill=palette["accent"], width=2))

    def step(self, energy: float, color: str) -> None:
        t = time.monotonic()
        for i, item in enumerate(self.items):
            target = 0.08 + energy * (0.35 + 0.65 * abs(math.sin(i * 0.9 + t * 7) * math.sin(i * 0.37 - t * 3)))
            self.levels[i] += (target - self.levels[i]) * 0.4
            half = max(1.0, self.levels[i] * self.h / 2)
            x = self.coords(item)[0]
            self.coords(item, x, self.h / 2 - half, x, self.h / 2 + half)
            self.itemconfigure(item, fill=color)


# ════════════════════════════════════════════════════════════════════════════
#  The window
# ════════════════════════════════════════════════════════════════════════════

class JunoApp(ctk.CTk, _DnDBase, AppServices):
    def __init__(self, paths: B.AppPaths, log: Any, start_mini: bool = False):
        super().__init__()
        self.paths = paths
        self.log = log
        self.dnd_available = False
        self.dnd_version: str | None = None
        self.dnd_error: str | None = None
        self._init_dnd()
        self.settings = SettingsStore(paths.settings_file, log)
        self.models_dir = models_dir(paths)
        self.migration_notes = migrate_from_multitool(paths, self.settings, log, self.models_dir)
        self.theme = ThemeManager(self, self.settings, paths.themes_dir, log)
        self.toasts = ToastManager(self)
        self.updater = JunoUpdater(paths, self.settings, log)
        self.file_types: dict[str, tuple[str, Callable[[Path], Any]]] = {}
        self._ui_queue: "queue.Queue[tuple[Callable[..., Any], tuple, dict]]" = queue.Queue()
        self._closing = False
        self.mini = False
        self.current_page = ""
        self.pages: dict[str, Any] = {}
        self.nav_buttons: dict[str, Any] = {}
        self.home: dict[str, Any] = {}
        self._status_text = "Ready"
        self._status_after: Any = None
        self._cursor = True
        self._tick_n = 0
        self._telemetry = Telemetry()
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_tid = 0
        self.palette = self.theme.palette

        self.title(APP_NAME)
        self.minsize(1100, 700)
        self._restore_geometry()
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self.configure(fg_color=self.palette["bg"])

        self.ctx = JunoContext(self)
        self.core = JunoCore(self.ctx, self)
        self._build_shell()
        self.theme.apply()
        self.show_page("home")
        self.register_drop_target(self, self._on_drop, ("files", "text"))
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind("<Control-k>", lambda _e: self.command.focus_set())
        self.after(20, self._pump_ui_queue)
        self.after(400, self._tick)
        self.after(70, self._anim)
        self.after(30_000, self._auto_update_check)
        self.after(300, self._start_hotkey)
        self.after(600, self.core.power_on)
        self.after(900, self.core._startup)
        if self.core.settings.get("listen_mode") in ("talk", "wake"):
            self.after(1500, lambda: self.core.set_listen_mode(self.core.settings["listen_mode"]))
        for note in self.migration_notes:
            self.after(2500, lambda n=note: self.notify("Welcome to Juno", n, subtitle="Moved over from Multi-Tool",
                                                        icon="J", duration=14))
        if start_mini or self.core.settings.get("start_mini"):
            self.after(200, self.enter_mini)
        elif self.core.settings.get("boot", True):
            self.after(60, self._boot)

    # ── layout ───────────────────────────────────────────────────────────
    def _build_shell(self) -> None:
        t, p = self.theme, self.palette
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        side = t.tag(ctk.CTkFrame(self, width=238, corner_radius=0), "sidebar", keep_shape=True)
        side.grid(row=0, column=0, rowspan=2, sticky="nsw")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)
        self._logo(side).grid(row=0, column=0, sticky="ew", padx=12, pady=(14, 8))
        t.tag(ctk.CTkFrame(side, height=1, corner_radius=0), "divider").grid(row=1, column=0, sticky="new", padx=12)
        nav = t.tag(ctk.CTkFrame(side, fg_color="transparent"), "transparent")
        nav.grid(row=2, column=0, sticky="nsew", padx=10, pady=(10, 0))
        nav.grid_columnconfigure(0, weight=1)
        for i, (key, label, glyph) in enumerate(NAV):
            btn = t.tag(ctk.CTkButton(nav, text=f" {glyph}   {label}", anchor="w", height=34, corner_radius=3,
                                      command=lambda k=key: self.show_page(k)), "nav")
            btn.grid(row=i, column=0, sticky="ew", pady=1)
            badge = t.tag(ctk.CTkLabel(btn, text="", height=16, width=26, corner_radius=3, fg_color=p["selected"],
                                       font=t.font(10, "bold")), "accent")
            badge._mt_keep_font = True
            btn._badge = badge
            self.nav_buttons[key] = btn
        self._voice_box(side).grid(row=3, column=0, sticky="ew", padx=10, pady=(6, 8))
        bar = t.tag(ctk.CTkFrame(side, fg_color="transparent"), "transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 12))
        bar.grid_columnconfigure((0, 1), weight=1)
        t.tag(ctk.CTkButton(bar, text="⧉ Mini mode", height=30, command=self.enter_mini), "secondary").grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        t.tag(ctk.CTkButton(bar, text="⏻ Quit", height=30, command=self.quit_app), "ghost").grid(
            row=0, column=1, sticky="ew", padx=(3, 0))

        main = t.tag(ctk.CTkFrame(self, corner_radius=0), "surface", keep_shape=True)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        self._topbar(main).grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        self.viewport = t.tag(ctk.CTkFrame(main, fg_color="transparent"), "transparent")
        self.viewport.grid(row=1, column=0, sticky="nsew", padx=18)
        self.viewport.grid_columnconfigure(0, weight=1)
        self.viewport.grid_rowconfigure(0, weight=1)
        self._bottombar(main).grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 6))

        line = t.tag(ctk.CTkFrame(self, height=24, corner_radius=0), "statusbar", keep_shape=True)
        line.grid(row=1, column=1, sticky="ew")
        line.grid_columnconfigure(0, weight=1)
        self.status_label = t.tag(ctk.CTkLabel(line, text="", anchor="w", height=22, font=t.font(11)), "muted")
        self.status_label._mt_keep_font = True
        self.status_label.grid(row=0, column=0, sticky="ew", padx=12)
        build = f" · build {self.updater.build}" if self.updater.build else ""
        t.tag(ctk.CTkLabel(line, text=f"JUNO v{APP_VERSION}{build}  ", height=22, font=t.font(10)), "muted").grid(
            row=0, column=1, sticky="e")
        self._render_status()

    def _logo(self, parent: Any) -> Any:
        p = self.palette
        c = tk.Canvas(parent, width=214, height=64, highlightthickness=0, bd=0)
        c._mt_role = "transparent"
        cx, cy = 30, 32
        for r, color, w in ((25, p["dim"], 1), (21, p["glow"], 2), (14, p["accent"], 2)):
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w)
        c.create_arc(cx - 25, cy - 25, cx + 25, cy + 25, start=30, extent=80, style="arc", outline=p["accent"], width=3)
        c.create_arc(cx - 25, cy - 25, cx + 25, cy + 25, start=210, extent=80, style="arc", outline=p["accent"], width=3)
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=p["accent2"], outline="")
        for dx in (-1, 1):
            c.create_text(66 + dx, 24, text="J U N O", anchor="w", fill=p["glow"], font=(p["mono_font"], 21, "bold"))
        c.create_text(66, 24, text="J U N O", anchor="w", fill=p["accent"], font=(p["mono_font"], 21, "bold"))
        c.create_text(67, 47, text="COMMAND CENTER", anchor="w", fill=p["text_muted"], font=(p["mono_font"], 9, "bold"))
        return c

    def _voice_box(self, parent: Any) -> Any:
        t, p = self.theme, self.palette
        box = t.tag(ctk.CTkFrame(parent), "card")
        box.grid_columnconfigure(0, weight=1)
        t.tag(ctk.CTkLabel(box, text="VOICE STATUS", anchor="w", font=t.font(10, "bold")), "accent").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.side_wave = Wave(box, p, 190, 34)
        self.side_wave.grid(row=1, column=0, pady=(4, 0))
        self.voice_state = t.tag(ctk.CTkLabel(box, text="Idle", height=18, font=t.font(11)), "muted")
        self.voice_state.grid(row=2, column=0)
        self.mic = tk.Canvas(box, width=84, height=84, highlightthickness=0, bd=0, cursor="hand2")
        self.mic._mt_role = "transparent"
        self.mic.grid(row=3, column=0, pady=(4, 0))
        self.mic.bind("<Button-1>", lambda _e: self.core.toggle_voice_chat())
        t.tag(ctk.CTkLabel(box, text="Tap to talk · Ctrl+Alt+J", height=16, font=t.font(10)), "muted").grid(
            row=4, column=0)
        self.mode_seg = ctk.CTkSegmentedButton(box, values=list(LISTEN_MODES.values()), height=26,
                                               command=self._mode_chosen)
        self.mode_seg.grid(row=5, column=0, sticky="ew", padx=10, pady=(8, 10))
        self.refresh_voice()
        return box

    def _mode_chosen(self, label: str) -> None:
        mode = next(k for k, v in LISTEN_MODES.items() if v == label)
        self.core.set_listen_mode(mode)

    def _draw_mic(self) -> None:
        p, c = self.palette, self.mic
        state = self.core.listener.state
        on = state not in ("off",)
        color = {"hearing": p["accent2"], "muted": p["danger"], "loading": p["warning"]}.get(state, p["accent"])
        c.delete("all")
        pulse = (math.sin(time.monotonic() * 4) * 0.5 + 0.5) if on else 0
        for r, col, w in ((40, p["dim"], 1), (34 + 3 * pulse, color if on else p["glow"], 2), (27, p["glow"], 1)):
            c.create_oval(42 - r, 42 - r, 42 + r, 42 + r, outline=col, width=w)
        c.create_oval(17, 17, 67, 67, fill=p["selected"] if on else p["card_hover"], outline=color, width=2)
        # microphone glyph
        c.create_rectangle(37, 27, 47, 45, fill=color, outline="")
        c.create_oval(37, 22, 47, 32, fill=color, outline="")
        c.create_oval(37, 40, 47, 50, fill=color, outline="")
        c.create_arc(31, 30, 53, 52, start=200, extent=140, style="arc", outline=color, width=2)
        c.create_line(42, 52, 42, 58, fill=color, width=2)
        c.create_line(36, 58, 48, 58, fill=color, width=2)
        if state == "muted":
            c.create_line(28, 28, 56, 56, fill=p["danger"], width=3)

    def _topbar(self, parent: Any) -> Any:
        t, p = self.theme, self.palette
        bar = t.tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        bar.grid_columnconfigure(1, weight=1)
        pill = t.tag(ctk.CTkFrame(bar), "card")
        pill._mt_no_deco = True
        pill.grid(row=0, column=0, sticky="w")
        t.tag(ctk.CTkLabel(pill, text="SYSTEM STATUS", font=t.font(11, "bold")), "text").pack(side="left",
                                                                                              padx=(12, 8), pady=7)
        self.sys_dot = Dot(pill, p["accent"])
        self.sys_dot.pack(side="left")
        self.sys_text = t.tag(ctk.CTkLabel(pill, text="OPTIMAL", font=t.font(11, "bold")), "accent")
        self.sys_text._mt_keep_font = True
        self.sys_text.pack(side="left", padx=(6, 12))
        clock = t.tag(ctk.CTkFrame(bar, fg_color="transparent"), "transparent")
        clock.grid(row=0, column=1)
        self.date_label = t.tag(ctk.CTkLabel(clock, text="", height=16, font=t.font(11)), "muted")
        self.date_label._mt_keep_font = True
        self.date_label.pack()
        self.time_label = t.tag(ctk.CTkLabel(clock, text="", height=30, font=t.font(26, "bold")), "accent")
        self.time_label._mt_keep_font = True
        self.time_label.pack()
        right = t.tag(ctk.CTkFrame(bar, fg_color="transparent"), "transparent")
        right.grid(row=0, column=2, sticky="e")
        self.command = ctk.CTkEntry(right, width=340, height=34,
                                    placeholder_text="> command or question…  Ctrl+K")
        self.command.pack(side="left")
        self.command.bind("<Return>", lambda _e: self._run_command())
        for glyph, page in (("≡", "activity"), ("⚙", "settings")):
            t.tag(ctk.CTkButton(right, text=glyph, width=34, height=34, command=lambda k=page: self.show_page(k)),
                  "secondary").pack(side="left", padx=(6, 0))
        op = t.tag(ctk.CTkFrame(right), "card")
        op._mt_no_deco = True
        op.pack(side="left", padx=(8, 0))
        user = (os.environ.get("USERNAME") or getpass.getuser() or "Operator")[:16]
        t.tag(ctk.CTkLabel(op, text="OPERATOR", height=14, font=t.font(9, "bold")), "muted").pack(padx=12, pady=(5, 0))
        t.tag(ctk.CTkLabel(op, text=user, height=16, font=t.font(12, "bold")), "title").pack(padx=12, pady=(0, 5))
        return bar

    def _bottombar(self, parent: Any) -> Any:
        t, p = self.theme, self.palette
        bar = t.tag(ctk.CTkFrame(parent), "card")
        bar._mt_no_deco = True
        bar.grid_columnconfigure(1, weight=1)
        chips = t.tag(ctk.CTkFrame(bar, fg_color="transparent"), "transparent")
        chips.grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.chips: dict[str, Any] = {}
        for key, title in (("brain", "BRAIN"), ("voice", "VOICE"), ("integrations", "INTEGRATIONS")):
            chip = t.tag(ctk.CTkFrame(chips), "entry")
            chip.pack(side="left", padx=4)
            t.tag(ctk.CTkLabel(chip, text=title, height=12, font=t.font(9, "bold")), "muted").pack(
                anchor="w", padx=10, pady=(4, 0))
            value = t.tag(ctk.CTkLabel(chip, text="…", height=16, font=t.font(11, "bold")), "text")
            value._mt_keep_font = True
            value.pack(anchor="w", padx=10, pady=(0, 4))
            self.chips[key] = value
        talk = t.tag(ctk.CTkFrame(bar, fg_color="transparent"), "transparent")
        talk.grid(row=0, column=1)
        self.talk_wave_l = Wave(talk, p, 110, 30, bars=16)
        self.talk_wave_l.pack(side="left", padx=6)
        self.talk = tk.Canvas(talk, width=250, height=46, highlightthickness=0, bd=0, cursor="hand2")
        self.talk._mt_role = "transparent"
        self.talk.pack(side="left")
        self.talk.bind("<Button-1>", lambda _e: self.core.listen_once())
        self.talk_wave_r = Wave(talk, p, 110, 30, bars=16)
        self.talk_wave_r.pack(side="left", padx=6)
        t.tag(ctk.CTkButton(bar, text="⧉  MINI MODE", width=130, height=34, command=self.enter_mini), "secondary").grid(
            row=0, column=2, sticky="e", padx=10)
        return bar

    def _draw_talk(self) -> None:
        p, c = self.palette, self.talk
        state = self.core.listener.state
        c.delete("all")
        glow = (math.sin(time.monotonic() * 3) * 0.5 + 0.5) if state != "off" else 0.2
        c.create_rectangle(4, 4, 246, 42, outline=p["glow"], width=1)
        c.create_rectangle(1, 1, 249, 45, outline=p["accent"] if glow > 0.5 else p["glow"], width=1)
        c.create_rectangle(7, 7, 243, 39, fill=p["selected"] if state != "off" else p["card"], outline="")
        title = f"TALK TO {self.core.character['name'].upper()}"
        sub = {"listening": "I'm listening…", "hearing": "Hearing you…", "paused": "Speaking…",
               "muted": "Your mic is muted", "loading": "Starting…"}.get(state, "tap · or press Ctrl+Alt+J")
        if self.core.voice_out.active:
            sub = "Speaking…"
        c.create_text(125, 18, text=title, fill=p["accent"], font=(p["mono_font"], 12, "bold"))
        c.create_text(125, 33, text=sub, fill=p["text"], font=(p["mono_font"], 9))

    # ── pages ────────────────────────────────────────────────────────────
    def show_page(self, key: str) -> None:
        if key not in dict((k, 1) for k, _l, _g in NAV):
            key = "home"
        if self.mini and key != self.current_page:
            pass
        old = self.current_page
        if key not in self.pages or key in ("activity", "integrations", "phrases"):
            if key in self.pages:
                self.pages[key].destroy()
            self.pages[key] = self._build_page(key)
        for k, page in self.pages.items():
            if k != key:
                page.grid_remove()
        self.pages[key].grid(row=0, column=0, sticky="nsew")
        self.current_page = key
        for k, btn in self.nav_buttons.items():
            btn._mt_active = k == key
            self.theme.apply(btn)
        if old and old != key:
            self.theme.play("nav")
        if key == "chat":
            try:
                self.core.main_chat.entry.focus_set()
            except Exception:
                pass
        self._update_badges()

    def select(self, key: str) -> None:  # AppServices compatibility
        self.exit_mini()
        self.show_page("settings" if key == B.SETTINGS_KEY else key)

    def refresh_page(self, rebuild: bool = False) -> None:
        key = self.current_page
        if not key:
            return
        if rebuild and key not in ("home", "chat"):
            self.after(10, lambda: self._rebuild(key))
        else:
            self.refresh_status()

    def _rebuild(self, key: str) -> None:
        if key in self.pages:
            self.pages[key].destroy()
            del self.pages[key]
        if self.current_page == key:
            self.show_page(key)

    def _build_page(self, key: str) -> Any:
        builder = getattr(self, f"_page_{key}")
        if key in ("home", "chat"):
            page = self.theme.tag(ctk.CTkFrame(self.viewport, fg_color="transparent"), "transparent")
        else:
            page = self.theme.tag(ctk.CTkScrollableFrame(self.viewport, fg_color="transparent"), "transparent")
        page.grid_columnconfigure(0, weight=1)
        builder(page)
        self.theme.apply(page)
        return page

    def panel(self, parent: Any, title: str, link: tuple[str, Callable[[], Any]] | None = None,
              badge: str = "") -> tuple[Any, Any]:
        t = self.theme
        frame = t.tag(ctk.CTkFrame(parent), "card")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        head = t.tag(ctk.CTkFrame(frame, fg_color="transparent"), "transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        head.grid_columnconfigure(0, weight=1)
        t.tag(ctk.CTkLabel(head, text="▣ " + title.upper(), anchor="w", height=18, font=t.font(11, "bold")),
              "accent").grid(row=0, column=0, sticky="w")
        if badge:
            t.tag(ctk.CTkLabel(head, text=badge, height=16, font=t.font(9, "bold")), "success").grid(
                row=0, column=1, sticky="e")
        if link:
            t.tag(ctk.CTkButton(head, text=link[0] + " ›", height=18, width=10, font=t.font(10), command=link[1]),
                  "ghost").grid(row=0, column=2, sticky="e")
        body = t.tag(ctk.CTkFrame(frame, fg_color="transparent"), "transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        body.grid_columnconfigure(0, weight=1)
        return frame, body

    def heading(self, page: Any, title: str, sub: str) -> None:
        t = self.theme
        t.tag(ctk.CTkLabel(page, text=title.upper(), anchor="w", font=t.font(22, "bold")), "title").grid(
            row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        t.tag(ctk.CTkLabel(page, text=sub, anchor="w", justify="left", wraplength=900), "muted").grid(
            row=1, column=0, sticky="ew", padx=4, pady=(0, 12))

    # ── Command Center ───────────────────────────────────────────────────
    def _page_home(self, page: Any) -> None:
        t, p, core = self.theme, self.palette, self.core
        page.grid_columnconfigure((0, 2), weight=3, uniform="c")
        page.grid_columnconfigure(1, weight=4, uniform="c")
        page.grid_rowconfigure(0, weight=5)
        page.grid_rowconfigure((1, 2), weight=3)
        pad = {"padx": 5, "pady": 5, "sticky": "nsew"}
        h = self.home = {}

        # AI core overview
        frame, body = self.panel(page, "AI core overview")
        frame.grid(row=0, column=0, **pad)
        h["overview"] = {}
        for i, (key, glyph) in enumerate((("brain", "◎"), ("memory", "▤"), ("voice", "♪"), ("listening", "◉"),
                                          ("integrations", "⌘"), ("system", "⛨"))):
            row = t.tag(ctk.CTkFrame(body), "entry")
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(1, weight=1)
            t.tag(ctk.CTkLabel(row, text=glyph, width=30, height=30, font=t.font(15, "bold")), "accent").grid(
                row=0, column=0, rowspan=2, padx=(6, 6), pady=3)
            t.tag(ctk.CTkLabel(row, text=key.title(), anchor="w", height=16, font=t.font(12, "bold")), "text").grid(
                row=0, column=1, sticky="ew", pady=(4, 0))
            value = t.tag(ctk.CTkLabel(row, text="", anchor="w", height=14, font=t.font(10)), "success")
            value._mt_keep_font = True
            value.grid(row=1, column=1, sticky="ew", pady=(0, 4))
            h["overview"][key] = value

        # stage
        frame, body = self.panel(page, "Core", badge="")
        frame.grid(row=0, column=1, **pad)
        body.grid_rowconfigure(0, weight=1)
        self.stage_box = t.tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        self.stage_box.grid(row=0, column=0, sticky="nsew")
        self.stage_box.grid_columnconfigure(0, weight=1)
        self.stage_box.grid_rowconfigure(0, weight=1)
        self.stage_name = t.tag(ctk.CTkLabel(body, text="", height=20, font=t.font(15, "bold")), "accent")
        self.stage_name._mt_keep_font = True
        self.stage_name.grid(row=1, column=0)
        self.stage_state = t.tag(ctk.CTkLabel(body, text="", height=16, font=t.font(10)), "muted")
        self.stage_state._mt_keep_font = True
        self.stage_state.grid(row=2, column=0)
        self.on_stage_rebuilt(core.build_stage(self.stage_box))

        # live feed
        frame, body = self.panel(page, "Live intelligence feed", ("View all", lambda: self.show_page("activity")),
                                 badge="● LIVE")
        frame.grid(row=0, column=2, **pad)
        h["feed"] = body

        # integrations
        frame, body = self.panel(page, "Integrations", ("Manage", lambda: self.show_page("integrations")))
        frame.grid(row=1, column=0, **pad)
        body.grid_columnconfigure((0, 1), weight=1, uniform="i")
        h["tiles"] = body

        # recent commands
        frame, body = self.panel(page, "Recent commands", ("Activity", lambda: self.show_page("activity")))
        frame.grid(row=1, column=1, **pad)
        h["recent"] = body

        # quick commands
        frame, body = self.panel(page, "Quick commands")
        frame.grid(row=1, column=2, **pad)
        for i, (text, fn) in enumerate((("＋  Add a phrase", lambda: self.show_page("phrases")),
                                        ("❯  Open chat", lambda: self.show_page("chat")),
                                        ("◉  Talk now", self.core.listen_once),
                                        ("◈  Change avatar", lambda: self.show_page("avatar")),
                                        ("⧉  Mini mode", self.enter_mini))):
            t.tag(ctk.CTkButton(body, text=text, anchor="w", height=28, command=fn), "secondary").grid(
                row=i, column=0, sticky="ew", pady=2)

        # system monitor
        frame, body = self.panel(page, "System monitor")
        frame.grid(row=2, column=0, **pad)
        body.grid_columnconfigure((0, 1, 2), weight=1)
        h["rings"] = {}
        for i, key in enumerate(("CPU", "RAM", "DISK")):
            ring = Ring(body, key, p)
            ring.grid(row=0, column=i, pady=4)
            h["rings"][key] = ring

        # memory insights
        frame, body = self.panel(page, "Memory insights", ("Memory", lambda: self.show_page("memory")))
        frame.grid(row=2, column=1, **pad)
        body.grid_columnconfigure(0, weight=1)
        self.constellation = tk.Canvas(body, width=260, height=96, highlightthickness=0, bd=0)
        self.constellation._mt_role = "transparent"
        self.constellation.grid(row=0, column=0, sticky="nsew")
        stats = t.tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        stats.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        h["stats"] = {}
        for i, key in enumerate(("Memories", "Messages", "Phrases")):
            t.tag(ctk.CTkLabel(stats, text=key.upper(), anchor="w", height=12, font=t.font(9, "bold")), "muted").grid(
                row=i * 2, column=0, sticky="w")
            value = t.tag(ctk.CTkLabel(stats, text="0", anchor="w", height=20, font=t.font(16, "bold")), "title")
            value._mt_keep_font = True
            value.grid(row=i * 2 + 1, column=0, sticky="w")
            h["stats"][key] = value
        self._draw_constellation()

        # brain status
        frame, body = self.panel(page, "Brain status", ("Manage", lambda: self.show_page("brain")))
        frame.grid(row=2, column=2, **pad)
        body.grid_columnconfigure((0, 1), weight=1, uniform="b")
        h["brain"] = body
        self._fill_home()

    def on_stage_rebuilt(self, canvas: Any) -> None:
        for child in self.stage_box.winfo_children():
            if child is not canvas:
                child.grid_forget()
        canvas.grid(row=0, column=0)
        self._update_stage_labels()

    def _update_stage_labels(self) -> None:
        core = self.core
        if not hasattr(self, "stage_name"):
            return
        name = " ".join(core.character["name"].upper()) if core.style() != "character" else core.character["name"]
        try:
            self.stage_name.configure(text=f"{name}  ·  AI CORE v{APP_VERSION}")
            state = core.listener.state
            mode = core.avatar.mode
            text = ("● THINKING" if mode == "thinking" else "● SPEAKING" if core.voice_out.active else
                    {"listening": "● LISTENING", "hearing": "● HEARING YOU", "muted": "● MIC MUTED",
                     "paused": "● LISTENING (PAUSED)", "loading": "● STARTING MIC"}.get(state, "● STANDING BY"))
            self.stage_state.configure(text=text)
        except Exception:
            pass

    def _fill_home(self) -> None:
        if not self.home:
            return
        t, p, core = self.theme, self.palette, self.core
        h = self.home
        # overview
        brain_text, brain_role = self._brain_summary()
        rows = {
            "brain": (brain_text, brain_role),
            "memory": (f"{len(core.memories)} stored", "success"),
            "voice": (self._voice_summary(), "success"),
            "listening": ({"off": "Off", "talk": "Talk mode", "wake": f"“Hey {core.wake_word().title()}”"}
                          .get(core.settings.get("listen_mode", "off"), "Off")
                          + ("" if core.listener.state == "off" else " · live"), "success"
                          if core.listener.state != "off" else "muted"),
            "integrations": (f"{self._ready_count()} ready", "success"),
            "system": ("Optimal" if brain_role == "success" else "Needs attention", brain_role),
        }
        for key, (text, role) in rows.items():
            label = h["overview"][key]
            label.configure(text=text)
            t.tag(label, role)
            t.apply(label)
        # tiles
        box = h["tiles"]
        for child in box.winfo_children():
            child.destroy()
        items = [i for i in core.integrations.items.values() if i.id not in ("phrases",)][:8]
        for n, integ in enumerate(items):
            on = core.integrations.enabled(integ)
            tile = t.tag(ctk.CTkFrame(box), "entry")
            tile.grid(row=n // 2, column=n % 2, sticky="ew", padx=2, pady=2)
            t.tag(ctk.CTkLabel(tile, text=integ.icon, width=24, font=t.font(13, "bold")), "accent").pack(
                side="left", padx=(6, 4))
            col = t.tag(ctk.CTkFrame(tile, fg_color="transparent"), "transparent")
            col.pack(side="left", fill="x", expand=True, pady=2)
            t.tag(ctk.CTkLabel(col, text=integ.name, anchor="w", height=14, font=t.font(11, "bold")), "text").pack(
                anchor="w")
            text, role = integ.status() if on else ("Off", "muted")
            t.tag(ctk.CTkLabel(col, text=text[:22], anchor="w", height=12, font=t.font(9)), role).pack(anchor="w")
        t.apply(box)
        # brain tiles
        box = h["brain"]
        for child in box.winfo_children():
            child.destroy()
        provider = core.settings["provider"]
        entries = [("On this PC", provider == "local", self._brain_summary()[0] if provider == "local" else "Standby"),
                   ("Claude API", provider == "anthropic", "Active" if provider == "anthropic" else "Not used"),
                   ("Neural voice", core.addons.installed("voices"),
                    "Installed" if core.addons.installed("voices") else "Not downloaded"),
                   ("Speech input", core.addons.installed("listen"),
                    "Installed" if core.addons.installed("listen") else "Not downloaded")]
        for n, (name, on, status) in enumerate(entries):
            tile = t.tag(ctk.CTkFrame(box), "entry")
            tile.grid(row=n // 2, column=n % 2, sticky="ew", padx=2, pady=2)
            t.tag(ctk.CTkLabel(tile, text=name, anchor="w", height=14, font=t.font(11, "bold")), "text").pack(
                anchor="w", padx=8, pady=(4, 0))
            t.tag(ctk.CTkLabel(tile, text=status[:24], anchor="w", height=12, font=t.font(9)),
                  "success" if on else "muted").pack(anchor="w", padx=8, pady=(0, 4))
        t.apply(box)
        # stats
        h["stats"]["Memories"].configure(text=str(len(core.memories)))
        h["stats"]["Messages"].configure(text=str(len(core.history)))
        phrases = core.integrations.items.get("phrases")
        h["stats"]["Phrases"].configure(text=str(len(phrases.entries()) if phrases else 0))
        self._fill_feed()

    def _fill_feed(self) -> None:
        if not self.home:
            return
        t, core = self.theme, self.core
        for key, limit in (("feed", 6), ("recent", 5)):
            box = self.home[key]
            for child in box.winfo_children():
                child.destroy()
            entries = list(core.activity)[::-1]
            if key == "recent":
                entries = [e for e in entries if e.get("integration")]
            entries = entries[:limit]
            if not entries:
                t.tag(ctk.CTkLabel(box, text="Nothing yet — try “open Spotify” or “what time is it”.",
                                   anchor="w", justify="left", wraplength=280), "muted").grid(row=0, column=0,
                                                                                          sticky="ew")
            for i, entry in enumerate(entries):
                row = t.tag(ctk.CTkFrame(box, fg_color="transparent"), "transparent")
                row.grid(row=i, column=0, sticky="ew", pady=1)
                row.grid_columnconfigure(1, weight=1)
                stamp = time.strftime("%H:%M", time.localtime(entry.get("t", 0)))
                level = entry.get("level", "info")
                if key == "recent":
                    t.tag(ctk.CTkLabel(row, text=stamp, width=44, anchor="w", font=t.font(10)), "muted").grid(
                        row=0, column=0, sticky="w")
                    t.tag(ctk.CTkLabel(row, text=entry.get("title", ""), anchor="w", height=16,
                                       font=t.font(11, "bold")), "text").grid(row=0, column=1, sticky="ew")
                    t.tag(ctk.CTkLabel(row, text=LEVEL_TAG.get(level, "INFO"), width=40, font=t.font(9, "bold")),
                          LEVEL_ROLE.get(level, "accent")).grid(row=0, column=2, sticky="e")
                    t.tag(ctk.CTkFrame(row, height=1, corner_radius=0), "divider").grid(row=1, column=1, columnspan=2,
                                                                                        sticky="ew")
                else:
                    t.tag(ctk.CTkLabel(row, text="▪", width=14, font=t.font(12)), LEVEL_ROLE.get(level, "accent")).grid(
                        row=0, column=0, rowspan=2, sticky="n")
                    t.tag(ctk.CTkLabel(row, text=entry.get("title", ""), anchor="w", height=15,
                                       font=t.font(11, "bold")), "text").grid(row=0, column=1, sticky="ew")
                    t.tag(ctk.CTkLabel(row, text=LEVEL_TAG.get(level, "INFO"), width=36, font=t.font(9, "bold")),
                          LEVEL_ROLE.get(level, "accent")).grid(row=0, column=2, sticky="e")
                    detail = entry.get("detail", "")
                    t.tag(ctk.CTkLabel(row, text=(detail[:58] + "…") if len(detail) > 58 else detail, anchor="w",
                                       height=13, font=t.font(9)), "muted").grid(row=1, column=1, columnspan=2,
                                                                                 sticky="ew")
            t.apply(box)

    def _draw_constellation(self) -> None:
        c, p = self.constellation, self.palette
        c.delete("all")
        n = 8 + min(24, len(self.core.memories) * 2)
        rnd = random.Random(42)
        pts = [(rnd.uniform(10, 250), rnd.uniform(10, 86)) for _ in range(n)]
        for i, (x, y) in enumerate(pts):
            near = sorted(pts, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)[1:3]
            for qx, qy in near:
                c.create_line(x, y, qx, qy, fill=p["glow"], width=1)
        for i, (x, y) in enumerate(pts):
            r = 2.5 if i % 4 else 3.5
            c.create_oval(x - r, y - r, x + r, y + r, fill=p["accent"], outline="", tags=("star", f"s{i}"))

    # ── Chat ─────────────────────────────────────────────────────────────
    def _page_chat(self, page: Any) -> None:
        page.grid_rowconfigure(0, weight=1)
        holder = self.theme.tag(ctk.CTkFrame(page), "card")
        holder._mt_no_deco = True
        holder.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        chat = self.core.main_chat
        if chat.win is not None:
            try:
                chat.win.destroy()
            except Exception:
                pass
            chat.win = None
        chat.container = holder
        chat._build()
        chat.refresh_controls()
        for entry in self.core.history[-30:]:
            if entry["role"] == "user":
                chat.add_user(entry["text"] or "(image)", len(E.message_images(entry)))
            else:
                chat.begin_reply()
                chat.stream(re.sub(r"^\[Action\] ", "▸ ", entry["text"]))
                chat.end_reply()

    # ── Integrations ─────────────────────────────────────────────────────
    def _page_integrations(self, page: Any) -> None:
        t, reg = self.theme, self.core.integrations
        self.heading(page, "Integrations", "Juno only uses an integration when you ask for something it covers — "
                                           "“open Spotify”, “play lofi on Spotify”, “google the weather”. Switch "
                                           "any off, set them up, or add your own (no code needed).")
        bar = t.tag(ctk.CTkFrame(page, fg_color="transparent"), "transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkButton(bar, text="＋ Add integration…", width=160, command=self._add_integration).pack(side="left")
        for text, fn in (("Create template", self._create_templates), ("Open folder",
                                                                      lambda: self.open_path(reg.user_dir)),
                         ("⟳ Reload", self.reload_plugins)):
            t.tag(ctk.CTkButton(bar, text=text, width=120, command=fn), "secondary").pack(side="left", padx=(8, 0))
        row = 3
        for name, error in reg.errors.items():
            t.tag(ctk.CTkLabel(page, text=f"⚠ {name}: {error}", anchor="w", wraplength=900), "danger").grid(
                row=row, column=0, sticky="ew", padx=4)
            row += 1
        for integ in reg.items.values():
            self._integration_card(page, row, integ)
            row += 1

    def _integration_card(self, page: Any, row: int, integ: Any) -> None:
        t, reg = self.theme, self.core.integrations
        card = t.tag(ctk.CTkFrame(page), "card")
        card.grid(row=row, column=0, sticky="ew", padx=(4, 10), pady=6)
        card.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(card, text=integ.icon, width=44, height=44, font=t.font(20, "bold")), "accent").grid(
            row=0, column=0, rowspan=3, padx=(12, 10), pady=12, sticky="n")
        on = reg.enabled(integ)
        status, role = integ.status() if on else ("Off", "muted")
        t.tag(ctk.CTkLabel(card, text=f"{integ.name}   ·   {integ.category}   ·   {integ.source}", anchor="w",
                           font=t.font(14, "bold")), "title").grid(row=0, column=1, sticky="ew", pady=(12, 0))
        t.tag(ctk.CTkLabel(card, text=integ.description, anchor="w", justify="left", wraplength=640), "text").grid(
            row=1, column=1, sticky="ew")
        try:
            examples = [a.examples[0] if a.examples else example_of(a.patterns[0]) for a in integ.actions()][:7]
        except Exception as exc:
            examples = [f"(error: {exc})"]
        if examples:
            t.tag(ctk.CTkLabel(card, text="Say:  " + "  ·  ".join(f"“{e}”" for e in examples), anchor="w",
                               justify="left", wraplength=640, font=t.font(11)), "muted").grid(
                row=2, column=1, sticky="ew", pady=(2, 10))
        right = t.tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        right.grid(row=0, column=2, rowspan=3, sticky="ne", padx=12, pady=12)
        t.tag(ctk.CTkLabel(right, text=status, font=t.font(11, "bold")), role).pack(anchor="e")
        var = ctk.BooleanVar(value=on)
        ctk.CTkSwitch(right, text="On", variable=var,
                      command=lambda: (reg.set_enabled(integ.id, var.get()), self._rebuild("integrations"),
                                       self._fill_home())).pack(anchor="e", pady=(6, 0))
        if integ.fields or isinstance(integ, Spotify):
            box = t.tag(ctk.CTkFrame(card), "entry")
            box.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=(0, 12))
            box.grid_columnconfigure(1, weight=1)
            for i, f in enumerate(integ.fields):
                self._field_row(box, i, integ, f)
            if isinstance(integ, Spotify):
                line = t.tag(ctk.CTkFrame(box, fg_color="transparent"), "transparent")
                line.grid(row=len(integ.fields) * 2, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
                if integ.connected():
                    if not integ.has_library_access():
                        ctk.CTkButton(line, text="Reconnect (to use your library)", width=230,
                                      command=lambda: self._spotify_connect(integ)).pack(side="left", padx=(0, 8))
                    else:
                        t.tag(ctk.CTkButton(line, text="⟳ Refresh library", width=140,
                                            command=lambda: self._spotify_refresh(integ)), "secondary").pack(
                            side="left", padx=(0, 8))
                    t.tag(ctk.CTkButton(line, text="Disconnect Spotify", width=160,
                                        command=lambda: (integ.disconnect(), self._rebuild("integrations"))),
                          "ghost").pack(side="left")
                else:
                    ctk.CTkButton(line, text="Connect Spotify account", width=200,
                                  command=lambda: self._spotify_connect(integ)).pack(side="left")
                t.tag(ctk.CTkLabel(line, text="Needs Spotify Premium. Your Liked Songs and playlists are searched first.", anchor="w"),
                      "muted").pack(side="left", padx=10)

    def _field_row(self, box: Any, n: int, integ: Any, f: Field) -> None:
        t, api = self.theme, self.core.integrations.api
        i = n * 2
        t.tag(ctk.CTkLabel(box, text=f.label, anchor="w", width=200), "text").grid(row=i, column=0, sticky="w",
                                                                                    padx=10, pady=(8, 0))
        value = api.get(integ.id, f.key, f.default)
        if f.kind == "bool":
            var = ctk.BooleanVar(value=bool(value))
            ctk.CTkSwitch(box, text="", variable=var,
                          command=lambda: (api.set(integ.id, f.key, var.get()), integ.on_settings_changed())).grid(
                row=i, column=1, sticky="w", pady=(8, 0))
            return
        if f.kind == "choice":
            current = str(value) if str(value) in f.choices else (f.default or (f.choices[0] if f.choices else ""))

            def chose(v: str) -> None:
                api.set(integ.id, f.key, v)
                integ.on_settings_changed()
                self.set_status(f"{integ.name}: {f.label} → {v}", "success")
                self._rebuild("integrations")
            menu = ctk.CTkOptionMenu(box, values=list(f.choices) or [current], command=chose, width=220)
            menu.set(current)
            menu.grid(row=i, column=1, sticky="w", pady=(8, 0))
            if f.help:
                t.tag(ctk.CTkLabel(box, text=f.help, anchor="w", justify="left", wraplength=620, font=t.font(10)),
                      "muted").grid(row=i + 1, column=1, columnspan=2, sticky="ew", padx=(0, 10))
            return
        entry = ctk.CTkEntry(box, show="•" if f.kind == "secret" else "")
        entry.insert(0, api.secret(integ.id, f.key) if f.kind == "secret" else str(value or ""))
        entry.grid(row=i, column=1, sticky="ew", pady=(8, 0))

        def save(_e: Any = None) -> None:
            if f.kind == "secret":
                api.set_secret(integ.id, f.key, entry.get().strip())
            else:
                api.set(integ.id, f.key, entry.get().strip())
            integ.on_settings_changed()
            self.set_status(f"{integ.name}: {f.label} saved", "success")
        entry.bind("<FocusOut>", save)
        entry.bind("<Return>", save)
        if f.kind in ("path", "folder"):
            def browse() -> None:
                chosen = (filedialog.askdirectory(parent=self) if f.kind == "folder"
                          else filedialog.askopenfilename(parent=self))
                if chosen:
                    entry.delete(0, "end")
                    entry.insert(0, chosen)
                    save()
            t.tag(ctk.CTkButton(box, text="Browse…", width=80, command=browse), "secondary").grid(
                row=i, column=2, padx=10, pady=(8, 0))
        if f.help:
            t.tag(ctk.CTkLabel(box, text=f.help, anchor="w", justify="left", wraplength=620, font=t.font(10)),
                  "muted").grid(row=i + 1, column=1, columnspan=2, sticky="ew", padx=(0, 10))

    def _spotify_connect(self, integ: Spotify) -> None:
        self.set_status("Finish signing in to Spotify in your browser…")

        def done(error: str | None) -> None:
            if error:
                self.set_status(f"Spotify: {error}", "error")
                self.notify("Spotify didn't connect", error, subtitle="Integrations ▸ Spotify", icon="♫", duration=20)
            else:
                status = integ.status()[0]
                self.set_status(f"Spotify: {status}", "success")
                self.core.log_activity("Spotify connected", status, "ok", "spotify")
            self._rebuild("integrations")
        integ.connect(done)

    def _spotify_refresh(self, integ: Spotify) -> None:
        self.set_status("Reading your Spotify library…")

        def done(lib: Any) -> None:
            self.set_status(f"Spotify: {len(lib.get('tracks', []))} liked songs, "
                            f"{len(lib.get('playlists', []))} playlists", "success")
            self._rebuild("integrations")

        def failed(exc: BaseException) -> None:
            self.set_status(f"Spotify: {exc}", "error")
        self.run_in_thread(lambda: integ.sync(full=True), on_done=done, on_error=failed, name="spotify-sync")

    def _add_integration(self) -> None:
        names = filedialog.askopenfilenames(parent=self, title="Add integration",
                                            filetypes=[("Juno integrations", "*.junoint *.py"), ("All files", "*.*")])
        self.install_files([Path(n) for n in names or ()])

    def _create_templates(self) -> None:
        folder = self.core.integrations.user_dir
        pack, py = folder / "my-tools.junoint", folder / "_example_coin_flip.py"
        if not pack.exists():
            pack.write_text(json.dumps(TEMPLATE_PACK, indent=2), encoding="utf-8")
        if not py.exists():
            py.write_text(TEMPLATE_PY, encoding="utf-8")
        self.reload_plugins()
        self.open_path(folder)
        self.set_status("Templates created — edit them, then press Reload. (Rename the .py without the _ to "
                        "turn it on.)", "success")

    def install_files(self, files: list[Path]) -> None:
        folder = self.core.integrations.user_dir
        added = []
        for path in files:
            ext = path.suffix.lower()
            if ext in self.file_types:
                self.file_types[ext][1](path)
                added.append(path.name)
                continue
            if ext not in (".junoint", ".py"):
                continue
            try:
                if ext == ".junoint":
                    PackIntegration(json.loads(path.read_text(encoding="utf-8")), path)
                else:
                    load_python_integrations(path)
                shutil.copy2(path, folder / path.name)
                added.append(path.name)
            except Exception as exc:
                self.set_status(f"{path.name} isn't a working integration: {exc}", "error")
                return
        if added:
            self.reload_plugins()
            self.set_status("Added " + ", ".join(added), "success")

    def reload_plugins(self) -> None:
        self.core.integrations.api.apps.refresh()
        self.core.integrations.load()
        self.core._import_bundled_characters()
        if "integrations" in self.pages:
            self._rebuild("integrations")
        self._fill_home()
        self._update_badges()

    def _on_drop(self, payload: Any) -> None:
        if payload.files:
            self.install_files(list(payload.files))

    # ── Phrases ──────────────────────────────────────────────────────────
    def _page_phrases(self, page: Any) -> None:
        t = self.theme
        phrases = self.core.integrations.items["phrases"]
        self.heading(page, "Phrases", "Your own words for things on your PC. Say “open <phrase>” — or just the "
                                      "phrase — and Juno opens it. Example: “Blender” → Blender, “game folder” → "
                                      "D:\\Games, “docs” → a website.")
        form = t.tag(ctk.CTkFrame(page), "card")
        form.grid(row=2, column=0, sticky="ew", padx=(4, 10), pady=(0, 12))
        form.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(form, text="Phrase", anchor="w", width=80), "text").grid(row=0, column=0, padx=12,
                                                                                     pady=(12, 4), sticky="w")
        phrase = ctk.CTkEntry(form, placeholder_text="e.g. blender")
        phrase.grid(row=0, column=1, sticky="ew", pady=(12, 4))
        t.tag(ctk.CTkLabel(form, text="Opens", anchor="w", width=80), "text").grid(row=1, column=0, padx=12,
                                                                                    pady=4, sticky="w")
        target = ctk.CTkEntry(form, placeholder_text="app, file, folder or website")
        target.grid(row=1, column=1, sticky="ew", pady=4)
        buttons = t.tag(ctk.CTkFrame(form, fg_color="transparent"), "transparent")
        buttons.grid(row=0, column=2, rowspan=2, padx=12, pady=(12, 4), sticky="n")

        def put(value: str) -> None:
            if value:
                target.delete(0, "end")
                target.insert(0, value)
                if not phrase.get().strip():
                    phrase.insert(0, Path(value).stem.lower() if "://" not in value else "")
        t.tag(ctk.CTkButton(buttons, text="App / file…", width=110,
                            command=lambda: put(filedialog.askopenfilename(parent=self))), "secondary").pack(pady=2)
        t.tag(ctk.CTkButton(buttons, text="Folder…", width=110,
                            command=lambda: put(filedialog.askdirectory(parent=self))), "secondary").pack(pady=2)
        apps = self.core.integrations.api.apps.names()[:400]
        t.tag(ctk.CTkLabel(form, text="Installed", anchor="w", width=80), "text").grid(row=2, column=0, padx=12,
                                                                                        pady=4, sticky="w")
        pick = ctk.CTkComboBox(form, values=apps or ["(no Start-menu apps found)"],
                               command=lambda v: (put(str(self.core.integrations.api.apps.find(v) or v)),
                                                  phrase.delete(0, "end"), phrase.insert(0, v.lower())))
        pick.set("pick an installed app…")
        pick.grid(row=2, column=1, sticky="ew", pady=4)

        def add() -> None:
            words, dest = phrase.get().strip(), target.get().strip()
            if not words or not dest:
                self.set_status("Type a phrase and what it opens", "warning")
                return
            kind = "url" if re.match(r"^(https?://|www\.)|^[\w-]+(\.[\w-]+)+(/|$)", dest) else \
                "folder" if Path(dest).is_dir() else "app"
            entries = [e for e in phrases.entries() if normalize(e["phrase"]) != normalize(words)]
            entries.append({"phrase": words, "target": dest, "kind": kind})
            self.core.integrations.api.set("phrases", "entries", entries)
            self.core.integrations.compile()
            self.core.log_activity("Phrase added", f"“{words}” → {dest}", "ok", "phrases")
            self._rebuild("phrases")
        ctk.CTkButton(form, text="＋ Add phrase", width=140, command=add).grid(row=3, column=1, sticky="w", pady=(4, 12))

        row = 3
        for entry in phrases.entries():
            line = t.tag(ctk.CTkFrame(page), "entry")
            line.grid(row=row, column=0, sticky="ew", padx=(4, 10), pady=2)
            line.grid_columnconfigure(1, weight=1)
            t.tag(ctk.CTkLabel(line, text=f"“{entry['phrase']}”", anchor="w", width=200, font=t.font(13, "bold")),
                  "accent").grid(row=0, column=0, padx=12, pady=8, sticky="w")
            t.tag(ctk.CTkLabel(line, text=f"→  {entry['target']}", anchor="w"), "text").grid(row=0, column=1,
                                                                                            sticky="ew")
            t.tag(ctk.CTkButton(line, text="▶ Test", width=70,
                                command=lambda w=entry["phrase"]: self.core.send_message(f"open {w}")),
                  "secondary").grid(row=0, column=2, padx=4)

            def remove(w: str = entry["phrase"]) -> None:
                self.core.integrations.api.set("phrases", "entries",
                                               [e for e in phrases.entries() if e["phrase"] != w])
                self.core.integrations.compile()
                self._rebuild("phrases")
            t.tag(ctk.CTkButton(line, text="✕", width=34, command=remove), "danger").grid(row=0, column=3,
                                                                                         padx=(0, 10))
            row += 1
        if not phrases.entries():
            t.tag(ctk.CTkLabel(page, text="No phrases yet. Juno can already open any Start-menu app by name — phrases "
                                          "are for your own nicknames, files, folders and sites.", anchor="w",
                               wraplength=900), "muted").grid(row=row, column=0, sticky="ew", padx=4)

    # ── Avatar ───────────────────────────────────────────────────────────
    def _page_avatar(self, page: Any) -> None:
        t, core = self.theme, self.core
        self.heading(page, "Avatar", "How Juno looks in this window and on your desktop in mini mode.")
        grid = t.tag(ctk.CTkFrame(page, fg_color="transparent"), "transparent")
        grid.grid(row=2, column=0, sticky="ew", padx=4)
        self._previews = []
        styles = [(k, v[0], v[1]) for k, v in ORB_STYLES.items()] + [
            ("character", "Character", "An animated assistant character — Juno, Vex or one you import")]
        for i, (key, title, about) in enumerate(styles):
            grid.grid_columnconfigure(i, weight=1, uniform="a")
            selected = core.style() == key
            card = t.tag(ctk.CTkFrame(grid), "card")
            card.grid(row=0, column=i, sticky="nsew", padx=5, pady=5)
            canvas = tk.Canvas(card, width=150, height=150, highlightthickness=0, bd=0, cursor="hand2")
            canvas._mt_role = "skip"
            canvas.configure(bg=self.palette["card"])
            canvas.pack(pady=(12, 4))
            if key != "character":
                self._previews.append((canvas, OrbRenderer(key, 150, self.palette)))
            else:
                self._character_preview(canvas)
            t.tag(ctk.CTkLabel(card, text=title.upper(), font=t.font(13, "bold")),
                  "accent" if selected else "title").pack()
            t.tag(ctk.CTkLabel(card, text=about, wraplength=190, justify="center", font=t.font(10)), "muted").pack(
                padx=8)
            t.tag(ctk.CTkButton(card, text="✓ In use" if selected else "Use this", width=120,
                                command=lambda k=key: (core.set_avatar_style(k), self._rebuild("avatar"))),
                  "accent" if selected else "secondary").pack(pady=(8, 12))
            canvas.bind("<Button-1>", lambda _e, k=key: (core.set_avatar_style(k), self._rebuild("avatar")))
        box = t.tag(ctk.CTkFrame(page), "card")
        box.grid(row=3, column=0, sticky="ew", padx=(4, 10), pady=(12, 0))
        box.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(box, text="▣ CHARACTER (used when the avatar style is Character)", anchor="w",
                           font=t.font(11, "bold")), "accent").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    padx=12, pady=(10, 4))
        inner = t.tag(ctk.CTkFrame(box, fg_color="transparent"), "transparent")
        inner.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 10))
        core._tab_character(inner)

    def _character_preview(self, canvas: Any) -> None:
        try:
            look = self.core.character["look"]
            st = dict(E.DEFAULT_STATE)
            canvas.delete("all")
            E.draw_procedural(canvas, look, st, 0.5, 35, 8, 1)
        except Exception:
            canvas.create_text(75, 75, text="☺", fill=self.palette["accent"], font=(self.palette["mono_font"], 40))

    # ── engine-backed pages ──────────────────────────────────────────────
    def _engine_page(self, page: Any, title: str, sub: str, fn: Callable[[Any], None]) -> None:
        self.heading(page, title, sub)
        box = self.theme.tag(ctk.CTkFrame(page), "card")
        box.grid(row=2, column=0, sticky="ew", padx=(4, 10))
        inner = self.theme.tag(ctk.CTkFrame(box, fg_color="transparent"), "transparent")
        inner.pack(fill="both", expand=True, padx=10, pady=10)
        fn(inner)

    def _page_brain(self, page: Any) -> None:
        t, core = self.theme, self.core
        self._engine_page(page, "Brain & Voice", "Juno's AI runs on this PC by default — free, private, offline after "
                                                "a one-time download. Voices and speech input work the same way.",
                          core._tab_ai)
        box = t.tag(ctk.CTkFrame(page), "card")
        box.grid(row=3, column=0, sticky="ew", padx=(4, 10), pady=(12, 0))
        box.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(box, text="▣ LISTENING", anchor="w", font=t.font(11, "bold")), "accent").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))
        seg = ctk.CTkSegmentedButton(box, values=list(LISTEN_MODES.values()),
                                     command=lambda v: self._mode_chosen(v))
        seg.set(LISTEN_MODES.get(core.settings.get("listen_mode", "off"), "Off"))
        seg.grid(row=1, column=0, padx=12, pady=4, sticky="w")
        t.tag(ctk.CTkLabel(box, text="Off: only the mic button / hotkey.  Talk: everything you say goes to Juno.  "
                                     "Hey Juno: always listening, but only acts when you start with her name. "
                                     "All speech is recognised on your PC and never saved.", anchor="w",
                           justify="left", wraplength=640), "muted").grid(row=1, column=1, sticky="ew", padx=8)
        t.tag(ctk.CTkLabel(box, text="Wake word", anchor="w"), "text").grid(row=2, column=0, sticky="w", padx=12,
                                                                            pady=6)
        wake = ctk.CTkEntry(box, width=160)
        wake.insert(0, core.wake_word())
        wake.grid(row=2, column=1, sticky="w", pady=6)
        wake.bind("<FocusOut>", lambda _e: core.set_setting("wake_word", wake.get().strip().lower() or "juno"))
        hot = ctk.BooleanVar(value=bool(core.settings.get("hotkey", True)))
        ctk.CTkSwitch(box, text="Ctrl+Alt+J anywhere = listen for one command", variable=hot,
                      command=lambda: (core.set_setting("hotkey", hot.get()), self._restart_hotkey())).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 6))
        speak = ctk.BooleanVar(value=bool(core.settings.get("speak_actions", True)))
        ctk.CTkSwitch(box, text="Say a short confirmation after doing something", variable=speak,
                      command=lambda: core.set_setting("speak_actions", speak.get())).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12))

    def _page_personality(self, page: Any) -> None:
        self._engine_page(page, "Personality", "Tune how Juno talks. Instructions are followed in every chat.",
                          self.core._tab_personality)

    def _page_memory(self, page: Any) -> None:
        self._engine_page(page, "Memory", "Things Juno remembers about you. Stored only on this PC.",
                          self.core._tab_memory)

    def _page_permissions(self, page: Any) -> None:
        self._engine_page(page, "Permissions", "What Juno is allowed to do. Integrations also only run when you "
                                               "ask for them.", self.core._tab_permissions)

    # ── Activity ─────────────────────────────────────────────────────────
    def _page_activity(self, page: Any) -> None:
        t = self.theme
        self.heading(page, "Activity", "Everything Juno did or heard — newest first.")
        t.tag(ctk.CTkButton(page, text="Clear", width=90,
                            command=lambda: (self.core.activity.clear(), self.core._save_activity(),
                                             self._rebuild("activity"), self._fill_feed())), "ghost").grid(
            row=0, column=0, sticky="e", padx=10)
        for i, entry in enumerate(list(self.core.activity)[::-1][:150]):
            row = t.tag(ctk.CTkFrame(page), "entry")
            row.grid(row=i + 2, column=0, sticky="ew", padx=(4, 10), pady=1)
            row.grid_columnconfigure(2, weight=1)
            level = entry.get("level", "info")
            t.tag(ctk.CTkLabel(row, text=time.strftime("%d %b %H:%M", time.localtime(entry.get("t", 0))), width=100,
                               anchor="w", font=t.font(10)), "muted").grid(row=0, column=0, padx=(10, 4), pady=5)
            t.tag(ctk.CTkLabel(row, text=LEVEL_TAG.get(level, "INFO"), width=44, font=t.font(9, "bold")),
                  LEVEL_ROLE.get(level, "accent")).grid(row=0, column=1)
            t.tag(ctk.CTkLabel(row, text=f"{entry.get('title', '')} — {entry.get('detail', '')}", anchor="w",
                               justify="left", wraplength=760), "text").grid(row=0, column=2, sticky="ew", padx=6)

    # ── Settings ─────────────────────────────────────────────────────────
    def _page_settings(self, page: Any) -> None:
        t, core = self.theme, self.core
        self.heading(page, "Settings", "Updates, start-up and how the window behaves.")
        frame, body = self.panel(page, "Updates")
        frame.grid(row=2, column=0, sticky="ew", padx=(4, 10), pady=5)
        self._updates_card(body)

        frame, body = self.panel(page, "Start-up & window")
        frame.grid(row=3, column=0, sticky="ew", padx=(4, 10), pady=5)
        startup = ctk.BooleanVar(value=start_with_windows_enabled())
        ctk.CTkSwitch(body, text="Start Juno when Windows starts (in mini mode)", variable=startup,
                      command=lambda: self._set_startup(startup.get())).grid(row=0, column=0, sticky="w", pady=3)
        for i, (key, text) in enumerate((("start_mini", "Open in mini mode"),
                                         ("boot", "Boot screen when Juno opens")), start=1):
            var = ctk.BooleanVar(value=bool(core.settings.get(key)))
            ctk.CTkSwitch(body, text=text, variable=var,
                          command=lambda k=key, v=var: core.set_setting(k, v.get())).grid(row=i, column=0,
                                                                                         sticky="w", pady=3)
        line = t.tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        line.grid(row=3, column=0, sticky="w", pady=3)
        t.tag(ctk.CTkLabel(line, text="Close button (✕):"), "text").pack(side="left")
        close = ctk.CTkSegmentedButton(line, values=["Mini mode", "Quit"],
                                       command=lambda v: core.set_setting("close_action",
                                                                          "mini" if v == "Mini mode" else "quit"))
        close.set("Mini mode" if core.settings.get("close_action") == "mini" else "Quit")
        close.pack(side="left", padx=10)

        frame, body = self.panel(page, "Sound & motion")
        frame.grid(row=4, column=0, sticky="ew", padx=(4, 10), pady=5)
        sounds = ctk.BooleanVar(value=self.theme.player.enabled)
        ctk.CTkSwitch(body, text="Interface sounds", variable=sounds,
                      command=lambda: (setattr(self.theme.player, "enabled", sounds.get()),
                                       self.settings.set("core", "ui_sounds", sounds.get()))).grid(
            row=0, column=0, sticky="w", pady=3)
        vol = ctk.CTkSlider(body, from_=0, to=1, number_of_steps=20, width=220,
                            command=lambda v: (setattr(self.theme.player, "volume", round(float(v), 2)),
                                               self.settings.set("core", "ui_volume", round(float(v), 2))))
        vol.set(self.theme.player.volume)
        vol.grid(row=0, column=1, sticky="w", padx=12)
        calm = ctk.BooleanVar(value=bool(core.settings.get("reduced_motion")))
        ctk.CTkSwitch(body, text="Reduce motion (slower avatar, no screen flicker)", variable=calm,
                      command=lambda: core.set_setting("reduced_motion", calm.get())).grid(row=1, column=0,
                                                                                          columnspan=2, sticky="w",
                                                                                          pady=3)

        frame, body = self.panel(page, "Storage")
        frame.grid(row=5, column=0, sticky="ew", padx=(4, 10), pady=5)
        for i, (label, path) in enumerate((("Settings, memory & your integrations", self.paths.data_dir),
                                           ("Downloaded AI models & voices", self.models_dir),
                                           ("Logs", self.paths.logs_dir))):
            t.tag(ctk.CTkLabel(body, text=f"{label}:  {path}", anchor="w"), "text").grid(row=i, column=0,
                                                                                         sticky="ew", pady=2)
            t.tag(ctk.CTkButton(body, text="Open", width=70, command=lambda pth=path: self.open_path(pth)),
                  "secondary").grid(row=i, column=1, padx=8)

    def _set_startup(self, on: bool) -> None:
        try:
            set_start_with_windows(on)
            self.set_status("Juno will start with Windows" if on else "Juno won't start with Windows", "success")
        except Exception as exc:
            self.set_status(f"Couldn't change start-up: {exc}", "error")

    # AppServices hooks
    def _refresh_settings_page(self) -> None:
        if "settings" in self.pages:
            self._rebuild("settings")

    def _refresh_theme_menus(self) -> None:
        pass

    # ── live updates ─────────────────────────────────────────────────────
    def refresh_status(self) -> None:
        self._fill_home()
        self._update_badges()

    def refresh_voice(self) -> None:
        mode = self.core.settings.get("listen_mode", "off")
        try:
            self.mode_seg.set(LISTEN_MODES.get(mode, "Off"))
        except Exception:
            pass
        self._draw_mic()
        self._fill_home()

    def on_activity(self, entry: dict[str, Any]) -> None:
        self._fill_feed()
        self._update_badges()
        if "activity" in self.pages and self.current_page != "activity":
            self.pages.pop("activity").destroy()

    def _update_badges(self) -> None:
        counts = {"integrations": self._ready_count(), "memory": len(self.core.memories),
                  "phrases": len(self.core.integrations.items["phrases"].entries())
                  if "phrases" in self.core.integrations.items else 0}
        for key, btn in self.nav_buttons.items():
            n = counts.get(key)
            badge = btn._badge
            if n:
                badge.configure(text=str(n))
                badge.place(relx=1.0, rely=0.5, x=-8, anchor="e")
            else:
                badge.place_forget()

    def _ready_count(self) -> int:
        reg = self.core.integrations
        return sum(1 for i in reg.items.values() if reg.enabled(i) and i.id != "phrases")

    def _brain_summary(self) -> tuple[str, str]:
        core = self.core
        provider = core.settings["provider"]
        if provider == "local":
            manifest = core.local_manifest()
            if not core.engine.available():
                return "Engine missing", "danger"
            if manifest is None:
                return "Needs a one-time download", "warning"
            name = E.LOCAL_MODELS.get(core.settings["local_size"], {}).get("name", "local model")
            return f"{name} · on this PC", "success"
        if provider == "anthropic":
            return ("Claude API", "success") if core.api_key() else ("Claude API · no key", "warning")
        if provider == "openai":
            return "OpenAI-compatible server", "success"
        return "Off", "muted"

    def _voice_summary(self) -> str:
        config = self.core.voice_config()
        if config["engine"] == "neural":
            return E.KOKORO_LABEL.get(config["voice"], config["voice"]).split(" — ")[0] + f" · {config['speed']:.2f}×"
        return "Windows voice" + ("" if self.core.speech.available else " (unavailable)")

    def _tick(self) -> None:
        if self._closing:
            return
        self._tick_n += 1
        request = self.paths.data_dir / "show.request"
        if request.exists():
            try:
                request.unlink()
            except OSError:
                pass
            self.exit_mini()
            self.deiconify()
            self.lift()
            self.focus_force()
        try:
            self.time_label.configure(text=time.strftime("%H:%M:%S"))
            self.date_label.configure(text=time.strftime("%A, %d %B %Y"))
            text, role = self._brain_summary()
            self.sys_text.configure(text={"success": "OPTIMAL", "warning": "SETUP NEEDED", "danger": "DEGRADED"}
                                    .get(role, "STANDBY"))
            self.theme.tag(self.sys_text, "accent" if role == "success" else role)
            self.theme.apply(self.sys_text)
            self.sys_dot.set(self.palette.get(role, self.palette["accent"]) if role != "success"
                             else self.palette["accent"])
            self.chips["brain"].configure(text=text[:30])
            self.chips["voice"].configure(text=self._voice_summary()[:30])
            self.chips["integrations"].configure(text=f"{self._ready_count()} ready")
            self._cursor = not self._cursor
            self._render_status()
            if self.home and self._tick_n % 2 == 0 and self.current_page == "home":
                cpu, mem = self._telemetry.cpu(), self._telemetry.memory()
                try:
                    usage = shutil.disk_usage(Path.home().anchor or "/")
                    disk = 100 * usage.used / usage.total
                except Exception:
                    disk = None
                for key, value in (("CPU", cpu), ("RAM", mem), ("DISK", disk)):
                    self.home["rings"][key].draw(value)
            if self._tick_n % 10 == 0 and self.current_page == "home":
                self._fill_home()
        except Exception:
            self.log.exception("tick failed")
        self.after(1000, self._tick)

    def _anim(self) -> None:
        """~15 fps: waveforms, mic, talk button, stage labels, twinkling memory stars."""
        if self._closing:
            return
        try:
            if self.state() != "withdrawn" and self.state() != "iconic":
                core = self.core
                state = core.listener.state
                speaking = core.voice_out.active
                energy = 0.85 if speaking else 0.6 if state == "hearing" else 0.18 if state in ("listening",) \
                    else 0.05
                color = self.palette["accent2"] if state == "hearing" else self.palette["accent"]
                for wave in (self.side_wave, self.talk_wave_l, self.talk_wave_r):
                    wave.step(energy, color)
                self._draw_mic()
                self._draw_talk()
                self._update_stage_labels()
                if self.home and self.current_page == "home" and random.random() < 0.3:
                    stars = self.constellation.find_withtag("star")
                    if stars:
                        star = random.choice(stars)
                        self.constellation.itemconfigure(star, fill=random.choice(
                            (self.palette["accent"], self.palette["accent2"], self.palette["glow"])))
                if self.current_page == "avatar" and getattr(self, "_previews", None):
                    t = time.monotonic()
                    for canvas, renderer in self._previews:
                        self._paint_preview(canvas, renderer, t)
        except Exception:
            self.log.exception("animation failed")
        self.after(70, self._anim)

    def _paint_preview(self, canvas: Any, renderer: OrbRenderer, t: float) -> None:
        from PIL import ImageTk
        if not canvas.winfo_exists():
            return
        img = renderer.render(t, "idle", 0.0, glow=True)
        photo = getattr(canvas, "_photo", None)
        if photo is None:
            canvas._photo = ImageTk.PhotoImage(img, master=canvas)
            canvas.create_image(0, 0, image=canvas._photo, anchor="nw")
        else:
            photo.paste(img)

    def set_status(self, text: str, level: str = "info") -> None:
        self._status_text = " ".join(str(text).split())
        self._status_level = level
        self._render_status()
        if level == "error":
            self.theme.play("error")
            self.log.error("Status: %s", text)
        elif level == "success":
            self.theme.play("success")
        role = {"success": "success", "warning": "warning", "error": "danger"}.get(level, "muted")
        self.theme.tag(self.status_label, role)
        self.theme.apply(self.status_label)
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except Exception:
                pass
        self._status_after = self.after(9000, lambda: self.set_status("Ready", "_idle")) \
            if level in ("info", "success") else None

    def _render_status(self) -> None:
        try:
            self.status_label.configure(text=f"> {self._status_text[:220]} " + ("█" if self._cursor else " "))
        except Exception:
            pass

    def _run_command(self) -> None:
        text = self.command.get().strip()
        if not text:
            return
        self.command.delete(0, "end")
        if not self.core.try_command(text):
            self.show_page("chat")
            self.core.send_message(text)

    # ── boot screen ──────────────────────────────────────────────────────
    def _boot(self) -> None:
        p, core = self.palette, self.core
        brain, _role = self._brain_summary()
        lines = [f"JUNO BIOS v{APP_VERSION}  (C) 2026",
                 "> memory test ................ OK",
                 f"> neural core ................ {brain}",
                 f"> voice ...................... {self._voice_summary()}",
                 f"> integrations ............... {self._ready_count()} online",
                 f"> long-term memory ........... {len(core.memories)} facts",
                 f"> avatar ..................... {core.style().upper()}",
                 f"GOOD {'MORNING' if time.localtime().tm_hour < 12 else 'AFTERNOON' if time.localtime().tm_hour < 18 else 'EVENING'}, "
                 f"{(os.environ.get('USERNAME') or 'OPERATOR').upper()}."]
        overlay = tk.Frame(self, bg=p["bg"], cursor="hand2")
        overlay._mt_role = "skip"
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        text = tk.Text(overlay, bg=p["bg"], fg=p["accent"], bd=0, highlightthickness=0, insertwidth=0,
                       font=(p["mono_font"], 14), cursor="hand2", wrap="none")
        text._mt_role = "skip"
        text.tag_configure("dim", foreground=p["text_muted"])
        text.tag_configure("hot", foreground=p["accent2"])
        text.place(relx=0.5, rely=0.45, anchor="center", relwidth=0.6, relheight=0.45)
        bar = tk.Frame(overlay, bg=p["dim"], height=3)
        bar.place(relx=0.2, rely=0.72, relwidth=0.6)
        fill = tk.Frame(bar, bg=p["accent"], height=3)
        state = {"line": 0, "col": 0, "done": 0, "alive": True}
        total = sum(len(line) for line in lines)

        def finish(_e: Any = None) -> None:
            if state["alive"]:
                state["alive"] = False
                overlay.destroy()
        for w in (overlay, text, bar):
            w.bind("<Button-1>", finish)
        self.theme.play("boot")

        def step() -> None:
            if not state["alive"] or self._closing:
                return
            if state["line"] >= len(lines):
                self.after(450, finish)
                return
            line = lines[state["line"]]
            chunk = line[state["col"]:state["col"] + 5]
            text.insert("end", chunk, "hot" if state["line"] == len(lines) - 1 else "dim" if line.startswith(">")
                        else "")
            state["col"] += len(chunk)
            state["done"] += len(chunk)
            fill.place(x=0, y=0, relheight=1, relwidth=min(1.0, state["done"] / total))
            if state["col"] >= len(line):
                text.insert("end", "\n")
                state["line"] += 1
                state["col"] = 0
                self.after(80, step)
            else:
                self.after(10, step)
        step()

    # ── mini mode ────────────────────────────────────────────────────────
    def enter_mini(self) -> None:
        if self.mini or self._closing:
            return
        self.mini = True
        self._save_geometry()
        self.withdraw()
        desk = self.core.make_desk()
        desk.show()
        self.core.chat.refresh_controls()
        if not self.settings.get("juno", "mini_hint_shown", False):
            self.settings.set("juno", "mini_hint_shown", True)
            desk.say("I'm down here. Click me to chat, double-click to open the full window, right-click for more.", 9)
        self.core.log_activity("Mini mode", "Window hidden — avatar on the desktop", "info")

    def exit_mini(self) -> None:
        if not self.mini:
            return
        self.mini = False
        if self.core.avatar.desk is not None:
            self.core.avatar.desk.hide()
        self.core.mini_chat.hide()
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.current_page == "chat":
            self._rebuild("chat")

    def mini_menu(self, event: Any) -> None:
        core = self.core
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Chat", command=core.mini_chat.show)
        menu.add_command(label="Talk (one command)", command=core.listen_once)
        modes = tk.Menu(menu, tearoff=0)
        var = tk.StringVar(value=core.settings.get("listen_mode", "off"))
        for key, label in LISTEN_MODES.items():
            modes.add_radiobutton(label=label, variable=var, value=key, command=lambda k=key: core.set_listen_mode(k))
        menu.add_cascade(label="Listening", menu=modes)
        menu.add_command(label="Unmute voice" if core.settings.get("muted") else "Mute voice",
                         command=core.toggle_mute)
        menu.add_separator()
        menu.add_command(label="Open Juno", command=self.exit_mini)
        menu.add_command(label="Quit Juno", command=self.quit_app)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_unmap(self, event: Any) -> None:
        if event.widget is self and not self.mini and not self._closing:
            try:
                if self.state() == "iconic":
                    self.after(60, lambda: (self.deiconify(), self.enter_mini()) if self.state() == "iconic"
                               else None)
            except Exception:
                pass

    def on_close_button(self) -> None:
        if self.core.settings.get("close_action", "mini") == "mini":
            if not self.settings.get("juno", "close_hint_shown", False):
                self.settings.set("juno", "close_hint_shown", True)
                self.notify("Juno is still here", "Closing the window keeps me running in mini mode. "
                            "Right-click me ▸ Quit to close me completely (or change it in Settings).",
                            subtitle="Mini mode", icon="J", duration=12)
            self.enter_mini()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        self._on_close()

    def _save_geometry(self) -> None:
        try:
            if self.state() == "normal":
                self.settings.set("core", "geometry", self.geometry())
            self.settings.set("core", "maximized", self.state() == "zoomed")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self.mini:
            self._save_geometry()
        self._closing = True
        self._stop_hotkey()
        try:
            self.core.shutdown()
            self.core.avatar.destroy()
        except Exception:
            self.log.exception("shutdown failed")
        try:
            self.toasts.close_all()
        except Exception:
            pass
        self.destroy()

    # ── global hotkey (Ctrl+Alt+J) ───────────────────────────────────────
    def _start_hotkey(self) -> None:
        if not IS_WINDOWS or not self.core.settings.get("hotkey", True) or self._hotkey_thread is not None:
            return

        def loop() -> None:
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            self._hotkey_tid = kernel32.GetCurrentThreadId()
            if not user32.RegisterHotKey(None, 1, 0x0002 | 0x0001 | 0x4000, ord("J")):  # CTRL|ALT|NOREPEAT
                self.log.warning("Ctrl+Alt+J is taken by another app")
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.call_in_ui(self._on_hotkey)
            user32.UnregisterHotKey(None, 1)

        self._hotkey_thread = threading.Thread(target=loop, daemon=True, name="hotkey")
        self._hotkey_thread.start()

    def _stop_hotkey(self) -> None:
        if self._hotkey_thread is not None and self._hotkey_tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._hotkey_tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        self._hotkey_thread = None
        self._hotkey_tid = 0

    def _restart_hotkey(self) -> None:
        self._stop_hotkey()
        self.after(200, self._start_hotkey)

    def _on_hotkey(self) -> None:
        self.core.listen_once()


# ════════════════════════════════════════════════════════════════════════════
#  Helpers: models folder, migration from Multi-Tool, start with Windows
# ════════════════════════════════════════════════════════════════════════════

def models_dir(paths: B.AppPaths) -> Path:
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA") and not paths.portable:
        return Path(os.environ["LOCALAPPDATA"]) / "Juno" / "models"
    return paths.data_dir / "models"


def migrate_from_multitool(paths: B.AppPaths, settings: SettingsStore, log: Any, models: Path) -> list[str]:
    """First run: bring over the companion's memory, characters, settings and downloaded models."""
    if settings.get("juno", "migrated", False):
        return []
    settings.set("juno", "migrated", True)
    roaming = os.environ.get("MT_MIGRATE_FROM") or (os.environ.get("APPDATA", "") and
                                                     str(Path(os.environ["APPDATA"]) / "MultiTool"))
    if not roaming or not Path(roaming).is_dir():
        return []
    old = Path(roaming)
    moved = []
    plugin = old / "plugin_data" / "desktop_companion"
    try:
        if (plugin / "memory.json").is_file() and not (paths.data_dir / "memory.json").exists():
            shutil.copy2(plugin / "memory.json", paths.data_dir / "memory.json")
            moved.append("memories")
        if (plugin / "characters").is_dir() and not (paths.data_dir / "characters").exists():
            shutil.copytree(plugin / "characters", paths.data_dir / "characters")
            moved.append("characters")
        old_settings = json.loads((old / "settings.json").read_text(encoding="utf-8")) \
            if (old / "settings.json").is_file() else {}
        section = old_settings.get("plugin:desktop_companion") or {}
        skip = {"power_on_start", "home", "bundled_imported"}
        for key, value in section.items():
            if key not in skip and settings.get("juno", key, None) is None:
                settings.set("juno", key, value)
        if section:
            moved.append("settings")
    except Exception as exc:
        log.warning("Migration from Multi-Tool partly failed: %s", exc)
    old_models = Path(os.environ.get("MT_MIGRATE_MODELS") or (Path(os.environ.get("LOCALAPPDATA", "")) /
                                                              "MultiTool" / "models"))
    if old_models.is_dir() and not models.exists():
        try:
            models.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_models, models)
            moved.append("downloaded brain & voices")
        except OSError:
            try:
                shutil.copytree(old_models, models)
                moved.append("downloaded brain & voices")
            except Exception as exc:
                log.warning("Couldn't bring the models over: %s", exc)
    if not moved:
        return []
    log.info("Migrated from Multi-Tool: %s", moved)
    return ["Brought over your " + ", ".join(moved) + " from Multi-Tool's companion. Multi-Tool no longer "
            "includes an assistant — Juno is the new home."]


def start_with_windows_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "Juno")
            return True
    except Exception:
        return False


def set_start_with_windows(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            target = sys.executable if IS_FROZEN else f'{sys.executable}" "{Path(sys.argv[0]).resolve()}'
            winreg.SetValueEx(key, "Juno", 0, winreg.REG_SZ, f'"{target}" --mini')
        else:
            try:
                winreg.DeleteValue(key, "Juno")
            except FileNotFoundError:
                pass


def main() -> int:
    if "--after-update" in sys.argv:
        try:
            B.wait_for_process(int(sys.argv[sys.argv.index("--after-update") + 1]))
        except (ValueError, IndexError):
            pass
    paths = B.resolve_paths()
    B.ensure_stdio(paths)
    log = B.setup_logging(paths)
    log.info("Starting Juno %s (frozen=%s) from %s", APP_VERSION, IS_FROZEN, paths.app_dir)
    if not B.acquire_single_instance():
        try:  # ask the running Juno to come out of mini mode / to the front
            (paths.data_dir / "show.request").write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        B.focus_existing_window()
        return 0
    B.set_app_user_model_id()
    B.configure_environment(paths, log)
    ctk.set_appearance_mode("dark")
    B._set_default_font()
    try:
        app = JunoApp(paths, log, start_mini="--mini" in sys.argv)
    except Exception:
        log.critical("Startup failed", exc_info=True)
        B.show_fatal_error(f"Juno could not start.\n\n{traceback.format_exc(limit=3)}\n\n"
                           f"Full log: {paths.logs_dir / 'juno.log'}")
        return 1
    app.mainloop()
    log.info("Exited cleanly")
    return 0
