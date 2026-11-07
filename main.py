"""
Multi-Tool — Desktop Utility Belt (core shell)
==============================================

The core window is only a layout shell + router. Every tool lives in ./plugins/
and is discovered at runtime, Stremio-style.

Folder layout (next to main.py in dev, next to MultiTool.exe when built):

    plugins/            one .py file per tool, or a folder with __init__.py
    bin/                bundled engines: ffmpeg.exe, ffprobe.exe (+ their DLLs),
                        yt-dlp.exe, deno.exe,
                        tesseract/tesseract.exe (+ tessdata/)
    assets/icon.ico     optional window icon
    data/               settings, logs, themes, per-plugin data (dev/portable mode)

PLUGIN CONTRACT (API v1)
------------------------
A plugin file must define `class Plugin`:

    import customtkinter as ctk

    class Plugin:
        meta_title = "Media Downloader"           # required (sidebar + header)
        meta_description = "Pull MP4/MP3 ..."     # shown under the page title
        meta_icon = "⬇"                           # optional, ONE character (stick to
                                                  # BMP symbols; Tk 8.6 can't draw most emoji)
        meta_order = 10                           # optional sidebar sort key
        meta_api_version = 1                      # optional, refuses to load on newer API

        def __init__(self, ctx):                  # ctx = PluginContext (see below)
            self.ctx = ctx                        # start background daemons here if the
                                                  # tool must run while its tab is hidden

        def get_frame(self, master):              # called lazily, first time the tab opens
            frame = ctk.CTkFrame(master, fg_color="transparent")
            ...
            return frame

        # optional hooks
        def on_show(self): ...
        def on_hide(self): ...
        def on_theme_changed(self, palette): ...
        def shutdown(self): ...                   # stop threads / hotkeys / file watchers

Files/folders starting with "_" are ignored by the loader but stay importable,
so shared helpers can live in plugins/_common.py and be imported normally.

PluginContext gives each plugin:
    ctx.plugin_id, ctx.app, ctx.paths, ctx.log, ctx.data_dir, ctx.dnd_available
    ctx.theme.tag(widget, role) / .apply(widget) / .palette / .font() / .subscribe(fn)
    ctx.bin_path("ffmpeg", required=True)      -> Path to bin/ffmpeg.exe
    ctx.subprocess_kwargs()                    -> hides console windows of child processes
    ctx.register_drop_target(widget, callback, accept=("files", "text"))
    ctx.call_in_ui(fn, *args)                  -> safe from worker threads
    ctx.run_in_thread(fn, *args, on_done=..., on_error=...)
    ctx.get_setting(key, default) / ctx.set_setting(key, value)
    ctx.copy_to_clipboard(text), ctx.set_status(text, level), ctx.open_path(path)
    ctx.register_file_type(".ext", "Label", handler)   -> Settings ▸ Install… accepts that file type

THEME PACKS (.mttheme)
----------------------
A theme is a single .mttheme file — plain JSON, or a zip containing theme.json plus an
optional banner image. Install by dropping it on Settings or via Settings ▸ Install theme….
    {
      "name": "My Theme", "author": "me", "version": "1.0", "description": "…",
      "extends": "Default Dark",                 # unspecified colours come from here
      "colors":  { "bg": "#000000", "accent": "#ffcc00", "radius": 0, "title_font": "Bahnschrift", … },
      "effects": { "uppercase_titles": true, "title_prefix": "// ", "glitch_titles": true, … },
      "banner":  "banner.png"                    # zip packs only
    }
Colour keys: see BUILTIN_THEMES. Effect keys: see EFFECT_DEFAULTS.

THEME ROLES (set with ctx.theme.tag(widget, role)):
    frames:  "transparent" | "surface" | "sidebar" | "card" | "entry" | "divider"
    buttons: "accent" (default) | "secondary" | "ghost" | "danger"
    labels:  "text" (default) | "muted" | "title" | "accent" | "accent2" |
             "danger" | "success" | "warning"
    any:     "skip"  -> theme engine never touches this widget or its children
                        (use for screen overlays / custom-drawn canvases)
Untagged widgets are themed automatically by type.
"""

from __future__ import annotations

import copy
import ctypes
import importlib
import importlib.util
import inspect
import io
import json
import logging
import logging.handlers
import multiprocessing
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import tkinter as tk

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD

    _DnDBase = TkinterDnD.DnDWrapper
    _DND_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # app still runs, drag-and-drop is just disabled
    TkinterDnD = None
    DND_FILES, DND_TEXT = "DND_Files", "DND_Text"
    _DND_IMPORT_ERROR = _exc

    class _DnDBase:  # type: ignore[no-redef]
        pass

try:
    from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass as _CTkBase
except Exception:
    _CTkBase = None
_CTkCanvas = getattr(ctk, "CTkCanvas", None)


# ════════════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════════════

APP_NAME = "Multi-Tool"          # placeholder product name — change in one place
APP_ID = "MultiTool"             # filesystem / mutex safe id
APP_VERSION = "0.5.0"
PLUGIN_API_VERSION = 1
PLUGIN_NAMESPACE = "multitool_plugins"

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))

SETTINGS_KEY = "__settings__"
EMPTY_KEY = "__empty__"
DEFAULT_THEME = "Default Dark"

# Engines the confirmed modules expect in /bin (name, who uses it)
KNOWN_ENGINES: list[tuple[str, str]] = [
    ("ffmpeg", "Media Downloader, Gameplay Clipper, Audio Extractor"),
    ("ffprobe", "Media Downloader, Audio Extractor"),
    ("yt-dlp", "Media Downloader"),
    ("deno", "Media Downloader (YouTube challenge solving)"),
    ("tesseract", "Optics OCR"),
]

URL_RE = re.compile(r"https?://[^\s<>\"'{}|\\^`]+", re.IGNORECASE)


# ════════════════════════════════════════════════════════════════════════════
#  Themes
# ════════════════════════════════════════════════════════════════════════════

BUILTIN_THEMES: dict[str, dict[str, Any]] = {
    "Default Dark": {
        "bg": "#0e0f14",
        "surface": "#0e0f14",
        "sidebar": "#14151c",
        "card": "#171922",
        "card_hover": "#1f2230",
        "control": "#1d202b",
        "control_hover": "#282c3b",
        "border": "#242836",
        "border_strong": "#363b4f",
        "text": "#e6e7ee",
        "text_muted": "#8b8fa3",
        "title": "#f5f6fa",
        "accent": "#6c7bff",
        "accent_hover": "#5867f0",
        "accent_text": "#ffffff",
        "accent2": "#9da5c0",
        "selected": "#6c7bff",
        "selected_hover": "#5867f0",
        "danger": "#ef5b5b",
        "danger_hover": "#d94848",
        "success": "#43c47a",
        "warning": "#e3a83a",
        "entry_bg": "#0b0c10",
        "scrollbar": "#2a2d3c",
        "scrollbar_hover": "#3b3f53",
        "nav_active_bg": "#1f2233",
        "nav_active_text": "#ffffff",
        "nav_active_border": "#1f2233",
        "nav_active_border_width": 0,
        "drop_highlight": "#6c7bff",
        "shadow": "#07080b",
        "card_border_width": 1,
        "radius": 10,
        "font": "Segoe UI",
        "title_font": "Segoe UI Semibold",
        "mono_font": "Consolas",
    },
    "Cyberpunk Neon": {
        "bg": "#07070d",
        "surface": "#07070d",
        "sidebar": "#0a0a14",
        "card": "#0d0d1a",
        "card_hover": "#16162c",
        "control": "#11112a",
        "control_hover": "#1c1c40",
        "border": "#00b896",
        "border_strong": "#00ffcc",
        "text": "#d8fff6",
        "text_muted": "#6f82a1",
        "title": "#00ffcc",
        "accent": "#00ffcc",
        "accent_hover": "#00d9ad",
        "accent_text": "#07070d",
        "accent2": "#ff2bd6",
        "selected": "#ff2bd6",
        "selected_hover": "#d91fb5",
        "danger": "#ff3366",
        "danger_hover": "#e0204f",
        "success": "#39ff14",
        "warning": "#ffe600",
        "entry_bg": "#04040a",
        "scrollbar": "#3a1046",
        "scrollbar_hover": "#ff2bd6",
        "nav_active_bg": "#140c24",
        "nav_active_text": "#ff2bd6",
        "nav_active_border": "#ff2bd6",
        "nav_active_border_width": 1,
        "drop_highlight": "#ff2bd6",
        "shadow": "#5a0f4c",
        "card_border_width": 1,
        "radius": 0,
        "font": "Segoe UI",
        "title_font": "Bahnschrift Condensed",
        "mono_font": "Consolas",
    },
}

FRAME_ROLE_COLORS = {
    "bg": "bg",
    "surface": "surface",
    "sidebar": "sidebar",
    "statusbar": "sidebar",
    "card": "card",
    "entry": "entry_bg",
    "divider": "border",
}

LABEL_ROLE_COLORS = {
    None: "text",
    "text": "text",
    "muted": "text_muted",
    "title": "title",
    "accent": "accent",
    "accent2": "accent2",
    "danger": "danger",
    "success": "success",
    "warning": "warning",
}


THEME_EXTENSIONS = (".mttheme", ".json")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_KEYS = {"font", "title_font", "mono_font"}

# Optional shell decorations a theme pack can switch on ("effects" in theme.json)
EFFECT_DEFAULTS: dict[str, Any] = {
    "uppercase_titles": False,   # page titles in CAPS
    "uppercase_nav": False,      # sidebar entries in CAPS
    "title_prefix": "",          # e.g. "// "
    "nav_active_prefix": "",     # e.g. "▶ " in front of the selected tool
    "status_prefix": "",         # e.g. "> " in the status bar
    "brand_tagline": "",         # replaces "Desktop Utility Belt" under the logo
    "glitch_titles": False,      # page title briefly scrambles every few seconds
    "glitch_colors": [],         # colours used while glitching (default: accent2 + danger)
    "divider_height": 1,         # thickness of the header / sidebar dividers
    "divider_color": "border",   # palette key for those dividers
    "sidebar_stripe": 0,         # width of a coloured stripe between sidebar and content
    "sidebar_stripe_color": "accent",
    # layered / "3D" panels
    "card_echo": 0,              # offset (px) of an outline copy behind every card — stacked-panel depth
    "card_echo_color": "shadow", # palette key for that echo
    "corner_brackets": 0,        # length (px) of HUD corner brackets on cards
    "bracket_color": "accent",
    "nav_indicator": 0,          # width (px) of a bar on the selected sidebar entry
    # header
    "title_size": 24,
    "title_shadow": "",          # palette key: offset copy behind the page title (chromatic/3D)
    "title_decrypt": False,      # page title "decrypts" into place when a page opens
    "kicker": "",                # small line above the title, e.g. "MODULE_{index:02d} // {state}"
    "hex_ticker": False,         # streaming hex telemetry in the header
    # motion
    "scan_sweep": False,         # a scan line sweeps the page when it opens
    "boot_sequence": False,      # terminal boot animation when the app starts
    "boot_lines": [],            # custom boot text lines
    # status bar
    "status_clock": False,
    "status_telemetry": False,   # live CPU / memory readout
    "blink_cursor": False,
    # typography
    "body_font": "",             # font family for all regular text (e.g. a monospace terminal look)
    # sound
    "synth_sounds": "",          # built-in synthesized UI sound set: neon, hazard, phosphor, amber, soft
}
EFFECT_LIMITS = {"title_size": (14, 40), "card_echo": (0, 16), "corner_brackets": (0, 40)}
SOUND_EVENTS = ("click", "nav", "open", "success", "error", "notify", "boot")


# ── synthesized UI sounds (no audio files needed) ───────────────────────────
# A note: (start s, duration s, start Hz, end Hz, wave, volume). Waves: sine, square, saw, tri, noise.
SYNTH_RATE = 22050
_SYNTH_STYLES: dict[str, dict[str, Any]] = {
    "neon": {"wave": "square", "bright": 1.0, "drive": 1.6},
    "hazard": {"wave": "saw", "bright": 0.8, "drive": 2.6},
    "phosphor": {"wave": "sine", "bright": 0.9, "drive": 1.0},
    "amber": {"wave": "tri", "bright": 0.7, "drive": 1.2},
    "soft": {"wave": "sine", "bright": 1.1, "drive": 1.0},
}


def _synth_events(style: str) -> dict[str, list[tuple]]:
    w = _SYNTH_STYLES.get(style, _SYNTH_STYLES["soft"])["wave"]
    b = _SYNTH_STYLES.get(style, _SYNTH_STYLES["soft"])["bright"]
    rnd = random.Random(style)
    boot = [(0.0, 0.75, 70 * b, 900 * b, "saw" if w != "sine" else "sine", 0.16),
            (0.0, 0.6, 0, 0, "noise", 0.035)]
    for i in range(14):
        boot.append((0.08 + i * 0.045, 0.028, rnd.choice((1200, 1600, 2000, 2400, 2800)) * b,
                     None, w, 0.10))
    boot += [(0.82, 0.7, 523 * b, None, "tri", 0.16), (0.82, 0.7, 659 * b, None, "tri", 0.13),
             (0.82, 0.7, 784 * b, None, "tri", 0.12), (0.86, 0.66, 1046 * b, None, w, 0.07)]
    return {
        "click": [(0, 0.032, 2500 * b, 2100 * b, w, 0.22), (0, 0.012, 0, 0, "noise", 0.05)],
        "nav": [(0, 0.045, 900 * b, 1350 * b, w, 0.18), (0.04, 0.06, 1800 * b, None, "sine", 0.20)],
        "open": [(0, 0.2, 300 * b, 1400 * b, "saw" if w != "sine" else "sine", 0.12),
                 (0.12, 0.12, 1568 * b, None, "sine", 0.18), (0.18, 0.16, 2093 * b, None, "sine", 0.14)],
        "success": [(0, 0.12, 1046 * b, None, w, 0.16), (0.07, 0.12, 1318 * b, None, w, 0.16),
                    (0.14, 0.22, 1568 * b, None, w, 0.18)],
        "error": [(0, 0.11, 230, 190, "square", 0.22), (0.12, 0.2, 180, 140, "square", 0.22),
                  (0, 0.3, 0, 0, "noise", 0.03)],
        "notify": [(0, 0.16, 1568 * b, None, "sine", 0.24), (0.11, 0.3, 2093 * b, None, "sine", 0.22),
                   (0.11, 0.3, 1046 * b, None, w, 0.06)],
        "boot": boot,
    }


def synth_wav(style: str, event: str) -> bytes:
    """Render one UI sound to WAV bytes (pure Python, ~10 ms)."""
    import math
    import struct
    import wave
    notes = _synth_events(style).get(event, [])
    drive = _SYNTH_STYLES.get(style, _SYNTH_STYLES["soft"])["drive"]
    length = max((n[0] + n[1] for n in notes), default=0.05) + 0.02
    total = int(length * SYNTH_RATE)
    buf = [0.0] * total
    rnd = random.Random(event)
    for start, dur, f0, f1, shape, vol in notes:
        f1 = f0 if f1 is None else f1
        first, count = int(start * SYNTH_RATE), int(dur * SYNTH_RATE)
        phase = 0.0
        tau = max(dur / 3.2, 0.004)
        for i in range(count):
            j = first + i
            if j >= total:
                break
            t = i / SYNTH_RATE
            freq = f0 + (f1 - f0) * (i / max(count - 1, 1))
            phase = (phase + freq / SYNTH_RATE) % 1.0
            if shape == "sine":
                v = math.sin(2 * math.pi * phase)
            elif shape == "square":
                v = 1.0 if phase < 0.5 else -1.0
            elif shape == "saw":
                v = 2.0 * phase - 1.0
            elif shape == "tri":
                v = 4.0 * abs(phase - 0.5) - 1.0
            else:
                v = rnd.uniform(-1.0, 1.0)
            env = min(1.0, t / 0.003) * math.exp(-t / tau) * min(1.0, (dur - t) / 0.008)
            buf[j] += v * vol * env
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SYNTH_RATE)
        wav.writeframes(b"".join(struct.pack("<h", int(32000 * math.tanh(v * drive) / math.tanh(drive)))
                                 for v in buf))
    return out.getvalue()


@dataclass
class ThemeInfo:
    name: str
    palette: dict[str, Any]
    effects: dict[str, Any]
    author: str = ""
    description: str = ""
    version: str = ""
    source: Path | None = None   # None = built into the app (cannot be removed)
    banner: str | None = None    # image inside a zipped .mttheme, shown instead of the app name
    sounds: dict[str, str] = field(default_factory=dict)  # event -> wav inside a zipped .mttheme

    @property
    def builtin(self) -> bool:
        return self.source is None


BUILTIN_THEME_INFO: dict[str, dict[str, Any]] = {
    "Default Dark": {
        "description": "Calm graphite with soft depth and an indigo accent. The default — always available.",
        "effects": {"card_echo": 4, "card_echo_color": "shadow", "nav_indicator": 3, "status_clock": True,
                    "title_size": 26},
    },
    "Cyberpunk Neon": {
        "description": "Near-black HUD: neon cyan frames, magenta depth layers, decrypting titles, live telemetry.",
        "effects": {"nav_active_prefix": "› ", "status_prefix": "> ", "uppercase_titles": True,
                    "card_echo": 6, "card_echo_color": "accent2", "corner_brackets": 12,
                    "bracket_color": "border_strong", "nav_indicator": 3, "title_shadow": "accent2",
                    "title_decrypt": True, "title_size": 28, "kicker": "MODULE_{index:02d} // {state}",
                    "hex_ticker": True, "scan_sweep": True, "status_clock": True, "status_telemetry": True,
                    "blink_cursor": True, "sidebar_stripe": 1, "sidebar_stripe_color": "accent",
                    "divider_color": "accent2", "synth_sounds": "neon", "boot_sequence": True,
                    "boot_lines": ["NEURAL LINK // MULTI-TOOL v{version}",
                                   "> ICE breaker .............. bypassed",
                                   "> mounting modules ......... {plugins} found",
                                   "> optics calibration ....... ok",
                                   "> theme ..................... {theme}",
                                   "LINK ESTABLISHED"]},
    },
}


def _merge_effects(raw: Any, warnings: list[str]) -> dict[str, Any]:
    effects = dict(EFFECT_DEFAULTS)
    if not isinstance(raw, dict):
        return effects
    for key, value in raw.items():
        default = EFFECT_DEFAULTS.get(key)
        if key not in EFFECT_DEFAULTS:
            warnings.append(f"unknown effect {key!r}")
        elif isinstance(default, bool):
            effects[key] = bool(value)
        elif isinstance(default, int) and isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = EFFECT_LIMITS.get(key, (0, 12))
            effects[key] = max(lo, min(int(value), hi))
        elif isinstance(default, list) and isinstance(value, list):
            if key == "glitch_colors":
                effects[key] = [v for v in value if isinstance(v, str) and HEX_COLOR_RE.match(v)]
            else:
                effects[key] = [str(v)[:90] for v in value[:16]]
        elif isinstance(default, str) and isinstance(value, str):
            effects[key] = value[:60]
        else:
            warnings.append(f"invalid effect {key!r}")
    return effects


def builtin_theme_infos() -> dict[str, ThemeInfo]:
    out = {}
    for name, palette in BUILTIN_THEMES.items():
        meta = BUILTIN_THEME_INFO.get(name, {})
        out[name] = ThemeInfo(name=name, palette=dict(palette), effects=_merge_effects(meta.get("effects"), []),
                              author="Built in", description=meta.get("description", ""))
    return out


def read_theme_pack(path: Path) -> tuple[ThemeInfo, list[str]]:
    """A theme pack is a .mttheme file: plain JSON, or a zip holding theme.json (+ banner image).
    Plain .json files in the themes folder are read the same way."""
    warnings: list[str] = []
    banner_member = None
    sound_members: dict[str, str] = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            member = next((n for n in names if n.rsplit("/", 1)[-1].lower() == "theme.json"), None)
            if member is None:
                raise ValueError("the pack has no theme.json inside")
            raw = json.loads(zf.read(member).decode("utf-8-sig"))
            prefix = member[: -len("theme.json")]
            banner = raw.get("banner") if isinstance(raw, dict) else None
            if isinstance(banner, str) and (prefix + banner) in names:
                banner_member = prefix + banner
            elif banner:
                warnings.append(f"banner {banner!r} not found in pack")
            sounds_raw = raw.get("sounds") if isinstance(raw, dict) else None
            for event, member_name in (sounds_raw.items() if isinstance(sounds_raw, dict) else []):
                if event in SOUND_EVENTS and isinstance(member_name, str) and (prefix + member_name) in names \
                        and member_name.lower().endswith(".wav"):
                    sound_members[event] = prefix + member_name
                else:
                    warnings.append(f"sound {event!r} skipped")
    else:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("theme must be a JSON object")
    name = str(raw.get("name") or path.stem).strip()[:60] or path.stem
    base = BUILTIN_THEMES.get(str(raw.get("extends", DEFAULT_THEME)), BUILTIN_THEMES[DEFAULT_THEME])
    palette = dict(base)
    colors = raw.get("colors")
    if not isinstance(colors, dict):  # older flat format: colour keys at top level
        colors = {k: v for k, v in raw.items() if k in palette}
    for key, value in colors.items():
        if key not in palette:
            warnings.append(f"unknown colour key {key!r}")
        elif key in FONT_KEYS and isinstance(value, str) and value.strip():
            palette[key] = value.strip()
        elif isinstance(palette[key], int) and isinstance(value, (int, float)) and not isinstance(value, bool):
            palette[key] = max(0, min(int(value), 24))
        elif isinstance(palette[key], str) and isinstance(value, str) and HEX_COLOR_RE.match(value):
            palette[key] = value
        else:
            warnings.append(f"invalid value for {key!r}")
    info = ThemeInfo(name=name, palette=palette, effects=_merge_effects(raw.get("effects"), warnings),
                     author=str(raw.get("author", ""))[:60], description=str(raw.get("description", ""))[:200],
                     version=str(raw.get("version", ""))[:20], source=path, banner=banner_member,
                     sounds=sound_members)
    return info, warnings


def load_theme_packs(themes_dir: Path, log: logging.Logger) -> dict[str, ThemeInfo]:
    out: dict[str, ThemeInfo] = {}
    for file in sorted(themes_dir.iterdir(), key=lambda f: f.name.lower()) if themes_dir.is_dir() else []:
        if file.suffix.lower() not in THEME_EXTENSIONS or not file.is_file():
            continue
        try:
            info, warnings = read_theme_pack(file)
            if info.name in BUILTIN_THEMES or info.name in out:
                info.name = f"{info.name} ({file.stem})"
            for w in warnings:
                log.warning("Theme %s: %s", file.name, w)
            out[info.name] = info
            log.info("Loaded theme pack %r from %s", info.name, file.name)
        except Exception as exc:
            log.warning("Skipping theme file %s: %s", file, exc)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Paths, environment, logging (portable-build aware)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AppPaths:
    app_dir: Path          # folder holding main.py / MultiTool.exe
    resource_dir: Path     # read-only bundled resources (PyInstaller _MEIPASS)
    plugins_dir: Path
    bin_dir: Path
    data_dir: Path         # writable user data
    logs_dir: Path
    themes_dir: Path
    plugin_data_dir: Path
    settings_file: Path
    portable: bool


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_paths() -> AppPaths:
    if IS_FROZEN:
        app_dir = Path(sys.executable).resolve().parent
        resource_dir = Path(getattr(sys, "_MEIPASS", app_dir))
    else:
        app_dir = Path(__file__).resolve().parent
        resource_dir = app_dir

    # Dev runs and builds shipped with a `portable.flag` file keep data beside the app.
    # Installed builds use %APPDATA%\MultiTool (Program Files is read-only).
    portable = (not IS_FROZEN) or (app_dir / "portable.flag").exists()
    data_dir = app_dir / "data"
    if not portable or not _is_writable_dir(data_dir):
        portable = False
        if IS_WINDOWS:
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            data_dir = Path(base) / APP_ID
        else:
            data_dir = Path.home() / f".{APP_ID.lower()}"

    logs_dir = data_dir / "logs"
    themes_dir = data_dir / "themes"
    plugin_data_dir = data_dir / "plugin_data"
    for d in (data_dir, logs_dir, themes_dir, plugin_data_dir):
        d.mkdir(parents=True, exist_ok=True)

    plugins_dir = app_dir / "plugins"
    bin_dir = app_dir / "bin"
    for d in (plugins_dir, bin_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only install location; the installer ships these folders

    return AppPaths(
        app_dir=app_dir,
        resource_dir=resource_dir,
        plugins_dir=plugins_dir,
        bin_dir=bin_dir,
        data_dir=data_dir,
        logs_dir=logs_dir,
        themes_dir=themes_dir,
        plugin_data_dir=plugin_data_dir,
        settings_file=data_dir / "settings.json",
        portable=portable,
    )


def ensure_stdio(paths: AppPaths) -> None:
    """PyInstaller --windowed sets sys.stdout/stderr to None; libraries like tqdm
    (used by Whisper) and yt-dlp crash when they write to it. Point them at a file."""
    if sys.stdout is None or sys.stderr is None:
        stream = open(paths.logs_dir / "console.log", "a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def setup_logging(paths: AppPaths) -> logging.Logger:
    log = logging.getLogger(APP_ID)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir / "multitool.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    log.addHandler(file_handler)

    if not IS_FROZEN:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.setLevel(logging.INFO)
        log.addHandler(console)

    def excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        name = args.thread.name if args.thread else "?"
        log.error("Unhandled exception in thread %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return log


def configure_environment(paths: AppPaths, log: logging.Logger) -> None:
    """Make bundled engines win over anything on the user's machine — for THIS process
    and its children only. The user's Windows PATH is never touched."""
    extra = [d for d in (paths.bin_dir, paths.bin_dir / "tesseract") if d.is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join([str(d) for d in extra] + [os.environ.get("PATH", "")])
    tessdata = paths.bin_dir / "tesseract" / "tessdata"
    if tessdata.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)

    # plugins/ on sys.path lets plugins share helpers (plugins/_common.py -> `import _common`)
    if str(paths.plugins_dir) not in sys.path:
        sys.path.insert(0, str(paths.plugins_dir))
    if IS_FROZEN:
        sys.dont_write_bytecode = True  # don't litter the install folder with __pycache__
    log.debug("PATH prefix: %s", [str(d) for d in extra])


def find_binary(bin_dir: Path, name: str, required: bool = False) -> Path | None:
    """bin/<name>.exe or bin/<name>/<name>.exe (tesseract ships as a folder)."""
    stem = name[:-4] if name.lower().endswith(".exe") else name
    filename = stem + (".exe" if IS_WINDOWS else "")
    for candidate in (bin_dir / filename, bin_dir / stem / filename):
        if candidate.is_file():
            return candidate
    if required:
        raise FileNotFoundError(f"Bundled engine '{filename}' is missing. Expected it at: {bin_dir / filename}")
    return None


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Pass to subprocess.Popen/run so ffmpeg/yt-dlp don't flash console windows."""
    if not IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def open_in_os(path: Path | str) -> None:
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


# ── Windows integration ─────────────────────────────────────────────────────

_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """Global hotkeys (F8, Ctrl+Alt+S) would collide if two copies ran."""
    global _INSTANCE_MUTEX
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}_SingleInstance")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def focus_existing_window() -> None:
    if not IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def set_app_user_model_id() -> None:
    """Own taskbar identity/icon instead of being grouped under python.exe."""
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.Desktop")
        except Exception:
            pass


def show_fatal_error(message: str) -> None:
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} failed to start", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


# ════════════════════════════════════════════════════════════════════════════
#  Settings store (atomic JSON)
# ════════════════════════════════════════════════════════════════════════════

class SettingsStore:
    def __init__(self, path: Path, log: logging.Logger):
        self.path = path
        self.log = log
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
            raise ValueError("root is not an object")
        except Exception as exc:
            backup = self.path.with_name(self.path.stem + ".corrupt.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.log.warning("settings.json was unreadable (%s); moved to %s", exc, backup.name)
            return {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(section, {}).get(key, default))

    def set(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(section, {})[key] = value
            self._save_locked()

    def _save_locked(self) -> None:
        tmp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_name, self.path)
        except Exception:
            self.log.exception("Failed to save settings")
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


# ════════════════════════════════════════════════════════════════════════════
#  Theme engine
# ════════════════════════════════════════════════════════════════════════════

def _safe_configure(widget: Any, **kwargs: Any) -> None:
    """Configure what the widget supports; silently skip options it doesn't."""
    if not kwargs:
        return
    try:
        widget.configure(**kwargs)
    except Exception:
        for key, value in kwargs.items():
            try:
                widget.configure(**{key: value})
            except Exception:
                pass


def _is_ctk(widget: Any) -> bool:
    if _CTkBase is not None and isinstance(widget, _CTkBase):
        return True
    return isinstance(widget, (ctk.CTk, ctk.CTkToplevel, ctk.CTkScrollableFrame)) or hasattr(
        widget, "_apply_appearance_mode"
    )


def _container_of(widget: Any) -> Any:
    """CTkScrollableFrame lives inside a hidden host frame; that host is what gets gridded."""
    return getattr(widget, "_parent_frame", widget)


def _resolve_color(color: Any) -> Any:
    if isinstance(color, (list, tuple)) and color:
        return color[-1]  # (light, dark) -> app is always dark
    return color


def _alive(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


class SoundPlayer:
    """Theme UI sounds through Windows MCI: several sounds can overlap, and they never interrupt
    other audio the app plays."""

    def __init__(self, root: tk.Misc, log: logging.Logger):
        self.root = root
        self.log = log
        self.enabled = True
        self.volume = 0.6
        self.available = IS_WINDOWS
        self._n = 0
        self._last: dict[str, float] = {}

    def play(self, path: Path | None, key: str = "") -> None:
        if not (self.available and self.enabled and path) or self.volume <= 0:
            return
        now = time.monotonic()
        if now - self._last.get(key, 0) < 0.05:  # debounce bursts (double events)
            return
        self._last[key] = now
        try:
            mci = ctypes.windll.winmm.mciSendStringW
            self._n = (self._n + 1) % 10000
            alias = f"mtsnd{self._n}"
            if mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None) != 0:
                return
            mci(f"setaudio {alias} volume to {int(self.volume * 1000)}", None, 0, None)
            mci(f"play {alias}", None, 0, None)
            self.root.after(5000, lambda: mci(f"close {alias}", None, 0, None))
        except Exception as exc:
            self.log.debug("sound failed: %s", exc)


class ThemeManager:
    """Walks the live widget tree and restyles it from the active palette.
    Runs on every theme switch and on every newly mounted plugin page."""

    def __init__(self, root: tk.Misc, settings: SettingsStore, themes_dir: Path, log: logging.Logger):
        self._root = root
        self._settings = settings
        self.themes_dir = themes_dir
        self.log = log
        self.infos: dict[str, ThemeInfo] = {}
        self.themes: dict[str, dict[str, Any]] = {}
        self.name = DEFAULT_THEME
        self._listeners: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        self._fonts: dict[tuple, ctk.CTkFont] = {}
        self._banners: dict[str, Any] = {}
        self._sound_cache: dict[tuple[str, str], Path | None] = {}
        self.sound_dir = themes_dir.parent / "cache" / "theme_sounds"
        self.player = SoundPlayer(root, log)
        self.player.enabled = bool(settings.get("core", "ui_sounds", True))
        self.player.volume = float(settings.get("core", "ui_volume", 0.6))
        self.reduce_motion = bool(settings.get("core", "reduce_motion", False))
        self.reload()
        saved = settings.get("core", "theme", DEFAULT_THEME)
        self.name = saved if saved in self.themes else DEFAULT_THEME

    # ── sounds ────────────────────────────────────────────────────────────
    def sound_path(self, event: str) -> Path | None:
        info = self.infos.get(self.name)
        if info is None:
            return None
        if event not in info.sounds or info.source is None:
            style = info.effects.get("synth_sounds") or ""
            if not style or event not in SOUND_EVENTS:
                return None
            key = ("synth:" + style, event)
            if key not in self._sound_cache:
                path = self.sound_dir / f"synth-{re.sub(r'[^a-z0-9]+', '', style.lower()) or 'soft'}" / f"{event}.wav"
                try:
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_suffix(".tmp")
                        tmp.write_bytes(synth_wav(style, event))
                        os.replace(tmp, path)
                except Exception as exc:
                    self.log.warning("Could not make UI sound %s/%s: %s", style, event, exc)
                    path = None
                self._sound_cache[key] = path
            return self._sound_cache[key]
        key = (self.name, event)
        if key not in self._sound_cache:
            path = None
            try:
                slug = re.sub(r"[^\w\-]+", "-", self.name).strip("-").lower() or "theme"
                folder = self.sound_dir / slug
                folder.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(info.source) as zf:
                    data = zf.read(info.sounds[event])
                path = folder / f"{event}.wav"
                if not path.exists() or path.stat().st_size != len(data):
                    path.write_bytes(data)
            except Exception as exc:
                self.log.warning("Theme sound %s unavailable: %s", event, exc)
                path = None
            self._sound_cache[key] = path
        return self._sound_cache[key]

    def play(self, event: str) -> None:
        self.player.play(self.sound_path(event), event)

    def has_sounds(self, name: str | None = None) -> bool:
        info = self.infos.get(name or self.name)
        return bool(info and (info.sounds or info.effects.get("synth_sounds")))

    # ── theme packs ───────────────────────────────────────────────────────
    def reload(self) -> None:
        """Re-scan the themes folder (built-in themes always come first)."""
        self.infos = builtin_theme_infos()
        self.infos.update(load_theme_packs(self.themes_dir, self.log))
        self.themes = {name: info.palette for name, info in self.infos.items()}
        self._banners.clear()
        self._sound_cache = {k: v for k, v in getattr(self, "_sound_cache", {}).items()
                             if k[0] in self.infos or k[0].startswith("synth:")}
        if self.name not in self.themes:
            self.name = DEFAULT_THEME

    def info(self, name: str | None = None) -> ThemeInfo:
        return self.infos[name or self.name]

    def install(self, src: Path) -> str:
        """Validate and copy a .mttheme into the themes folder. Returns the installed theme's name."""
        info, _warnings = read_theme_pack(src)  # raises if the pack is broken
        slug = re.sub(r"[^\w\-]+", "-", info.name).strip("-").lower() or "theme"
        dest = self.themes_dir / f"{slug}{src.suffix.lower()}"
        for existing in self.infos.values():  # replacing an older copy of the same theme
            if existing.source and existing.name == info.name and existing.source != dest:
                existing.source.unlink(missing_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        self.reload()
        installed = next((n for n, i in self.infos.items() if i.source and i.source.resolve() == dest.resolve()),
                         info.name)
        self.log.info("Installed theme %r -> %s", installed, dest.name)
        return installed

    def remove(self, name: str) -> None:
        info = self.infos.get(name)
        if info is None or info.builtin:
            raise ValueError("built-in themes can't be removed")
        info.source.unlink(missing_ok=True)
        was_active = name == self.name
        self.reload()
        if was_active:
            self.set_theme(DEFAULT_THEME)

    @property
    def effects(self) -> dict[str, Any]:
        return self.infos[self.name].effects

    def banner_image(self, name: str | None = None) -> Any:
        """PIL image of a theme's banner, or None."""
        name = name or self.name
        if name in self._banners:
            return self._banners[name]
        image = None
        info = self.infos.get(name)
        if info and info.banner and info.source:
            try:
                from PIL import Image
                with zipfile.ZipFile(info.source) as zf:
                    image = Image.open(io.BytesIO(zf.read(info.banner)))
                    image.load()
            except Exception as exc:
                self.log.warning("Theme %s: banner unreadable: %s", name, exc)
        self._banners[name] = image
        return image

    # ── public API ────────────────────────────────────────────────────────
    @property
    def palette(self) -> dict[str, Any]:
        return self.themes[self.name]

    def names(self) -> list[str]:
        return list(self.themes)

    def set_theme(self, name: str) -> None:
        if name not in self.themes:
            self.log.warning("Unknown theme %r", name)
            return
        self.name = name
        self._settings.set("core", "theme", name)
        started = time.perf_counter()
        self.apply()
        self.log.info("Theme -> %s (%.0f ms)", name, (time.perf_counter() - started) * 1000)
        palette = dict(self.palette)
        for owner, fn in list(self._listeners):
            try:
                fn(palette)
            except Exception:
                self.log.exception("Theme listener from %s failed", owner)

    def subscribe(self, fn: Callable[[dict[str, Any]], None], owner: str = "core") -> Callable[[], None]:
        entry = (owner, fn)
        self._listeners.append(entry)

        def unsubscribe() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)
        return unsubscribe

    def unsubscribe_owner(self, owner: str) -> None:
        self._listeners = [(o, f) for (o, f) in self._listeners if o != owner]

    @staticmethod
    def tag(widget: Any, role: str | None, **options: Any) -> Any:
        """Assign a theme role (and extras like font_size=18, keep_shape=True). Returns widget."""
        widget._mt_role = role
        for key, value in options.items():
            setattr(widget, f"_mt_{key}", value)
        return widget

    def scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self._root))
        except Exception:
            return 1.0

    def font(self, size: int = 13, weight: str = "normal", family: str | None = None,
             mono: bool = False) -> ctk.CTkFont:
        p = self.palette
        fam = family or (p["mono_font"] if mono else p["font"])
        key = (fam, size, weight)
        if key not in self._fonts:
            self._fonts[key] = ctk.CTkFont(family=fam, size=size, weight=weight)
        return self._fonts[key]

    def apply(self, widget: Any = None) -> None:
        target = widget if widget is not None else self._root
        if target is self._root:
            parent_bg = self.palette["bg"]
        else:
            parent_bg = self.effective_bg(_container_of(target).master)
        try:
            self._walk(target, parent_bg, 0)
        except Exception:
            self.log.exception("Theme apply failed")

    def effective_bg(self, widget: Any) -> str:
        w = widget
        while w is not None:
            try:
                if isinstance(w, (ctk.CTk, ctk.CTkToplevel)):
                    return _resolve_color(w.cget("fg_color"))
                if _is_ctk(w):
                    fg = w.cget("fg_color")
                    if fg and fg != "transparent":
                        return _resolve_color(fg)
                else:
                    bg = w.cget("bg")
                    if bg:
                        return bg
            except Exception:
                pass
            w = getattr(w, "master", None)
        return self.palette["bg"]

    # ── tree walk ─────────────────────────────────────────────────────────
    def _walk(self, w: Any, parent_bg: str, depth: int) -> None:
        if depth > 250:
            return
        try:
            if not w.winfo_exists():
                return
        except Exception:
            return
        role = getattr(w, "_mt_role", None)
        if role == "skip":
            return
        if isinstance(w, ctk.CTkFrame) and not isinstance(w, ctk.CTkSegmentedButton):
            scroll = self._scroll_child(w)
            if scroll is not None:  # hidden host of a CTkScrollableFrame -> style the real widget
                self._walk(scroll, parent_bg, depth + 1)
                return
        own_bg = self._style(w, role, parent_bg)
        for child in self._children(w):
            self._walk(child, own_bg, depth + 1)

    @staticmethod
    def _scroll_child(frame: Any) -> Any:
        try:
            for child in frame.winfo_children():
                if isinstance(child, tk.Canvas):
                    for grandchild in child.winfo_children():
                        if isinstance(grandchild, ctk.CTkScrollableFrame):
                            return grandchild
        except Exception:
            pass
        return None

    @staticmethod
    def _children(w: Any) -> list[Any]:
        if isinstance(w, ctk.CTkTabview):
            return list(getattr(w, "_tab_dict", {}).values())
        if isinstance(w, ctk.CTkSegmentedButton):
            return []
        is_container = isinstance(w, (ctk.CTk, ctk.CTkToplevel, ctk.CTkFrame, ctk.CTkScrollableFrame)) or (
            isinstance(w, (tk.Frame, tk.Toplevel, tk.Tk)) and not _is_ctk(w)
        )
        if not is_container:
            return []  # never descend into a CTk widget's private internals
        try:
            kids = w.winfo_children()
        except Exception:
            return []
        if _CTkCanvas is not None:
            kids = [k for k in kids if not isinstance(k, _CTkCanvas)]
        return kids

    def _style(self, w: Any, role: str | None, parent_bg: str) -> str:
        p = self.palette
        shape = {} if getattr(w, "_mt_keep_shape", False) else {"corner_radius": p["radius"]}

        if isinstance(w, (ctk.CTk, ctk.CTkToplevel)):
            color = p[FRAME_ROLE_COLORS.get(role or "bg", "bg")]
            _safe_configure(w, fg_color=color)
            return color

        if isinstance(w, ctk.CTkScrollableFrame):
            color = p[FRAME_ROLE_COLORS[role]] if role in FRAME_ROLE_COLORS else parent_bg
            _safe_configure(w, fg_color=color, bg_color=parent_bg,
                            scrollbar_button_color=p["scrollbar"],
                            scrollbar_button_hover_color=p["scrollbar_hover"])
            if role == "card":
                _safe_configure(w, border_color=p["border"], border_width=p["card_border_width"])
                self._decorate(w, parent_bg)
            return color

        if not _is_ctk(w):
            return self._style_tk(w, role, parent_bg)

        _safe_configure(w, bg_color=parent_bg)

        if isinstance(w, ctk.CTkSegmentedButton):
            _safe_configure(w, fg_color=p["control"], selected_color=p["selected"],
                            selected_hover_color=p["selected_hover"], unselected_color=p["control"],
                            unselected_hover_color=p["control_hover"], text_color=p["text"],
                            text_color_disabled=p["text_muted"])
            return parent_bg

        if isinstance(w, ctk.CTkFrame):
            try:
                current = w.cget("fg_color")
            except Exception:
                current = None
            if role in FRAME_ROLE_COLORS:
                color = p[FRAME_ROLE_COLORS[role]]
                kwargs: dict[str, Any] = {"fg_color": color}
                if role == "card":
                    kwargs.update(border_color=p["border"], border_width=p["card_border_width"], **shape)
                _safe_configure(w, **kwargs)
                if role == "card" and not getattr(w, "_mt_no_deco", False):
                    self._decorate(w, parent_bg)
                return color
            if role == "transparent" or current == "transparent":
                if current != "transparent":
                    _safe_configure(w, fg_color="transparent")
                return parent_bg
            color = p["card"]  # untagged opaque frame -> card colours, keeps its own geometry
            _safe_configure(w, fg_color=color, border_color=p["border"])
            return color

        if isinstance(w, ctk.CTkTabview):
            _safe_configure(w, fg_color=p["card"], border_color=p["border"],
                            segmented_button_fg_color=p["control"],
                            segmented_button_selected_color=p["selected"],
                            segmented_button_selected_hover_color=p["selected_hover"],
                            segmented_button_unselected_color=p["control"],
                            segmented_button_unselected_hover_color=p["control_hover"],
                            text_color=p["text"])
            return p["card"]

        self._apply_body_font(w, role)
        if isinstance(w, ctk.CTkButton):
            self._style_button(w, role, shape)
            self._bind_click_sound(w, role)
        elif isinstance(w, ctk.CTkLabel):
            _safe_configure(w, text_color=p[LABEL_ROLE_COLORS.get(role, "text")])
            if role == "title":
                size = int(getattr(w, "_mt_font_size", 18))
                _safe_configure(w, font=self.font(size, "bold", family=p["title_font"]))
        elif isinstance(w, ctk.CTkEntry):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"], text_color=p["text"],
                            placeholder_text_color=p["text_muted"], **shape)
        elif isinstance(w, ctk.CTkTextbox):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"], text_color=p["text"],
                            scrollbar_button_color=p["scrollbar"],
                            scrollbar_button_hover_color=p["scrollbar_hover"], **shape)
        elif isinstance(w, ctk.CTkOptionMenu):
            _safe_configure(w, fg_color=p["control"], button_color=p["control_hover"],
                            button_hover_color=p["selected"], text_color=p["text"],
                            dropdown_fg_color=p["card"], dropdown_hover_color=p["control_hover"],
                            dropdown_text_color=p["text"], **shape)
        elif isinstance(w, ctk.CTkComboBox):
            _safe_configure(w, fg_color=p["entry_bg"], border_color=p["border"],
                            button_color=p["control_hover"], button_hover_color=p["selected"],
                            text_color=p["text"], dropdown_fg_color=p["card"],
                            dropdown_hover_color=p["control_hover"], dropdown_text_color=p["text"], **shape)
        elif isinstance(w, ctk.CTkSwitch):
            _safe_configure(w, fg_color=p["control_hover"], progress_color=p["accent"],
                            button_color=p["text"], button_hover_color=p["title"], text_color=p["text"])
        elif isinstance(w, ctk.CTkCheckBox):
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            border_color=p["border_strong"], checkmark_color=p["accent_text"],
                            text_color=p["text"])
        elif isinstance(w, ctk.CTkRadioButton):
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            border_color=p["border_strong"], text_color=p["text"])
        elif isinstance(w, ctk.CTkProgressBar):
            _safe_configure(w, fg_color=p["control"], progress_color=p["accent"], border_color=p["border"])
        elif isinstance(w, ctk.CTkSlider):
            _safe_configure(w, fg_color=p["control"], progress_color=p["accent"],
                            button_color=p["accent"], button_hover_color=p["accent_hover"])
        elif isinstance(w, ctk.CTkScrollbar):
            _safe_configure(w, fg_color="transparent", button_color=p["scrollbar"],
                            button_hover_color=p["scrollbar_hover"])
        return parent_bg

    # ── decorations: stacked echo panel + HUD corner brackets ───────────────
    def _decorate(self, w: Any, parent_bg: str) -> None:
        e, p = self.effects, self.palette
        echo_px, bracket_px = e["card_echo"], e["corner_brackets"]
        deco = getattr(w, "_mt_deco", None)
        if deco is None:
            if not (echo_px or bracket_px):
                return
            deco = {"echo": None, "brackets": []}
            w._mt_deco = deco
            target = _container_of(w)
            try:
                tk.Misc.bind(target, "<Configure>", lambda _e, w=w: self._sync_echo(w), "+")
                tk.Misc.bind(target, "<Map>", lambda _e, w=w: self._sync_echo(w), "+")
                tk.Misc.bind(target, "<Unmap>", lambda _e, w=w: self._hide_echo(w), "+")
                tk.Misc.bind(target, "<Destroy>", lambda ev, w=w: self._drop_echo(w, ev), "+")
            except Exception:
                pass
        target = _container_of(w)
        # a plugin may have destroyed our decoration widgets along with its own children
        if deco["echo"] is not None and not _alive(deco["echo"]):
            deco["echo"] = None
        if deco["brackets"] and not all(_alive(f) for f in deco["brackets"]):
            deco["brackets"] = []
        if echo_px:
            color = p.get(e["card_echo_color"], p.get("shadow", p["border"]))
            if deco["echo"] is None:
                try:
                    deco["echo"] = tk.Frame(target.master, bd=0, highlightthickness=1)
                    deco["echo"]._mt_role = "skip"
                except Exception:
                    deco["echo"] = None
            if deco["echo"] is not None:
                fill = color if e["card_echo_color"] == "shadow" else parent_bg
                deco["echo"].configure(bg=fill, highlightbackground=color, highlightcolor=color)
        elif deco["echo"] is not None:
            deco["echo"].place_forget()
        if bracket_px:
            color = p.get(e["bracket_color"], p["accent"])
            if not deco["brackets"]:
                deco["brackets"] = [tk.Frame(target, bd=0, highlightthickness=0) for _ in range(8)]
                for f in deco["brackets"]:
                    f._mt_role = "skip"
            t = 2
            specs = [(0, 0, "nw", bracket_px, t), (0, 0, "nw", t, bracket_px),
                     (1, 0, "ne", bracket_px, t), (1, 0, "ne", t, bracket_px),
                     (0, 1, "sw", bracket_px, t), (0, 1, "sw", t, bracket_px),
                     (1, 1, "se", bracket_px, t), (1, 1, "se", t, bracket_px)]
            for frame, (rx, ry, anchor, width, height) in zip(deco["brackets"], specs):
                frame.configure(bg=color, width=width, height=height)
                frame.place(relx=rx, rely=ry, anchor=anchor)
                frame.lift()
        else:
            for frame in deco["brackets"]:
                frame.place_forget()
        self._sync_echo(w)

    def _sync_echo(self, w: Any) -> None:
        deco = getattr(w, "_mt_deco", None)
        echo = deco and deco.get("echo")
        if not echo:
            return
        try:
            off = self.effects["card_echo"]
            target = _container_of(w)
            if off <= 0 or not target.winfo_ismapped():
                echo.place_forget()
                return
            echo.place(x=target.winfo_x() + off, y=target.winfo_y() + off,
                       width=target.winfo_width(), height=target.winfo_height())
            echo.lower(target)
        except Exception:
            pass

    def _hide_echo(self, w: Any) -> None:
        deco = getattr(w, "_mt_deco", None)
        if deco and deco.get("echo") is not None:
            try:
                deco["echo"].place_forget()
            except Exception:
                pass

    @staticmethod
    def _drop_echo(w: Any, event: Any) -> None:
        if getattr(event, "widget", None) is not _container_of(w):
            return
        deco = getattr(w, "_mt_deco", None)
        if deco and deco.get("echo") is not None:
            try:
                deco["echo"].destroy()
            except Exception:
                pass
            deco["echo"] = None

    # ── typography & sounds ───────────────────────────────────────────────
    def _apply_body_font(self, w: Any, role: str | None) -> None:
        if role in ("title", "nav") or getattr(w, "_mt_keep_font", False):
            return
        if not isinstance(w, (ctk.CTkLabel, ctk.CTkButton, ctk.CTkEntry, ctk.CTkTextbox, ctk.CTkOptionMenu,
                              ctk.CTkCheckBox, ctk.CTkSwitch, ctk.CTkComboBox, ctk.CTkRadioButton)):
            return
        family = self.effects["body_font"]
        try:
            if family:
                if not hasattr(w, "_mt_orig_font"):
                    w._mt_orig_font = w.cget("font")
                orig = w._mt_orig_font
                if isinstance(orig, ctk.CTkFont):
                    size, weight = orig.cget("size"), orig.cget("weight")
                elif isinstance(orig, (tuple, list)) and len(orig) >= 2:
                    size, weight = int(orig[1]), ("bold" if "bold" in orig[2:] else "normal")
                else:
                    size, weight = 13, "normal"
                w.configure(font=self.font(int(abs(size)), weight, family=family))
            elif hasattr(w, "_mt_orig_font"):
                w.configure(font=w._mt_orig_font)
                del w._mt_orig_font
        except Exception:
            pass

    def _bind_click_sound(self, w: Any, role: str | None) -> None:
        if role == "nav" or getattr(w, "_mt_sound_bound", False):
            return
        w._mt_sound_bound = True
        try:
            w.bind("<Button-1>", lambda _e: self.play("click"), add=True)
        except Exception:
            pass

    def _nav_indicator(self, w: Any, active: bool) -> None:
        width = self.effects["nav_indicator"]
        bar = getattr(w, "_mt_nav_bar", None)
        if active and width:
            if bar is None:
                bar = tk.Frame(w, bd=0, highlightthickness=0)
                bar._mt_role = "skip"
                w._mt_nav_bar = bar
            bar.configure(bg=self.palette["accent"], width=width)
            bar.place(x=0, rely=0.18, relheight=0.64)
            bar.lift()
        elif bar is not None:
            bar.place_forget()

    def _style_button(self, w: Any, role: str | None, shape: dict[str, Any]) -> None:
        p = self.palette
        common = {"text_color_disabled": p["text_muted"], **shape}
        if role == "nav":
            self._nav_indicator(w, bool(getattr(w, "_mt_active", False)))
            if getattr(w, "_mt_active", False):
                _safe_configure(w, fg_color=p["nav_active_bg"], hover_color=p["nav_active_bg"],
                                text_color=p["nav_active_text"], border_color=p["nav_active_border"],
                                border_width=p["nav_active_border_width"], **common)
            else:
                text = p["danger"] if getattr(w, "_mt_error", False) else p["text_muted"]
                _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"],
                                text_color=text, border_width=0, **common)
        elif role == "secondary":
            _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"], text_color=p["text"],
                            border_color=p["border_strong"], border_width=1, **common)
        elif role == "ghost":
            _safe_configure(w, fg_color="transparent", hover_color=p["card_hover"],
                            text_color=p["text_muted"], border_width=0, **common)
        elif role == "danger":
            _safe_configure(w, fg_color=p["danger"], hover_color=p["danger_hover"], text_color="#ffffff",
                            border_width=0, **common)
        else:
            _safe_configure(w, fg_color=p["accent"], hover_color=p["accent_hover"],
                            text_color=p["accent_text"], border_width=0, **common)

    def _style_tk(self, w: Any, role: str | None, parent_bg: str) -> str:
        """Plain tkinter widgets a plugin might use (Listbox for long lists, Canvas, Text)."""
        p = self.palette
        try:
            if isinstance(w, tk.Toplevel) or isinstance(w, tk.Canvas):
                if role is None:
                    return parent_bg  # overlays / custom canvases: hands off unless tagged
                color = parent_bg if role == "transparent" else p[FRAME_ROLE_COLORS.get(role, "surface")]
                w.configure(bg=color, highlightthickness=0)
                return color
            if isinstance(w, tk.Frame):
                color = p[FRAME_ROLE_COLORS[role]] if role in FRAME_ROLE_COLORS else parent_bg
                w.configure(bg=color)
                return color
            if isinstance(w, tk.Listbox):
                w.configure(bg=p["entry_bg"], fg=p["text"], selectbackground=p["selected"],
                            selectforeground="#ffffff", highlightthickness=0, borderwidth=0)
            elif isinstance(w, tk.Text):
                w.configure(bg=p["entry_bg"], fg=p["text"], insertbackground=p["text"],
                            selectbackground=p["selected"], highlightthickness=0, borderwidth=0)
            elif isinstance(w, tk.Label):
                w.configure(bg=parent_bg, fg=p[LABEL_ROLE_COLORS.get(role, "text")])
        except Exception:
            pass
        return parent_bg


class ThemeHandle:
    """Per-plugin view of the theme engine (subscriptions are cleaned up on unload)."""

    def __init__(self, manager: ThemeManager, owner: str):
        self._m = manager
        self._owner = owner

    @property
    def name(self) -> str:
        return self._m.name

    @property
    def palette(self) -> dict[str, Any]:
        return dict(self._m.palette)

    def tag(self, widget: Any, role: str | None, **options: Any) -> Any:
        return self._m.tag(widget, role, **options)

    def apply(self, widget: Any) -> None:
        self._m.apply(widget)

    def play(self, event: str) -> None:
        """Play a theme UI sound: click, nav, open, success, error, notify, boot (if the theme has one)."""
        self._m.play(event)

    @property
    def effects(self) -> dict[str, Any]:
        return dict(self._m.effects)

    def font(self, size: int = 13, weight: str = "normal", family: str | None = None,
             mono: bool = False) -> ctk.CTkFont:
        return self._m.font(size, weight, family, mono)

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        return self._m.subscribe(fn, owner=self._owner)


# ════════════════════════════════════════════════════════════════════════════
#  Drag and drop
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class DropPayload:
    files: list[Path] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    text: str = ""
    raw: str = ""

    @property
    def kind(self) -> str:
        if self.files:
            return "files"
        if self.urls:
            return "urls"
        return "text"

    def summary(self) -> str:
        parts = []
        if self.files:
            parts.append(f"{len(self.files)} file(s)")
        if self.urls:
            parts.append(f"{len(self.urls)} link(s)")
        if not parts and self.text:
            parts.append(f"{len(self.text)} chars of text")
        return ", ".join(parts) or "nothing"


def _read_url_shortcut(path: Path) -> str | None:
    """Browsers sometimes hand over a .url Internet Shortcut instead of plain text."""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().lower().startswith("url="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_drop_data(widget: Any, data: str) -> DropPayload:
    data = data or ""
    try:
        items = list(widget.tk.splitlist(data)) if data else []
    except Exception:
        items = []

    def _exists(item: str) -> bool:
        try:
            return bool(item) and Path(item).exists()
        except (OSError, ValueError):
            return False

    if items and len(items) <= 20000 and all(_exists(i) for i in items):
        files: list[Path] = []
        urls: list[str] = []
        for item in items:
            p = Path(item)
            if p.suffix.lower() == ".url":
                url = _read_url_shortcut(p)
                if url:
                    urls.append(url)
                    continue
            files.append(p)
        return DropPayload(files=files, urls=_dedupe(urls), text="", raw=data)

    text = data.strip()
    urls = [u.rstrip(").,;]'\"") for u in URL_RE.findall(text)]
    return DropPayload(files=[], urls=_dedupe(urls), text=text, raw=data)


def _iter_widget_tree(widget: Any) -> Iterator[Any]:
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        yield from _iter_widget_tree(child)


def _is_descendant(widget: Any, ancestor: Any) -> bool:
    w = getattr(widget, "master", None)
    while w is not None:
        if w is ancestor:
            return True
        w = getattr(w, "master", None)
    return False


# ════════════════════════════════════════════════════════════════════════════
#  Plugin system
# ════════════════════════════════════════════════════════════════════════════

class PluginError(Exception):
    """Contract violation; shown to the user without a traceback."""


@dataclass
class PluginRecord:
    key: str
    path: Path
    is_package: bool
    module_name: str
    title: str
    description: str = ""
    icon: str = "◆"
    order: int = 100
    enabled: bool = True
    module: Any = None
    instance: Any = None
    ctx: Any = None
    error: str | None = None

    @property
    def display_path(self) -> str:
        return f"{self.path.parent.name}/" if self.is_package else self.path.name

    @property
    def status(self) -> str:
        if not self.enabled:
            return "Disabled"
        return "Error" if self.error else "Loaded"


def _prettify(key: str) -> str:
    return re.sub(r"[_\-]+", " ", key).strip().title() or key


def discover_plugins(plugins_dir: Path) -> list[tuple[str, Path, bool]]:
    found: list[tuple[str, Path, bool]] = []
    if not plugins_dir.is_dir():
        return found
    for entry in sorted(plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_file() and entry.suffix.lower() == ".py":
            found.append((entry.stem, entry, False))
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.append((entry.name, entry / "__init__.py", True))
    return found


def import_plugin_module(rec: PluginRecord) -> types.ModuleType:
    if PLUGIN_NAMESPACE not in sys.modules:
        namespace = types.ModuleType(PLUGIN_NAMESPACE)
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules[PLUGIN_NAMESPACE] = namespace
    spec = importlib.util.spec_from_file_location(
        rec.module_name,
        rec.path,
        submodule_search_locations=[str(rec.path.parent)] if rec.is_package else None,
    )
    if spec is None or spec.loader is None:
        raise PluginError(f"Python could not create an import spec for {rec.path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[rec.module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(rec.module_name, None)
        raise
    return module


def instantiate_plugin(cls: type, ctx: "PluginContext") -> Any:
    try:
        params = [
            p for p in inspect.signature(cls).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        ]
    except (TypeError, ValueError):
        params = []
    if params:
        return cls(ctx)
    instance = cls()
    instance.ctx = ctx
    return instance


class PluginContext:
    """Everything a plugin may touch. Keeps plugins decoupled from shell internals."""

    def __init__(self, app: "App", plugin_id: str, plugin_path: Path):
        self._app = app
        self.plugin_id = plugin_id
        self.plugin_path = plugin_path
        self.log = app.log.getChild(f"plugin.{plugin_id}")
        self.theme = ThemeHandle(app.theme, plugin_id)

    @property
    def app(self) -> "App":
        return self._app

    @property
    def paths(self) -> AppPaths:
        return self._app.paths

    @property
    def dnd_available(self) -> bool:
        return self._app.dnd_available

    @property
    def data_dir(self) -> Path:
        d = self._app.paths.plugin_data_dir / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._app.settings.get(f"plugin:{self.plugin_id}", key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self._app.settings.set(f"plugin:{self.plugin_id}", key, value)

    def bin_path(self, name: str, required: bool = False) -> Path | None:
        return find_binary(self._app.paths.bin_dir, name, required)

    @staticmethod
    def subprocess_kwargs() -> dict[str, Any]:
        return hidden_subprocess_kwargs()

    def register_drop_target(self, widget: Any, callback: Callable[[DropPayload], None],
                             accept: tuple[str, ...] = ("files", "text")) -> bool:
        return self._app.register_drop_target(widget, callback, accept, owner=self.plugin_id)

    def call_in_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._app.call_in_ui(fn, *args, **kwargs)

    def run_in_thread(self, fn: Callable[..., Any], *args: Any,
                      on_done: Callable[[Any], None] | None = None,
                      on_error: Callable[[BaseException], None] | None = None,
                      **kwargs: Any) -> threading.Thread:
        return self._app.run_in_thread(fn, *args, on_done=on_done, on_error=on_error,
                                       name=f"{self.plugin_id}-worker", **kwargs)

    def copy_to_clipboard(self, text: str) -> None:
        self._app.copy_to_clipboard(text)

    def register_file_type(self, extension: str, label: str, handler: Callable[[Path], Any]) -> None:
        """Let Settings ▸ Install… (and drops on the Settings page) accept a file type for this plugin,
        e.g. ctx.register_file_type(".mtchar", "Companion characters", self.import_character)."""
        self._app.register_file_type(extension, label, handler, owner=self.plugin_id)

    def set_status(self, text: str, level: str = "info") -> None:
        self._app.call_in_ui(self._app.set_status, text, level)

    def open_path(self, path: Path | str) -> None:
        self._app.open_path(path)

    def notify(self, title: str, body: str = "", subtitle: str = "", image: Any = None, note: str = "",
               on_click: Callable[[], Any] | None = None, duration: float = 6.0, icon: str = "") -> None:
        """Heads-up pop-up in the corner of the screen (styled by the active theme). Thread-safe.
        image: a PIL image, an image file path or raw image bytes — shown as a round avatar."""
        self._app.call_in_ui(self._app.notify, title, body, subtitle=subtitle, image=image, note=note,
                             on_click=on_click, duration=duration, icon=icon, source=self.plugin_id)


# ════════════════════════════════════════════════════════════════════════════
#  Main window
# ════════════════════════════════════════════════════════════════════════════

TOAST_KEY_COLOR = "#fe01fe"  # made transparent on Windows so layered toasts can have see-through gaps


def work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor minus the taskbar."""
    if IS_WINDOWS:
        try:
            from ctypes import wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def round_avatar(image: Any, size: int, ring: str | None = None) -> Any:
    """PIL image / path / bytes → anti-aliased circular RGBA image (or None)."""
    try:
        from PIL import Image, ImageDraw, ImageOps
        if isinstance(image, (bytes, bytearray)):
            img = Image.open(io.BytesIO(image))
        elif isinstance(image, (str, Path)):
            img = Image.open(image)
        else:
            img = image
        img = ImageOps.fit(img.convert("RGBA"), (size * 4, size * 4), Image.LANCZOS)
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, img.size[0] - 1, img.size[1] - 1), fill=255)
        img.putalpha(mask)
        if ring:
            ImageDraw.Draw(img).ellipse((2, 2, img.size[0] - 3, img.size[1] - 3), outline=ring, width=8)
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


class Toast:
    """One heads-up pop-up: a borderless always-on-top window with the theme's layered card look."""

    def __init__(self, manager: "ToastManager", title: str, body: str, subtitle: str, image: Any,
                 note: str, on_click: Callable[[], Any] | None, duration: float, icon: str):
        self.m = manager
        app = manager.app
        theme = app.theme
        p, e = theme.palette, theme.effects
        scale = theme.scale()
        px = lambda v: max(1, round(v * scale))  # noqa: E731
        self.on_click = on_click
        self.duration = max(2.0, float(duration or 6.0))
        self.remaining = self.duration
        self.paused = False
        self.closed = False
        self._after: str | None = None
        self.x = self.y = 0
        self.target: tuple[int, int] | None = None
        self.off = px(e["card_echo"]) if e["card_echo"] else 0
        self.width = px(372)
        win = self.win = tk.Toplevel(app)
        win._mt_role = "skip"
        win.withdraw()
        win.overrideredirect(True)
        key = TOAST_KEY_COLOR if IS_WINDOWS else p["bg"]
        win.configure(bg=key)
        try:
            win.attributes("-topmost", True)
            if IS_WINDOWS:
                win.attributes("-transparentcolor", TOAST_KEY_COLOR)
                win.attributes("-toolwindow", True)
            win.attributes("-alpha", 0.0)
        except Exception:
            pass
        font = p["font"]
        title_font = p.get("title_font") or font
        body_family = e["body_font"] or font
        border = p.get("border_strong", p["accent"]) if e["corner_brackets"] else p["border"]

        if self.off:
            echo_color = p.get(e["card_echo_color"], p.get("shadow", p["border"]))
            fill = echo_color if e["card_echo_color"] == "shadow" else key
            self.echo = tk.Frame(win, bg=fill, bd=0, highlightthickness=px(1),
                                 highlightbackground=echo_color, highlightcolor=echo_color)
        else:
            self.echo = None
        card = self.card = tk.Frame(win, bg=p["card"], bd=0, highlightthickness=px(1),
                                    highlightbackground=border, highlightcolor=border, cursor="hand2")
        tk.Frame(card, bg=p["accent"], width=px(3), bd=0).pack(side="left", fill="y")
        inner = tk.Frame(card, bg=p["card"], bd=0)
        inner.pack(side="left", fill="both", expand=True, padx=(px(12), px(10)), pady=px(11))
        inner.grid_columnconfigure(1, weight=1)

        self._photo = None
        avatar = round_avatar(image, px(46), ring=p["accent"]) if image is not None else None
        if avatar is not None:
            try:
                from PIL import ImageTk
                self._photo = ImageTk.PhotoImage(avatar, master=win)
            except Exception:
                self._photo = None
        if self._photo is not None:
            pic = tk.Label(inner, image=self._photo, bg=p["card"], bd=0)
        else:
            letter = (icon or (title.strip()[:1] or "•")).upper()
            pic = tk.Label(inner, text=letter, bg=p["selected"], fg="#ffffff", width=3, height=1,
                           font=(title_font, -px(18), "bold"))
        pic.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, px(12)))

        head = tk.Frame(inner, bg=p["card"], bd=0)
        head.grid(row=0, column=1, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        tk.Label(head, text=title[:80], bg=p["card"], fg=p["title"], anchor="w",
                 font=(title_font, -px(15), "bold")).grid(row=0, column=0, sticky="ew")
        tk.Label(head, text="now", bg=p["card"], fg=p["text_muted"],
                 font=(body_family, -px(11))).grid(row=0, column=1, sticky="e", padx=(px(8), px(4)))
        close = tk.Label(head, text="✕", bg=p["card"], fg=p["text_muted"], cursor="hand2",
                         font=(font, -px(13)))
        close.grid(row=0, column=2, sticky="e")
        close.bind("<Button-1>", lambda _e: (self.close(), "break")[1])
        wrap = self.width - px(46) - px(12) - px(28)
        row = 1
        if subtitle:
            tk.Label(inner, text=subtitle[:120], bg=p["card"], fg=p["accent"], anchor="w", justify="left",
                     font=(body_family, -px(11), "bold"), wraplength=wrap).grid(row=row, column=1, sticky="ew")
            row += 1
        if body:
            text = body if len(body) <= 400 else body[:399] + "…"
            tk.Label(inner, text=text, bg=p["card"], fg=p["text"], anchor="w", justify="left",
                     font=(body_family, -px(13)), wraplength=wrap).grid(row=row, column=1, sticky="ew",
                                                                         pady=(px(3), 0))
            row += 1
        if note:
            box = tk.Frame(inner, bg=p["entry_bg"], bd=0, highlightthickness=0)
            box.grid(row=row, column=1, sticky="ew", pady=(px(7), 0))
            tk.Frame(box, bg=p["accent2"], width=px(2)).pack(side="left", fill="y")
            tk.Label(box, text=f"NOTE  {note[:200]}", bg=p["entry_bg"], fg=p["accent2"], anchor="w",
                     justify="left", font=(body_family, -px(11)), wraplength=wrap - px(14)).pack(
                side="left", fill="x", padx=px(6), pady=px(3))
            row += 1
        self.bar = tk.Frame(card, bg=p["accent"], height=px(2), bd=0)

        if e["corner_brackets"]:
            c = p.get(e["bracket_color"], p["accent"])
            size = px(10)
            for rx, ry, anchor in ((0, 0, "nw"), (1, 0, "ne"), (0, 1, "sw"), (1, 1, "se")):
                tk.Frame(card, bg=c, width=size, height=px(2)).place(relx=rx, rely=ry, anchor=anchor)
                tk.Frame(card, bg=c, width=px(2), height=size).place(relx=rx, rely=ry, anchor=anchor)

        win.update_idletasks()
        self.height = card.winfo_reqheight() or px(96)
        card.place(x=0, y=0, width=self.width, height=self.height)
        if self.echo is not None:
            self.echo.place(x=self.off, y=self.off, width=self.width, height=self.height)
            self.echo.lower(card)
        self.bar.place(x=0, rely=1.0, y=-px(2), relwidth=1)
        self.full_w, self.full_h = self.width + self.off, self.height + self.off

        def bind_all(widget: Any) -> None:
            if widget is not close:
                widget.bind("<Button-1>", self._clicked, add="+")
            widget.bind("<Enter>", lambda _e: self._pause(True), add="+")
            widget.bind("<Leave>", lambda _e: self._pause(False), add="+")
            for child in widget.winfo_children():
                bind_all(child)
        bind_all(win)

    def _clicked(self, _event: Any = None) -> None:
        callback = self.on_click
        self.close()
        if callback is not None:
            try:
                callback()
            except Exception:
                self.m.app.log.exception("Toast click handler failed")

    def _pause(self, paused: bool) -> None:
        self.paused = paused

    def show_at(self, x: int, y: int, animate: bool) -> None:
        self.target = (x, y)
        if not self.win.winfo_ismapped() and not self.closed:
            start_x = x + (self.full_w + 30 if animate else 0)
            self.x, self.y = start_x, y
            self.win.geometry(f"{self.full_w}x{self.full_h}+{start_x}+{y}")
            self.win.deiconify()
            self.win.lift()
            self._step(0, animate)
            self._after = self.win.after(100, self._countdown)
        else:
            self._step(0, animate)

    def _step(self, frame: int, animate: bool) -> None:
        if self.closed or self.target is None:
            return
        tx, ty = self.target
        if not animate:
            self.x, self.y = tx, ty
        else:
            self.x += round((tx - self.x) * 0.35) or (tx - self.x)
            self.y += round((ty - self.y) * 0.35) or (ty - self.y)
        try:
            self.win.geometry(f"+{self.x}+{self.y}")
            alpha = 0.97 if not animate else min(0.97, 0.25 + frame * 0.12)
            self.win.attributes("-alpha", alpha)
        except Exception:
            return
        if (self.x, self.y) != (tx, ty) or (animate and frame < 7):
            self.win.after(16, self._step, frame + 1, animate)

    def _countdown(self) -> None:
        if self.closed:
            return
        if not self.paused:
            self.remaining -= 0.1
            try:
                self.bar.place_configure(relwidth=max(0.0, self.remaining / self.duration))
            except Exception:
                pass
        if self.remaining <= 0:
            self.close()
            return
        self._after = self.win.after(100, self._countdown)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass
        self.m.closed(self)


class ToastManager:
    MAX_VISIBLE = 4

    def __init__(self, app: "App"):
        self.app = app
        self.toasts: list[Toast] = []

    def show(self, title: str, body: str = "", subtitle: str = "", image: Any = None, note: str = "",
             on_click: Callable[[], Any] | None = None, duration: float = 6.0, icon: str = "") -> Toast | None:
        if not self.app.settings.get("core", "toasts", True):
            return None
        try:
            toast = Toast(self, title, body, subtitle, image, note, on_click, duration, icon)
        except Exception:
            self.app.log.exception("Could not show notification")
            return None
        self.toasts.insert(0, toast)
        for old in self.toasts[self.MAX_VISIBLE:]:
            old.close()
        self.layout()
        self.app.theme.play("notify")
        return toast

    def closed(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.layout()

    def layout(self) -> None:
        left, top, right, bottom = work_area(self.app)
        scale = self.app.theme.scale()
        margin, gap = round(16 * scale), round(10 * scale)
        animate = not self.app.theme.reduce_motion
        corner = self.app.settings.get("core", "toast_corner", "top-right")
        y = top + margin if corner == "top-right" else bottom - margin
        for toast in list(self.toasts):
            x = right - margin - toast.full_w
            if corner == "top-right":
                toast.show_at(x, y, animate)
                y += toast.full_h + gap
            else:
                y -= toast.full_h
                toast.show_at(x, y, animate)
                y -= gap

    def close_all(self) -> None:
        for toast in list(self.toasts):
            toast.close()


# ── in-app updates ──────────────────────────────────────────────────────────
UPDATE_REPO_DEFAULT = "4sxnly/multi-tool"
UPDATE_BRANCH_DEFAULT = "main"
# repo paths that only change by rebuilding the installer (embedded Python, libraries, engines)
INSTALLER_PATHS = ("requirements.txt", "launcher.py", "build/", ".github/workflows/build.yml", "assets/")
# Plugins that moved out of Multi-Tool. An update moves them out of the plugins folder.
RETIRED_PLUGINS = {"desktop_companion.py": "The desktop companion is now its own app, Juno."}


def git_blob_sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@dataclass
class UpdateItem:
    path: str          # path in the repo
    sha: str           # git blob sha
    dest: Path
    kind: str          # core | plugin | theme | character
    new: bool = False


@dataclass
class UpdateCheck:
    items: list[UpdateItem] = field(default_factory=list)
    installer_needed: bool = False
    installer_url: str | None = None
    installer_name: str = ""
    installer_build: int | None = None
    message: str = ""

    def summary(self) -> str:
        if not self.items and not self.installer_needed:
            return "You're up to date."
        parts = []
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        names = {"core": "app core", "plugin": "plugin file", "theme": "theme", "character": "character"}
        for kind in ("core", "plugin", "theme", "character"):
            n = counts.get(kind, 0)
            if n:
                parts.append(names[kind] if kind == "core" else f"{n} {names[kind]}{'s' if n != 1 else ''}")
        text = ("Updates ready: " + ", ".join(parts) + ".") if parts else ""
        if self.installer_needed:
            extra = ("A new full installer is available (bundled engines/libraries changed)."
                     if self.installer_url else
                     "The bundled engines/libraries changed — a new installer is still being built. "
                     "Check again in a few minutes.")
            text = (text + " " + extra).strip()
        return text


class UpdateManager:
    """Keeps plugins, themes and the app core in sync with the GitHub repo, without rebuilding
    the installer. Only files that changed are downloaded (compared by git blob hash)."""

    # the repo also publishes Juno; only releases tagged build-N with a MultiTool installer are ours
    RELEASE_TAG_RE = re.compile(r"^build-(\d+)$")
    INSTALLER_PREFIX = "multitool"
    INSTALLER_PATHS = INSTALLER_PATHS

    def __init__(self, paths: AppPaths, settings: "SettingsStore", log: logging.Logger):
        self.paths = paths
        self.settings = settings
        self.log = log
        self.info: dict[str, Any] = {}
        try:
            self.info = json.loads((paths.app_dir / "build_info.json").read_text(encoding="utf-8-sig"))
        except Exception:
            self.info = {}
        self.repo = (settings.get("core", "update_repo", "") or self.info.get("repo") or UPDATE_REPO_DEFAULT)
        self.branch = self.info.get("branch") or UPDATE_BRANCH_DEFAULT
        self.api = os.environ.get("MT_GH_API", "https://api.github.com").rstrip("/")
        self.raw = os.environ.get("MT_GH_RAW", "https://raw.githubusercontent.com").rstrip("/")
        self.state_file = paths.data_dir / "update_state.json"
        self.last: UpdateCheck | None = None
        self.busy = False

    @property
    def enabled(self) -> bool:
        return IS_FROZEN or bool(os.environ.get("MT_UPDATE_DEV"))

    @property
    def build(self) -> int:
        try:
            return int(self.info.get("build") or 0)
        except Exception:
            return 0

    def _get(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        import urllib.request
        request = urllib.request.Request(url, headers={"User-Agent": f"{APP_ID}/{APP_VERSION}", "Accept": accept})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _map(self, path: str) -> tuple[Path, str] | None:
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts) or "\\" in path or ":" in path:
            return None
        if path == "main.py":
            return self.paths.app_dir / "core" / "main.py", "core"
        if parts[0] == "plugins" and len(parts) >= 2 and "__pycache__" not in parts:
            return self.paths.plugins_dir.joinpath(*parts[1:]), "plugin"
        if parts[:2] == ["extras", "themes"] and len(parts) == 3 and path.endswith(".mttheme"):
            return self.paths.themes_dir / parts[2], "theme"
        return None

    @staticmethod
    def _same(dest: Path, sha: str) -> bool:
        try:
            data = dest.read_bytes()
        except OSError:
            return False
        if git_blob_sha(data) == sha:
            return True
        return git_blob_sha(data.replace(b"\r\n", b"\n")) == sha  # a copy that got CRLF line endings

    def check(self) -> UpdateCheck:
        tree = json.loads(self._get(f"{self.api}/repos/{self.repo}/git/trees/{self.branch}?recursive=1"))
        blobs = {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"}
        if not blobs:
            raise RuntimeError("the repository listing came back empty")
        shipped: dict[str, str] = self.info.get("files") or {}
        known = set(shipped) | set(self._state().get("known", []))
        result = UpdateCheck()
        for path, sha in sorted(blobs.items()):
            mapped = self._map(path)
            if mapped is None:
                continue
            dest, kind = mapped
            if dest.exists():
                if self._same(dest, sha):
                    continue
                result.items.append(UpdateItem(path, sha, dest, kind))
            elif path not in known:  # brand-new file in the repo (removed/unselected ones stay removed)
                result.items.append(UpdateItem(path, sha, dest, kind, new=True))
        if shipped:
            for path, sha in blobs.items():
                if path.startswith(self.INSTALLER_PATHS) and shipped.get(path) != sha:
                    result.installer_needed = True
                    self.log.info("Installer-level change: %s", path)
                    break
        if result.installer_needed:
            try:
                number, asset = self._latest_installer()
                if asset and number > self.build:
                    result.installer_url = asset["browser_download_url"]
                    result.installer_name = asset["name"]
                    result.installer_build = number
            except Exception as exc:
                self.log.warning("Could not read the latest release: %s", exc)
        result.message = result.summary()
        self.last = result
        return result

    def _latest_installer(self) -> tuple[int, dict[str, Any] | None]:
        releases = json.loads(self._get(f"{self.api}/repos/{self.repo}/releases?per_page=30"))
        best: tuple[int, dict[str, Any] | None] = (0, None)
        for release in releases if isinstance(releases, list) else []:
            match = self.RELEASE_TAG_RE.match(release.get("tag_name", ""))
            if not match or release.get("draft"):
                continue
            asset = next((a for a in release.get("assets", []) if a.get("name", "").lower().endswith(".exe")
                          and a.get("name", "").lower().startswith(self.INSTALLER_PREFIX)), None)
            if asset and int(match.group(1)) > best[0]:
                best = (int(match.group(1)), asset)
        return best

    def apply(self, check: UpdateCheck, progress: Callable[[str], None] = lambda _t: None) -> dict[str, int]:
        import urllib.parse
        done: dict[str, int] = {}
        for i, item in enumerate(check.items, start=1):
            progress(f"Downloading {item.path} ({i}/{len(check.items)})")
            url = f"{self.raw}/{self.repo}/{self.branch}/{urllib.parse.quote(item.path)}"
            data = b""
            for _attempt in range(2):
                data = self._get(url, accept="*/*")
                if git_blob_sha(data) == item.sha:
                    break
            else:
                raise RuntimeError(f"{item.path} didn't match the expected file — try again in a minute")
            item.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = item.dest.with_name(item.dest.name + ".mtupdate")
            tmp.write_bytes(data)
            os.replace(tmp, item.dest)
            done[item.kind] = done.get(item.kind, 0) + 1
            self.log.info("Updated %s -> %s", item.path, item.dest)
        state = self._state()
        known = set(state.get("known", [])) | {item.path for item in check.items}
        state["known"] = sorted(known)
        state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)
        check.items = []
        return done

    def download_installer(self, check: UpdateCheck, progress: Callable[[str], None]) -> Path:
        import urllib.request
        assert check.installer_url
        folder = self.paths.data_dir / "cache" / "installer"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (check.installer_name or "MultiTool-Setup.exe")
        request = urllib.request.Request(check.installer_url, headers={"User-Agent": f"{APP_ID}/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=60) as response, open(target.with_suffix(".part"), "wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    progress(f"Downloading installer… {got * 100 // total}%")
        os.replace(target.with_suffix(".part"), target)
        return target


class TitleCanvas(tk.Canvas):
    """Page title drawn on a canvas so a theme can give it an offset 'chromatic' shadow and a pixel-exact
    size. Accepts configure(text=..., text_color=...) like a CTkLabel."""

    def __init__(self, master: Any, theme: "ThemeManager"):
        super().__init__(master, height=38, bd=0, highlightthickness=0)
        self._theme = theme
        self._text = ""
        self._color = "#ffffff"
        self._shadow_override: str | None = None
        tk.Misc.bind(self, "<Configure>", lambda _e: self._draw(), "+")

    def configure(self, cnf: Any = None, **kw: Any) -> Any:
        changed = False
        if "text" in kw:
            self._text = str(kw.pop("text"))
            changed = True
        if "text_color" in kw:
            self._color = kw.pop("text_color")
            changed = True
        if "shadow" in kw:
            self._shadow_override = kw.pop("shadow")
            changed = True
        result = super().configure(cnf, **kw) if (cnf or kw) else None
        if changed or "bg" in kw or "background" in kw:
            self._draw()
        return result

    config = configure

    def cget(self, key: str) -> Any:
        if key == "text":
            return self._text
        if key == "text_color":
            return self._color
        return super().cget(key)

    def _draw(self) -> None:
        try:
            e, p = self._theme.effects, self._theme.palette
            scale = self._theme.scale()
            size = e["title_size"]
            font = (p.get("title_font") or p["font"], -round(size * scale), "bold")
            height = round(size * 1.5 * scale)
            if int(float(super().cget("height"))) != height:
                super().configure(height=height)
            self.delete("all")
            y = height // 2
            shadow = self._shadow_override if self._shadow_override is not None else e["title_shadow"]
            if shadow:
                color = p.get(shadow, shadow) if not str(shadow).startswith("#") else shadow
                off = max(2, round(3 * scale))
                self.create_text(off, y + off // 2, text=self._text, anchor="w", font=font, fill=color)
            self.create_text(0, y, text=self._text, anchor="w", font=font, fill=self._color)
        except Exception:
            pass


class Telemetry:
    """Real CPU / memory figures for the status bar (Windows; elsewhere returns None)."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None

    @staticmethod
    def _ft(ft: Any) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def cpu(self) -> int | None:
        if not IS_WINDOWS:
            return None
        try:
            from ctypes import wintypes
            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                         ctypes.byref(user)):
                return None
            i, total = self._ft(idle), self._ft(kernel) + self._ft(user)
            last, self._last = self._last, (i, total)
            if last is None or total == last[1]:
                return None
            return max(0, min(100, round(100 * (1 - (i - last[0]) / (total - last[1])))))
        except Exception:
            return None

    @staticmethod
    def memory() -> int | None:
        if not IS_WINDOWS:
            return None
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.dwMemoryLoad)
        except Exception:
            return None


DEFAULT_BOOT_LINES = [
    "MULTI-TOOL BIOS v{version}",
    "> mounting /plugins ........ {plugins} modules",
    "> linking bundled engines ... ok",
    "> theme driver .............. {theme}",
    "> handshake ................. ok",
    "SYSTEM READY",
]


class App(ctk.CTk, _DnDBase):
    def __init__(self, paths: AppPaths, log: logging.Logger):
        super().__init__()
        self.paths = paths
        self.log = log
        self.dnd_available = False
        self.dnd_version: str | None = None
        self.dnd_error: str | None = None
        self._init_dnd()

        self.settings = SettingsStore(paths.settings_file, log)
        self.theme = ThemeManager(self, self.settings, paths.themes_dir, log)
        self.theme.subscribe(self._notify_plugins_theme, owner="core")
        self.theme.subscribe(lambda _p: self._apply_shell_effects(), owner="core")
        self._file_types: dict[str, tuple[str, str, Callable[[Path], Any]]] = {}
        self._raw_title = ""
        self._default_tagline = "Desktop Utility Belt"
        self._glitch_after: str | None = None
        self._decrypt_after: str | None = None
        self._sweep_after: str | None = None
        self._ticker_after: str | None = None
        self._banner_image: Any = None
        self._status_text = "Ready"
        self._cursor_on = True
        self._tick_n = 0
        self._telemetry = Telemetry()
        self._boot_overlay: Any = None
        self._booting = False
        self.toasts = ToastManager(self)
        self.updater = UpdateManager(paths, self.settings, log)

        self.records: list[PluginRecord] = []
        self._pages: dict[str, Any] = {}
        self._nav_buttons: dict[str, Any] = {}
        self._active_key: str | None = None
        self._ui_queue: "queue.Queue[tuple[Callable[..., Any], tuple, dict]]" = queue.Queue()
        self._status_after: str | None = None
        self._closing = False

        self.title(APP_NAME)
        self.minsize(960, 600)
        self._restore_geometry()
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_shell()
        self._load_plugins()
        self._rebuild_nav()
        self.theme.apply()
        self._apply_shell_effects()
        self.select(self._initial_page())

        self.bind("<Control-R>", lambda _e: self.reload_plugins())  # Ctrl+Shift+R
        self.after(20, self._pump_ui_queue)
        self.after(500, self._tick)
        self.after(60, self._maybe_boot)
        self.after(25_000, self._auto_update_check)
        loaded = sum(1 for r in self.records if r.instance is not None)
        failed = sum(1 for r in self.records if r.error)
        self.set_status(f"{loaded} plugin(s) loaded" + (f", {failed} failed" if failed else ""),
                        "warning" if failed else "info")

    # ── setup ─────────────────────────────────────────────────────────────
    def _init_dnd(self) -> None:
        if TkinterDnD is None:
            self.dnd_error = f"tkinterdnd2 could not be imported: {_DND_IMPORT_ERROR}"
            self.log.warning(self.dnd_error)
            return
        try:
            self.dnd_version = str(TkinterDnD._require(self))
            self.dnd_available = True
            self.log.info("tkdnd %s loaded", self.dnd_version)
        except Exception as exc:
            # Typical cause in a frozen build: tkdnd binaries not collected by PyInstaller
            self.dnd_error = f"tkdnd library failed to load: {exc}"
            self.log.warning(self.dnd_error)

    def _set_icon(self) -> None:
        for base in (self.paths.resource_dir, self.paths.app_dir):
            ico = base / "assets" / "icon.ico"
            if ico.is_file() and IS_WINDOWS:
                try:
                    self.iconbitmap(str(ico))
                    return
                except Exception:
                    self.log.debug("iconbitmap failed for %s", ico)

    def _screen_bounds(self) -> tuple[int, int, int, int]:
        if IS_WINDOWS:
            try:
                gsm = ctypes.windll.user32.GetSystemMetrics
                return gsm(76), gsm(77), gsm(78), gsm(79)  # virtual desktop (all monitors)
            except Exception:
                pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _restore_geometry(self) -> None:
        geometry = self.settings.get("core", "geometry", "") or ""
        match = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry)
        if match:
            w, h, x, y = (int(g) for g in match.groups())
            w, h = max(w, 960), max(h, 600)
            vx, vy, vw, vh = self._screen_bounds()
            if vx - 50 <= x <= vx + vw - 150 and vy - 10 <= y <= vy + vh - 100:
                self.geometry(f"{w}x{h}+{x}+{y}")
            else:
                self.geometry(f"{w}x{h}")
        else:
            self.geometry("1200x760")
        if self.settings.get("core", "maximized", False) and IS_WINDOWS:
            self.after(60, lambda: self.state("zoomed"))

    def _build_shell(self) -> None:
        tag = self.theme.tag
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._dividers: list[Any] = []
        self._theme_menus: list[Any] = []

        # ── sidebar ──
        self._sidebar = tag(ctk.CTkFrame(self, width=236, corner_radius=0), "sidebar", keep_shape=True)
        self._sidebar.grid(row=0, column=0, sticky="nsw")
        self._sidebar.grid_propagate(False)
        self._sidebar.grid_columnconfigure(0, weight=1)
        self._sidebar.grid_rowconfigure(2, weight=1)

        self._brand_title = tag(ctk.CTkLabel(self._sidebar, text=APP_NAME, anchor="w"), "title", font_size=21)
        self._brand_title.grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 0))
        self._brand_banner = tag(ctk.CTkLabel(self._sidebar, text=""), "transparent")
        self._brand_sub = tag(ctk.CTkLabel(self._sidebar, text="Desktop Utility Belt", anchor="w"), "muted")
        self._brand_sub.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))

        self._nav_frame = tag(ctk.CTkScrollableFrame(self._sidebar, corner_radius=0), "transparent")
        self._nav_frame.grid(row=2, column=0, sticky="nsew", padx=(8, 4))
        self._nav_frame.grid_columnconfigure(0, weight=1)

        bottom = tag(ctk.CTkFrame(self._sidebar, fg_color="transparent"), "transparent")
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 14))
        bottom.grid_columnconfigure(0, weight=1)
        divider = tag(ctk.CTkFrame(bottom, height=1, corner_radius=0, border_width=0), "skip")
        divider.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._dividers.append(divider)
        tag(ctk.CTkLabel(bottom, text="THEME", anchor="w", height=16,
                         font=self.theme.font(11, "bold")), "muted").grid(row=1, column=0, sticky="ew", padx=4)
        self._theme_var = ctk.StringVar(value=self.theme.name)
        menu = ctk.CTkOptionMenu(bottom, values=self.theme.names(), variable=self._theme_var,
                                 command=self._on_theme_selected, dynamic_resizing=False)
        menu.grid(row=2, column=0, sticky="ew", pady=(2, 8))
        self._theme_menus.append(menu)

        # optional coloured stripe between sidebar and content (theme effect)
        self._stripe = tag(ctk.CTkFrame(self, width=0, corner_radius=0, border_width=0), "skip")
        self._make_nav_button(bottom, SETTINGS_KEY, "⚙", "Settings").grid(row=3, column=0, sticky="ew")

        # ── main area ──
        main = tag(ctk.CTkFrame(self, corner_radius=0), "surface", keep_shape=True)
        main.grid(row=0, column=2, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        header = tag(ctk.CTkFrame(main, fg_color="transparent"), "transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(18, 10))
        header.grid_columnconfigure(0, weight=1)
        self._kicker = tag(ctk.CTkLabel(header, text="", anchor="w", height=16,
                                        font=self.theme.font(11, "bold", mono=True)), "accent", keep_font=True)
        self._page_title = tag(TitleCanvas(header, self.theme), "transparent")
        self._page_title.grid(row=1, column=0, sticky="ew")
        self._page_desc = tag(ctk.CTkLabel(header, text="", anchor="w", justify="left"), "muted")
        self._page_desc.grid(row=2, column=0, sticky="ew")
        self._ticker = tag(ctk.CTkLabel(header, text="", anchor="e", justify="right", height=16,
                                        font=self.theme.font(10, mono=True)), "muted", keep_font=True)
        header.bind("<Configure>", lambda e: self._page_desc.configure(wraplength=max(240, e.width - 20)))

        divider = tag(ctk.CTkFrame(main, height=1, corner_radius=0, border_width=0), "skip")
        divider.grid(row=1, column=0, sticky="ew", padx=26)
        self._dividers.append(divider)

        self.viewport = tag(ctk.CTkFrame(main, fg_color="transparent", corner_radius=0), "transparent")
        self.viewport.grid(row=2, column=0, sticky="nsew", padx=26, pady=16)
        self.viewport.grid_columnconfigure(0, weight=1)
        self.viewport.grid_rowconfigure(0, weight=1)
        self._sweep = tk.Frame(self.viewport, height=2, bd=0, highlightthickness=0)
        self._sweep._mt_role = "skip"

        status = tag(ctk.CTkFrame(main, height=30, corner_radius=0), "statusbar", keep_shape=True)
        status.grid(row=3, column=0, sticky="ew")
        status.grid_columnconfigure(0, weight=1)
        self._status_label = tag(ctk.CTkLabel(status, text="Ready", anchor="w", height=26), "muted")
        self._status_label.grid(row=0, column=0, sticky="ew", padx=14)
        self._telemetry_label = tag(ctk.CTkLabel(status, text="", anchor="e", height=26,
                                                 font=self.theme.font(11, mono=True)), "muted", keep_font=True)
        self._clock_label = tag(ctk.CTkLabel(status, text="", anchor="e", height=26,
                                             font=self.theme.font(11, "bold", mono=True)), "accent", keep_font=True)
        dnd_text = "Drag & drop ready" if self.dnd_available else "Drag & drop unavailable"
        tag(ctk.CTkLabel(status, text=dnd_text, anchor="e", height=26),
            "muted" if self.dnd_available else "warning").grid(row=0, column=3, sticky="e", padx=14)

    def _make_nav_button(self, parent: Any, key: str, icon: str, title: str) -> Any:
        btn = ctk.CTkButton(parent, text=f"{icon}   {title}", anchor="w", height=36,
                            command=lambda k=key: self.select(k))
        self.theme.tag(btn, "nav", active=False, error=False, text=f"{icon}   {title}")
        self._nav_buttons[key] = btn
        self._render_nav_text(btn)
        return btn

    # ── theme effects (shell decorations a theme pack can enable) ─────────
    def _render_nav_text(self, btn: Any) -> None:
        e = self.theme.effects
        text = getattr(btn, "_mt_text", "") or ""
        if getattr(btn, "_mt_active", False) and e["nav_active_prefix"]:
            text = e["nav_active_prefix"] + text
        if e["uppercase_nav"]:
            text = text.upper()
        try:
            btn.configure(text=text, font=self.theme.font(13, family=self.theme.palette["font"]))
        except Exception:
            pass

    def _rendered_title(self) -> str:
        e = self.theme.effects
        text = self._raw_title
        if text and e["uppercase_titles"]:
            text = text.upper()
        if text and e["title_prefix"]:
            text = e["title_prefix"] + text
        return text

    def _apply_shell_effects(self) -> None:
        e, p = self.theme.effects, self.theme.palette
        # brand: banner image from the theme pack, or the app name
        image = self.theme.banner_image()
        if image is not None:
            w, h = image.size
            scale = min(204 / max(w, 1), 72 / max(h, 1), 1.0)
            self._banner_image = ctk.CTkImage(light_image=image, dark_image=image,
                                              size=(max(1, int(w * scale)), max(1, int(h * scale))))
            self._brand_banner.configure(image=self._banner_image)
            self._brand_title.grid_remove()
            self._brand_banner.grid(row=0, column=0, sticky="w", padx=14, pady=(16, 2))
        else:
            self._banner_image = None
            self._brand_banner.grid_remove()
            self._brand_title.grid()
        self._brand_sub.configure(text=e["brand_tagline"] or self._default_tagline)
        # dividers and stripe
        divider_color = p.get(e["divider_color"], p["border"])
        for divider in self._dividers:
            _safe_configure(divider, height=max(1, e["divider_height"]), fg_color=divider_color,
                            bg_color=divider_color)
        if e["sidebar_stripe"] > 0:
            color = p.get(e["sidebar_stripe_color"], p["accent"])
            _safe_configure(self._stripe, width=e["sidebar_stripe"], fg_color=color, bg_color=color)
            self._stripe.grid(row=0, column=1, sticky="ns")
        else:
            self._stripe.grid_remove()
        # titles, nav, glitch
        self._page_title.configure(text=self._rendered_title(), text_color=p["title"])
        self._update_kicker()
        motion = not self.theme.reduce_motion
        if e["hex_ticker"] and motion:
            self._ticker.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        else:
            self._ticker.grid_remove()
        self._sweep.configure(bg=p["accent"])
        if e["status_telemetry"]:
            self._telemetry_label.grid(row=0, column=1, sticky="e", padx=(0, 6))
        else:
            self._telemetry_label.grid_remove()
        if e["status_clock"]:
            self._clock_label.grid(row=0, column=2, sticky="e", padx=(6, 0))
        else:
            self._clock_label.grid_remove()
        for btn in self._nav_buttons.values():
            self._render_nav_text(btn)
        self._render_status()
        self._schedule_glitch()
        self._schedule_ticker()

    # kicker ("MODULE_03 // ONLINE"), hex ticker, decrypt reveal, scan sweep, boot, status ticks
    def _page_state(self, key: str | None) -> tuple[int, str]:
        visible = self._visible_records()
        keys = [r.key for r in visible]
        if key == SETTINGS_KEY:
            return len(keys) + 1, "CONFIG"
        rec = self._record(key) if key else None
        index = keys.index(key) + 1 if key in keys else 0
        if rec is None:
            return index, "IDLE"
        return index, "FAULT" if rec.error else ("ONLINE" if rec.instance is not None else "OFFLINE")

    def _update_kicker(self) -> None:
        fmt = self.theme.effects["kicker"]
        if not fmt or self._active_key is None:
            self._kicker.grid_remove()
            return
        index, state = self._page_state(self._active_key)
        try:
            text = fmt.format(index=index, key=self._active_key, state=state, title=self._raw_title)
        except Exception:
            text = fmt
        self._kicker.configure(text=text)
        self._kicker.grid(row=0, column=0, sticky="ew")

    def _schedule_ticker(self) -> None:
        if self._ticker_after is not None:
            try:
                self.after_cancel(self._ticker_after)
            except Exception:
                pass
            self._ticker_after = None
        if self.theme.effects["hex_ticker"] and not self.theme.reduce_motion and not self._closing:
            self._ticker_after = self.after(140, self._ticker_step)

    def _ticker_step(self) -> None:
        self._ticker_after = None
        if self._closing:
            return
        rows = []
        for _ in range(2):
            rows.append(f"0x{random.getrandbits(16):04X}  " + " ".join(f"{random.getrandbits(8):02X}" for _ in range(6)))
        self._ticker.configure(text="\n".join(rows))
        self._schedule_ticker()

    DECRYPT_CHARS = "ABCDEF0123456789#$%&@*+=<>/\\|"

    def _start_decrypt(self) -> None:
        if self._decrypt_after is not None:
            try:
                self.after_cancel(self._decrypt_after)
            except Exception:
                pass
            self._decrypt_after = None
        if self.theme.effects["title_decrypt"] and not self.theme.reduce_motion and self._rendered_title():
            self._decrypt_step(0)

    def _decrypt_step(self, frame: int) -> None:
        self._decrypt_after = None
        base = self._rendered_title()
        frames = 12
        if self._closing or frame >= frames or not base:
            self._page_title.configure(text=base, text_color=self.theme.palette["title"])
            return
        revealed = int(len(base) * frame / frames)
        text = base[:revealed] + "".join(c if c.isspace() else random.choice(self.DECRYPT_CHARS)
                                         for c in base[revealed:])
        color = self.theme.palette["accent"] if frame % 3 else self.theme.palette["accent2"]
        self._page_title.configure(text=text, text_color=color)
        self._decrypt_after = self.after(28, self._decrypt_step, frame + 1)

    def _start_sweep(self) -> None:
        if not self.theme.effects["scan_sweep"] or self.theme.reduce_motion:
            return
        if self._sweep_after is not None:
            try:
                self.after_cancel(self._sweep_after)
            except Exception:
                pass
        self._sweep_step(0)

    def _sweep_step(self, frame: int) -> None:
        self._sweep_after = None
        steps = 16
        try:
            height = self.viewport.winfo_height()
            if self._closing or frame > steps or height < 10:
                self._sweep.place_forget()
                return
            self._sweep.place(x=0, y=int((height - 2) * frame / steps), relwidth=1)
            self._sweep.lift()
            self._sweep_after = self.after(18, self._sweep_step, frame + 1)
        except Exception:
            pass

    def _maybe_boot(self) -> None:
        e, p = self.theme.effects, self.theme.palette
        if self._booting or (not e["boot_sequence"] or self.theme.reduce_motion
                or not self.settings.get("core", "boot_anim", True)):
            return
        lines = list(e["boot_lines"] or DEFAULT_BOOT_LINES)
        values = {"version": APP_VERSION, "theme": self.theme.name,
                  "plugins": sum(1 for r in self.records if r.instance is not None)}
        lines = [self._safe_format(line, values) for line in lines][:16]
        overlay = tk.Frame(self, bg=p["bg"], bd=0, highlightthickness=0, cursor="hand2")
        overlay._mt_role = "skip"
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        family = p["mono_font"]
        scale = self.theme.scale()
        text = tk.Text(overlay, bg=p["bg"], fg=p["accent"], bd=0, highlightthickness=0, wrap="none",
                       font=(family, -round(15 * scale)), cursor="hand2", insertwidth=0)
        text._mt_role = "skip"
        text.tag_configure("dim", foreground=p["text_muted"])
        text.tag_configure("hot", foreground=p["accent2"])
        text.place(relx=0.5, rely=0.45, anchor="center", relwidth=0.62, relheight=0.5)
        bar = tk.Frame(overlay, bg=p["border"], height=3, bd=0)
        bar.place(relx=0.19, rely=0.73, relwidth=0.62)
        fill = tk.Frame(bar, bg=p["accent"], height=3, bd=0)
        hint = tk.Label(overlay, text="click to skip", bg=p["bg"], fg=p["text_muted"],
                        font=(family, -round(11 * scale)))
        hint.place(relx=0.5, rely=0.78, anchor="n")
        self._boot_overlay = overlay
        self._booting = True
        for widget in (overlay, text, hint, bar):
            widget.bind("<Button-1>", lambda _e: self._end_boot())
        self.bind("<Key>", lambda _e: self._end_boot(), add="+")
        self.theme.play("boot")
        total = sum(len(line) for line in lines) or 1
        state = {"line": 0, "col": 0, "done": 0}

        def step() -> None:
            if not self._booting or self._closing:
                return
            if state["line"] >= len(lines):
                self.after(380, self._end_boot)
                return
            line = lines[state["line"]]
            chunk = line[state["col"]:state["col"] + 4]
            tag_name = "hot" if state["line"] == len(lines) - 1 else ("dim" if line.startswith(">") else "")
            text.insert("end", chunk, tag_name)
            state["col"] += len(chunk)
            state["done"] += len(chunk)
            fill.place(x=0, y=0, relheight=1, relwidth=min(1.0, state["done"] / total))
            if state["col"] >= len(line):
                text.insert("end", "\n")
                state["line"] += 1
                state["col"] = 0
                self.after(70, step)
            else:
                self.after(12, step)

        step()

    @staticmethod
    def _safe_format(line: str, values: dict[str, Any]) -> str:
        try:
            return str(line).format(**values)
        except Exception:
            return str(line)

    def _end_boot(self) -> None:
        if not self._booting:
            return
        self._booting = False
        overlay, self._boot_overlay = self._boot_overlay, None
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
        self._start_decrypt()
        self._start_sweep()

    def _tick(self) -> None:
        if self._closing:
            return
        e = self.theme.effects
        self._tick_n += 1
        try:
            if e["status_clock"]:
                self._clock_label.configure(text=time.strftime("%H:%M:%S"))
            if e["status_telemetry"] and self._tick_n % 4 == 1:
                cpu, mem = self._telemetry.cpu(), self._telemetry.memory()
                parts = []
                if cpu is not None:
                    parts.append(f"CPU {cpu:>3d}%")
                if mem is not None:
                    parts.append(f"MEM {mem:>3d}%")
                self._telemetry_label.configure(text="   ".join(parts) + ("   │" if parts else ""))
            if e["blink_cursor"] and not self.theme.reduce_motion:
                self._cursor_on = not self._cursor_on
                self._render_status()
        except Exception:
            pass
        self.after(500, self._tick)

    def _render_status(self) -> None:
        e = self.theme.effects
        text = e["status_prefix"] + self._status_text
        text = text[:300] + ("…" if len(text) > 300 else "")
        if e["blink_cursor"]:
            text += " █" if (self._cursor_on or self.theme.reduce_motion) else "  "
        try:
            self._status_label.configure(text=text)
        except Exception:
            pass

    GLITCH_CHARS = "▓▒░█#@%&/\\<>[]=+*01"

    def _schedule_glitch(self) -> None:
        if self._glitch_after is not None:
            try:
                self.after_cancel(self._glitch_after)
            except Exception:
                pass
            self._glitch_after = None
        if self.theme.effects["glitch_titles"] and not self._closing:
            self._glitch_after = self.after(random.randint(3500, 9000), self._glitch_step, 0)

    def _glitch_step(self, frame: int) -> None:
        self._glitch_after = None
        e, p = self.theme.effects, self.theme.palette
        if self._closing or not e["glitch_titles"]:
            return
        base = self._rendered_title()
        colors = e["glitch_colors"] or [p["accent2"], p["danger"]]
        if frame < 4 and base.strip():
            chars = list(base)
            for _ in range(max(1, len(chars) // 5)):
                i = random.randrange(len(chars))
                if not chars[i].isspace():
                    chars[i] = random.choice(self.GLITCH_CHARS)
            self._page_title.configure(text="".join(chars), text_color=colors[frame % len(colors)])
            self._glitch_after = self.after(70, self._glitch_step, frame + 1)
        else:
            self._page_title.configure(text=base, text_color=p["title"])
            self._schedule_glitch()

    # ── plugin lifecycle ──────────────────────────────────────────────────
    def _load_plugins(self) -> None:
        disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
        self.records = []
        used: set[str] = set()
        started = time.perf_counter()
        for raw_key, path, is_package in discover_plugins(self.paths.plugins_dir):
            base = re.sub(r"\W", "_", raw_key).lower()
            key, n = base, 2
            while key in used or key in (SETTINGS_KEY, EMPTY_KEY):
                key, n = f"{base}_{n}", n + 1
            used.add(key)
            rec = PluginRecord(key=key, path=path, is_package=is_package,
                               module_name=f"{PLUGIN_NAMESPACE}.{key}", title=_prettify(raw_key))
            if key in disabled:
                rec.enabled = False
            else:
                self._import_and_init(rec)
            self.records.append(rec)
        self.log.info("Plugin scan: %d found in %.0f ms (%s)", len(self.records),
                      (time.perf_counter() - started) * 1000, self.paths.plugins_dir)

    def _import_and_init(self, rec: PluginRecord) -> None:
        try:
            module = import_plugin_module(rec)
            rec.module = module
            cls = getattr(module, "Plugin", None)
            if cls is None or not inspect.isclass(cls):
                raise PluginError(f"{rec.display_path} must define a class named `Plugin`.")
            rec.title = str(getattr(cls, "meta_title", None) or rec.title)
            needed = int(getattr(cls, "meta_api_version", 1))
            if needed > PLUGIN_API_VERSION:
                raise PluginError(f"Needs plugin API v{needed}; this build provides v{PLUGIN_API_VERSION}.")
            if not callable(getattr(cls, "get_frame", None)):
                raise PluginError("The Plugin class must implement get_frame(self, master).")
            rec.ctx = PluginContext(self, rec.key, rec.path)
            instance = instantiate_plugin(cls, rec.ctx)
            rec.instance = instance
            rec.title = str(getattr(instance, "meta_title", None) or rec.title)
            rec.description = str(getattr(instance, "meta_description", "") or "")
            icon = str(getattr(instance, "meta_icon", "") or "").strip()
            rec.icon = icon[:2] if icon else "◆"
            try:
                rec.order = int(getattr(instance, "meta_order", 100))
            except (TypeError, ValueError):
                rec.order = 100
            self.log.info("Loaded plugin %s (%s)", rec.key, rec.title)
        except PluginError as exc:
            rec.error = str(exc)
            self.log.error("Plugin %s rejected: %s", rec.key, exc)
        except KeyboardInterrupt:
            raise
        except BaseException:  # includes SystemExit from a badly written plugin
            rec.error = traceback.format_exc()
            self.log.error("Plugin %s failed to load:\n%s", rec.key, rec.error)

    def _safe_call(self, rec: PluginRecord, method: str, *args: Any) -> bool:
        fn = getattr(rec.instance, method, None)
        if not callable(fn):
            return True
        started = time.perf_counter()
        try:
            fn(*args)
            return True
        except Exception:
            self.log.exception("%s.%s() raised", rec.key, method)
            return False
        finally:
            elapsed = time.perf_counter() - started
            if elapsed > 1.0:
                self.log.warning("%s.%s() took %.1fs (keep hooks fast; use threads)", rec.key, method, elapsed)

    def _notify_plugins_theme(self, palette: dict[str, Any]) -> None:
        for rec in self.records:
            if rec.instance is not None:
                self._safe_call(rec, "on_theme_changed", dict(palette))

    def _unload_plugins(self) -> None:
        for rec in self.records:
            if rec.instance is not None:
                if rec.key == self._active_key:
                    self._safe_call(rec, "on_hide")
                self._safe_call(rec, "shutdown")
            self.theme.unsubscribe_owner(rec.key)
            for ext in [e for e, (owner, _l, _h) in self._file_types.items() if owner == rec.key]:
                del self._file_types[ext]
        for frame in self._pages.values():
            try:
                _container_of(frame).destroy()
            except Exception:
                pass
        self._pages.clear()
        self._active_key = None
        for name in [n for n in sys.modules if n.startswith(PLUGIN_NAMESPACE + ".")]:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()
        self.records = []

    def reload_plugins(self) -> None:
        previous = self._active_key
        self.log.info("Reloading plugins")
        self._unload_plugins()
        self._load_plugins()
        self._rebuild_nav()
        keys = self._visible_keys()
        self.select(previous if previous in keys or previous == SETTINGS_KEY else self._initial_page())
        loaded = sum(1 for r in self.records if r.instance is not None)
        failed = sum(1 for r in self.records if r.error)
        self.set_status(f"Reloaded: {loaded} loaded" + (f", {failed} failed" if failed else ""),
                        "warning" if failed else "success")

    # ── navigation ────────────────────────────────────────────────────────
    def _visible_records(self) -> list[PluginRecord]:
        visible = [r for r in self.records if r.enabled and (r.instance is not None or r.error)]
        return sorted(visible, key=lambda r: (r.order, r.title.lower()))

    def _visible_keys(self) -> list[str]:
        return [r.key for r in self._visible_records()]

    def _record(self, key: str) -> PluginRecord | None:
        return next((r for r in self.records if r.key == key), None)

    def _initial_page(self) -> str:
        keys = self._visible_keys()
        last = self.settings.get("core", "last_page")
        if last in keys or last == SETTINGS_KEY:
            return last
        return keys[0] if keys else EMPTY_KEY

    def _rebuild_nav(self) -> None:
        for child in self._nav_frame.winfo_children():
            child.destroy()
        self._nav_buttons = {k: b for k, b in self._nav_buttons.items() if k == SETTINGS_KEY}
        visible = self._visible_records()
        for row, rec in enumerate(visible):
            btn = self._make_nav_button(self._nav_frame, rec.key, "⚠" if rec.error else rec.icon, rec.title)
            btn._mt_error = bool(rec.error)
            btn.grid(row=row, column=0, sticky="ew", pady=1)
        if not visible:
            self.theme.tag(ctk.CTkLabel(self._nav_frame, text="No plugins loaded", anchor="w"), "muted").grid(
                row=0, column=0, sticky="ew", padx=10, pady=6)
        count = sum(1 for r in visible if not r.error)
        self._default_tagline = f"Desktop Utility Belt · {count} tool{'s' if count != 1 else ''}"
        self.theme.apply(self._sidebar)
        self._apply_shell_effects()

    def select(self, key: str) -> None:
        if key == self._active_key and key in self._pages:
            return
        old_key = self._active_key
        if old_key is not None:
            old_frame = self._pages.get(old_key)
            if old_frame is not None:
                _container_of(old_frame).grid_remove()
            old_rec = self._record(old_key)
            if old_rec is not None and old_rec.instance is not None:
                self._safe_call(old_rec, "on_hide")

        frame = self._pages.get(key) or self._build_page(key)
        _container_of(frame).grid(row=0, column=0, sticky="nsew", in_=self.viewport)
        self._active_key = key

        rec = self._record(key)
        if key == SETTINGS_KEY:
            title, desc = "Settings", "Appearance, plugins, bundled engines and storage locations."
        elif key == EMPTY_KEY:
            title, desc = "No plugins found", "Drop plugin .py files into the plugins folder, then reload."
        elif rec is not None:
            title = rec.title
            desc = "This plugin failed to load." if rec.error else rec.description
        else:
            title, desc = "", ""
        self._raw_title = title
        self._page_title.configure(text=self._rendered_title(), text_color=self.theme.palette["title"])
        self._page_desc.configure(text=desc)
        self._update_kicker()
        if old_key is not None:
            self.theme.play("nav")
        if not self._booting:
            self._start_decrypt()
            self._start_sweep()

        for k, btn in self._nav_buttons.items():
            active = k == key
            if getattr(btn, "_mt_active", None) != active:
                btn._mt_active = active
                self._render_nav_text(btn)
                self.theme.apply(btn)

        if key not in (EMPTY_KEY,):
            self.settings.set("core", "last_page", key)
        if rec is not None and rec.instance is not None and not rec.error:
            self._safe_call(rec, "on_show")

    def _build_page(self, key: str) -> Any:
        if key == SETTINGS_KEY:
            frame = self._build_settings_page()
        elif key == EMPTY_KEY:
            frame = self._build_empty_page()
        else:
            rec = self._record(key)
            if rec is None:
                frame = self._build_empty_page()
            elif rec.error or rec.instance is None:
                frame = self._build_error_page(rec)
            else:
                try:
                    frame = rec.instance.get_frame(self.viewport)
                    if frame is None or not isinstance(_container_of(frame), tk.Misc):
                        raise PluginError("get_frame() must return a tkinter/CustomTkinter widget.")
                except Exception as exc:
                    rec.error = str(exc) if isinstance(exc, PluginError) else traceback.format_exc()
                    self.log.error("Plugin %s get_frame failed:\n%s", key, rec.error)
                    btn = self._nav_buttons.get(key)
                    if btn is not None:
                        btn._mt_text = f"⚠   {rec.title}"
                        btn._mt_error = True
                        self._render_nav_text(btn)
                    frame = self._build_error_page(rec)
        if not hasattr(frame, "_mt_role"):
            frame._mt_role = "transparent"  # page roots blend into the viewport by default
        self._pages[key] = frame
        self.theme.apply(frame)
        return frame

    # ── built-in pages ────────────────────────────────────────────────────
    def _card(self, parent: Any, row: int, title: str, subtitle: str | None = None) -> Any:
        tag = self.theme.tag
        card = tag(ctk.CTkFrame(parent), "card")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14), padx=(0, 8))
        card.grid_columnconfigure(0, weight=1)
        tag(ctk.CTkLabel(card, text=title, anchor="w"), "title", font_size=15).grid(
            row=0, column=0, sticky="ew", padx=18, pady=(14, 2 if subtitle else 10))
        if subtitle:
            tag(ctk.CTkLabel(card, text=subtitle, anchor="w", justify="left", wraplength=700), "muted").grid(
                row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        body = tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        body.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 16))
        body.grid_columnconfigure(1, weight=1)
        return body

    def _build_settings_page(self) -> Any:
        tag = self.theme.tag
        page = tag(ctk.CTkScrollableFrame(self.viewport, corner_radius=0), "transparent")
        page.grid_columnconfigure(0, weight=1)

        # Appearance — theme gallery
        body = self._card(page, 0, "Themes",
                          "Themes are drop-in packs (.mttheme files). Drop one anywhere on this page or use "
                          "Install theme… — it's applied straight away. Default Dark is built in and always "
                          "here to go back to.")
        bar = tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ctk.CTkButton(bar, text="＋  Install theme…", width=150,
                      command=lambda: self.ask_install("theme")).pack(side="left")
        tag(ctk.CTkButton(bar, text="Back to default", width=140,
                          command=lambda: self._on_theme_selected(DEFAULT_THEME)), "secondary").pack(
            side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open themes folder", width=160,
                          command=lambda: self.open_path(self.paths.themes_dir)), "ghost").pack(side="left")
        for i, name in enumerate(self.theme.names(), start=1):
            self._theme_row(body, i, self.theme.info(name))

        # Updates
        body = self._card(page, 5, "Updates",
                          "New and changed plugins, themes and characters arrive straight from GitHub. "
                          "A full installer is only needed when the bundled engines change.")
        self._updates_card(body)

        # Interface — sounds and motion
        body = self._card(page, 1, "Interface",
                          "Sounds and animation come from the active theme. Turn them down or off here.")
        self._interface_controls(body)

        # Plugins
        body = self._card(page, 2, "Plugins",
                          f"Scanned from {self.paths.plugins_dir}. Toggle a plugin off to skip loading it "
                          "(useful for heavy tools); changes apply on reload (Ctrl+Shift+R).")
        bar = tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ctk.CTkButton(bar, text="＋  Install…", width=130, command=self.ask_install).pack(side="left")
        tag(ctk.CTkButton(bar, text="Reload plugins", width=140, command=self.reload_plugins),
            "secondary").pack(side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open plugins folder", width=160,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").pack(side="left")
        tag(ctk.CTkLabel(bar, text="or drop plugins, themes or characters anywhere on this page"), "muted").pack(
            side="left", padx=12)
        if not self.records:
            tag(ctk.CTkLabel(body, text="No plugin files found.", anchor="w"), "muted").grid(
                row=1, column=0, columnspan=3, sticky="w")
        for i, rec in enumerate(sorted(self.records, key=lambda r: (r.order, r.title.lower())), start=1):
            var = ctk.BooleanVar(value=rec.enabled)
            ctk.CTkSwitch(body, text="", width=46, variable=var,
                          command=lambda k=rec.key, v=var: self._toggle_plugin(k, v.get())).grid(
                row=i, column=0, sticky="w", pady=3)
            tag(ctk.CTkLabel(body, text=f"{rec.title}    ·    {rec.display_path}", anchor="w"), "text").grid(
                row=i, column=1, sticky="ew", padx=(4, 12))
            role = {"Loaded": "success", "Error": "danger", "Disabled": "muted"}[rec.status]
            tag(ctk.CTkLabel(body, text=rec.status, anchor="e"), role).grid(row=i, column=2, sticky="e")

        # Bundled engines
        body = self._card(page, 3, "Bundled engines",
                          f"Resolved only from {self.paths.bin_dir} — nothing is read from the system PATH.")
        row = 0
        for name, used_by in KNOWN_ENGINES:
            found = find_binary(self.paths.bin_dir, name)
            tag(ctk.CTkLabel(body, text="✓" if found else "✗", width=24),
                "success" if found else "danger").grid(row=row, column=0, sticky="w")
            tag(ctk.CTkLabel(body, text=f"{name}  —  {used_by}", anchor="w"), "text").grid(
                row=row, column=1, sticky="ew")
            tag(ctk.CTkLabel(body, text=str(found) if found else "missing", anchor="e"), "muted").grid(
                row=row, column=2, sticky="e")
            row += 1
        dnd_text = (f"tkdnd {self.dnd_version} loaded" if self.dnd_available
                    else (self.dnd_error or "unavailable"))
        tag(ctk.CTkLabel(body, text="✓" if self.dnd_available else "✗", width=24),
            "success" if self.dnd_available else "danger").grid(row=row, column=0, sticky="w")
        tag(ctk.CTkLabel(body, text=f"Drag & drop  —  {dnd_text}", anchor="w", justify="left",
                         wraplength=600), "text").grid(row=row, column=1, columnspan=2, sticky="ew")
        tag(ctk.CTkButton(body, text="Open bin folder", width=150,
                          command=lambda: self.open_path(self.paths.bin_dir)), "secondary").grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        # Storage
        body = self._card(page, 4, "Storage",
                          f"Mode: {'portable (data beside the app)' if self.paths.portable else 'installed (data in AppData)'}"
                          f"  ·  {APP_NAME} {APP_VERSION}  ·  Python {sys.version.split()[0]}"
                          f"  ·  Plugin API v{PLUGIN_API_VERSION}")
        locations = [
            ("App folder", self.paths.app_dir),
            ("Plugins", self.paths.plugins_dir),
            ("Bundled engines", self.paths.bin_dir),
            ("User data", self.paths.data_dir),
            ("Logs", self.paths.logs_dir),
        ]
        for r, (label, path) in enumerate(locations):
            tag(ctk.CTkLabel(body, text=label, anchor="w", width=130), "text").grid(row=r, column=0, sticky="w")
            tag(ctk.CTkLabel(body, text=str(path), anchor="w"), "muted").grid(row=r, column=1, sticky="ew")
            tag(ctk.CTkButton(body, text="Open", width=70, height=26,
                              command=lambda p=path: self.open_path(p)), "ghost").grid(row=r, column=2, sticky="e")
        self.register_drop_target(page, self._on_plugin_drop, accept=("files",))
        return page

    def _interface_controls(self, body: Any) -> None:
        tag = self.theme.tag
        sounds = ctk.BooleanVar(value=self.theme.player.enabled)
        boot = ctk.BooleanVar(value=bool(self.settings.get("core", "boot_anim", True)))
        calm = ctk.BooleanVar(value=self.theme.reduce_motion)

        def set_sounds() -> None:
            self.theme.player.enabled = sounds.get()
            self.settings.set("core", "ui_sounds", sounds.get())

        def set_boot() -> None:
            self.settings.set("core", "boot_anim", boot.get())

        def set_calm() -> None:
            self.theme.reduce_motion = calm.get()
            self.settings.set("core", "reduce_motion", calm.get())
            self._apply_shell_effects()

        def set_volume(value: float) -> None:
            self.theme.player.volume = round(float(value), 2)
            self.settings.set("core", "ui_volume", self.theme.player.volume)

        ctk.CTkSwitch(body, text="UI sounds", variable=sounds, command=set_sounds).grid(
            row=0, column=0, sticky="w", pady=4)
        volume = ctk.CTkSlider(body, from_=0, to=1, number_of_steps=20, width=220, command=set_volume)
        volume.set(self.theme.player.volume)
        volume.grid(row=0, column=1, sticky="w", padx=(12, 8))
        tag(ctk.CTkButton(body, text="▶  Test", width=80, command=lambda: self.theme.play("notify")),
            "ghost").grid(row=0, column=2, sticky="e")
        ctk.CTkSwitch(body, text="Boot animation on start (themes that have one)", variable=boot,
                      command=set_boot).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ctk.CTkSwitch(body, text="Reduce motion (no glitch, decrypt or sweep effects)", variable=calm,
                      command=set_calm).grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
        pops = ctk.BooleanVar(value=bool(self.settings.get("core", "toasts", True)))
        ctk.CTkSwitch(body, text="Heads-up pop-ups", variable=pops,
                      command=lambda: self.settings.set("core", "toasts", pops.get())).grid(
            row=4, column=0, sticky="w", pady=4)
        corner = ctk.CTkOptionMenu(body, values=["top-right", "bottom-right"], width=140,
                                   command=lambda v: self.settings.set("core", "toast_corner", v))
        corner.set(self.settings.get("core", "toast_corner", "top-right"))
        corner.grid(row=4, column=1, sticky="w", padx=(12, 8))
        tag(ctk.CTkButton(body, text="Preview", width=80, command=lambda: self.notify(
            "Preview", "This is how heads-up messages look with the current theme.",
            subtitle=f"{APP_NAME} · {self.theme.name}", note="pop-ups stack in the corner — hover to pause",
            icon="✦")), "ghost").grid(row=4, column=2, sticky="e")
        note = ("This theme has its own sound pack." if self.theme.has_sounds()
                else "The active theme has no sounds — try a theme pack with sounds.")
        if not self.theme.player.available:
            note = "UI sounds are Windows-only."
        tag(ctk.CTkLabel(body, text=note, anchor="w"), "muted").grid(row=3, column=0, columnspan=3, sticky="w")

    def _theme_row(self, parent: Any, row: int, info: ThemeInfo) -> None:
        tag = self.theme.tag
        active = info.name == self.theme.name
        swatches = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        swatches.grid(row=row, column=0, sticky="w", pady=5, padx=(0, 14))
        for key in ("bg", "card", "accent", "accent2", "selected", "text"):
            tag(ctk.CTkFrame(swatches, width=20, height=26, corner_radius=0, fg_color=info.palette[key],
                             border_width=1, border_color=info.palette["border"]), "skip").pack(side="left")
        text_box = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        text_box.grid(row=row, column=1, sticky="ew")
        tag(ctk.CTkLabel(text_box, text=info.name + (f"   v{info.version}" if info.version else ""), anchor="w",
                         font=self.theme.font(13, "bold")), "text").pack(fill="x")
        byline = " · ".join(x for x in (info.author or "", info.description) if x)
        tag(ctk.CTkLabel(text_box, text=byline, anchor="w", justify="left", wraplength=520), "muted").pack(fill="x")
        actions = tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        actions.grid(row=row, column=2, sticky="e")
        if active:
            tag(ctk.CTkLabel(actions, text="●  Active", width=90), "success").pack(side="left", padx=4)
        else:
            tag(ctk.CTkButton(actions, text="Apply", width=90,
                              command=lambda n=info.name: self._on_theme_selected(n)), "secondary").pack(
                side="left", padx=4)
        if not info.builtin:
            tag(ctk.CTkButton(actions, text="Remove", width=80,
                              command=lambda n=info.name: self._remove_theme(n)), "ghost").pack(side="left")

    def _refresh_settings_page(self) -> None:
        """Rebuild Settings (after a theme/plugin change) — deferred so a button can trigger it safely."""
        def rebuild() -> None:
            frame = self._pages.pop(SETTINGS_KEY, None)
            if frame is None:
                return
            was_active = self._active_key == SETTINGS_KEY
            try:
                _container_of(frame).destroy()
            except Exception:
                pass
            if was_active:
                self._active_key = None
                self.select(SETTINGS_KEY)
        self.after(10, rebuild)

    def _refresh_theme_menus(self) -> None:
        for menu in self._theme_menus:
            try:
                menu.configure(values=self.theme.names())
            except Exception:
                pass
        self._theme_var.set(self.theme.name)

    def _remove_theme(self, name: str) -> None:
        try:
            self.theme.remove(name)
        except Exception as exc:
            self.set_status(f"Couldn't remove theme: {exc}", "error")
            return
        self._refresh_theme_menus()
        self._refresh_settings_page()
        self.set_status(f"Removed theme {name}", "success")

    def _build_error_page(self, rec: PluginRecord) -> Any:
        tag = self.theme.tag
        page = ctk.CTkFrame(self.viewport, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        tag(ctk.CTkLabel(page, text=f"⚠  {rec.title} failed to load", anchor="w",
                         font=self.theme.font(16, "bold")), "danger").grid(row=0, column=0, sticky="ew")
        tag(ctk.CTkLabel(page, text=str(rec.path), anchor="w"), "muted").grid(row=1, column=0, sticky="ew")
        box = ctk.CTkTextbox(page, wrap="none", font=self.theme.font(12, mono=True))
        box.grid(row=2, column=0, sticky="nsew", pady=12)
        box.insert("1.0", rec.error or "Unknown error")
        box.configure(state="disabled")
        bar = tag(ctk.CTkFrame(page, fg_color="transparent"), "transparent")
        bar.grid(row=3, column=0, sticky="w")
        tag(ctk.CTkButton(bar, text="Copy error", width=120,
                          command=lambda: self.copy_to_clipboard(rec.error or "")), "secondary").pack(side="left")
        ctk.CTkButton(bar, text="Reload plugins", width=140, command=self.reload_plugins).pack(side="left", padx=8)
        tag(ctk.CTkButton(bar, text="Open plugins folder", width=160,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").pack(side="left")
        return page

    def _build_empty_page(self) -> Any:
        tag = self.theme.tag
        page = ctk.CTkFrame(self.viewport, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        body = self._card(page, 0, "Your utility belt is empty",
                          "Each tool is a Python file in the plugins folder that defines `class Plugin` "
                          "with meta_title, meta_description and get_frame(master). Files starting with "
                          "an underscore are ignored.")
        tag(ctk.CTkLabel(body, text=str(self.paths.plugins_dir), anchor="w"), "accent").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ctk.CTkButton(body, text="＋  Install…", width=150, command=self.ask_install).grid(
            row=1, column=0, sticky="w")
        tag(ctk.CTkButton(body, text="Open plugins folder", width=170,
                          command=lambda: self.open_path(self.paths.plugins_dir)), "secondary").grid(
            row=1, column=1, sticky="w", padx=8)
        self.register_drop_target(page, self._on_plugin_drop, accept=("files",))
        return page

    # ── services used by PluginContext ────────────────────────────────────
    def call_in_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if threading.current_thread() is threading.main_thread():
            fn(*args, **kwargs)
        else:
            self._ui_queue.put((fn, args, kwargs))

    def _pump_ui_queue(self) -> None:
        for _ in range(300):
            try:
                fn, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                self.log.exception("UI callback %r failed", getattr(fn, "__qualname__", fn))
        if not self._closing:
            self.after(20, self._pump_ui_queue)

    def run_in_thread(self, fn: Callable[..., Any], *args: Any,
                      on_done: Callable[[Any], None] | None = None,
                      on_error: Callable[[BaseException], None] | None = None,
                      name: str = "worker", **kwargs: Any) -> threading.Thread:
        def runner() -> None:
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                self.log.exception("Background task %s failed", name)
                if on_error is not None:
                    self._ui_queue.put((on_error, (exc,), {}))
                else:
                    self._ui_queue.put((self.set_status, (f"Task failed: {exc}", "error"), {}))
                return
            if on_done is not None:
                self._ui_queue.put((on_done, (result,), {}))

        thread = threading.Thread(target=runner, name=name, daemon=True)
        thread.start()
        return thread

    def copy_to_clipboard(self, text: str) -> None:
        def _copy() -> None:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        self.call_in_ui(_copy)

    def open_path(self, path: Path | str) -> None:
        try:
            target = Path(path)
            if not target.exists() and not target.suffix:
                target.mkdir(parents=True, exist_ok=True)
            open_in_os(target)
        except Exception as exc:
            self.set_status(f"Could not open {path}: {exc}", "error")

    def set_status(self, text: str, level: str = "info") -> None:
        role = {"info": "muted", "success": "success", "warning": "warning", "error": "danger"}.get(level, "muted")
        text = " ".join(str(text).split())
        self._status_text = text
        self._cursor_on = True
        self._render_status()
        if level == "error":
            self.theme.play("error")
        elif level == "success":
            self.theme.play("success")
        self.theme.tag(self._status_label, role)
        self.theme.apply(self._status_label)
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except Exception:
                pass
            self._status_after = None
        if level in ("info", "success"):
            self._status_after = self.after(8000, lambda: self.set_status("Ready", "_idle"))
        if level == "error":
            self.log.error("Status: %s", text)

    def notify(self, title: str, body: str = "", subtitle: str = "", image: Any = None, note: str = "",
               on_click: Callable[[], Any] | None = None, duration: float = 6.0, icon: str = "",
               source: str = "core") -> None:
        if self._closing:
            return
        self.toasts.show(title, body, subtitle=subtitle, image=image, note=note, on_click=on_click,
                         duration=duration, icon=icon)

    # ── updates ───────────────────────────────────────────────────────────
    UPDATE_INTERVAL_MS = 6 * 3600 * 1000

    def _auto_update_check(self) -> None:
        if self._closing:
            return
        self.after(self.UPDATE_INTERVAL_MS, self._auto_update_check)
        if self.updater.enabled and self.settings.get("core", "auto_update_check", True):
            self.check_for_updates(quiet=True)

    def check_for_updates(self, quiet: bool = False) -> None:
        if self.updater.busy:
            return
        if not self.updater.enabled:
            self._update_message("Updates are off in a development copy — run the installed app.")
            return
        self.updater.busy = True
        self._update_message("Checking GitHub for updates…")

        def work() -> None:
            try:
                result = self.updater.check()
                self.call_in_ui(done, result, None)
            except Exception as exc:
                self.call_in_ui(done, None, exc)

        def done(result: UpdateCheck | None, exc: Exception | None) -> None:
            self.updater.busy = False
            if exc is not None:
                self.log.warning("Update check failed: %s", exc)
                self._update_message(f"Couldn't check for updates ({exc}).")
                return
            self._update_message(result.message)
            if quiet and (result.items or result.installer_url):
                self.notify("Update available", result.message, subtitle=f"{APP_NAME} · click to open Settings",
                            icon="↑", duration=10, on_click=lambda: self.select(SETTINGS_KEY))

        self.run_in_thread(work)

    def install_updates(self) -> None:
        check = self.updater.last
        if check is None or self.updater.busy:
            return
        self.updater.busy = True
        progress = lambda text: self.call_in_ui(self._update_message, text)  # noqa: E731

        def work() -> None:
            try:
                done = self.updater.apply(check, progress) if check.items else {}
                installer = None
                if check.installer_url and not done.get("core"):
                    installer = self.updater.download_installer(check, progress)
                self.call_in_ui(finished, done, installer, None)
            except Exception as exc:
                self.call_in_ui(finished, {}, None, exc)

        def finished(done: dict[str, int], installer: Path | None, exc: Exception | None) -> None:
            self.updater.busy = False
            if exc is not None:
                self.log.exception("Update failed", exc_info=exc)
                self._update_message(f"Update failed: {exc}")
                self.set_status(f"Update failed: {exc}", "error")
                return
            if done.get("theme"):
                self.theme.reload()
                self._refresh_theme_menus()
            if done.get("plugin") or done.get("character"):
                self.reload_plugins()
            if installer is not None:
                self._update_message("Starting the installer — Multi-Tool will reopen when it's done.")
                self._run_installer(installer)
                return
            if done.get("core"):
                self._update_message("Core updated — restarting to finish…")
                self.after(600, self.restart)
                return
            total = sum(done.values())
            self._update_message(f"Updated {total} file{'s' if total != 1 else ''}. You're up to date.")
            self.set_status("Updates installed", "success")
            self._refresh_settings_page()

        self.run_in_thread(work)

    def _run_installer(self, installer: Path) -> None:
        try:
            subprocess.Popen([str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTART"],
                             close_fds=True)
        except Exception as exc:
            self._update_message(f"Couldn't start the installer: {exc}")
            return
        self.after(800, self._on_close)

    def restart(self) -> None:
        args = [sys.executable] if IS_FROZEN else [sys.executable, str(Path(sys.argv[0]).resolve())]
        try:
            subprocess.Popen(args + ["--after-update", str(os.getpid())], close_fds=True,
                             cwd=str(self.paths.app_dir))
        except Exception as exc:
            self.set_status(f"Restart failed — please reopen {APP_NAME}: {exc}", "error")
            return
        self._on_close()

    def _update_message(self, text: str) -> None:
        self._update_text = text
        label = getattr(self, "_update_label", None)
        try:
            if label is not None and label.winfo_exists():
                label.configure(text=text)
            button = getattr(self, "_update_button", None)
            if button is not None and button.winfo_exists():
                last = self.updater.last
                ready = bool(last and (last.items or last.installer_url)) and not self.updater.busy
                if ready:
                    button.configure(text="Download & install" if last.installer_url and not last.items
                                     else "Update now")
                    button.grid()
                else:
                    button.grid_remove()
        except Exception:
            pass

    def _updates_card(self, body: Any) -> None:
        tag = self.theme.tag
        up = self.updater
        build = f" · build {up.build}" if up.build else ""
        tag(ctk.CTkLabel(body, text=f"{APP_NAME} {APP_VERSION}{build}   ·   source: github.com/{up.repo}",
                         anchor="w"), "text").grid(row=0, column=0, columnspan=3, sticky="ew")
        self._update_label = tag(ctk.CTkLabel(body, text=getattr(self, "_update_text", "") or
                                              ("Plugins, themes and the app core update in place — no reinstall."
                                               if up.enabled else "Updates are off in a development copy."),
                                              anchor="w", justify="left", wraplength=640), "muted")
        self._update_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 8))
        bar = tag(ctk.CTkFrame(body, fg_color="transparent"), "transparent")
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        ctk.CTkButton(bar, text="Check for updates", width=160,
                      command=lambda: self.check_for_updates()).grid(row=0, column=0, sticky="w")
        self._update_button = ctk.CTkButton(bar, text="Update now", width=150, command=self.install_updates)
        self._update_button.grid(row=0, column=1, sticky="w", padx=8)
        auto = ctk.BooleanVar(value=bool(self.settings.get("core", "auto_update_check", True)))
        ctk.CTkSwitch(bar, text="Check automatically", variable=auto,
                      command=lambda: self.settings.set("core", "auto_update_check", auto.get())).grid(
            row=0, column=2, sticky="w", padx=8)
        self._update_message(getattr(self, "_update_text", "") or self._update_label.cget("text"))

    def register_drop_target(self, widget: Any, callback: Callable[[DropPayload], None],
                             accept: tuple[str, ...] = ("files", "text"), owner: str = "core") -> bool:
        """Registers `widget` and every child window under it (CTk widgets are covered by
        internal canvases, so registering only the outer frame would miss most drops)."""
        if not self.dnd_available:
            return False
        dnd_types = ([DND_FILES] if "files" in accept else []) + ([DND_TEXT] if "text" in accept else [])
        if not dnd_types:
            return False
        target = widget

        def on_drop(event: Any) -> Any:
            self._drop_feedback(target, False)
            action = getattr(event, "action", "copy")
            try:
                payload = parse_drop_data(target, getattr(event, "data", ""))
            except Exception:
                self.log.exception("Could not parse drop data")
                return action
            if not (payload.files or payload.urls or payload.text):
                return action
            try:
                callback(payload)
            except Exception as exc:
                self.log.exception("Drop handler of %s failed", owner)
                self.set_status(f"Drop failed: {exc}", "error")
            return action

        def on_enter(event: Any) -> Any:
            self._drop_feedback(target, True)
            return getattr(event, "action", "copy")

        def on_leave(event: Any) -> Any:
            self._drop_feedback(target, False)
            return getattr(event, "action", "copy")

        bound = 0
        for w in _iter_widget_tree(_container_of(widget)):
            current = getattr(w, "_mt_drop_target", None)
            if current is not None and current is not target and _is_descendant(current, target):
                continue  # a nested drop target owns this child window
            try:
                self._ensure_dnd_methods(w)
                w.drop_target_register(*dnd_types)
                w.dnd_bind("<<Drop>>", on_drop)
                w.dnd_bind("<<DropEnter>>", on_enter)
                w.dnd_bind("<<DropLeave>>", on_leave)
                w._mt_drop_target = target
                bound += 1
            except Exception as exc:
                self.log.debug("DnD register failed on %s: %s", w, exc)
        return bound > 0

    @staticmethod
    def _ensure_dnd_methods(widget: Any) -> None:
        if hasattr(widget, "drop_target_register") or TkinterDnD is None:
            return
        for name, value in vars(TkinterDnD.DnDWrapper).items():
            if name.startswith("__"):
                continue
            setattr(widget, name, types.MethodType(value, widget) if inspect.isfunction(value) else value)

    def _drop_feedback(self, target: Any, active: bool) -> None:
        try:
            if active:
                if getattr(target, "_mt_drop_saved", None) is None:
                    target._mt_drop_saved = (target.cget("border_color"), target.cget("border_width"))
                target.configure(border_color=self.theme.palette["drop_highlight"], border_width=2)
            else:
                saved = getattr(target, "_mt_drop_saved", None)
                if saved is not None:
                    target.configure(border_color=saved[0], border_width=saved[1])
                    target._mt_drop_saved = None
        except Exception:
            pass

    # ── plugin installation ───────────────────────────────────────────────
    def register_file_type(self, extension: str, label: str, handler: Callable[[Path], Any],
                           owner: str = "core") -> None:
        ext = extension.lower() if extension.startswith(".") else "." + extension.lower()
        self._file_types[ext] = (owner, label, handler)

    def ask_install(self, kind: str = "any") -> None:
        from tkinter import filedialog
        types = [("Themes", "*.mttheme")] if kind == "theme" else [
            ("Everything Multi-Tool can install",
             " ".join(["*.py", "*.zip", "*.mttheme"] + [f"*{e}" for e in self._file_types])),
            ("Plugins", "*.py *.zip"), ("Themes", "*.mttheme"),
        ] + [(label, f"*{ext}") for ext, (_o, label, _h) in self._file_types.items()]
        chosen = filedialog.askopenfilenames(parent=self, title="Install into Multi-Tool",
                                             filetypes=types + [("All files", "*.*")])
        if chosen:
            self.install_files([Path(c) for c in chosen])

    def ask_install_plugin(self) -> None:
        self.ask_install()

    def _on_plugin_drop(self, payload: DropPayload) -> None:
        if payload.files:
            self.install_files(payload.files)
        else:
            self.set_status("Drop plugin, theme or character files here.", "warning")

    def install_files(self, files: list[Path]) -> None:
        """One entry point for everything installable: plugins (.py/.zip), themes (.mttheme)
        and any file type a plugin registered (e.g. companion characters)."""
        plugin_files: list[Path] = []
        problems: list[str] = []
        themes: list[str] = []
        handled: list[str] = []
        for src in files:
            suffix = src.suffix.lower()
            try:
                if suffix == ".mttheme":
                    themes.append(self.theme.install(src))
                elif suffix in (".py", ".zip"):
                    plugin_files.append(src)
                elif suffix in self._file_types:
                    _owner, label, handler = self._file_types[suffix]
                    result = handler(src)
                    handled.append(str(result or src.name))
                else:
                    problems.append(f"{src.name} (not a plugin, theme or known file type)")
            except Exception as exc:
                self.log.exception("Install of %s failed", src)
                problems.append(f"{src.name} ({exc})")
        if themes:
            self._refresh_theme_menus()
            self._on_theme_selected(themes[-1])
            self.set_status(f"Installed theme: {', '.join(themes)}", "success")
        if handled:
            self.set_status(f"Installed: {', '.join(handled)}", "success")
        if plugin_files:
            self.install_plugins(plugin_files, problems)
        elif problems:
            self.set_status("Not installed: " + "; ".join(problems), "error" if not (themes or handled) else "warning")

    def install_plugins(self, files: list[Path], problems: list[str] | None = None) -> None:
        """Copy .py plugins (or .zip packages containing one plugin folder) into plugins/, then reload."""
        installed: list[str] = []
        problems = problems if problems is not None else []
        for src in files:
            try:
                suffix = src.suffix.lower()
                if suffix == ".py" and src.is_file():
                    dest = self.paths.plugins_dir / src.name
                    if src.resolve() != dest.resolve():
                        shutil.copy2(src, dest)
                    installed.append(re.sub(r"\W", "_", src.stem).lower())
                elif suffix == ".zip" and zipfile.is_zipfile(src):
                    installed.extend(self._install_zip(src))
                else:
                    problems.append(f"{src.name} (not a .py or .zip plugin)")
            except PermissionError:
                problems.append(f"{src.name} (plugins folder is read-only)")
            except Exception as exc:
                problems.append(f"{src.name} ({exc})")
        if installed:
            disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
            self.settings.set("core", "disabled_plugins", sorted(disabled - set(installed)))
            self.settings.set("core", "last_page", installed[0])
            self.log.info("Installed plugins: %s", installed)
            # reload after this event returns: the page that received the drop gets rebuilt
            self.after(50, self._after_install, installed, problems)
        elif problems:
            self.set_status("Not installed: " + "; ".join(problems), "error")

    def _after_install(self, installed: list[str], problems: list[str]) -> None:
        self.reload_plugins()
        if installed[0] in self._visible_keys():
            self.select(installed[0])
        msg = f"Installed {len(installed)} plugin(s): {', '.join(installed)}"
        if problems:
            msg += "  ·  skipped: " + "; ".join(problems)
        self.set_status(msg, "warning" if problems else "success")

    def _install_zip(self, src: Path) -> list[str]:
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or not any(n.count("/") == 1 and n.endswith("/__init__.py") for n in names):
                raise PluginError("zip must contain exactly one plugin folder with an __init__.py")
            top = tops.pop()
            target_root = self.paths.plugins_dir.resolve()
            dest_dir = target_root / top
            for name in names:
                out = (target_root / name).resolve()
                if target_root not in out.parents:
                    raise PluginError("zip contains unsafe paths")
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            zf.extractall(target_root)
        return [re.sub(r"\W", "_", top).lower()]

    # ── settings actions ──────────────────────────────────────────────────
    def _on_theme_selected(self, name: str) -> None:
        self._theme_var.set(name)
        self.theme.set_theme(name)
        self._refresh_settings_page()
        self.theme.play("open")
        self._start_decrypt()
        self._start_sweep()

    def _toggle_plugin(self, key: str, enabled: bool) -> None:
        disabled = set(self.settings.get("core", "disabled_plugins", []) or [])
        (disabled.discard if enabled else disabled.add)(key)
        self.settings.set("core", "disabled_plugins", sorted(disabled))
        self.set_status("Plugin list changed — reload plugins (Ctrl+Shift+R) to apply.", "warning")

    # ── Tk plumbing ───────────────────────────────────────────────────────
    def report_callback_exception(self, exc: type, val: BaseException, tb: Any) -> None:
        self.log.error("Tk callback exception", exc_info=(exc, val, tb))
        try:
            self.set_status(f"Error: {val}", "error")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            state = self.state()
            self.settings.set("core", "maximized", state == "zoomed")
            if state == "normal":
                self.settings.set("core", "geometry", self.geometry())
        except Exception:
            self.log.exception("Could not save window geometry")
        self._schedule_glitch()  # cancels the pending glitch timer (closing flag is set)
        self._schedule_ticker()
        self.toasts.close_all()
        self._unload_plugins()
        self.log.info("Shutting down")
        self.destroy()


# ════════════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════════════

def _set_default_font() -> None:
    """CTk's default family (Roboto) isn't installed on most Windows PCs."""
    if not IS_WINDOWS:
        return
    try:
        ctk.ThemeManager.theme["CTkFont"]["family"] = "Segoe UI"
    except Exception:
        pass


def wait_for_process(pid: int, timeout: float = 20.0) -> None:
    """After an in-app update the new copy waits for the old one to close before taking the instance lock."""
    if not IS_WINDOWS or pid <= 0:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            kernel32.CloseHandle(handle)
    except Exception:
        pass


def retire_plugins(paths: AppPaths, log: logging.Logger) -> list[str]:
    """Move plugins that left Multi-Tool out of the plugins folder (kept in data/retired, not deleted)."""
    moved = []
    for name, why in RETIRED_PLUGINS.items():
        src = paths.plugins_dir / name
        if not src.exists():
            continue
        dest = paths.data_dir / "retired" / name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            shutil.move(str(src), str(dest))
            moved.append(why)
            log.info("Retired plugin %s: %s", name, why)
        except OSError as exc:
            log.warning("Could not retire %s: %s", name, exc)
    return moved


def main() -> int:
    if "--after-update" in sys.argv:
        try:
            wait_for_process(int(sys.argv[sys.argv.index("--after-update") + 1]))
        except (ValueError, IndexError):
            pass
    paths = resolve_paths()
    ensure_stdio(paths)
    log = setup_logging(paths)
    log.info("Starting %s %s (frozen=%s, portable=%s) from %s", APP_NAME, APP_VERSION, IS_FROZEN,
             paths.portable, paths.app_dir)

    if not acquire_single_instance():
        log.info("Another instance is already running; focusing it.")
        focus_existing_window()
        return 0

    set_app_user_model_id()
    configure_environment(paths, log)
    retired = retire_plugins(paths, log)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    _set_default_font()

    try:
        app = App(paths, log)
        for why in retired:
            app.after(2500, lambda w=why: app.notify("Moved to its own app", w, subtitle=APP_NAME, icon="→",
                                                     duration=12))
    except Exception:
        log.critical("Startup failed", exc_info=True)
        show_fatal_error(f"{APP_NAME} could not start.\n\n{traceback.format_exc(limit=3)}\n\n"
                         f"Full log: {paths.logs_dir / 'multitool.log'}")
        return 1
    app.mainloop()
    log.info("Exited cleanly")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for multiprocessing inside a PyInstaller exe
    sys.exit(main())
