"""
Juno engine — the brain, voices, voice chat, characters and chat panel behind the Juno app.
(Grew out of the desktop companion that used to live inside Multi-Tool.)

A small animated character that lives on your desktop — only while you switch it on — and chats
with you using a small open-source AI that runs entirely on your PC (Qwen3.5 via the bundled
llama.cpp engine — downloaded once from inside the app, then offline). It speaks replies with lip
sync (natural Kokoro neural voices or Windows voices, adjustable speed), can hold a hands-free voice
conversation (offline Moonshine speech recognition + Silero voice detection, mic only while you turn
it on, with a "mute me" switch), and can look at up to four images per message — screenshots,
shared windows, pasted (Ctrl+V), dropped or picked files. Online providers (Claude API, any
OpenAI-compatible server) are optional alternatives.

Design rules (from the product brief):
  * Personality governs how it behaves; separate Permissions govern what it can access.
  * Read-only on the computer: it can't click, type, open apps or change settings.
  * Screenshots are temporary context for one exchange and are never saved.
  * Memories are only saved after you approve them, and you can edit/delete them any time.
  * Proactive behaviour is opt-in and has a Do Not Disturb switch; unsolicited speech is off.

CHARACTER PACKS (.mtchar)
-------------------------
A character is a zip file with the extension .mtchar containing character.json (plus image
layers if it uses the sprite rig). Install it by dropping it on the Companion page or on
Settings, or with “Import character…”. “Export template…” writes an editable starter pack
with a README that documents every field. Anything a pack leaves out falls back to the
default character, so a pack can be as small as a name and two colours.

    character.json
      id, name, author, version, description
      persona    pronouns, backstory, speaking_style, greetings[], idle_lines[], defaults{5 sliders 0..1}
      voice      kokoro (neural voice id, e.g. "af_heart", "am_onyx", "bf_emma"), speed 0.6..1.6,
                 prefer[] (Windows voice names, first match wins), rate -10..10, pitch -10..10, volume 0..100
      look       rig "procedural": colours + hair_style (bob | long | spiky | buzz)
                 rig "sprite":     size [w,h] + layers[] of PNGs (see README in the template)
      animation  blink_interval [min,max], breath_rate, motion, walk_speed, idle_actions{name:{weight,cooldown}}
      expressions  per-mood face parameters (neutral, amused, curious, focused, skeptical, tired, pleased, concerned)
"""

from __future__ import annotations

import array
import base64
import copy
import ctypes
import hashlib
import http.client
import io
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
import zipfile
from pathlib import Path
from typing import Any, Callable

import tkinter as tk

import customtkinter as ctk

try:
    from PIL import Image, ImageGrab, ImageOps
except Exception:  # pragma: no cover
    Image = ImageGrab = ImageOps = None

try:
    import winsound
except Exception:
    winsound = None

class _LazyModule:
    """Imports a heavy module the first time it's actually used (faster start, less memory until then)."""

    def __init__(self, name: str):
        self._name = name
        self._mod: Any = None

    def __getattr__(self, attr: str) -> Any:
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


def _lazy(name: str) -> Any:
    import importlib.util
    try:
        return _LazyModule(name) if importlib.util.find_spec(name) is not None else None
    except Exception:  # pragma: no cover
        return None


np = _lazy("numpy")
sd = _lazy("sounddevice")      # PortAudio is only loaded when the mic or neural voice is first used

IS_WINDOWS = sys.platform == "win32"
TRANSPARENT_KEY = "#fe00fe"   # colour keyed out of the overlay window — never use it in art
MOODS = ["neutral", "amused", "curious", "focused", "skeptical", "tired", "pleased", "concerned"]
PERSONALITY_AXES = [
    ("warmth", "Cool", "Warm"),
    ("sincerity", "Dry", "Sincere"),
    ("chattiness", "Quiet", "Chatty"),
    ("directness", "Gentle", "Direct"),
    ("energy", "Low energy", "Lively"),
]
CLAUDE_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]

# ════════════════════════════════════════════════════════════════════════════
#  Default character
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_EXPRESSIONS: dict[str, dict[str, float]] = {
    "neutral":   {"brow_raise": 0.0, "brow_angle": 0.0, "brow_asym": 0.0, "eye_open": 1.0, "squint": 0.0,
                  "mouth_curve": 0.15, "blush": 0.0},
    "amused":    {"brow_raise": 0.3, "brow_angle": 0.1, "brow_asym": 0.0, "eye_open": 0.85, "squint": 0.35,
                  "mouth_curve": 0.85, "blush": 0.35},
    "curious":   {"brow_raise": 0.6, "brow_angle": 0.2, "brow_asym": 0.35, "eye_open": 1.1, "squint": 0.0,
                  "mouth_curve": 0.05, "blush": 0.0},
    "focused":   {"brow_raise": -0.25, "brow_angle": -0.45, "brow_asym": 0.0, "eye_open": 0.85, "squint": 0.15,
                  "mouth_curve": 0.0, "blush": 0.0},
    "skeptical": {"brow_raise": 0.0, "brow_angle": -0.2, "brow_asym": 0.85, "eye_open": 0.8, "squint": 0.1,
                  "mouth_curve": -0.2, "blush": 0.0},
    "tired":     {"brow_raise": -0.2, "brow_angle": 0.4, "brow_asym": 0.0, "eye_open": 0.55, "squint": 0.0,
                  "mouth_curve": -0.1, "blush": 0.0},
    "pleased":   {"brow_raise": 0.2, "brow_angle": 0.2, "brow_asym": 0.0, "eye_open": 0.95, "squint": 0.2,
                  "mouth_curve": 0.6, "blush": 0.2},
    "concerned": {"brow_raise": 0.25, "brow_angle": 0.8, "brow_asym": 0.0, "eye_open": 1.0, "squint": 0.0,
                  "mouth_curve": -0.45, "blush": 0.0},
}

DEFAULT_CHARACTER: dict[str, Any] = {
    "format": 1,
    "id": "juno",
    "name": "Juno",
    "author": "Juno",
    "version": "1.0",
    "description": "An upbeat ex-netrunner with a neon streak, cyber-eyes and a dry sense of humour.",
    "persona": {
        "pronouns": "they/them",
        "backstory": ("Juno used to patch city-grid firmware for a living and now keeps a friendly eye on "
                      "your desktop. Juno loves clean code, game dev, good music and finding the one log line "
                      "that explains everything. Juno has no gender — the voice is whatever the user picks."),
        "speaking_style": "Casual, quick and warm, with the occasional dry joke. Plain words, never corporate.",
        "greetings": ["Hey! Systems green. What are we building today?",
                      "Back online. Shout if you need a second pair of eyes.",
                      "Morning, boss. Or evening. I lose track in here."],
        "idle_lines": ["Still here if you need me.", "Hydration check. Just saying.",
                       "That's a lot of tabs. Respect.", "If it compiles first try, I'm suspicious."],
        "defaults": {"warmth": 0.65, "sincerity": 0.5, "chattiness": 0.35, "directness": 0.6, "energy": 0.6},
    },
    "voice": {"prefer": ["Aria", "Guy", "Jenny", "David", "Zira", "Mark"], "rate": 1, "pitch": 0, "volume": 90,
              "kokoro": "af_heart", "natural": "alba", "speed": 1.05},
    "look": {
        "rig": "procedural",
        "scale": 1.0,
        "hair_style": "bob",
        "skin": "#c98e6e", "skin_shade": "#a8705a", "hair": "#18242e", "hair_highlight": "#ff2bd6",
        "eyes": "#29e3ff", "brows": "#141c24", "lips": "#a35a5a", "blush": "#e08a8a",
        "jacket": "#1f2233", "jacket_trim": "#ff2bd6", "shirt": "#0d0f16", "pants": "#171a26",
        "shoes": "#0b0b10", "cyberware": "#29e3ff", "outline": "#06070b",
    },
    "animation": {
        "blink_interval": [2.2, 5.5],
        "breath_rate": 0.22,
        "motion": 1.0,
        "walk_speed": 110,
        "idle_actions": {
            "glance": {"weight": 4, "cooldown": 5},
            "look_at_cursor": {"weight": 3, "cooldown": 8},
            "shift_weight": {"weight": 3, "cooldown": 7},
            "head_tilt": {"weight": 2, "cooldown": 10},
            "adjust_sleeve": {"weight": 1, "cooldown": 25},
            "stretch": {"weight": 1, "cooldown": 60},
            "stroll": {"weight": 1, "cooldown": 150},
        },
    },
    "expressions": {},
}

TEMPLATE_README = """MULTI-TOOL CHARACTER PACK
=========================

A character pack is this folder zipped up with the extension .mtchar
(zip the FILES, not the folder — character.json must be at the top level).
Install it in Juno: Avatar page → Import character…, or drop the file on Juno's window.

character.json — every field is optional except "id" and "name"; missing values fall back
to the default character (Juno).

id            unique, lowercase, letters/numbers/dashes. Same id = the pack updates in place.
name, author, version, description

persona
  pronouns        e.g. "they/them", "she/her", "he/him" (Juno itself is they/them)
  backstory       who they are (sent to the AI as character background)
  speaking_style  how they talk
  greetings       lines shown in a speech bubble when the app starts (if enabled)
  idle_lines      lines occasionally shown when activity is "Chatty"
  defaults        starting personality sliders, each 0..1:
                  warmth (cool→warm), sincerity (dry→sincere), chattiness (quiet→chatty),
                  directness (gentle→direct), energy (low→lively)

voice  (the user can always pick a different voice in Juno — these are the character's defaults)
  natural  Natural (Pocket TTS) voice: one of alba, marius, javert, jean, fantine, eponine, azelma, cosette
           — OR the name of a .wav file inside this pack (5–10 s of clear speech, e.g. "voice.wav") and Juno
           speaks in that voice. Only use recordings you have permission to use.
  kokoro   Kokoro voice id, e.g. "af_heart", "am_michael", "bm_george"
  speed    0.6..1.6
  prefer   Windows voices: list of name fragments, first installed match wins, e.g. ["Aria"] or ["Guy", "David"]
  rate     -10..10   speed (Windows voices)
  pitch    -10..10   (Windows voices)
  volume   0..100    (Windows voices)

look
  rig: "procedural"  — the built-in drawn body. Recolour it with any of:
     skin, skin_shade, hair, hair_highlight, eyes, brows, lips, blush, jacket, jacket_trim,
     shirt, pants, shoes, cyberware, outline        (all "#rrggbb"; never use #fe00fe)
     hair_style: "bob" | "long" | "spiky" | "buzz"
     scale: 0.7 .. 1.4

  rig: "sprite"  — your own artwork as PNG layers (transparent background).
     "size": [180, 240]                design size of the character area in pixels
     "layers": drawn first to last. Each layer:
        { "name": "body",  "image": "body.png", "x": 0, "y": 0, "follow": "body" }
        { "name": "head",  "image": "head.png", "x": 30, "y": 10, "follow": "head" }
        { "name": "eyes",  "kind": "eyes", "follow": "head", "x": 60, "y": 60, "look": [3, 2],
          "states": { "open": "eyes_open.png", "half": "eyes_half.png", "closed": "eyes_closed.png" } }
        { "name": "mouth", "kind": "mouth", "follow": "head", "x": 75, "y": 92,
          "states": { "closed": "m_closed.png", "slight": "m_slight.png", "open": "m_open.png",
                      "wide": "m_wide.png", "smile": "m_smile.png", "frown": "m_frown.png" } }
        { "name": "brows", "kind": "brows", "follow": "head", "x": 55, "y": 50,
          "states": { "neutral": "brows.png", "focused": "brows_focused.png", "concerned": "brows_worried.png" } }
     follow: "body" (breathing/sway), "head" (body + head motion), "none" (static)
     look:   pixels the layer shifts when the character looks around (good for pupils)
     Missing states fall back to the nearest one (eyes→open, mouth→closed, brows→neutral).
     Partially transparent pixels are cut at 50% (desktop overlays can't blend).

animation
  blink_interval  [min, max] seconds between blinks
  breath_rate     breaths per second (0.15–0.35 looks natural)
  motion          0..1.5 overall movement amount
  walk_speed      pixels per second when strolling
  idle_actions    weight (how likely) and cooldown (seconds) per action:
                  glance, look_at_cursor, shift_weight, head_tilt, adjust_sleeve, stretch, stroll
                  set weight 0 to disable one

expressions     override face parameters per mood (neutral, amused, curious, focused,
                skeptical, tired, pleased, concerned):
                brow_raise -1..1, brow_angle -1..1 (negative = frown), brow_asym 0..1,
                eye_open 0.3..1.2, squint 0..1, mouth_curve -1..1 (smile/frown), blush 0..1
"""


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = deep_merge(base.get(key), value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ID_RE = re.compile(r"[^a-z0-9\-]+")


def normalize_character(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge a pack over the default character and sanitise values."""
    if not isinstance(raw, dict):
        raise ValueError("character.json must contain a JSON object")
    data = deep_merge(DEFAULT_CHARACTER, raw)
    data["id"] = ID_RE.sub("-", str(data.get("id") or data.get("name") or "character").lower()).strip("-")[:40] \
        or "character"
    data["name"] = str(data.get("name") or data["id"])[:40]
    look = data["look"]
    color_keys = {k for k, v in DEFAULT_CHARACTER["look"].items() if isinstance(v, str) and v.startswith("#")}
    for key in color_keys:
        value = look.get(key)
        if not isinstance(value, str) or not HEX_RE.match(value) or value.lower() == TRANSPARENT_KEY:
            look[key] = DEFAULT_CHARACTER["look"][key]
    if look.get("hair_style") not in ("bob", "long", "spiky", "buzz"):
        look["hair_style"] = "bob"
    try:
        look["scale"] = max(0.6, min(float(look.get("scale", 1.0)), 1.5))
    except (TypeError, ValueError):
        look["scale"] = 1.0
    expressions = deep_merge(DEFAULT_EXPRESSIONS, data.get("expressions") or {})
    data["expressions"] = {m: {k: float(v) for k, v in e.items() if isinstance(v, (int, float))}
                           for m, e in expressions.items() if isinstance(e, dict)}
    defaults = data["persona"].get("defaults") or {}
    data["persona"]["defaults"] = {axis: max(0.0, min(float(defaults.get(axis, 0.5)), 1.0))
                                   for axis, _l, _r in PERSONALITY_AXES}
    for key in ("greetings", "idle_lines"):
        lines = data["persona"].get(key)
        data["persona"][key] = [str(x)[:200] for x in lines] if isinstance(lines, list) else []
    voice = data["voice"]
    voice["prefer"] = [str(v) for v in voice.get("prefer", [])] if isinstance(voice.get("prefer"), list) else []
    for key, lo, hi in (("rate", -10, 10), ("pitch", -10, 10), ("volume", 0, 100)):
        try:
            voice[key] = int(max(lo, min(float(voice.get(key, 0)), hi)))
        except (TypeError, ValueError):
            voice[key] = DEFAULT_CHARACTER["voice"][key]
    if str(voice.get("kokoro", "")) not in KOKORO_SID:
        voice["kokoro"] = DEFAULT_CHARACTER["voice"]["kokoro"]
    natural = str(voice.get("natural") or "")
    if natural not in POCKET_LABEL and not re.fullmatch(r"[\w .()-]{1,80}\.wav", natural, re.I):
        voice["natural"] = DEFAULT_CHARACTER["voice"]["natural"]
    try:
        voice["speed"] = round(max(0.6, min(float(voice.get("speed", 1.1)), 1.6)), 2)
    except (TypeError, ValueError):
        voice["speed"] = 1.1
    return data


class CharacterLibrary:
    """Built-in character + installed packs in <plugin data>/characters/<id>/."""

    def __init__(self, folder: Path, log: Any):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.characters: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, Path | None] = {}
        self.reload()

    def reload(self) -> None:
        self.characters = {"juno": normalize_character(DEFAULT_CHARACTER)}
        self.folders = {"juno": None}
        for sub in sorted(self.folder.iterdir()) if self.folder.is_dir() else []:
            manifest = sub / "character.json"
            if not manifest.is_file():
                continue
            try:
                data = normalize_character(json.loads(manifest.read_text(encoding="utf-8-sig")))
                if data["id"] != sub.name:
                    data["id"] = sub.name
                self.characters[data["id"]] = data
                self.folders[data["id"]] = sub
            except Exception as exc:
                self.log.warning("Character pack %s skipped: %s", sub.name, exc)

    def import_file(self, src: Path) -> str:
        """Install a .mtchar (zip) or a bare character.json. Returns the character id."""
        if src.stat().st_size > 40 * 1024 * 1024:
            raise ValueError("pack is larger than 40 MB")
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                manifest = next((n for n in names if n.rsplit("/", 1)[-1] == "character.json"), None)
                if manifest is None:
                    raise ValueError("no character.json inside the pack")
                prefix = manifest[: -len("character.json")]
                raw = json.loads(zf.read(manifest).decode("utf-8-sig"))
                data = normalize_character(raw)
                cid = self._free_id(data["id"])
                dest = self.folder / cid
                staging = self.folder / f".staging-{uuid.uuid4().hex[:8]}"
                staging.mkdir(parents=True)
                try:
                    for name in names:
                        if not name.startswith(prefix):
                            continue
                        rel = name[len(prefix):]
                        out = (staging / rel).resolve()
                        if staging.resolve() not in out.parents:
                            raise ValueError("pack contains unsafe paths")
                        if Path(rel).suffix.lower() not in (".json", ".png", ".txt", ".md", ".wav"):
                            continue
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(zf.read(name))
                    raw["id"] = cid
                    (staging / "character.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False),
                                                            encoding="utf-8")
                    if dest.exists():
                        shutil.rmtree(dest)
                    staging.rename(dest)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)
        else:
            raw = json.loads(src.read_text(encoding="utf-8-sig"))
            data = normalize_character(raw)
            cid = self._free_id(data["id"])
            raw["id"] = cid
            dest = self.folder / cid
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "character.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        self.reload()
        if cid not in self.characters:
            raise ValueError("the pack was copied but could not be loaded (see log)")
        return cid

    def _free_id(self, cid: str) -> str:
        return f"{cid}-custom" if cid == "juno" else cid

    def remove(self, cid: str) -> None:
        folder = self.folders.get(cid)
        if folder is None:
            raise ValueError("the built-in character can't be removed")
        shutil.rmtree(folder, ignore_errors=True)
        self.reload()

    @staticmethod
    def export_template(dest: Path) -> None:
        template = copy.deepcopy(DEFAULT_CHARACTER)
        template.update(id="my-character", name="My Character", author="you",
                        description="Describe your character here.")
        template["expressions"] = copy.deepcopy(DEFAULT_EXPRESSIONS)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("character.json", json.dumps(template, indent=2, ensure_ascii=False))
            zf.writestr("README.txt", TEMPLATE_README)


# ════════════════════════════════════════════════════════════════════════════
#  Secrets (API key encrypted for the current Windows user via DPAPI)
# ════════════════════════════════════════════════════════════════════════════

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def protect_secret(text: str) -> str:
    raw = text.encode("utf-8")
    if IS_WINDOWS:
        try:
            buf = ctypes.create_string_buffer(raw, len(raw))
            blob_in = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            blob_out = _Blob()
            if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                                      ctypes.byref(blob_out)):
                data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return "dpapi:" + base64.b64encode(data).decode("ascii")
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        kind, _, payload = stored.partition(":")
        data = base64.b64decode(payload)
        if kind == "dpapi" and IS_WINDOWS:
            buf = ctypes.create_string_buffer(data, len(data))
            blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
            blob_out = _Blob()
            if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                                        ctypes.byref(blob_out)):
                out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return out.decode("utf-8")
            return ""
        if kind == "b64":
            return data.decode("utf-8")
    except Exception:
        pass
    return ""


# ════════════════════════════════════════════════════════════════════════════
#  Conversation: prompt, reply parsing, providers
# ════════════════════════════════════════════════════════════════════════════

def _axis_text(axis: str, v: float) -> str:
    table = {
        "warmth": ("cool and matter-of-fact", "friendly but even-keeled", "warm and friendly"),
        "sincerity": ("dry, deadpan humour", "a mix of earnest and wry", "sincere and earnest"),
        "chattiness": ("very brief — usually one or two short sentences",
                       "concise — usually two to four sentences", "chatty — up to a short paragraph"),
        "directness": ("gentle and tactful", "clear but kind", "direct and to the point"),
        "energy": ("calm and low-key", "relaxed but engaged", "lively and upbeat"),
    }
    low, mid, high = table[axis]
    return low if v < 0.34 else high if v > 0.66 else mid


def build_system_prompt(character: dict[str, Any], personality: dict[str, float], instructions: str,
                        memories: list[str], speak: bool, allow_memory: bool) -> str:
    persona = character["persona"]
    lines = [
        f"You are {character['name']}, the user's personal desktop assistant — a helpful, quick-witted 'Jarvis' "
        f"for their Windows PC, running in an app called Juno." + (f" Pronouns: {persona.get('pronouns')}." if persona.get("pronouns") else ""),
        "Juno's integrations carry out PC actions (opening apps and files, music, volume, web searches). The app "
        "runs them for you when the user asks — lines starting with [Action] in the chat are results of that. "
        "Never claim you did something on the PC unless an [Action] line says so. If the user wants something no "
        "integration covers, say so briefly and suggest adding a phrase or integration in Juno.",
        f"Background: {persona.get('backstory', '')}",
        f"Speaking style: {persona.get('speaking_style', '')}",
        "Personality: " + "; ".join(f"{_axis_text(a, personality.get(a, 0.5))}" for a, _l, _r in PERSONALITY_AXES) + ".",
    ]
    if instructions.strip():
        lines.append("The user's own instructions for you (follow them):\n" + instructions.strip())
    if allow_memory and memories:
        lines.append("Things the user asked you to remember:\n" + "\n".join(f"- {m}" for m in memories))
    lines.append(
        "How to reply:\n"
        f"- Your replies appear in a small chat panel{' and are read aloud' if speak else ''}: write plain "
        "conversational text. No markdown, headings, bullet lists or emoji unless the user asks.\n"
        "- Start every reply with exactly one mood tag that drives your facial expression (it is hidden from "
        "the user): " + " ".join(f"[{m}]" for m in MOODS) + ".\n"
        "- You only see the screen when the user shares a screenshot. Never claim to see anything else.\n"
        "- You yourself can't click, type or run things. Juno's integrations do PC actions when the user says them "
        "as commands (\"open blender\", \"play yeat\", \"search up X on firefox\", \"type hello\"); if they ask "
        "you to do one in a roundabout way, tell them the short command to say.\n"
        "- The user often writes casually: slang, abbreviations, typos, no punctuation (\"rn\", \"ngl\", \"fr\", "
        "\"lowkey\", \"bet\", \"wyd\", \"idk\", \"lmk\", \"ts\", \"icl\"). Understand it without commenting on it, "
        "and reply naturally and relaxed. You can match their vibe a little, but don't overdo slang.\n"
        "- If you can't help, say so briefly and stay in character.")
    if allow_memory:
        lines.append(
            "- If the user tells you a lasting fact they'd want you to keep (their name, a project, a preference), "
            "you may end the reply with [remember: the fact in a few words]. The user approves it before it is "
            "saved. Never use this for passwords, money, health or other sensitive details.")
    return "\n\n".join(lines)


TAG_RE = re.compile(r"^\s*\[(" + "|".join(MOODS) + r")\]\s*", re.IGNORECASE)
REMEMBER_RE = re.compile(r"\[remember:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)


class ReplyFilter:
    """Strips the mood tag and [remember: …] tags from a streamed reply as it arrives."""

    def __init__(self) -> None:
        self.raw = ""
        self.shown = 0          # chars of `clean` already emitted
        self.expression = "neutral"
        self._tag_done = False

    def _clean(self, final: bool) -> str:
        text = self.raw
        if not self._tag_done:
            m = TAG_RE.match(text)
            if m:
                self.expression = m.group(1).lower()
                self._tag_done = True
            elif text.lstrip().startswith("[") and "]" not in text and len(text) < 30 and not final:
                return ""  # might still become a mood tag
            else:
                self._tag_done = True
        m = TAG_RE.match(text)
        if m:
            text = text[m.end():]
        text = REMEMBER_RE.sub("", text)
        if not final:  # hold back an unfinished [remember: … tag
            idx = text.lower().rfind("[remember")
            partial = text.rfind("[")
            if idx != -1 and "]" not in text[idx:]:
                text = text[:idx]
            elif partial != -1 and "]" not in text[partial:] and "[remember".startswith(text[partial:].lower()):
                text = text[:partial]
        return text

    def feed(self, chunk: str) -> str:
        self.raw += chunk
        clean = self._clean(final=False)
        new = clean[self.shown:]
        self.shown = len(clean)
        return new

    def finish(self) -> tuple[str, str, str, list[str]]:
        """Returns (remaining_text_to_show, full_clean_text, expression, memory_suggestions)."""
        clean = self._clean(final=True).rstrip()
        rest = clean[self.shown:]
        self.shown = len(clean)
        memories = [m.strip().rstrip(".")[:200] for m in REMEMBER_RE.findall(self.raw) if m.strip()]
        return rest, clean.strip(), self.expression, memories


class ProviderError(Exception):
    pass


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
    except Exception:
        msg = None
    hints = {401: "The API key was rejected — check it on Juno's Brain page.",
             403: "This API key isn't allowed to use that model.",
             404: "Model or endpoint not found — check the model name / base URL.",
             429: "Rate limit or out of credits — wait a moment or check your API billing.",
             529: "The AI service is overloaded — try again in a minute."}
    hint = hints.get(exc.code, f"HTTP {exc.code}")
    return f"{hint}" + (f" ({msg})" if msg else "")


def _sse_lines(resp: Any, cancel: threading.Event) -> Any:
    for raw in resp:
        if cancel.is_set():
            return
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            yield line[5:].strip()


def stream_anthropic(api_key: str, model: str, system: str, messages: list[dict[str, Any]],
                     on_text: Callable[[str], None], cancel: threading.Event,
                     base_url: str = "https://api.anthropic.com", low_latency: bool = True) -> None:
    def to_blocks(m: dict[str, Any]) -> Any:
        blocks: list[dict[str, Any]] = []
        for media, data in message_images(m):
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        blocks.append({"type": "text", "text": m["text"] or "(image)"})
        return blocks

    body: dict[str, Any] = {"model": model, "max_tokens": 700, "system": system, "stream": True,
                            "messages": [{"role": m["role"], "content": to_blocks(m)} for m in messages]}
    if low_latency:
        body["output_config"] = {"effort": "low"}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            for data in _sse_lines(resp, cancel):
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                    on_text(event["delta"].get("text", ""))
                elif kind == "error":
                    raise ProviderError(event.get("error", {}).get("message", "stream error"))
    except urllib.error.HTTPError as exc:
        message = _http_error_message(exc)
        if exc.code == 400 and low_latency and ("effort" in message or "output_config" in message):
            return stream_anthropic(api_key, model, system, messages, on_text, cancel, base_url, False)
        raise ProviderError(message) from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"Couldn't reach the AI service: {exc.reason}") from None
    except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
        raise ProviderError(f"The connection to the AI dropped: {exc}") from None


def stream_openai_compatible(api_key: str, model: str, system: str, messages: list[dict[str, Any]],
                             on_text: Callable[[str], None], cancel: threading.Event, base_url: str,
                             extra: dict[str, Any] | None = None, max_tokens: int = 700) -> None:
    def to_content(m: dict[str, Any]) -> Any:
        images = message_images(m)
        if not images:
            return m["text"]
        return [{"type": "text", "text": m["text"] or "(image)"}] + [
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}} for media, data in images]

    body = {"model": model, "stream": True, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] +
                        [{"role": m["role"], "content": to_content(m)} for m in messages]}
    body.update(extra or {})
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with _urlopen(req, 300) as resp:
            for data in _sse_lines(resp, cancel):
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                for choice in event.get("choices", []):
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        on_text(text)
    except urllib.error.HTTPError as exc:
        raise ProviderError(_http_error_message(exc)) from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"Couldn't reach the AI service: {exc.reason}") from None
    except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
        raise ProviderError(f"The connection to the AI dropped: {exc}") from None


def prepare_history(history: list[dict[str, Any]], limit: int = 24) -> list[dict[str, Any]]:
    """Last N turns; only the newest user message keeps its screenshot (shares are temporary)."""
    recent = [dict(m) for m in history[-limit:]]
    while recent and recent[0]["role"] != "user":
        recent.pop(0)
    last_user = max((i for i, m in enumerate(recent) if m["role"] == "user"), default=-1)
    for i, m in enumerate(recent):
        if message_images(m) and i != last_user:
            m["image"] = None
            m["images"] = []
            m["text"] = (m["text"] + " " if m["text"] else "") + "[An image was shared here earlier; it is no longer attached.]"
    return recent


# ════════════════════════════════════════════════════════════════════════════
#  Offline AI: llama.cpp server bundled in bin/, model downloaded once by the app
# ════════════════════════════════════════════════════════════════════════════

LOCAL_MODELS: dict[str, dict[str, Any]] = {
    "tiny": {"label": "Tiny", "name": "Qwen3.5 0.8B", "approx_gb": 1.0, "min_ram_gb": 4,
             "about": "Fastest, runs on almost anything. Simple chat.",
             "repos": ["unsloth/Qwen3.5-0.8B-GGUF", "ggml-org/Qwen3.5-0.8B-GGUF", "bartowski/Qwen_Qwen3.5-0.8B-GGUF"]},
    "light": {"label": "Light", "name": "Qwen3.5 2B", "approx_gb": 1.9, "min_ram_gb": 6,
              "about": "Good balance of speed and smarts.",
              "repos": ["unsloth/Qwen3.5-2B-GGUF", "ggml-org/Qwen3.5-2B-GGUF", "bartowski/Qwen_Qwen3.5-2B-GGUF"]},
    "standard": {"label": "Standard", "name": "Qwen3.5 4B", "approx_gb": 3.4, "min_ram_gb": 12,
                 "about": "Smartest; best at reading screenshots. Needs a decent PC.",
                 "repos": ["unsloth/Qwen3.5-4B-GGUF", "ggml-org/Qwen3.5-4B-GGUF", "bartowski/Qwen_Qwen3.5-4B-GGUF"]},
}
QUANT_PREFERENCE = ["Q4_K_M", "UD-Q4_K_XL", "Q4_K_S", "Q4_0", "IQ4_XS"]
MMPROJ_PREFERENCE = ["F16", "BF16", "F32"]
HF_BASE = os.environ.get("MT_HF_BASE", "https://huggingface.co")
LOCAL_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.0}


def system_ram_gb() -> float:
    if IS_WINDOWS:
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / 1024 ** 3
        except Exception:
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except Exception:
        return 8.0


def recommended_model() -> str:
    ram = system_ram_gb()
    return "standard" if ram >= 14 else "light" if ram >= 7 else "tiny"


def _is_local_url(url: str) -> bool:
    return bool(re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url))


def _urlopen(req: Any, timeout: float) -> Any:
    """Local servers are opened without any system proxy getting in the way."""
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if _is_local_url(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _hf_get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Juno/1.0"})
    with _urlopen(req, 30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_files(tree: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Choose the 4-bit model file and the vision projector from a Hugging Face file listing."""
    files = [f for f in tree if f.get("type", "file") == "file" and str(f.get("path", "")).lower().endswith(".gguf")]
    projectors = [f for f in files if f["path"].rsplit("/", 1)[-1].lower().startswith("mmproj")]
    weights = [f for f in files if f not in projectors and "/" not in f["path"]]
    model = None
    for quant in QUANT_PREFERENCE:
        hits = [f for f in weights if re.search(rf"[-_.]{re.escape(quant)}(?:[-_.][^/]*)?\.gguf$", f["path"], re.I)]
        if hits:
            model = min(hits, key=lambda f: len(f["path"]))
            break
    mmproj = None
    for kind in MMPROJ_PREFERENCE:
        hits = [f for f in projectors if re.search(rf"{kind}\.gguf$", f["path"], re.I)]
        if hits:
            mmproj = min(hits, key=lambda f: len(f["path"]))
            break
    return model, mmproj


class ModelManager:
    """Downloads a model once (resumable, SHA-256 checked) into %LOCALAPPDATA%\\Juno\\models."""

    def __init__(self, root: Path, log: Any, hf_base: str = HF_BASE):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.hf_base = hf_base.rstrip("/")
        self.downloading: str | None = None

    def folder(self, key: str) -> Path:
        return self.root / key

    def installed(self, key: str) -> dict[str, Any] | None:
        manifest = self.folder(key) / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            model = self.folder(key) / data["model"]["file"]
            if not model.is_file() or model.stat().st_size != data["model"]["size"]:
                return None
            data["model_path"] = str(model)
            if data.get("mmproj"):
                proj = self.folder(key) / data["mmproj"]["file"]
                data["mmproj_path"] = str(proj) if proj.is_file() else None
            return data
        except Exception:
            return None

    def resolve(self, key: str) -> dict[str, Any]:
        errors = []
        for repo in LOCAL_MODELS[key]["repos"]:
            try:
                tree = _hf_get_json(f"{self.hf_base}/api/models/{repo}/tree/main")
            except Exception as exc:
                errors.append(f"{repo}: {exc}")
                continue
            model, mmproj = pick_files(tree)
            if model:
                def entry(f: dict[str, Any] | None) -> dict[str, Any] | None:
                    if not f:
                        return None
                    lfs = f.get("lfs") or {}
                    return {"path": f["path"], "file": f["path"].rsplit("/", 1)[-1],
                            "size": int(lfs.get("size") or f.get("size") or 0), "sha256": lfs.get("oid")}
                return {"repo": repo, "model": entry(model), "mmproj": entry(mmproj)}
            errors.append(f"{repo}: no 4-bit GGUF found")
        raise ProviderError("Couldn't find the model online right now. " + "; ".join(errors[-2:]))

    def download(self, key: str, progress: Callable[[int, int, str], None], cancel: threading.Event) -> dict[str, Any]:
        plan = self.resolve(key)
        parts = [p for p in (plan["model"], plan["mmproj"]) if p]
        total = sum(p["size"] for p in parts)
        folder = self.folder(key)
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        have = sum((folder / (p["file"] + ".part")).stat().st_size for p in parts if (folder / (p["file"] + ".part")).exists())
        if free < (total - have) * 1.05 + 200 * 1024 ** 2:
            raise ProviderError(f"Not enough disk space: need about {(total - have) / 1024 ** 3:.1f} GB free "
                                f"on the drive with {self.root}.")
        done_before = 0
        for part in parts:
            final = folder / part["file"]
            if final.is_file() and final.stat().st_size == part["size"]:
                done_before += part["size"]
                continue
            for attempt in range(5):  # flaky Wi-Fi: resume automatically a few times
                try:
                    self._fetch(plan["repo"], part, final,
                                lambda n, base=done_before: progress(base + n, total, part["file"]), cancel)
                    break
                except (ProviderError, urllib.error.URLError, ConnectionError, TimeoutError,
                        http.client.HTTPException) as exc:
                    if cancel.is_set() or "checksum" in str(exc) or attempt == 4:
                        raise ProviderError(str(exc) if isinstance(exc, ProviderError)
                                            else f"Download interrupted ({exc}) — press Download to resume.") from None
                    self.log.info("Download interrupted (%s); retrying", exc)
                    time.sleep(2 * (attempt + 1))
            done_before += part["size"]
        manifest = {"repo": plan["repo"], "model": plan["model"], "mmproj": plan["mmproj"],
                    "name": LOCAL_MODELS[key]["name"], "downloaded": time.strftime("%Y-%m-%d")}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for stale in folder.iterdir():  # leftovers from an older model version
            if stale.name not in ("manifest.json", plan["model"]["file"], (plan["mmproj"] or {}).get("file")):
                try:
                    stale.unlink()
                except OSError:
                    pass
        return self.installed(key) or manifest

    def _fetch(self, repo: str, part: dict[str, Any], final: Path, progress: Callable[[int], None],
               cancel: threading.Event) -> None:
        import hashlib
        tmp = final.with_name(final.name + ".part")
        digest = hashlib.sha256()
        have = tmp.stat().st_size if tmp.exists() else 0
        if have > part["size"]:
            tmp.unlink()
            have = 0
        if have:
            with open(tmp, "rb") as fh:
                for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                    digest.update(block)
        url = f"{self.hf_base}/{repo}/resolve/main/{urllib.request.quote(part['path'])}"
        headers = {"User-Agent": "Juno/1.0"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with _urlopen(req, 60) as resp:
            if have and getattr(resp, "status", 200) != 206:  # server ignored the range: start over
                have = 0
                digest = hashlib.sha256()
            with open(tmp, "ab" if have else "wb") as out:
                last = 0.0
                while True:
                    if cancel.is_set():
                        raise ProviderError("Download paused — press Download again to resume.")
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    have += len(block)
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        progress(have)
        progress(have)
        if part["size"] and have != part["size"]:
            raise ProviderError("The download was interrupted — press Download again to resume.")
        if part.get("sha256") and digest.hexdigest() != part["sha256"]:
            tmp.unlink(missing_ok=True)
            raise ProviderError("The downloaded file was corrupted (checksum mismatch) — please try again.")
        os.replace(tmp, final)

    def delete(self, key: str) -> None:
        shutil.rmtree(self.folder(key), ignore_errors=True)


def attach_kill_on_close(proc: subprocess.Popen) -> Any:
    """Put the server in a Windows job object so it dies with Juno, even after a crash."""
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
        return job
    except Exception:
        return None


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalEngine:
    """Runs bin/llama-vulkan (GPU) or bin/llama-cpu llama-server.exe on 127.0.0.1 only."""

    def __init__(self, plugin: "Plugin"):
        self.p = plugin
        self.proc: subprocess.Popen | None = None
        self.job: Any = None
        self.port: int | None = None
        self.model_path: str | None = None
        self.backend: str | None = None
        self.last_used = 0.0
        self.vulkan_bad = False
        self.lock = threading.Lock()
        self.log_file = plugin.ctx.data_dir / "llama-server.log"
        self._lean: dict[str, list[str]] = {}

    def _lean_flags(self, exe: Path) -> list[str]:
        """Memory savers this llama-server build understands: no RAM prompt cache, 8-bit K cache."""
        key = str(exe)
        if key not in self._lean:
            flags: list[str] = []
            try:
                out = subprocess.run([key, "--help"], capture_output=True, text=True, timeout=20, cwd=str(exe.parent),
                                     **self.p.ctx.subprocess_kwargs())
                text = (out.stdout or "") + (out.stderr or "")
                if "--cache-ram" in text:
                    flags += ["--cache-ram", "0"]
                if "--cache-type-k" in text:
                    flags += ["--cache-type-k", "q8_0"]
            except Exception as exc:  # noqa: BLE001
                self.p.ctx.log.info("Couldn't read llama-server options: %s", exc)
            self._lean[key] = flags
        return self._lean[key]

    def exe(self, backend: str) -> Path | None:
        path = self.p.ctx.paths.bin_dir / f"llama-{backend}" / ("llama-server.exe" if IS_WINDOWS else "llama-server")
        return path if path.is_file() else None

    def available(self) -> bool:
        return bool(self.exe("vulkan") or self.exe("cpu"))

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def ensure(self, manifest: dict[str, Any], on_status: Callable[[str], None]) -> str:
        with self.lock:
            if self.running() and self.model_path == manifest["model_path"]:
                self.last_used = time.monotonic()
                return self.base_url
            self._stop_locked()
            choice = self.p.settings.get("local_backend", "auto")
            order = {"gpu": ["vulkan"], "cpu": ["cpu"]}.get(choice, ["cpu"] if self.vulkan_bad else ["vulkan", "cpu"])
            problems = []
            for backend in order:
                exe = self.exe(backend)
                if exe is None:
                    continue
                on_status("Waking up the AI on your " + ("graphics card…" if backend == "vulkan" else "processor…"))
                proc = job = port = None
                for extra in ([*self._lean_flags(exe)], []):
                    port = _free_port()
                    args = [str(exe), "-m", manifest["model_path"], "--host", "127.0.0.1", "--port", str(port),
                            "-c", "8192", "-np", "1", "--jinja", "--chat-template-kwargs",
                            '{"enable_thinking":false}', "-ngl", "99" if backend == "vulkan" else "0", *extra]
                    if manifest.get("mmproj_path"):
                        args += ["--mmproj", manifest["mmproj_path"]]
                    log = open(self.log_file, "w", encoding="utf-8", errors="replace")
                    try:
                        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                cwd=str(exe.parent), **self.p.ctx.subprocess_kwargs())
                    finally:
                        log.close()
                    job = attach_kill_on_close(proc)
                    if self._wait_ready(proc, port, 240):
                        break
                    self._kill(proc)
                    if not extra:
                        break
                    self.p.ctx.log.info("llama-server didn't like the memory-saving options; retrying without")
                    self._lean[str(exe)] = []
                if proc is not None and proc.poll() is None:
                    self.proc, self.job, self.port = proc, job, port
                    self.model_path, self.backend = manifest["model_path"], backend
                    self.last_used = time.monotonic()
                    self.p.ctx.log.info("Local AI ready on %s (port %s)", backend, port)
                    return self.base_url
                self._kill(proc)
                tail = self._log_tail()
                problems.append(f"{backend}: {tail}")
                self.p.ctx.log.warning("llama-server (%s) failed to start:\n%s", backend, tail)
                if backend == "vulkan":
                    self.vulkan_bad = True
            if not problems:
                raise ProviderError("The offline AI engine is missing from the app's bin folder — reinstall Juno.")
            raise ProviderError("The offline AI couldn't start. " + problems[-1][-300:])

    def _wait_ready(self, proc: subprocess.Popen, port: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if proc.poll() is not None:
                return False
            try:
                with _urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/health"), 2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _log_tail(self) -> str:
        try:
            lines = [l for l in self.log_file.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return " | ".join(lines[-3:])
        except OSError:
            return "no log"

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_locked(self) -> None:
        if self.proc is not None:
            self._kill(self.proc)
        if self.job is not None and IS_WINDOWS:
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.job))
            except Exception:
                pass
        self.proc = self.job = self.port = self.model_path = self.backend = None

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def crashed_on_gpu(self) -> bool:
        return self.backend == "vulkan" and self.proc is not None and self.proc.poll() is not None


# ════════════════════════════════════════════════════════════════════════════
#  Voice: Windows speech synthesis + volume-based lip sync
# ════════════════════════════════════════════════════════════════════════════

def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SpeechEngine:
    """Uses the voices built into Windows (SAPI). No downloads; falls back to PowerShell if COM is unavailable."""

    def __init__(self, log: Any):
        self.log = log
        self.voices: list[str] = []
        self.available = IS_WINDOWS and winsound is not None

    def list_voices(self) -> list[str]:
        if not self.available:
            return []
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = voice.GetVoices()
            self.voices = [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
        except Exception as exc:
            self.log.info("SAPI COM unavailable (%s); using PowerShell voice list", exc)
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                     ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"],
                    capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.voices = [v.strip() for v in out.stdout.splitlines() if v.strip()]
            except Exception:
                self.voices = []
        return self.voices

    def pick(self, explicit: str, prefer: list[str]) -> str | None:
        if explicit and explicit in self.voices:
            return explicit
        for fragment in prefer:
            for name in self.voices:
                if fragment.lower() in name.lower():
                    return name
        return self.voices[0] if self.voices else None

    def synthesize(self, text: str, voice_name: str | None, rate: int, pitch: int, volume: int, out: Path) -> Path:
        text = text.strip()[:900]
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            try:
                fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
                fmt.Type = 22  # 22 kHz 16-bit mono
                stream.Format = fmt
            except Exception:
                pass
            stream.Open(str(out), 3)  # SSFMCreateForWrite
            if voice_name:
                tokens = voice.GetVoices()
                for i in range(tokens.Count):
                    if tokens.Item(i).GetDescription() == voice_name:
                        voice.Voice = tokens.Item(i)
                        break
            voice.AudioOutputStream = stream
            voice.Rate = max(-10, min(rate, 10))
            voice.Volume = max(0, min(volume, 100))
            voice.Speak(f'<pitch absmiddle="{max(-10, min(pitch, 10))}">{_xml_escape(text)}</pitch>', 8)  # SVSFIsXML
            stream.Close()
            return out
        except Exception as exc:  # no pywin32 / COM problem → PowerShell's System.Speech
            self.log.info("SAPI COM synthesis unavailable (%s); using PowerShell", exc)
        select = f"$s.SelectVoice('{voice_name.replace(chr(39), '')}');" if voice_name else ""
        script = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;" + select +
                  f"$s.Rate={max(-10, min(rate, 10))};$s.Volume={max(0, min(volume, 100))};"
                  f"$s.SetOutputToWaveFile('{out}');$s.Speak([Console]::In.ReadToEnd());$s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], input=text,
                       encoding="utf-8", capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.exists():
            raise RuntimeError("speech synthesis produced no audio")
        return out

    @staticmethod
    def play(path: Path) -> None:
        if winsound is not None:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    @staticmethod
    def stop() -> None:
        if winsound is not None:
            try:
                winsound.PlaySound(None, 0)
            except Exception:
                pass


def wav_envelope(path: Path, window: float = 0.03) -> tuple[list[float], float]:
    """Loudness per 30 ms window, normalised 0..1, for mouth movement."""
    with wave.open(str(path), "rb") as wf:
        channels, width, rate, frames = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        raw = wf.readframes(frames)
    duration = frames / float(rate or 1)
    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - len(raw) % 2])
        if sys.byteorder == "big":
            samples.byteswap()
    elif width == 1:
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    else:
        return [], duration
    if channels > 1:
        samples = samples[::channels]
    step = max(1, int(rate * window))
    levels = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        if not chunk:
            break
        levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    peak = max(levels) if levels else 0
    if peak <= 0:
        return [0.0] * len(levels), duration
    norm = [min(1.0, lv / (peak * 0.75)) for lv in levels]
    smooth = [norm[0]] if norm else []
    for v in norm[1:]:
        smooth.append(smooth[-1] * 0.35 + v * 0.65)
    return smooth, duration


# ════════════════════════════════════════════════════════════════════════════
#  Neural voices + voice chat (sherpa-onnx, bundled; voice models downloaded in-app)
# ════════════════════════════════════════════════════════════════════════════

SHERPA_RELEASES = os.environ.get("MT_SHERPA_BASE", "https://github.com/k2-fsa/sherpa-onnx/releases/download")
HF_VOICES = os.environ.get("MT_VOICES_BASE", "https://huggingface.co/kyutai/tts-voices/resolve/main")
# Kyutai's voice recordings that Pocket TTS clones from (the same ones its own catalog uses)
POCKET_VOICE_FILES: list[tuple[str, str, str]] = [
    ("alba", "alba-mackenna/casual.wav", "Alba — warm, casual · female"),
    ("marius", "voice-donations/Selfie.wav", "Marius — relaxed · male"),
    ("javert", "voice-donations/Butter.wav", "Javert — clear, low · male"),
    ("jean", "ears/p010/freeform_speech_01.wav", "Jean — natural · male"),
    ("fantine", "vctk/p244_023.wav", "Fantine — bright · female"),
    ("eponine", "vctk/p262_023.wav", "Eponine — soft · female"),
    ("azelma", "vctk/p303_023.wav", "Azelma — light · female"),
    ("cosette", "expresso/ex04-ex02_confused_001_channel1_499s.wav", "Cosette — expressive · female"),
]
POCKET_LABEL = {key: label for key, _p, label in POCKET_VOICE_FILES}
VOICE_ADDONS: dict[str, dict[str, Any]] = {
    "voices": {
        "name": "Kokoro voices",
        "about": "28 lighter English voices (Kokoro) — fastest on older PCs. About 130 MB, one time.",
        "parts": [{"url": f"{SHERPA_RELEASES}/tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2",
                   "size": 132_353_410, "kind": "tar"}],
        "needs": ["model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"],
    },
    "natural": {
        "name": "Natural voices",
        "about": "Kyutai Pocket TTS — the most human-sounding offline voices, male and female. About 110 MB, one "
                 "time — then fully offline.",
        "parts": [{"url": f"{SHERPA_RELEASES}/tts-models/sherpa-onnx-pocket-tts-int8-2026-01-26.tar.bz2",
                   "size": 98_000_000, "kind": "tar", "keep_tests": True}] + [
            {"url": f"{HF_VOICES}/{path}", "size": 900_000, "kind": "file", "name": f"voices/{key}.wav",
             "optional": True} for key, path, _label in POCKET_VOICE_FILES],
        "needs": ["lm_flow.int8.onnx", "lm_main.int8.onnx", "encoder.onnx", "decoder.int8.onnx",
                  "text_conditioner.onnx", "vocab.json", "token_scores.json"],
    },
    "listen": {
        "name": "Voice chat",
        "about": "Offline speech recognition (Moonshine) and voice detection. About 110 MB, one time.",
        "parts": [{"url": f"{SHERPA_RELEASES}/asr-models/sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
                   "size": 107_600_538, "kind": "tar"},
                  {"url": f"{SHERPA_RELEASES}/asr-models/silero_vad.onnx", "size": 643_854, "kind": "file",
                   "name": "silero_vad.onnx"}],
        "needs": ["preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx",
                  "tokens.txt", "silero_vad.onnx"],
    },
}

# Kokoro v1.0 speaker ids (sherpa-onnx kokoro-multi-lang-v1_0): English voices only
KOKORO_VOICES: list[tuple[str, int, str]] = [
    ("af_heart", 3, "Heart — warm · US"), ("af_bella", 2, "Bella — bright · US"),
    ("af_nova", 7, "Nova — clear · US"), ("af_sky", 10, "Sky — airy · US"), ("af_sarah", 9, "Sarah — calm · US"),
    ("af_nicole", 6, "Nicole — soft, close · US"), ("af_jessica", 4, "Jessica — casual · US"),
    ("af_river", 8, "River — relaxed · US"), ("af_kore", 5, "Kore — steady · US"),
    ("af_aoede", 1, "Aoede — gentle · US"), ("af_alloy", 0, "Alloy — neutral · US"),
    ("am_michael", 16, "Michael — friendly · US"), ("am_adam", 11, "Adam — deep · US"),
    ("am_eric", 13, "Eric — confident · US"), ("am_liam", 15, "Liam — young · US"),
    ("am_onyx", 17, "Onyx — low, smooth · US"), ("am_fenrir", 14, "Fenrir — rough · US"),
    ("am_echo", 12, "Echo — narrator · US"), ("am_puck", 18, "Puck — playful · US"),
    ("am_santa", 19, "Santa — jolly · US"),
    ("bf_emma", 21, "Emma — polished · UK"), ("bf_isabella", 22, "Isabella — warm · UK"),
    ("bf_alice", 20, "Alice — crisp · UK"), ("bf_lily", 23, "Lily — light · UK"),
    ("bm_george", 26, "George — classic · UK"), ("bm_fable", 25, "Fable — storyteller · UK"),
    ("bm_daniel", 24, "Daniel — measured · UK"), ("bm_lewis", 27, "Lewis — gravelly · UK"),
]
KOKORO_SID = {key: sid for key, sid, _ in KOKORO_VOICES}
KOKORO_LABEL = {key: label for key, _, label in KOKORO_VOICES}
KOKORO_BY_LABEL = {label: key for key, _, label in KOKORO_VOICES}
SPEED_RANGE = (0.6, 1.6)

_SHERPA: dict[str, Any] = {}


def sherpa() -> Any:
    """Import sherpa-onnx lazily (it's heavy); raises with a readable message if it isn't bundled."""
    if "mod" not in _SHERPA:
        try:
            import sherpa_onnx
            _SHERPA["mod"] = sherpa_onnx
        except Exception as exc:
            _SHERPA["mod"] = None
            _SHERPA["error"] = str(exc)
    if _SHERPA["mod"] is None:
        raise RuntimeError("the voice engine (sherpa-onnx) isn't in this build — update Juno "
                           f"({_SHERPA.get('error', 'not installed')})")
    return _SHERPA["mod"]


def sherpa_ok() -> bool:
    try:
        sherpa()
        return True
    except RuntimeError:
        return False


def _make(cls: Any, kwargs: dict[str, Any], optional: tuple[str, ...] = ()) -> Any:
    """Build a sherpa config, dropping optional arguments an older/newer version doesn't accept."""
    kwargs = dict(kwargs)
    while True:
        try:
            return cls(**kwargs)
        except TypeError:
            drop = next((k for k in optional if k in kwargs), None)
            if drop is None:
                raise
            kwargs.pop(drop)


class AddonManager:
    """Downloads the optional voice models into %LOCALAPPDATA%\\Juno\\models\\voice\\<key>."""

    def __init__(self, root: Path, log: Any):
        self.root = root / "voice"
        self.log = log
        self.downloading: str | None = None

    def folder(self, key: str) -> Path:
        return self.root / key

    def installed(self, key: str) -> bool:
        folder = self.folder(key)
        return all((folder / name).exists() for name in VOICE_ADDONS[key]["needs"])

    @staticmethod
    def total(key: str) -> int:
        return sum(part["size"] for part in VOICE_ADDONS[key]["parts"])

    def download(self, key: str, progress: Callable[[int, int], None], cancel: threading.Event) -> None:
        folder = self.folder(key)
        staging = folder / ".download"
        staging.mkdir(parents=True, exist_ok=True)
        total, before = self.total(key), 0
        for part in VOICE_ADDONS[key]["parts"]:
            name = part.get("name") or part["url"].rsplit("/", 1)[-1]
            if part["kind"] == "file" and (folder / name).exists():
                before += part["size"]
                continue
            target = staging / Path(name).name
            try:
                self._fetch(part["url"], target, lambda n, b=before: progress(b + n, total), cancel)
            except ProviderError as exc:
                if not part.get("optional") or cancel.is_set():
                    raise
                self.log.info("Skipped optional download %s: %s", name, exc)
                before += part["size"]
                continue
            if part["kind"] == "tar":
                self._extract(target, folder, cancel, keep_tests=bool(part.get("keep_tests")))
                target.unlink(missing_ok=True)
            else:
                (folder / name).parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, folder / name)
            before += part["size"]
        shutil.rmtree(staging, ignore_errors=True)
        if not self.installed(key):
            raise RuntimeError("the download finished but some files are missing — try again")

    def _fetch(self, url: str, target: Path, progress: Callable[[int], None], cancel: threading.Event) -> None:
        part = target.with_name(target.name + ".part")
        for attempt in range(6):
            have = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "Juno"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with _urlopen(urllib.request.Request(url, headers=headers), 60) as resp:
                    if have and getattr(resp, "status", 200) != 206:
                        have = 0  # server ignored the range: start over
                    with open(part, "ab" if have else "wb") as fh:
                        while True:
                            if cancel.is_set():
                                raise ProviderError("Download paused — press Download again to resume.")
                            chunk = resp.read(1 << 17)
                            if not chunk:
                                break
                            fh.write(chunk)
                            have += len(chunk)
                            progress(have)
                os.replace(part, target)
                return
            except ProviderError:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 416:  # already complete
                    os.replace(part, target)
                    return
                if attempt == 5 or exc.code in (403, 404):
                    raise ProviderError(f"Download failed ({exc.code}).") from None
            except (urllib.error.URLError, OSError, ConnectionError, TimeoutError,
                    http.client.HTTPException) as exc:
                if attempt == 5:
                    raise ProviderError(f"Download failed: {exc}") from None
            time.sleep(min(2 ** attempt, 20))

    @staticmethod
    def _extract(archive: Path, dest: Path, cancel: threading.Event, keep_tests: bool = False) -> None:
        """Unpack a .tar.bz2, dropping its top folder and anything that tries to escape."""
        import tarfile
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                if cancel.is_set():
                    raise ProviderError("Download paused — press Download again to resume.")
                parts = Path(member.name).parts
                if len(parts) < 2 or any(p in ("..", "") for p in parts) or Path(member.name).is_absolute():
                    continue
                rel = Path(*parts[1:])
                if rel.parts[0] == "test_wavs" and not keep_tests:
                    continue
                out = dest / rel
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(member)
                    if src is not None:
                        with src, open(out, "wb") as fh:
                            shutil.copyfileobj(src, fh)

    def delete(self, key: str) -> None:
        shutil.rmtree(self.folder(key), ignore_errors=True)


class NeuralVoice:
    """Kokoro text-to-speech through sherpa-onnx. Loaded on first use, unloaded when Juno shuts down."""

    def __init__(self, folder: Path, log: Any):
        self.folder = folder
        self.log = log
        self._tts: Any = None
        self._lock = threading.Lock()

    def _create(self, full: bool) -> Any:
        so = sherpa()
        f = self.folder
        kokoro_kw: dict[str, Any] = {"model": str(f / "model.int8.onnx"), "voices": str(f / "voices.bin"),
                                     "tokens": str(f / "tokens.txt"), "data_dir": str(f / "espeak-ng-data")}
        if full:
            lexicons = [str(f / n) for n in ("lexicon-us-en.txt", "lexicon-zh.txt") if (f / n).exists()]
            if lexicons:
                kokoro_kw["lexicon"] = ",".join(lexicons)
            if (f / "dict").is_dir():
                kokoro_kw["dict_dir"] = str(f / "dict")
        kokoro = _make(so.OfflineTtsKokoroModelConfig, kokoro_kw, optional=("dict_dir", "lexicon"))
        threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        model = _make(so.OfflineTtsModelConfig, {"kokoro": kokoro, "num_threads": threads, "provider": "cpu",
                                                 "debug": False}, optional=("debug", "provider"))
        config = _make(so.OfflineTtsConfig, {"model": model, "max_num_sentences": 1}, optional=("max_num_sentences",))
        return so.OfflineTts(config)

    def load(self) -> Any:
        with self._lock:
            if self._tts is None:
                try:
                    self._tts = self._create(full=True)
                except Exception as exc:
                    self.log.info("Kokoro with lexicons failed (%s); trying the plain setup", exc)
                    self._tts = self._create(full=False)
            return self._tts

    last_used = 0.0

    def loaded(self) -> bool:
        return self._tts is not None

    def generate(self, text: str, voice: str, speed: float) -> tuple[bytes, int]:
        self.last_used = time.monotonic()
        tts = self.load()
        audio = tts.generate(text, sid=KOKORO_SID.get(voice, 3), speed=float(speed))
        samples, rate = audio.samples, int(audio.sample_rate)
        if samples is None or len(samples) == 0:
            raise RuntimeError("the voice engine returned no audio")
        return float_to_pcm16(samples), rate

    def unload(self) -> None:
        with self._lock:
            self._tts = None


def read_wav_float(path: Path, max_seconds: float = 12.0) -> tuple[list[float], int]:
    """Any PCM .wav → mono float samples (first max_seconds) and its sample rate."""
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        raw = wf.readframes(min(wf.getnframes(), int(rate * max_seconds)))
    if width == 2:
        data = array.array("h")
        data.frombytes(raw[: len(raw) - len(raw) % 2])
        scale = 32768.0
    elif width == 4:
        data = array.array("i")
        data.frombytes(raw[: len(raw) - len(raw) % 4])
        scale = 2147483648.0
    elif width == 1:
        return [(b - 128) / 128.0 for b in raw[::channels]], rate
    elif width == 3:
        data = array.array("i", (int.from_bytes(raw[i:i + 3], "little", signed=True) for i in
                                 range(0, len(raw) - len(raw) % 3, 3)))
        scale = 8388608.0
    else:
        raise RuntimeError("unsupported .wav format")
    if channels > 1:
        data = data[::channels]
    return [v / scale for v in data], rate


class PocketVoice:
    """Kyutai Pocket TTS through sherpa-onnx: clones a voice from a short recording. Loaded on first use."""

    def __init__(self, folder: Path, user_dir: Path, log: Any):
        self.folder = folder
        self.user_dir = user_dir          # drop your own .wav files here to add voices
        self.log = log
        self._tts: Any = None
        self._refs: dict[str, tuple[list[float], int]] = {}
        self._lock = threading.Lock()
        self.last_used = 0.0

    def voices(self) -> list[tuple[str, str]]:
        """[(key, label)] — Kyutai's voices, the samples that came with the model, then yours."""
        out = [(key, label) for key, _p, label in POCKET_VOICE_FILES if (self.folder / "voices" / f"{key}.wav").exists()]
        for wav in sorted((self.folder / "test_wavs").glob("*.wav")) if (self.folder / "test_wavs").is_dir() else []:
            out.append((f"sample:{wav.stem}", f"{wav.stem.replace('_', ' ').title()} · sample"))
        if self.user_dir.is_dir():
            for wav in sorted(self.user_dir.glob("*.wav")):
                out.append((f"mine:{wav.name}", f"{wav.stem.replace('_', ' ').title()} · your voice"))
        return out

    def path(self, key: str) -> Path | None:
        if key.startswith("file:"):
            p = Path(key[5:])
        elif key.startswith("mine:"):
            p = self.user_dir / key[5:]
        elif key.startswith("sample:"):
            p = self.folder / "test_wavs" / f"{key[7:]}.wav"
        else:
            p = self.folder / "voices" / f"{key}.wav"
        return p if p.is_file() else None

    def loaded(self) -> bool:
        return self._tts is not None

    def load(self) -> Any:
        with self._lock:
            if self._tts is None:
                so = sherpa()
                f = self.folder
                pocket = _make(so.OfflineTtsPocketModelConfig, {
                    "lm_flow": str(f / "lm_flow.int8.onnx"), "lm_main": str(f / "lm_main.int8.onnx"),
                    "encoder": str(f / "encoder.onnx"), "decoder": str(f / "decoder.int8.onnx"),
                    "text_conditioner": str(f / "text_conditioner.onnx"), "vocab_json": str(f / "vocab.json"),
                    "token_scores_json": str(f / "token_scores.json"), "voice_embedding_cache_capacity": 16},
                    optional=("voice_embedding_cache_capacity",))
                threads = max(1, min(4, (os.cpu_count() or 2) // 2))
                model = _make(so.OfflineTtsModelConfig, {"pocket": pocket, "num_threads": threads, "provider": "cpu",
                                                         "debug": False}, optional=("debug", "provider"))
                config = _make(so.OfflineTtsConfig, {"model": model, "max_num_sentences": 1},
                               optional=("max_num_sentences",))
                self._tts = so.OfflineTts(config)
            return self._tts

    def _reference(self, key: str) -> tuple[list[float], int]:
        if key not in self._refs:
            path = self.path(key) or next((self.path(k) for k, _l in self.voices() if self.path(k)), None)
            if path is None:
                raise RuntimeError("no voice recordings found — download Natural voices again")
            self._refs[key] = read_wav_float(path)
        return self._refs[key]

    def generate(self, text: str, voice: str, speed: float) -> tuple[bytes, int]:
        self.last_used = time.monotonic()
        so = sherpa()
        tts = self.load()
        ref, rate = self._reference(voice)
        gen = so.GenerationConfig()
        gen.reference_audio = ref
        gen.reference_sample_rate = rate
        try:
            gen.speed = float(speed)
        except Exception:
            pass
        try:
            audio = tts.generate(text, gen)
        except TypeError:
            if hasattr(tts, "generate_with_config"):
                audio = tts.generate_with_config(text, gen)
            else:
                audio = tts.generate(text, generation_config=gen)
        samples, out_rate = audio.samples, int(audio.sample_rate)
        if samples is None or len(samples) == 0:
            raise RuntimeError("the voice engine returned no audio")
        return float_to_pcm16(samples), out_rate

    def unload(self) -> None:
        with self._lock:
            self._tts = None
            self._refs.clear()


def float_to_pcm16(samples: Any) -> bytes:
    if np is not None:
        data = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        return (data * 32767).astype("<i2").tobytes()
    out = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def pcm_envelope(pcm: bytes, rate: int, window: float = 0.03) -> tuple[list[float], float]:
    """Loudness per 30 ms window (0..1) of mono 16-bit audio, for lip sync."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    duration = len(samples) / float(rate or 1)
    step = max(1, int(rate * window))
    levels = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step:4]  # every 4th sample is plenty for a loudness estimate
        if chunk:
            levels.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    peak = max(levels) if levels else 0
    if peak <= 0:
        return [0.0] * len(levels), duration
    norm = [min(1.0, lv / (peak * 0.75)) for lv in levels]
    smooth = [norm[0]] if norm else []
    for v in norm[1:]:
        smooth.append(smooth[-1] * 0.35 + v * 0.65)
    return smooth, duration


def read_wav_mono16(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise RuntimeError("unsupported speech audio format")
    if channels > 1:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - len(raw) % 2])
        raw = samples[::channels].tobytes()
    return raw, rate


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def split_for_speech(text: str, limit: int = 1200) -> list[str]:
    """Short first chunk (so speech starts fast), then sentence-sized chunks."""
    text = re.sub(r"[*_`#>]+", "", text).strip()[:limit]
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p and p.strip()]
    chunks: list[str] = []
    current = ""
    for part in parts:
        cap = 120 if not chunks else 260
        if current and len(current) + len(part) + 1 > cap:
            chunks.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return chunks


class AudioPlayer:
    """Plays mono 16-bit audio; blocks the calling (worker) thread and stops early on cancel."""

    def __init__(self, temp_dir: Path, log: Any):
        self.temp_dir = temp_dir
        self.log = log

    def play_blocking(self, pcm: bytes, rate: int, cancel: threading.Event) -> None:
        duration = len(pcm) / 2 / float(rate)
        if sd is not None and np is not None:
            try:
                sd.play(np.frombuffer(pcm, dtype="<i2"), rate)
                self._wait(duration, cancel, sd.stop)
                return
            except Exception as exc:
                self.log.info("sounddevice playback failed (%s); using winsound", exc)
        if winsound is not None:
            path = self.temp_dir / f"say-{uuid.uuid4().hex[:6]}.wav"
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(pcm)
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            self._wait(duration, cancel, lambda: winsound.PlaySound(None, 0))
            try:
                path.unlink()
            except OSError:
                pass
            return
        self._wait(duration, cancel, lambda: None)

    @staticmethod
    def _wait(duration: float, cancel: threading.Event, stop: Callable[[], Any]) -> None:
        end = time.monotonic() + duration + 0.04
        while time.monotonic() < end:
            if cancel.wait(0.02):
                try:
                    stop()
                except Exception:
                    pass
                return


class VoiceOut:
    """Speaks replies sentence by sentence: the next sentence is synthesised while the current one plays."""

    def __init__(self, plugin: "Plugin"):
        self.p = plugin
        self.cancel = threading.Event()
        self.active = False
        self.last_end = 0.0
        self.player = AudioPlayer(plugin.temp_dir, plugin.ctx.log)

    def stop(self) -> None:
        self.cancel.set()

    def busy(self, tail: float = 0.4) -> bool:
        return self.active or time.monotonic() - self.last_end < tail

    def say(self, text: str, on_done: Callable[[], None] | None = None) -> None:
        self.stop()
        cancel = self.cancel = threading.Event()
        config = self.p.voice_config()
        chunks = split_for_speech(text)
        if not chunks:
            return
        ui = self.p.ctx.call_in_ui
        pipe: "queue.Queue[Any]" = queue.Queue(maxsize=2)
        self.active = True

        def put(item: Any) -> bool:
            while not cancel.is_set():
                try:
                    pipe.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def produce() -> None:
            for chunk in chunks:
                if cancel.is_set():
                    break
                try:
                    item = self._synth(chunk, config)
                except Exception as exc:  # voice trouble → the next voice engine for the rest
                    if config["engine"] in ("natural", "neural"):
                        fallback = "neural" if config["engine"] == "natural" and self.p.neural_ready() else "windows"
                        self.p.ctx.log.warning("%s voice failed (%s); falling back to %s", config["engine"], exc,
                                               fallback)
                        config["engine"] = fallback
                        ui(self.p.voice_problem, str(exc))
                        try:
                            item = self._synth(chunk, config)
                        except Exception as exc2:
                            item = exc2
                    else:
                        item = exc
                if not put(item) or isinstance(item, Exception):
                    break
            put(None)

        def consume() -> None:
            try:
                while not cancel.is_set():
                    try:
                        item = pipe.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        self.p.ctx.log.warning("Speech failed: %s", item)
                        break
                    pcm, rate = item
                    envelope, duration = pcm_envelope(pcm, rate)
                    ui(self.p.avatar.start_speaking, envelope, duration)
                    self.player.play_blocking(pcm, rate, cancel)
            finally:
                self.active = False
                self.last_end = time.monotonic()
                ui(self.p.avatar.stop_speaking)
                if on_done is not None:
                    ui(on_done)

        threading.Thread(target=produce, daemon=True, name="juno-tts").start()
        threading.Thread(target=consume, daemon=True, name="juno-play").start()

    def _synth(self, text: str, config: dict[str, Any]) -> tuple[bytes, int]:
        if config["engine"] == "natural":
            return self.p.natural.generate(text, config["natural"], config["speed"])
        if config["engine"] == "neural":
            return self.p.neural.generate(text, config["voice"], config["speed"])
        out = self.p.temp_dir / f"sapi-{uuid.uuid4().hex[:6]}.wav"
        try:
            self.p.speech.synthesize(text, config["sapi_voice"], config["sapi_rate"], config["sapi_pitch"],
                                     config["sapi_volume"], out)
            return read_wav_mono16(out)
        finally:
            try:
                out.unlink()
            except OSError:
                pass


class VoiceChat:
    """Hands-free conversation: microphone → voice detection → offline speech recognition → chat.
    Only runs while the user has the mic switched on; audio is processed in memory and never saved."""

    RATE = 16000

    def __init__(self, plugin: "Plugin"):
        self.p = plugin
        self.running = False
        self.muted = False
        self.state = "off"           # off | loading | listening | hearing | paused | muted
        self.off_since = time.monotonic()
        self._stream: Any = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._recognizer: Any = None
        self._vad_path: Path | None = None
        self._thread: threading.Thread | None = None

    def available(self) -> tuple[bool, str]:
        if sd is None or np is None:
            return False, "microphone support isn't in this build — update Juno"
        if not sherpa_ok():
            return False, "the voice engine isn't in this build — update Juno"
        if not self.p.addons.installed("listen"):
            return False, "download"
        return True, ""

    def start(self, on_ready: Callable[[], None], on_error: Callable[[str], None]) -> None:
        if self.running:
            return
        self.running = True
        self._set_state("loading")
        self._queue = queue.Queue()

        def boot() -> None:
            try:
                self._load()
                self._stream = sd.InputStream(samplerate=self.RATE, channels=1, dtype="float32", blocksize=1600,
                                              callback=self._callback)
                self._stream.start()
            except Exception as exc:
                self.running = False
                self._close_stream()
                self._set_state("off")
                self.p.ctx.call_in_ui(on_error, f"Couldn't start the microphone: {exc}")
                return
            self.p.ctx.call_in_ui(on_ready)
            self._loop()

        self._thread = threading.Thread(target=boot, daemon=True, name="juno-mic")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._close_stream()
        self._set_state("off")

    def unload(self) -> None:
        self.stop()
        self._recognizer = None

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _callback(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        if self.running:
            self._queue.put(indata[:, 0].copy())

    def _load(self) -> None:
        if self._recognizer is None:
            so = sherpa()
            f = self.p.addons.folder("listen")
            self._recognizer = so.OfflineRecognizer.from_moonshine(
                preprocessor=str(f / "preprocess.onnx"), encoder=str(f / "encode.int8.onnx"),
                uncached_decoder=str(f / "uncached_decode.int8.onnx"),
                cached_decoder=str(f / "cached_decode.int8.onnx"), tokens=str(f / "tokens.txt"),
                num_threads=max(1, min(4, (os.cpu_count() or 2) // 2)))
        self._vad_path = self.p.addons.folder("listen") / "silero_vad.onnx"

    def _make_vad(self) -> Any:
        so = sherpa()
        config = so.VadModelConfig()
        config.silero_vad.model = str(self._vad_path)
        for attr, value in (("threshold", 0.5), ("min_silence_duration", 0.55), ("min_speech_duration", 0.25),
                            ("max_speech_duration", 20.0)):
            try:
                setattr(config.silero_vad, attr, value)
            except Exception:
                pass
        config.sample_rate = self.RATE
        return so.VoiceActivityDetector(config, buffer_size_in_seconds=30)

    def _paused(self) -> bool:
        return self.muted or self.p.voice_out.busy() or self.p._busy

    def _loop(self) -> None:
        vad = self._make_vad()
        window = 512  # silero VAD frame size at 16 kHz
        buffer = np.zeros(0, dtype=np.float32)
        was_paused = False
        heard = False
        while self.running:
            try:
                block = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._paused():
                if not was_paused:
                    vad = self._make_vad()  # forget anything half-heard (e.g. Juno's own voice)
                    buffer = np.zeros(0, dtype=np.float32)
                    was_paused = True
                    self._set_state("muted" if self.muted else "paused")
                continue
            if was_paused:
                was_paused = False
                self._set_state("listening")
            buffer = np.concatenate([buffer, block])
            while len(buffer) >= window:
                vad.accept_waveform(buffer[:window])
                buffer = buffer[window:]
            speaking = bool(vad.is_speech_detected())
            if speaking != heard:
                heard = speaking
                self._set_state("hearing" if speaking else "listening")
            while not vad.empty():
                segment = vad.front.samples
                vad.pop()
                if len(segment) < self.RATE * 0.35:
                    continue
                text = self._recognize(segment)
                if text:
                    self.p.ctx.call_in_ui(self.p.on_heard, text)

    def _recognize(self, samples: Any) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.RATE, samples)
        self._recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        if not re.search(r"[A-Za-z0-9]", text) or text.lower().strip(" .!?") in ("", "you", "uh", "um", "hmm"):
            return ""
        return text

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.off_since = time.monotonic() if state == "off" else 0.0
            try:
                self.p.ctx.call_in_ui(self.p.chat.refresh_mic)
            except Exception:
                pass


def message_images(message: dict[str, Any]) -> list[tuple[str, str]]:
    images = list(message.get("images") or [])
    if message.get("image"):
        images.insert(0, message["image"])
    return images


# ════════════════════════════════════════════════════════════════════════════
#  Screen sharing (only ever on an explicit click)
# ════════════════════════════════════════════════════════════════════════════

def work_area(root: Any) -> tuple[int, int, int, int]:
    if IS_WINDOWS:
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    try:
        return 0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight())
    except Exception:
        return 0, 0, 1920, 1080


def list_windows() -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """Visible top-level windows of OTHER apps: (hwnd, title, rect)."""
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    results: list[tuple[int, str, tuple[int, int, int, int]]] = []
    own_pid = os.getpid()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: Any, _lparam: Any) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title in ("Program Manager", "Windows Input Experience"):
            return True
        rect = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 120 or rect.bottom - rect.top < 80:
            return True
        results.append((int(hwnd or 0), title, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(callback, 0)
    return results


def capture_window(hwnd: int, rect: tuple[int, int, int, int]) -> Any:
    """PrintWindow renders the window even if it's partly covered; falls back to a screen grab of its rect."""
    if Image is None:
        raise RuntimeError("Pillow is missing")
    if IS_WINDOWS:
        try:
            from ctypes import wintypes
            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            user32.GetWindowDC.restype = wintypes.HDC
            user32.GetWindowDC.argtypes = [wintypes.HWND]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
            gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
            win_rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(win_rect))
            w, h = win_rect.right - win_rect.left, win_rect.bottom - win_rect.top
            hwnd_dc = user32.GetWindowDC(hwnd)
            mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
            bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
            old = gdi32.SelectObject(mem_dc, bmp)
            ok = user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(header), 0)
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hwnd_dc)
            if ok:
                img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
                # crop the invisible resize borders down to the visible frame
                left, top = rect[0] - win_rect.left, rect[1] - win_rect.top
                img = img.crop((max(0, left), max(0, top), max(0, left) + (rect[2] - rect[0]),
                                max(0, top) + (rect[3] - rect[1])))
                if img.getbbox() is not None:
                    return img
        except Exception:
            pass
    return ImageGrab.grab(bbox=rect, all_screens=True)


def encode_image(img: Any) -> tuple[str, str]:
    img = img.convert("RGB")
    img.thumbnail((1568, 1568))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


# ════════════════════════════════════════════════════════════════════════════
#  Avatar rendering
# ════════════════════════════════════════════════════════════════════════════

class Pen:
    """Draws in the character's 160×240 design space, scaled and optionally mirrored."""

    def __init__(self, canvas: Any, scale: float, ox: float, oy: float, facing: int, outline: str):
        self.c, self.s, self.ox, self.oy, self.f, self.outline = canvas, scale, ox, oy, facing, outline

    def X(self, x: float) -> float:
        return self.ox + self.s * (x if self.f > 0 else 160 - x)

    def Y(self, y: float) -> float:
        return self.oy + self.s * y

    def pts(self, pts: list[tuple[float, float]], dx: float = 0, dy: float = 0) -> list[float]:
        out: list[float] = []
        for x, y in pts:
            out += [self.X(x + dx), self.Y(y + dy)]
        return out

    def poly(self, pts: list[tuple[float, float]], fill: str, dx: float = 0, dy: float = 0, smooth: bool = False,
             outline: bool = True, width: float = 2) -> None:
        self.c.create_polygon(*self.pts(pts, dx, dy), fill=fill, smooth=smooth,
                              outline=self.outline if outline else "", width=width * self.s if outline else 0)

    def oval(self, x0: float, y0: float, x1: float, y1: float, fill: str, dx: float = 0, dy: float = 0,
             outline: bool = True, width: float = 1.5) -> None:
        a, b = self.X(x0 + dx), self.X(x1 + dx)
        self.c.create_oval(min(a, b), self.Y(y0 + dy), max(a, b), self.Y(y1 + dy), fill=fill,
                           outline=self.outline if outline else "", width=width * self.s if outline else 0)

    def circle(self, cx: float, cy: float, r: float, fill: str, dx: float = 0, dy: float = 0,
               outline: bool = False) -> None:
        self.oval(cx - r, cy - r, cx + r, cy + r, fill, dx, dy, outline)

    def line(self, pts: list[tuple[float, float]], fill: str, width: float, dx: float = 0, dy: float = 0,
             smooth: bool = False) -> None:
        self.c.create_line(*self.pts(pts, dx, dy), fill=fill, width=max(1.0, width * self.s), smooth=smooth,
                           capstyle="round", joinstyle="round")


ARM_POSES = {  # (elbow, hand) for the right arm in design space; left arm is mirrored around x=80
    "rest": ((116, 148), (114, 171)),
    "wave": ((128, 112), (134, 86)),
    "think": ((114, 146), (92, 105)),
    "stretch": ((122, 96), (110, 62)),
    "adjust": ((112, 150), (104, 160)),
    "explain": ((124, 140), (128, 124)),
}
HAIR_BACK = {
    "bob": [(38, 60), (40, 32), (58, 16), (80, 12), (102, 16), (120, 32), (122, 60), (123, 96), (112, 104),
            (104, 92), (56, 92), (48, 104), (37, 96)],
    "long": [(36, 60), (38, 30), (58, 14), (80, 10), (102, 14), (122, 30), (124, 60), (128, 140), (110, 146),
             (104, 100), (56, 100), (50, 146), (32, 140)],
    "spiky": [(42, 58), (44, 34), (62, 22), (98, 22), (116, 34), (118, 58)],
    "buzz": [(45, 58), (46, 36), (62, 24), (98, 24), (114, 36), (115, 58)],
}
HAIR_FRONT = {
    "bob": [(44, 64), (42, 40), (54, 24), (72, 17), (92, 17), (110, 26), (119, 42), (119, 68), (111, 48),
            (103, 40), (96, 53), (86, 39), (76, 50), (66, 37), (56, 54), (50, 45)],
    "long": [(44, 70), (42, 40), (54, 23), (72, 16), (92, 16), (110, 25), (119, 42), (120, 74), (112, 50),
             (100, 38), (90, 50), (80, 36), (68, 48), (56, 38), (50, 52)],
    "spiky": [(44, 62), (39, 40), (28, 31), (49, 30), (47, 15), (64, 24), (70, 5), (82, 20), (96, 7), (100, 24),
              (119, 14), (113, 32), (127, 37), (117, 49), (116, 64), (106, 46), (92, 52), (84, 40), (72, 50),
              (62, 40), (52, 52)],
    "buzz": [(46, 56), (47, 38), (62, 27), (98, 27), (113, 38), (114, 56), (104, 44), (80, 40), (56, 44)],
}


def draw_procedural(c: Any, look: dict[str, Any], st: dict[str, float], scale: float, ox: float, oy: float,
                    facing: int) -> None:
    pen = Pen(c, scale, ox, oy, facing, look["outline"])
    L = look
    breath = st["breath"] * 1.6
    sway = st["sway"]
    hop = st["hop"]
    bob = st["bob"]
    bdx, bdy = sway, -breath + bob + hop             # upper body offset
    hdx = st["head_dx"] + sway * 0.6 + st["look_x"] * facing * 1.6
    hdy = st["head_dy"] - breath * 0.8 + bob + hop + st["look_y"] * 1.2
    style = L.get("hair_style", "bob")

    # hair (back layer)
    pen.poly(HAIR_BACK[style], L["hair"], hdx, hdy, smooth=style not in ("spiky",))

    # legs + shoes
    phase = st["walk_phase"]
    for side, hip_x in ((-1, 71), (1, 89)):
        swing = math.sin(phase + (0 if side < 0 else math.pi)) * 7 * st["walking"]
        lift = max(0.0, math.cos(phase + (0 if side < 0 else math.pi))) * 4 * st["walking"]
        foot = (hip_x - 1 * side + swing, 226 - lift + hop)
        hip = (hip_x + sway * 0.5, 170 + hop)
        pen.line([hip, foot], L["outline"], 16)
        pen.line([hip, foot], L["pants"], 12)
        pen.oval(foot[0] - 9, foot[1] - 4, foot[0] + 8, foot[1] + 6, L["shoes"])

    def arm(side: int, pose: str, amount: float) -> None:
        rest_e, rest_h = ARM_POSES["rest"]
        pose_e, pose_h = ARM_POSES.get(pose, ARM_POSES["rest"])
        wave = math.sin(time.monotonic() * 9) * 6 if pose == "wave" else 0
        ex = rest_e[0] + (pose_e[0] - rest_e[0]) * amount
        ey = rest_e[1] + (pose_e[1] - rest_e[1]) * amount
        hx = rest_h[0] + (pose_h[0] + wave - rest_h[0]) * amount
        hy = rest_h[1] + (pose_h[1] - rest_h[1]) * amount
        if side < 0:  # mirror around the body centre
            ex, hx = 160 - ex, 160 - hx
        shoulder = (110 if side > 0 else 50, 124)
        pts = [shoulder, (ex, ey), (hx, hy)]
        pen.line(pts, L["outline"], 15, bdx, bdy)
        pen.line(pts, L["jacket"], 11, bdx, bdy)
        pen.line([(hx - 3, hy - 4), (hx + 3, hy - 4)], L["jacket_trim"], 2, bdx, bdy)
        pen.circle(hx, hy + 1, 5.5, L["skin"], bdx, bdy, outline=True)

    arm(-1, st["arm_l_pose"], st["arm_l_amt"])

    # neck, torso, collar
    pen.poly([(72, 94), (88, 94), (88, 116), (72, 116)], L["skin_shade"], hdx * 0.5 + bdx * 0.5, bdy)
    pen.poly([(56, 114), (104, 114), (116, 126), (110, 176), (50, 176), (44, 126)], L["jacket"], bdx, bdy)
    pen.poly([(70, 116), (90, 116), (86, 175), (74, 175)], L["shirt"], bdx, bdy, outline=False)
    pen.line([(70, 116), (74, 175)], L["jacket_trim"], 2, bdx, bdy)
    pen.line([(90, 116), (86, 175)], L["jacket_trim"], 2, bdx, bdy)
    pen.line([(51, 169), (109, 169)], L["outline"], 2.5, bdx, bdy)
    pen.poly([(63, 103), (97, 103), (101, 118), (80, 124), (59, 118)], L["jacket"], bdx, bdy)
    pen.line([(63, 103), (97, 103)], L["jacket_trim"], 2, bdx, bdy)

    arm(1, st["arm_r_pose"], st["arm_r_amt"])

    # head
    pen.oval(40, 62, 51, 81, L["skin"], hdx, hdy)
    pen.oval(109, 62, 120, 81, L["skin"], hdx, hdy)
    face = [(46, 62), (48, 40), (64, 30), (96, 30), (112, 40), (114, 62), (110, 84), (96, 100), (80, 106),
            (64, 100), (50, 84)]
    pen.poly(face, L["skin"], hdx, hdy, smooth=True)

    if st["blush"] > 0.08:
        r = 2.5 + st["blush"] * 2.5
        pen.oval(62 - r * 1.4, 84 - r * 0.6, 62 + r * 1.4, 84 + r * 0.6, L["blush"], hdx, hdy, outline=False)
        pen.oval(98 - r * 1.4, 84 - r * 0.6, 98 + r * 1.4, 84 + r * 0.6, L["blush"], hdx, hdy, outline=False)

    # eyes
    lx, ly = st["look_x"] * facing, st["look_y"]
    openness = max(0.0, st["eye_open"] * st["blink"])
    for cx in (66, 94):
        cy = 72
        rx, ry = 8.5, 6.4 * min(openness, 1.15)
        if ry < 1.0:
            pen.line([(cx - 8, cy), (cx, cy + 2), (cx + 8, cy)], L["outline"], 2.4, hdx, hdy, smooth=True)
            continue
        pen.oval(cx - rx, cy - ry, cx + rx, cy + ry, "#f6f3ef", hdx, hdy, width=1.6)
        ir = min(4.8, ry + 0.4)
        ix, iy = cx + lx * 3.2, cy + ly * min(2.2, ry * 0.4)
        pen.circle(ix, iy, ir, L["eyes"], hdx, hdy)
        pen.circle(ix, iy, ir * 0.45, "#05080c", hdx, hdy)
        pen.circle(ix - ir * 0.35, iy - ir * 0.4, max(0.8, ir * 0.25), "#ffffff", hdx, hdy)
        pen.line([(cx - rx - 0.5, cy - ry * 0.55), (cx, cy - ry - 0.6), (cx + rx + 0.5, cy - ry * 0.55)],
                 L["outline"], 2.6, hdx, hdy, smooth=True)
        if st["squint"] > 0.05:
            top = cy + ry * (1 - st["squint"] * 1.1)
            pen.poly([(cx - rx - 1, top), (cx + rx + 1, top), (cx + rx + 1, cy + ry + 1.5), (cx - rx - 1, cy + ry + 1.5)],
                     L["skin"], hdx, hdy, outline=False)
            pen.line([(cx - rx + 1, top + 0.5), (cx + rx - 1, top + 0.5)], L["skin_shade"], 1.4, hdx, hdy)

    # brows
    raise_ = st["brow_raise"] * 4
    ang = st["brow_angle"] * 3.5
    asym = st["brow_asym"] * 4
    by = 60
    pen.line([(56, by - raise_ + ang * 0.5), (73, by - raise_ - ang)], L["brows"], 3, hdx, hdy)
    pen.line([(87, by - raise_ - ang - asym), (104, by - raise_ + ang * 0.5 - asym)], L["brows"], 3, hdx, hdy)

    # nose + mouth
    pen.line([(80, 78), (82.5, 83), (79, 84)], L["skin_shade"], 1.6, hdx, hdy)
    mo = st["mouth_open"]
    if mo > 0.06:
        w = 6.5 + mo * 3.5
        h = 1.5 + mo * 7.5
        pen.oval(80 - w, 90 - h * 0.35, 80 + w, 90 + h, "#3b1219", hdx, hdy, width=1.4)
        if h > 4.5:
            pen.oval(80 - w * 0.55, 90 + h * 0.35, 80 + w * 0.55, 90 + h * 0.9, "#c4596a", hdx, hdy, outline=False)
        if mo > 0.45:
            pen.line([(80 - w * 0.6, 90 - h * 0.35 + 1.4), (80 + w * 0.6, 90 - h * 0.35 + 1.4)], "#f1ece8", 1.8,
                     hdx, hdy)
    else:
        curve = st["mouth_curve"]
        pen.line([(71.5, 90 - curve * 1.6), (80, 90 + curve * 3.6), (88.5, 90 - curve * 1.6)], L["lips"], 2.3,
                 hdx, hdy, smooth=True)

    # cyberware: temple implant + cheek circuit
    pen.line([(111, 50), (113, 62)], L["cyberware"], 2, hdx, hdy)
    pen.circle(113.2, 65.5, 1.4, L["cyberware"], hdx, hdy)
    pen.line([(100, 88), (104, 92), (108, 92)], L["cyberware"], 1.3, hdx, hdy)

    # hair (front layer) + highlight streak
    pen.poly(HAIR_FRONT[style], L["hair"], hdx, hdy, smooth=style not in ("spiky",))
    if style in ("bob", "long"):
        pen.line([(98, 21), (106, 33), (112, 52), (114, 64)], L["hair_highlight"], 3.2, hdx, hdy, smooth=True)
    elif style == "spiky":
        pen.line([(70, 8), (76, 24), (80, 32)], L["hair_highlight"], 3, hdx, hdy)
    else:
        pen.line([(62, 30), (98, 30)], L["hair_highlight"], 2, hdx, hdy)


class SpriteRig:
    """Character made from PNG layers (see README in the exported template)."""

    def __init__(self, folder: Path, look: dict[str, Any], scale: float, log: Any):
        from PIL import ImageTk  # needs real Tk
        self._ImageTk = ImageTk
        self.folder = folder
        self.look = look
        self.scale = scale
        self.log = log
        self.size = look.get("size") or [180, 240]
        self.layers = [l for l in look.get("layers", []) if isinstance(l, dict)]
        self._cache: dict[tuple[str, str, int], Any] = {}

    def _image(self, filename: str, facing: int) -> Any:
        key = (filename, "", facing)
        if key in self._cache:
            return self._cache[key]
        path = (self.folder / filename).resolve()
        if self.folder.resolve() not in path.parents or not path.is_file():
            self._cache[key] = None
            return None
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        sx = 160 / max(1, self.size[0]) * self.scale
        img = img.resize((max(1, int(w * sx)), max(1, int(h * sx))), Image.LANCZOS)
        alpha = img.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
        img.putalpha(alpha)
        if facing < 0:
            img = ImageOps.mirror(img)
        photo = self._ImageTk.PhotoImage(img)
        self._cache[key] = photo
        return photo

    @staticmethod
    def state_for(layer: dict[str, Any], st: dict[str, float], expression: str) -> str | None:
        states = layer.get("states")
        if not isinstance(states, dict):
            return None
        kind = layer.get("kind")
        if kind == "eyes":
            openness = st["eye_open"] * st["blink"]
            order = ["open"] if openness > 0.7 else ["half", "open"] if openness > 0.3 else ["closed", "half", "open"]
        elif kind == "mouth":
            mo = st["mouth_open"]
            if mo > 0.06:
                order = ["wide", "open", "slight"] if mo > 0.7 else ["open", "wide", "slight"] if mo > 0.35 \
                    else ["slight", "open", "closed"]
            elif st["mouth_curve"] > 0.4:
                order = ["smile", "closed"]
            elif st["mouth_curve"] < -0.3:
                order = ["frown", "closed"]
            else:
                order = ["closed"]
            order.append("closed")
        elif kind == "brows":
            order = [expression, "neutral"]
        else:
            order = [expression, "neutral", "default"]
        for name in order:
            if name in states:
                return name
        return next(iter(states), None)

    def draw(self, c: Any, st: dict[str, float], scale: float, ox: float, oy: float, facing: int,
             expression: str) -> None:
        unit = 160 / max(1, self.size[0]) * scale
        breath = st["breath"] * 1.6
        body_dx, body_dy = st["sway"], -breath + st["bob"] + st["hop"]
        head_dx = body_dx + st["head_dx"] + st["look_x"] * facing * 1.6
        head_dy = body_dy + st["head_dy"] + st["look_y"] * 1.2
        for layer in self.layers:
            state = self.state_for(layer, st, expression)
            filename = layer["states"][state] if state else layer.get("image")
            if not filename:
                continue
            photo = self._image(str(filename), facing)
            if photo is None:
                continue
            follow = layer.get("follow", "body")
            dx, dy = (head_dx, head_dy) if follow == "head" else (body_dx, body_dy) if follow == "body" else (0, 0)
            lookv = layer.get("look") or [0, 0]
            dx += st["look_x"] * facing * float(lookv[0])
            dy += st["look_y"] * float(lookv[1])
            x = float(layer.get("x", 0)) * unit + dx * scale
            y = float(layer.get("y", 0)) * unit + dy * scale
            if facing < 0:
                x = self.size[0] * unit - x - photo.width()
            c.create_image(ox + x, oy + y, image=photo, anchor="nw")


# ════════════════════════════════════════════════════════════════════════════
#  Avatar: overlay window, behaviour scheduler, movement
# ════════════════════════════════════════════════════════════════════════════

FACE_KEYS = ("brow_raise", "brow_angle", "brow_asym", "eye_open", "squint", "mouth_curve", "blush")
DEFAULT_STATE: dict[str, Any] = {
    "breath": 0.0, "sway": 0.0, "bob": 0.0, "hop": 0.0, "head_dx": 0.0, "head_dy": 0.0,
    "look_x": 0.0, "look_y": 0.0, "blink": 1.0, "mouth_open": 0.0, "walk_phase": 0.0, "walking": 0.0,
    "arm_l_pose": "rest", "arm_l_amt": 0.0, "arm_r_pose": "rest", "arm_r_amt": 0.0,
    **DEFAULT_EXPRESSIONS["neutral"],
}
ACTIVITY = {  # (min gap, max gap, chance to skip an idle action)
    "quiet": (9.0, 18.0, 0.5),
    "occasional": (4.0, 9.0, 0.25),
    "chatty": (2.5, 6.0, 0.1),
}


def approach(current: float, target: float, rate: float, dt: float) -> float:
    return current + (target - current) * (1 - math.exp(-rate * dt))


class Avatar:
    DESIGN_W, DESIGN_H, BUBBLE_H, MARGIN = 160, 240, 80, 10

    def __init__(self, plugin: "Plugin"):
        self.p = plugin
        self.app = plugin.ctx.app
        self.win: Any = None
        self.canvas: Any = None
        self.rig: Any = None
        self.character: dict[str, Any] = {}
        self.scale = 1.0
        self.st: dict[str, Any] = dict(DEFAULT_STATE)
        self.expression = "neutral"
        self.expression_until = 0.0
        self.mode = "idle"          # idle | thinking | speaking
        self.action: tuple[str, float, dict[str, Any]] | None = None
        self.cooldowns: dict[str, float] = {}
        now = time.monotonic()
        self.next_idle = now + 3
        self.next_blink = now + 1.5
        self.blink_start = -1.0
        self.double_blink = False
        self.speech: tuple[list[float], float, float] | None = None
        self.bubble: tuple[str, float] | None = None
        self.stroll: dict[str, Any] | None = None
        self.facing = -1
        self.x = self.y = 0
        self.visible = False
        self._after: Any = None
        self._last = now
        self.drag: dict[str, Any] | None = None
        self.panel_side = 0          # -1 panel is left of the avatar, +1 right, 0 closed
        self.pose_r: tuple[str, float] = ("rest", 0.0)
        self.pose_l: tuple[str, float] = ("rest", 0.0)
        self.hop_until = 0.0

    # ── window ────────────────────────────────────────────────────────────
    @property
    def size(self) -> tuple[int, int]:
        s = self.scale
        return (int((self.DESIGN_W + 2 * self.MARGIN) * s), int((self.DESIGN_H + self.BUBBLE_H) * s))

    def build(self) -> None:
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.app)
        self.win._mt_role = "skip"  # theme engine must never recolour the overlay
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.configure(bg=TRANSPARENT_KEY)
            self.win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            pass
        w, h = self.size
        self.canvas = tk.Canvas(self.win, width=w, height=h, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas._mt_role = "skip"
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)

    def set_character(self, character: dict[str, Any], folder: Path | None, size_factor: float) -> None:
        self.character = character
        self.scale = size_factor * float(character["look"].get("scale", 1.0))
        self.rig = None
        if character["look"].get("rig") == "sprite" and folder is not None:
            try:
                self.rig = SpriteRig(folder, character["look"], self.scale, self.p.ctx.log)
            except Exception as exc:
                self.p.ctx.log.warning("Sprite rig failed (%s); using the drawn body", exc)
        if self.canvas is not None:
            w, h = self.size
            self.canvas.configure(width=w, height=h)
            self.place(self.x, self.y, clamp=True)
        self._schedule_blink(time.monotonic())

    def home_position(self) -> tuple[int, int]:
        left, top, right, bottom = work_area(self.app)
        w, h = self.size
        home = self.p.settings.get("home")
        if isinstance(home, list) and len(home) == 2:
            return int(home[0]), int(home[1])
        if self.p.settings.get("anchor") == "left":
            return left + 16, bottom - h
        return right - w - 16, bottom - h

    def place(self, x: int, y: int, clamp: bool = False) -> None:
        if clamp:
            left, top, right, bottom = work_area(self.app)
            w, h = self.size
            x = max(left - w // 3, min(x, right - (2 * w) // 3))
            y = max(top - h // 3, min(y, bottom - h))
        self.x, self.y = int(x), int(y)
        if self.win is not None:
            w, h = self.size
            self.win.geometry(f"{w}x{h}+{self.x}+{self.y}")

    def show(self) -> None:
        self.build()
        x, y = self.home_position()
        self.place(x, y, clamp=True)
        left, _t, right, _b = work_area(self.app)
        self.facing = -1 if self.x > (left + right) / 2 else 1
        self.win.deiconify()
        self.visible = True
        if self._after is None:
            self._last = time.monotonic()
            self._after = self.app.after(33, self.tick)

    def hide(self) -> None:
        self.visible = False
        if self.win is not None:
            self.win.withdraw()
        if self._after is not None:
            try:
                self.app.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def destroy(self) -> None:
        self.hide()
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    # ── public reactions ──────────────────────────────────────────────────
    def set_expression(self, mood: str, seconds: float = 6.0) -> None:
        self.expression = mood if mood in self.character.get("expressions", DEFAULT_EXPRESSIONS) else "neutral"
        self.expression_until = time.monotonic() + seconds

    def say(self, text: str, seconds: float = 6.0) -> None:
        text = " ".join(text.split())
        self.bubble = (text if len(text) <= 140 else text[:138] + "…", time.monotonic() + seconds)

    def gesture(self, pose: str, seconds: float, arm: str = "r") -> None:
        until = time.monotonic() + seconds
        if arm in ("r", "both"):
            self.pose_r = (pose, until)
        if arm in ("l", "both"):
            self.pose_l = (pose, until)

    def hop(self) -> None:
        self.hop_until = time.monotonic() + 0.18

    def start_speaking(self, envelope: list[float], duration: float) -> None:
        self.speech = (envelope, time.monotonic(), duration)
        self.mode = "speaking"

    def stop_speaking(self) -> None:
        self.speech = None
        if self.mode == "speaking":
            self.mode = "idle"

    # ── input ─────────────────────────────────────────────────────────────
    def _on_press(self, event: Any) -> None:
        self.drag = {"sx": event.x_root, "sy": event.y_root, "wx": self.x, "wy": self.y, "moved": False}

    def _on_drag(self, event: Any) -> None:
        if not self.drag:
            return
        dx, dy = event.x_root - self.drag["sx"], event.y_root - self.drag["sy"]
        if abs(dx) + abs(dy) > 4:
            self.drag["moved"] = True
            self.stroll = None
            self.place(self.drag["wx"] + dx, self.drag["wy"] + dy)
            self.set_expression("curious", 1.5)

    def _on_release(self, _event: Any) -> None:
        drag, self.drag = self.drag, None
        if drag and drag["moved"]:
            self.place(self.x, self.y, clamp=True)
            self.p.set_setting("home", [self.x, self.y])
            left, _t, right, _b = work_area(self.app)
            self.facing = -1 if self.x > (left + right) / 2 else 1
            self.p.chat.reposition()
        else:
            self.hop()
            self.set_expression("amused", 1.2)
            self.p.chat.toggle()

    def _on_menu(self, event: Any) -> None:
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="Chat", command=self.p.chat.show)
        if self.p.perm("screen"):
            menu.add_command(label="Share a screenshot…", command=lambda: self.p.share_screen(open_chat=True))
        menu.add_separator()
        dnd = bool(self.p.settings.get("dnd"))
        menu.add_command(label=("Turn off" if dnd else "Turn on") + " Do Not Disturb", command=self.p.toggle_dnd)
        menu.add_command(label="Back to my corner", command=self.p.reset_home)
        menu.add_command(label=("Stop" if self.p.listener.state != "off" else "Start") + " voice chat",
                         command=self.p.toggle_voice_chat)
        menu.add_command(label="Unmute voice" if self.p.settings.get("muted") else "Mute voice",
                         command=self.p.toggle_mute)
        menu.add_command(label="Companion settings", command=self.p.open_settings_page)
        menu.add_command(label="Hide (keep running)", command=self.p.hide_avatar)
        menu.add_separator()
        menu.add_command(label=f"Shut down {self.p.character['name']}", command=self.p.power_off)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ── behaviour ─────────────────────────────────────────────────────────
    def _anim(self) -> dict[str, Any]:
        return self.character.get("animation", DEFAULT_CHARACTER["animation"])

    def _schedule_blink(self, now: float) -> None:
        lo, hi = (self._anim().get("blink_interval") or [2.2, 5.5])[:2]
        self.next_blink = now + random.uniform(float(lo), float(hi))

    def _activity(self) -> tuple[float, float, float]:
        level = "quiet" if self.p.settings.get("dnd") else self.p.settings.get("activity", "occasional")
        return ACTIVITY.get(level, ACTIVITY["occasional"])

    def _pick_idle_action(self, now: float) -> None:
        lo, hi, skip = self._activity()
        self.next_idle = now + random.uniform(lo, hi)
        if random.random() < skip:
            return
        reduced = bool(self.p.settings.get("reduced_motion"))
        dnd = bool(self.p.settings.get("dnd"))
        options = []
        for name, spec in (self._anim().get("idle_actions") or {}).items():
            weight = float(spec.get("weight", 1)) if isinstance(spec, dict) else 0
            if weight <= 0 or now < self.cooldowns.get(name, 0):
                continue
            if name == "stroll" and (reduced or dnd or not self.p.settings.get("walk", True)
                                     or self.p.chat.is_open or self._activity()[0] > 8):
                continue
            if name == "stretch" and reduced:
                continue
            options.append((name, weight, float(spec.get("cooldown", 10))))
        if not options:
            return
        total = sum(w for _n, w, _c in options)
        r = random.uniform(0, total)
        for name, weight, cooldown in options:
            r -= weight
            if r <= 0:
                self.cooldowns[name] = now + cooldown
                self._start_action(name, now)
                return

    def _start_action(self, name: str, now: float) -> None:
        if name == "glance":
            self.action = (name, now + random.uniform(0.8, 1.6),
                           {"x": random.choice([-0.9, -0.6, 0.6, 0.9]), "y": random.uniform(-0.4, 0.3)})
        elif name == "look_at_cursor":
            self.action = (name, now + random.uniform(2.5, 4.0), {})
        elif name == "shift_weight":
            self.action = (name, now + random.uniform(2.0, 3.5), {"sway": random.choice([-3.0, 3.0])})
        elif name == "head_tilt":
            self.action = (name, now + random.uniform(1.5, 2.5), {"dx": random.choice([-2.5, 2.5])})
        elif name == "adjust_sleeve":
            self.gesture("adjust", 1.4, "l")
        elif name == "stretch":
            self.gesture("stretch", 1.6, "both")
            self.set_expression("tired", 1.8)
        elif name == "stroll":
            left, _t, right, _b = work_area(self.app)
            w, _h = self.size
            inward = 1 if self.x < (left + right) / 2 else -1
            target = self.x + inward * random.randint(70, 170)
            target = max(left + 8, min(target, right - w - 8))
            self.stroll = {"home": self.x, "target": target, "phase": "out", "pause_until": 0.0}

    def _cursor_look(self) -> tuple[float, float]:
        try:
            px, py = self.app.winfo_pointerxy()
        except Exception:
            return 0.0, 0.0
        w, _h = self.size
        hx = self.x + w / 2
        hy = self.y + (self.BUBBLE_H + 70) * self.scale
        dx, dy = (px - hx) / 260.0, (py - hy) / 220.0
        return max(-1.0, min(dx, 1.0)), max(-1.0, min(dy, 1.0))

    def tick(self) -> None:
        self._after = None
        if not self.visible or self.win is None:
            return
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        try:
            self._update(now, dt)
            self._draw()
        except Exception:
            self.p.ctx.log.exception("Avatar tick failed")
        from .orb import frame_delay
        busy = self.mode != "idle" or bool(self.stroll) or bool(self.drag) or self.action is not None or \
            now < self.hop_until or self.pose_r[0] != "rest" or self.pose_l[0] != "rest" or \
            0 <= now - self.blink_start < 0.4
        self._after = self.app.after(frame_delay(self.p.settings, busy), self.tick)

    def _update(self, now: float, dt: float) -> None:
        st = self.st
        anim = self._anim()
        motion = float(anim.get("motion", 1.0)) * (0.35 if self.p.settings.get("reduced_motion") else 1.0)

        # face from expression (temporary moods fall back to neutral)
        if now > self.expression_until and self.mode == "idle":
            self.expression = "neutral"
        mood = "focused" if self.mode == "thinking" else self.expression
        face = self.character.get("expressions", DEFAULT_EXPRESSIONS).get(mood) or DEFAULT_EXPRESSIONS["neutral"]
        targets = {k: float(face.get(k, DEFAULT_EXPRESSIONS["neutral"][k])) for k in FACE_KEYS}

        # blinking (independent of everything else)
        if self.blink_start < 0 and now >= self.next_blink:
            self.blink_start = now
            self.double_blink = random.random() < 0.15
        if self.blink_start >= 0:
            p = (now - self.blink_start) / 0.15
            if p >= 1:
                if self.double_blink:
                    self.double_blink = False
                    self.blink_start = now + 0.08
                else:
                    self.blink_start = -1
                    self._schedule_blink(now)
                st["blink"] = 1.0
            else:
                st["blink"] = 1 - math.sin(math.pi * max(0.0, p))
        else:
            st["blink"] = 1.0

        # idle actions
        if self.action and now > self.action[1]:
            self.action = None
        if self.mode == "idle" and not self.stroll and not self.drag and now >= self.next_idle:
            self._pick_idle_action(now)

        look = (0.0, 0.0)
        sway_t, head_dx_t = 0.0, 0.0
        if self.action:
            name, _until, data = self.action
            if name == "glance":
                look = (data["x"], data["y"])
            elif name == "look_at_cursor":
                look = self._cursor_look()
            elif name == "shift_weight":
                sway_t = data["sway"]
            elif name == "head_tilt":
                head_dx_t = data["dx"]
                targets["brow_asym"] = max(targets["brow_asym"], 0.3)
        if self.panel_side and self.mode != "thinking":
            look = (0.85 * self.panel_side, 0.1)
        if self.mode == "thinking":
            look = (0.45 * self.facing, -0.7)
        if self.drag:
            look = self._cursor_look()

        # speech → mouth
        mouth_t = 0.0
        if self.speech:
            env, start, duration = self.speech
            t = now - start
            if t >= duration:
                self.stop_speaking()
            elif env:
                mouth_t = env[min(len(env) - 1, int(t / 0.03))]
        st["mouth_open"] = approach(st["mouth_open"], mouth_t, 22, dt)

        # arms
        for attr, side in (("pose_r", "r"), ("pose_l", "l")):
            pose, until = getattr(self, attr)
            if self.mode == "thinking" and side == "r":
                pose, until = "think", now + 1
            active = pose != "rest" and now < until
            key_pose, key_amt = f"arm_{side}_pose", f"arm_{side}_amt"
            if active:
                st[key_pose] = pose
            st[key_amt] = approach(st[key_amt], 1.0 if active else 0.0, 7, dt)
            if not active and st[key_amt] < 0.02:
                st[key_pose] = "rest"

        # stroll (short walk away from home and back)
        walking = 0.0
        if self.stroll:
            s = self.stroll
            if s["phase"] == "pause":
                if now >= s["pause_until"]:
                    s["phase"] = "back"
                look = (math.sin(now * 0.8) * 0.7, 0.0)
            else:
                goal = s["target"] if s["phase"] == "out" else s["home"]
                speed = float(anim.get("walk_speed", 110)) * self.scale
                step = speed * dt
                if abs(goal - self.x) <= step:
                    self.place(goal, self.y)
                    if s["phase"] == "out":
                        s["phase"], s["pause_until"] = "pause", now + random.uniform(2.0, 4.0)
                    else:
                        self.stroll = None
                        left, _t, right, _b = work_area(self.app)
                        self.facing = -1 if self.x > (left + right) / 2 else 1
                else:
                    direction = 1 if goal > self.x else -1
                    self.facing = direction
                    self.place(self.x + direction * step, self.y)
                    walking = 1.0
        st["walking"] = approach(st["walking"], walking, 10, dt)
        if st["walking"] > 0.01:
            st["walk_phase"] += dt * 9.0
        st["bob"] = -abs(math.sin(st["walk_phase"])) * 2.2 * st["walking"]

        # breathing, speaking head motion, hop
        rate = float(anim.get("breath_rate", 0.22))
        st["breath"] = (math.sin(now * 2 * math.pi * rate) * 0.5 + 0.5) * motion
        if self.mode == "speaking":
            head_dx_t += math.sin(now * 3.1) * 0.8 * motion
        st["hop"] = approach(st["hop"], -7.0 if now < self.hop_until else 0.0, 25, dt)
        st["sway"] = approach(st["sway"], sway_t * motion, 3, dt)
        st["head_dx"] = approach(st["head_dx"], head_dx_t * motion, 4, dt)
        st["head_dy"] = approach(st["head_dy"], 0.0, 4, dt)
        st["look_x"] = approach(st["look_x"], look[0], 8, dt)
        st["look_y"] = approach(st["look_y"], look[1], 8, dt)
        for key in FACE_KEYS:
            st[key] = approach(st[key], targets[key], 9, dt)

        if self.bubble and now > self.bubble[1]:
            self.bubble = None

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        s = self.scale
        ox, oy = self.MARGIN * s, self.BUBBLE_H * s
        if self.rig is not None:
            self.rig.draw(c, self.st, s, ox, oy, self.facing, self.expression)
        else:
            draw_procedural(c, self.character["look"], self.st, s, ox, oy, self.facing)
        w, _h = self.size
        outline = self.character["look"].get("outline", "#06070b")
        accent = self.character["look"].get("cyberware", "#29e3ff")
        if self.mode == "thinking":
            t = time.monotonic()
            for i in range(3):
                r = (2.2 + 1.6 * (math.sin(t * 6 - i * 0.9) * 0.5 + 0.5)) * s
                cx = w / 2 + (28 + i * 11) * s * (1 if self.facing > 0 else -1)
                cy = oy + (6 - i * 6) * s
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=accent, outline=outline)
        if self.bubble:
            text = self.bubble[0]
            font_size = max(8, int(10 * s))
            pad = 8 * s
            item = c.create_text(w / 2, pad + 2, text=text, width=w - 3 * pad, fill="#14151a",
                                 font=("Segoe UI", font_size), anchor="n", justify="center")
            try:
                x0, y0, x1, y1 = c.bbox(item)
            except Exception:
                x0, y0, x1, y1 = pad, pad, w - pad, oy - pad
            x0, y0, x1, y1 = x0 - pad, y0 - pad * 0.6, x1 + pad, min(y1 + pad * 0.6, oy - 4 * s)
            r = 9 * s
            pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
                   w / 2 + 7 * s, y1, w / 2, y1 + 8 * s, w / 2 - 7 * s, y1, x0 + r, y1, x0, y1, x0, y1 - r,
                   x0, y0 + r, x0, y0]
            bubble = c.create_polygon(pts, smooth=True, fill="#f4f4f8", outline=outline, width=max(1, 2 * s))
            c.tag_lower(bubble, item)


# ════════════════════════════════════════════════════════════════════════════
#  Chat panel
# ════════════════════════════════════════════════════════════════════════════

class ChatPanel:
    WIDTH, HEIGHT = 420, 640

    def __init__(self, plugin: "Plugin"):
        self.p = plugin
        self.win: Any = None
        self.is_open = False
        self._thumbs: list[Any] = []
        self._pending_memories: list[str] = []
        self._streaming = False
        self._voice_open = False
        self._setup_visible = False
        self.addon_widgets: dict[str, dict[str, Any]] = {}

    # ── layout ───────────────────────────────────────────────────────────
    def _build(self) -> None:
        ctx = self.p.ctx
        t = ctx.theme
        win = ctk.CTkToplevel(ctx.app)
        win.withdraw()
        win.title(f"Chat — {self.p.character['name']}")
        win.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        win.minsize(360, 440)
        win.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        self.win = win
        self._build_body(win)

    def _build_body(self, win: Any) -> None:
        ctx = self.p.ctx
        t = ctx.theme
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)

        # header: status light, name, controls
        head = t.tag(ctk.CTkFrame(win, corner_radius=0), "sidebar")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        self.dot = t.tag(tk.Canvas(head, width=14, height=14, bd=0, highlightthickness=0), "transparent")
        self.dot.grid(row=0, column=0, rowspan=2, padx=(14, 6))
        self.name_label = t.tag(ctk.CTkLabel(head, text=self.p.character["name"], anchor="w"), "title", font_size=17)
        self.name_label.grid(row=0, column=1, sticky="ew", pady=(10, 0))
        self.status_label = t.tag(ctk.CTkLabel(head, text="Idle", anchor="w", height=18), "muted")
        self.status_label.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        buttons = t.tag(ctk.CTkFrame(head, fg_color="transparent"), "transparent")
        buttons.grid(row=0, column=2, rowspan=2, padx=(4, 10))
        self.mic_btn = t.tag(ctk.CTkButton(buttons, text="🎙", width=36, height=30, command=self.p.toggle_voice_chat),
                             "secondary")
        self.mic_btn.pack(side="left", padx=2)
        self.me_btn = t.tag(ctk.CTkButton(buttons, text="Mute me", width=74, height=30,
                                          command=self.p.toggle_mic_mute), "ghost")
        self.voice_btn = t.tag(ctk.CTkButton(buttons, text="🔊", width=36, height=30, command=self.toggle_voice_row),
                               "secondary")
        self.voice_btn.pack(side="left", padx=2)
        t.tag(ctk.CTkButton(buttons, text="■", width=32, height=30, command=self.p.stop_all), "ghost").pack(
            side="left", padx=2)
        t.tag(ctk.CTkButton(buttons, text="⏻", width=32, height=30, command=self.p.power_off), "danger").pack(
            side="left", padx=(2, 0))

        # voice settings (collapsible)
        self.voice_row = t.tag(ctk.CTkFrame(win), "card")
        self.voice_row.grid_columnconfigure(1, weight=1)

        # transcript
        self.log = ctk.CTkTextbox(win, wrap="word", font=t.font(13))
        self.log.grid(row=2, column=0, sticky="nsew", padx=10, pady=(10, 6))
        self.log.configure(state="disabled")

        # brain download (offline AI)
        self.setup_row = t.tag(ctk.CTkFrame(win), "card")
        self.setup_row.grid_columnconfigure(0, weight=1)
        self.setup_label = t.tag(ctk.CTkLabel(self.setup_row, text="", anchor="w", justify="left",
                                              wraplength=self.WIDTH - 150), "text")
        self.setup_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        self.setup_btn = ctk.CTkButton(self.setup_row, text="Download", width=100, height=28)
        self.setup_btn.grid(row=0, column=1, rowspan=2, padx=8, pady=6)
        self.setup_bar = ctk.CTkProgressBar(self.setup_row, height=6)
        self.setup_bar.set(0)

        # voice add-on download offer (voice chat / neural voices)
        self.addon_row = t.tag(ctk.CTkFrame(win), "card")
        self.addon_row.grid_columnconfigure(0, weight=1)

        # memory suggestion
        self.memory_row = t.tag(ctk.CTkFrame(win), "card")
        self.memory_label = t.tag(ctk.CTkLabel(self.memory_row, text="", anchor="w", justify="left",
                                               wraplength=self.WIDTH - 170), "text")
        self.memory_label.pack(side="left", padx=8, pady=6, fill="x", expand=True)
        t.tag(ctk.CTkButton(self.memory_row, text="✕", width=30, height=26,
                            command=lambda: self._resolve_memory(False)), "ghost").pack(side="right", padx=(0, 6))
        ctk.CTkButton(self.memory_row, text="Save", width=56, height=26,
                      command=lambda: self._resolve_memory(True)).pack(side="right", padx=4)

        # attached images
        self.attach_row = t.tag(ctk.CTkFrame(win), "card")
        self.attach_tiles = t.tag(ctk.CTkFrame(self.attach_row, fg_color="transparent"), "transparent")
        self.attach_tiles.pack(side="top", fill="x", padx=8, pady=(8, 2))
        self.attach_note = t.tag(ctk.CTkLabel(self.attach_row, text="", anchor="w", height=18), "muted")
        self.attach_note.pack(side="top", fill="x", padx=10, pady=(0, 6))

        # tools + entry
        tools = t.tag(ctk.CTkFrame(win, fg_color="transparent"), "transparent")
        tools.grid(row=8, column=0, sticky="ew", padx=10)
        self.shot_btn = t.tag(ctk.CTkButton(tools, text="Screenshot", width=92, height=26,
                                            command=lambda: self.p.share_screen()), "secondary")
        self.shot_btn.pack(side="left")
        self.window_btn = t.tag(ctk.CTkButton(tools, text="Window…", width=78, height=26,
                                              command=self.p.choose_window), "secondary")
        self.window_btn.pack(side="left", padx=4)
        t.tag(ctk.CTkButton(tools, text="Image…", width=70, height=26, command=self.p.pick_image_files),
              "secondary").pack(side="left")
        t.tag(ctk.CTkButton(tools, text="New chat", width=76, height=26, command=self.p.new_chat), "ghost").pack(
            side="right")

        entry_row = t.tag(ctk.CTkFrame(win, fg_color="transparent"), "transparent")
        entry_row.grid(row=9, column=0, sticky="ew", padx=10, pady=(6, 10))
        entry_row.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(entry_row, height=38,
                                  placeholder_text=f"Message {self.p.character['name']}…  (Ctrl+V pastes images)")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _e: self._send())
        self.entry.bind("<<Paste>>", self._paste)
        win.bind("<Control-v>", self._paste_window, add="+")
        ctk.CTkButton(entry_row, text="Send", width=70, height=38, command=self._send).grid(row=0, column=1,
                                                                                          padx=(6, 0))
        ctx.theme.apply(win)
        self.apply_theme()
        self.refresh_controls()
        ctx.register_drop_target(win, self.p.on_chat_drop, accept=("files", "text"))

    def apply_theme(self) -> None:
        if self.win is None:
            return
        p = self.p.ctx.theme.palette
        try:
            self.log.tag_config("you", foreground=p["accent"], spacing1=10)
            self.log.tag_config("them", foreground=p["accent2"], spacing1=10)
            self.log.tag_config("body", lmargin1=10, lmargin2=10, spacing3=2)
            self.log.tag_config("sys", foreground=p["text_muted"], spacing1=6, spacing3=4)
        except Exception:
            pass
        self.refresh_mic()

    def refresh_controls(self) -> None:
        if self.win is None:
            return
        state = "normal" if self.p.perm("screen") else "disabled"
        self.shot_btn.configure(state=state)
        self.window_btn.configure(state=state if IS_WINDOWS else "disabled")
        self.name_label.configure(text=self.p.character["name"])
        if hasattr(self.win, "title"):
            self.win.title(f"Chat — {self.p.character['name']}")
        self.voice_btn.configure(text="🔇" if self.p.settings["muted"] or not self.p.perm("voice") else "🔊")
        self.mic_btn.configure(state="normal" if self.p.perm("mic") else "disabled")
        self.refresh_mic()
        if self._voice_open:
            self._build_voice_row()

    MIC_TEXT = {"loading": "Starting voice chat…", "listening": "Listening — just talk",
                "hearing": "Hearing you…", "paused": "Listening paused while I talk",
                "muted": "Your mic is muted"}

    def refresh_mic(self) -> None:
        if self.win is None:
            return
        t = self.p.ctx.theme
        p = t.palette
        state = self.p.listener.state
        on = state != "off"
        try:
            t.tag(self.mic_btn, "accent" if on else "secondary")
            t.apply(self.mic_btn)
            if on:
                self.me_btn.pack(side="left", padx=2, after=self.mic_btn)
                self.me_btn.configure(text="Unmute me" if self.p.listener.muted else "Mute me")
            else:
                self.me_btn.pack_forget()
            color = {"listening": p["accent"], "hearing": p["success"], "loading": p["warning"],
                     "paused": p["accent2"], "muted": p["danger"]}.get(state, p["text_muted"])
            if self.p.avatar.mode == "speaking":
                color = p["accent2"]
            self.dot.delete("all")
            self.dot.create_oval(2, 2, 12, 12, fill=color, outline="")
            if on:
                self.status_label.configure(text=self.MIC_TEXT.get(state, ""))
        except Exception:
            pass

    # ── voice settings popover ───────────────────────────────────────────
    def toggle_voice_row(self) -> None:
        if self.win is None:
            return
        self._voice_open = not self._voice_open
        if self._voice_open:
            self._build_voice_row()
            self.voice_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(10, 0))
        else:
            self.voice_row.grid_remove()

    def _build_voice_row(self) -> None:
        t = self.p.ctx.theme
        row = self.voice_row
        for child in row.winfo_children():
            if getattr(child, "_mt_role", None) != "skip":  # keep the theme's frame decorations
                child.destroy()
        p = self.p
        speak = ctk.BooleanVar(value=not p.settings["muted"] and p.perm("voice"))
        ctk.CTkSwitch(row, text="Speak replies", variable=speak,
                      command=lambda: p.set_muted(not speak.get())).grid(row=0, column=0, columnspan=2, sticky="w",
                                                                          padx=10, pady=(8, 4))
        engines = {"Natural": "auto", "Kokoro": "neural", "Windows": "windows"}
        current = p.voice_config()["engine"]
        seg = ctk.CTkSegmentedButton(row, values=list(engines), width=200,
                                     command=lambda v: (p.set_setting("voice_engine", engines[v]),
                                                        self._build_voice_row(), p.refresh_voice_ui()))
        seg.set({"natural": "Natural", "neural": "Kokoro"}.get(current, "Windows"))
        seg.grid(row=0, column=2, sticky="e", padx=10, pady=(8, 4))

        t.tag(ctk.CTkLabel(row, text="Voice", width=52, anchor="w"), "muted").grid(row=1, column=0, sticky="w",
                                                                                    padx=(10, 0))
        names, selected, setter = p.voice_choices()
        menu = ctk.CTkOptionMenu(row, values=names or ["(no voices)"], width=210, dynamic_resizing=False,
                                 command=lambda v: (setter(v), p.refresh_voice_ui()))
        menu.set(selected or (names[0] if names else "(no voices)"))
        menu.grid(row=1, column=1, sticky="ew", pady=3)
        t.tag(ctk.CTkButton(row, text="▶ Try", width=60, height=28, command=p.preview_voice), "secondary").grid(
            row=1, column=2, sticky="e", padx=10)

        t.tag(ctk.CTkLabel(row, text="Speed", width=52, anchor="w"), "muted").grid(row=2, column=0, sticky="w",
                                                                                    padx=(10, 0))
        value = t.tag(ctk.CTkLabel(row, text="", width=46), "text")
        lo, hi = SPEED_RANGE
        slider = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=int(round((hi - lo) / 0.05)))
        speed = p.voice_speed()
        slider.set(speed)
        value.configure(text=f"{speed:.2f}×")

        def moved(v: float) -> None:
            value.configure(text=f"{float(v):.2f}×")
            p.set_setting("voice_speed", round(float(v), 2))
        slider.configure(command=moved)
        slider.grid(row=2, column=1, sticky="ew", pady=(3, 8))
        value.grid(row=2, column=2, sticky="e", padx=10, pady=(3, 8))
        if current == "windows" and not p.addons.installed("natural"):
            self._addon_offer(row, 3, "natural")
        t.apply(row)

    def _addon_offer(self, parent: Any, row: int, key: str) -> None:
        t = self.p.ctx.theme
        info = VOICE_ADDONS[key]
        box = t.tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        box.grid(row=row, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 8))
        box.grid_columnconfigure(0, weight=1)
        label = t.tag(ctk.CTkLabel(box, text=f"{info['name']}: {info['about']}", anchor="w", justify="left",
                                   wraplength=self.WIDTH - 140), "muted")
        label.grid(row=0, column=0, sticky="ew")
        button = ctk.CTkButton(box, text="Download", width=96, height=28,
                               command=lambda: self.p.start_addon_download(key))
        button.grid(row=0, column=1, padx=(8, 0))
        bar = ctk.CTkProgressBar(box, height=6)
        bar.set(0)
        self.addon_widgets[key] = {"label": label, "button": button, "bar": bar}
        self.p.update_addon_ui(key)

    def offer_addon(self, key: str) -> None:
        if self.win is None:
            self._build()
        for child in self.addon_row.winfo_children():
            if getattr(child, "_mt_role", None) != "skip":
                child.destroy()
        self._addon_offer(self.addon_row, 0, key)
        self.p.ctx.theme.apply(self.addon_row)
        self.addon_row.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 6))

    def hide_addon(self) -> None:
        if self.win is not None:
            self.addon_row.grid_remove()

    # ── open / close / placement ─────────────────────────────────────────
    def show(self) -> None:
        if self.win is None:
            self._build()
        self.reposition()
        self.win.deiconify()
        self.win.lift()
        self.is_open = True
        try:
            self.entry.focus_set()
        except Exception:
            pass
        self.p.on_chat_opened()

    def hide(self) -> None:
        if self.win is not None:
            self.win.withdraw()
        self.is_open = False
        self.p.avatar.panel_side = 0

    def toggle(self) -> None:
        self.hide() if self.is_open else self.show()

    def reposition(self) -> None:
        if self.win is None:
            return
        a = self.p.avatar
        left, top, right, bottom = work_area(self.p.ctx.app)
        aw, ah = a.size
        if a.x - self.WIDTH - 8 >= left:
            x, side = a.x - self.WIDTH - 8, -1
        else:
            x, side = min(a.x + aw + 8, right - self.WIDTH), 1
        y = max(top, min(a.y + ah - self.HEIGHT - 10, bottom - self.HEIGHT - 40))
        a.panel_side = side if a.visible else 0
        self.win.geometry(f"{self.WIDTH}x{self.HEIGHT}+{int(x)}+{int(y)}")

    # ── transcript ───────────────────────────────────────────────────────
    def _write(self, text: str, *tags: str) -> None:
        if self.win is None:
            self._build()
        self.log.configure(state="normal")
        self.log.insert("end", text, tags) if tags else self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def add_user(self, text: str, images: int = 0, spoken: bool = False) -> None:
        self._write("\n" + ("You 🎙" if spoken else "You") + "\n", "you")
        note = f"  [{images} image{'s' if images != 1 else ''} attached]" if images else ""
        self._write(text + note + "\n", "body")

    def begin_reply(self) -> None:
        self._write(f"\n{self.p.character['name']}\n", "them")
        self._streaming = True

    def stream(self, text: str) -> None:
        if text:
            self._write(text, "body")

    def end_reply(self) -> None:
        if self._streaming:
            self._write("\n")
        self._streaming = False

    def system(self, text: str) -> None:
        self._write(f"\n{text}\n", "sys")

    def clear(self) -> None:
        if self.win is None:
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_status(self, text: str) -> None:
        if self.win is not None:
            if self.p.listener.state != "off" and text in ("Idle", ""):
                text = self.MIC_TEXT.get(self.p.listener.state, text)
            self.status_label.configure(text=text)
            self.refresh_mic()

    # ── attachments ──────────────────────────────────────────────────────
    def show_attachments(self, items: list[dict[str, Any]]) -> None:
        if self.win is None:
            self._build()
        t = self.p.ctx.theme
        for child in self.attach_tiles.winfo_children():
            child.destroy()
        self._thumbs = []
        if not items:
            self.attach_row.grid_remove()
            return
        for index, item in enumerate(items):
            tile = t.tag(ctk.CTkFrame(self.attach_tiles, width=84, height=60), "entry")
            tile.pack(side="left", padx=(0, 6))
            tile.pack_propagate(False)
            thumb = item["thumb"]
            image = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
            self._thumbs.append(image)
            t.tag(ctk.CTkLabel(tile, text="", image=image), "transparent").place(relx=0.5, rely=0.5, anchor="center")
            t.tag(ctk.CTkButton(tile, text="✕", width=20, height=20, corner_radius=10,
                                command=lambda i=index: self.p.remove_attachment(i)), "danger").place(
                relx=1.0, rely=0.0, anchor="ne", x=-2, y=2)
        count = len(items)
        self.attach_note.configure(text=f"{count} image{'s' if count != 1 else ''} · sent with your next message"
                                        f"{'' if count < MAX_IMAGES else '  (max)'}")
        t.apply(self.attach_row)
        self.attach_row.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 6))

    def _paste(self, _event: Any = None) -> Any:
        return "break" if self.p.paste_from_clipboard() else None

    def _paste_window(self, event: Any) -> Any:
        if event.widget is not None and "entry" in str(event.widget).lower():
            return None  # the entry's own <<Paste>> handler deals with it
        return self._paste(event)

    # ── offline-brain setup row ──────────────────────────────────────────
    def offer_setup(self, plugin: "Plugin") -> None:
        """One-click download of the offline brain, right inside the chat."""
        if self.win is None:
            self._build()
        info = LOCAL_MODELS[plugin.settings["local_size"]]
        if plugin.models.downloading:
            self.update_setup_progress(plugin._download_text(), 0)
        else:
            self.setup_label.configure(text=f"{plugin.character['name']} needs a brain: {info['name']} "
                                            f"(about {info['approx_gb']:.1f} GB, one-time download, then fully offline).")
            self.setup_btn.configure(text="Download", command=plugin.start_download, state="normal")
            self.setup_bar.grid_remove()
        self.setup_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
        self._setup_visible = True

    def update_setup_progress(self, text: str, fraction: float | None) -> None:
        if self.win is None or not self._setup_visible:
            return
        if fraction is None:
            self.setup_bar.grid_remove()
            if text:
                self.setup_label.configure(text=text)
            self.setup_btn.configure(text="Resume", command=self.p.start_download)
            return
        self.setup_label.configure(text=text)
        self.setup_bar.set(fraction)
        self.setup_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.setup_btn.configure(text="Pause", command=self.p.cancel_download)

    def hide_setup(self) -> None:
        if self.win is not None:
            self.setup_row.grid_remove()
        self._setup_visible = False

    # ── memory suggestions ───────────────────────────────────────────────
    def offer_memories(self, memories: list[str]) -> None:
        self._pending_memories.extend(memories)
        self._next_memory()

    def _next_memory(self) -> None:
        if self.win is None:
            return
        if not self._pending_memories:
            self.memory_row.grid_remove()
            return
        self.memory_label.configure(text=f"Remember this?  “{self._pending_memories[0]}”")
        self.memory_row.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))

    def _resolve_memory(self, save: bool) -> None:
        if self._pending_memories:
            fact = self._pending_memories.pop(0)
            if save:
                self.p.add_memory(fact)
        self._next_memory()

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text and not self.p.attachments:
            return
        self.entry.delete(0, "end")
        self.p.send_message(text)


# ════════════════════════════════════════════════════════════════════════════
#  Plugin
# ════════════════════════════════════════════════════════════════════════════

SETTING_DEFAULTS: dict[str, Any] = {
    "character": "juno",
    "size": "M",
    "activity": "occasional",
    "walk": True,
    "reduced_motion": False,
    "greet": True,
    "power_on_start": False,     # Juno only runs when switched on
    "dnd": False,
    "home": None,
    "anchor": "right",
    "personality": {},
    "instructions": "",
    "provider": "local",
    "local_size": "",
    "local_backend": "auto",
    "local_idle": "5",
    "model": "claude-haiku-5-5",
    "base_url": "",
    "api_key": "",
    "voice_engine": "auto",      # auto (neural when downloaded) | neural | windows
    "neural_voice": "",          # Kokoro voice id; "" = the character's choice
    "voice_speed": None,         # 0.6..1.6; None = the character's default
    "voice_name": "",
    "voice_pitch": 0,
    "voice_volume": 0,
    "muted": False,
    "perm_screen": True,
    "perm_memory": True,
    "perm_voice": True,
    "perm_proactive": True,
    "perm_mic": True,
}
SIZES = {"S": 0.8, "M": 1.0, "L": 1.25}
MAX_IMAGES = 4
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


class Plugin:
    meta_title = "Desktop Companion"
    meta_description = ("A character for your desktop, powered by a small AI that runs offline on your PC. Switch it "
                        "on when you want company: chat or talk out loud, paste or share images, pick a natural voice "
                        "and speed, and swap in new characters as packs.")
    meta_icon = "☺"
    meta_order = 20
    meta_api_version = 1

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.settings: dict[str, Any] = {k: ctx.get_setting(k, v) for k, v in SETTING_DEFAULTS.items()}
        self.library = CharacterLibrary(ctx.data_dir / "characters", ctx.log)
        self.memory_file = ctx.data_dir / "memory.json"
        self.memories: list[dict[str, Any]] = self._load_memories()
        self.speech = SpeechEngine(ctx.log)
        self.history: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.powered = False
        self._busy = False
        self._cancel = threading.Event()
        self._speech_files: list[Path] = []
        self._quip_after: Any = None
        self._built = False
        self.temp_dir = ctx.data_dir / "tmp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        local_root = ctx.models_dir
        self.models = ModelManager(local_root, ctx.log)
        self.engine = LocalEngine(self)
        self.addons = AddonManager(local_root, ctx.log)
        self.neural = NeuralVoice(self.addons.folder("voices"), ctx.log)
        self.natural = PocketVoice(self.addons.folder("natural"), ctx.data_dir / "voices", ctx.log)
        self.voice_out = VoiceOut(self)
        self.listener = VoiceChat(self)
        self._addon_cancel: threading.Event | None = None
        self._addon_state: dict[str, dict[str, Any]] = {}
        self._download_cancel: threading.Event | None = None
        self._download_state: dict[str, Any] = {}
        if self.settings["local_size"] not in LOCAL_MODELS:
            self.settings["local_size"] = recommended_model()
        if self.settings["provider"] == "anthropic" and not self.settings.get("api_key"):
            self.set_setting("provider", "local")   # older default; the user never set up a key
        self._idle_after = ctx.app.after(60_000, self._idle_check)

        self._import_bundled_characters()
        if self.settings["character"] not in self.library.characters:
            self.settings["character"] = "juno"
        self.character = self.library.characters[self.settings["character"]]
        self.avatar = Avatar(self)
        self.chat = ChatPanel(self)
        self.avatar.set_character(self.character, self.library.folders.get(self.character["id"]),
                                  SIZES.get(self.settings["size"], 1.0))
        ctx.register_file_type(".mtchar", "Companion characters", self.import_character)
        if self.speech.available:
            ctx.run_in_thread(self.speech.list_voices, on_done=lambda _v: self._refresh_voice_menu(),
                              on_error=lambda _e: None)
        if self.settings["power_on_start"]:
            ctx.app.after(800, self.power_on)

    def _import_bundled_characters(self) -> None:
        """Packs shipped by the installer in <app>/extras/characters are imported once each."""
        bundled = self.ctx.paths.app_dir / "extras" / "characters"
        if not bundled.is_dir():
            return
        done = set(self.ctx.get_setting("bundled_imported", []) or [])
        for pack in sorted(bundled.glob("*.mtchar")):
            key = f"{pack.name}:{pack.stat().st_size}"
            if key in done:
                continue
            try:
                self.library.import_file(pack)
            except Exception as exc:
                self.ctx.log.warning("Bundled character %s skipped: %s", pack.name, exc)
            done.add(key)
        self.ctx.set_setting("bundled_imported", sorted(done))

    # ── settings helpers ──────────────────────────────────────────────────
    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.ctx.set_setting(key, value)

    def perm(self, name: str) -> bool:
        return bool(self.settings.get(f"perm_{name}", True))

    def personality(self) -> dict[str, float]:
        base = dict(self.character["persona"]["defaults"])
        base.update((self.settings.get("personality") or {}).get(self.character["id"], {}))
        return base

    def api_key(self) -> str:
        return unprotect_secret(self.settings.get("api_key") or "")

    def local_manifest(self) -> dict[str, Any] | None:
        return self.models.installed(self.settings["local_size"])

    # ── offline AI: download / warm-up / idle unload ──────────────────────
    def start_download(self) -> None:
        if self.models.downloading:
            return
        key = self.settings["local_size"]
        self._download_cancel = threading.Event()
        cancel = self._download_cancel
        self.models.downloading = key
        self._download_state = {"done": 0, "total": int(LOCAL_MODELS[key]["approx_gb"] * 1024 ** 3), "error": ""}
        self._update_download_ui()

        def progress(done: int, total: int, _name: str) -> None:
            self._download_state.update(done=done, total=total)
            self.ctx.call_in_ui(self._update_download_ui)

        def work() -> dict[str, Any]:
            return self.models.download(key, progress, cancel)

        def done(_manifest: dict[str, Any]) -> None:
            self.models.downloading = None
            self.ctx.set_status(f"{LOCAL_MODELS[key]['name']} downloaded — {self.character['name']} can talk offline now",
                                "success")
            self.chat.hide_setup()
            if self.avatar.visible:
                self.avatar.say("Brain installed! Talk to me.", 6)
                self.avatar.set_expression("pleased", 4)
            self._update_download_ui()
            self._refresh_page(rebuild=True)

        def failed(exc: BaseException) -> None:
            self.models.downloading = None
            message = str(exc) if isinstance(exc, ProviderError) else f"Download failed: {exc}"
            self._download_state["error"] = message
            self.ctx.set_status(message, "warning" if cancel.is_set() else "error")
            self._update_download_ui()

        self.ctx.run_in_thread(work, on_done=done, on_error=failed)

    def cancel_download(self) -> None:
        if self._download_cancel is not None:
            self._download_cancel.set()

    def delete_model(self) -> None:
        if self.models.downloading:
            return
        self.ctx.run_in_thread(self.engine.stop, on_done=lambda _r: (
            self.models.delete(self.settings["local_size"]), self._refresh_page(rebuild=True),
            self.ctx.set_status("Model deleted", "success")))

    def _download_text(self) -> str:
        st = self._download_state
        if self.models.downloading:
            done, total = st.get("done", 0), max(1, st.get("total", 1))
            return f"Downloading {LOCAL_MODELS[self.models.downloading]['name']}…  " \
                   f"{done / 1024 ** 3:.2f} / {total / 1024 ** 3:.2f} GB  ({done * 100 // total}%)"
        return st.get("error", "")

    def _update_download_ui(self) -> None:
        text = self._download_text()
        frac = (self._download_state.get("done", 0) / max(1, self._download_state.get("total", 1))
                if self.models.downloading else 0)
        self.chat.update_setup_progress(text, frac if self.models.downloading else None)
        widgets = getattr(self, "_dl_widgets", None)
        if widgets and self._built:
            try:
                widgets["label"].configure(text=text or self._brain_status()[0])
                if self.models.downloading:
                    widgets["bar"].set(frac)
                    widgets["bar"].grid()
                    widgets["primary"].configure(text="Pause", command=self.cancel_download)
                else:
                    widgets["bar"].grid_remove()
                    widgets["primary"].configure(
                        text="Resume download" if self._download_state.get("error") else "Download",
                        command=self.start_download)
            except Exception:
                pass

    def _brain_status(self) -> tuple[str, str]:
        info = LOCAL_MODELS[self.settings["local_size"]]
        if not self.engine.available():
            return "The offline AI engine is missing from the app's bin folder — reinstall Juno.", "danger"
        if self.models.downloading:
            return self._download_text(), "text"
        if self.local_manifest() is None:
            return (f"{info['name']} isn't downloaded yet (about {info['approx_gb']:.1f} GB, one time). "
                    f"After that it works offline — no account, nothing sent online."), "warning"
        if self.engine.running():
            where = "graphics card" if self.engine.backend == "vulkan" else "processor"
            return f"{info['name']} is running on your {where}. Fully offline.", "success"
        return f"{info['name']} is installed — it wakes up automatically when you chat.", "success"

    def warm_up(self) -> None:
        manifest = self.local_manifest()
        if self.settings["provider"] != "local" or manifest is None or self.engine.running() or self._busy:
            return
        self.ctx.run_in_thread(self.engine.ensure, manifest, lambda _t: None,
                               on_done=lambda _r: self._refresh_status_bar(), on_error=lambda _e: None)

    def _idle_check(self) -> None:
        self._idle_after = self.ctx.app.after(60_000, self._idle_check)
        minutes = self.settings.get("local_idle", "15")
        now = time.monotonic()
        # voices and speech recognition are cheap to reload, so they're let go after a few quiet minutes
        neural = getattr(self, "neural", None)
        if neural is not None and neural.loaded() and now - neural.last_used > 300 and not self.voice_out.active:
            self.ctx.log.info("Unloading the voice model (unused for 5 minutes)")
            self.ctx.run_in_thread(neural.unload)
        natural = getattr(self, "natural", None)
        if natural is not None and natural.loaded() and now - natural.last_used > 300 and not self.voice_out.active:
            self.ctx.log.info("Unloading the natural voice model (unused for 5 minutes)")
            self.ctx.run_in_thread(natural.unload)
        lst = self.listener
        if getattr(lst, "_recognizer", None) is not None and lst.state == "off" and \
                now - getattr(lst, "off_since", now) > 180:
            self.ctx.log.info("Unloading speech recognition (mic off for 3 minutes)")
            lst._recognizer = None
        if minutes == "never" or not self.engine.running() or self._busy:
            return
        if now - self.engine.last_used > int(minutes) * 60:
            self.ctx.log.info("Unloading the local AI after %s idle minutes", minutes)
            self.ctx.run_in_thread(self.engine.stop)

    # ── power: Juno only exists on screen (and uses memory) while switched on ──
    def power_on(self) -> None:
        if self.powered:
            self.avatar.show()
            return
        self.powered = True
        self.ctx.log.info("%s switched on", self.character["name"])
        self._startup()
        self._refresh_page(rebuild=True)
        self.ctx.set_status(f"{self.character['name']} is on — right-click the avatar for options.", "success")

    def power_off(self) -> None:
        if not self.powered:
            return
        self.powered = False
        self.ctx.log.info("%s shut down", self.character["name"])
        self._cancel.set()
        self.listener.unload()
        self.voice_out.stop()
        self.speech.stop()
        self.avatar.stop_speaking()
        self.chat.hide()
        self.avatar.hide()
        if self._quip_after is not None:
            try:
                self.ctx.app.after_cancel(self._quip_after)
            except Exception:
                pass
            self._quip_after = None
        self.ctx.run_in_thread(self._release_engines)
        self._refresh_page(rebuild=True)
        self.ctx.set_status(f"{self.character['name']} is shut down — nothing of it is running.")

    def _release_engines(self) -> None:
        self.engine.stop()
        self.neural.unload()
        self.natural.unload()

    def toggle_power(self) -> None:
        self.power_off() if self.powered else self.power_on()

    def open_chat(self) -> None:
        self.power_on()
        self.chat.show()

    def _startup(self) -> None:
        self.avatar.show()
        if self.settings["greet"] and self.perm("proactive") and not self.settings["dnd"]:
            greetings = self.character["persona"]["greetings"]
            if greetings:
                self.ctx.app.after(900, lambda: (self.avatar.say(random.choice(greetings), 7),
                                                 self.avatar.gesture("wave", 1.8),
                                                 self.avatar.set_expression("pleased", 3)))
        self._schedule_quip()

    def _schedule_quip(self) -> None:
        if self._quip_after is not None:
            try:
                self.ctx.app.after_cancel(self._quip_after)
            except Exception:
                pass
        self._quip_after = self.ctx.app.after(random.randint(12, 25) * 60_000, self._quip)

    def _quip(self) -> None:
        self._quip_after = None
        lines = self.character["persona"]["idle_lines"]
        if (lines and self.powered and self.avatar.visible and self.settings["activity"] == "chatty"
                and self.perm("proactive")
                and not self.settings["dnd"] and not self._busy and not self.chat.is_open):
            self.avatar.say(random.choice(lines), 6)
            self.avatar.set_expression("pleased", 2)
        self._schedule_quip()

    def shutdown(self) -> None:
        self._cancel.set()
        self.cancel_download()
        if self._addon_cancel is not None:
            self._addon_cancel.set()
        self.listener.unload()
        self.voice_out.stop()
        self.speech.stop()
        try:
            self.ctx.app.after_cancel(self._idle_after)
        except Exception:
            pass
        self.engine.stop()
        if self._quip_after is not None:
            try:
                self.ctx.app.after_cancel(self._quip_after)
            except Exception:
                pass
        self.avatar.destroy()
        if self.chat.win is not None:
            try:
                self.chat.win.destroy()
            except Exception:
                pass

    def on_theme_changed(self, _palette: dict[str, Any]) -> None:
        self.chat.apply_theme()

    # ── avatar controls ───────────────────────────────────────────────────
    def show_avatar(self) -> None:
        self.power_on()
        self.avatar.show()
        self._refresh_page()

    def hide_avatar(self) -> None:
        self.chat.hide()
        self.avatar.hide()
        self.ctx.set_status(f"{self.character['name']} is hidden — show them again from the Companion page.")
        self._refresh_page()

    def reset_home(self) -> None:
        self.set_setting("home", None)
        self.avatar.stroll = None
        if self.avatar.visible:
            self.avatar.show()
            self.chat.reposition()

    def toggle_dnd(self) -> None:
        self.set_setting("dnd", not self.settings["dnd"])
        if self.settings["dnd"]:
            self.avatar.stroll = None
            self.avatar.bubble = None
        self._refresh_page()

    def open_settings_page(self) -> None:
        app = self.ctx.app
        try:
            app.deiconify()
            app.lift()
            app.select(self.ctx.plugin_id)
        except Exception:
            pass

    def on_chat_opened(self) -> None:
        if not self.powered:
            self.power_on()
        self.avatar.set_expression("pleased", 1.5)
        self.chat.reposition()
        if self.settings["provider"] == "local":
            if self.local_manifest() is None:
                self.chat.offer_setup(self)
            else:
                self.warm_up()

    def toggle_mute(self) -> None:
        self.set_muted(not self.settings["muted"])

    def set_muted(self, muted: bool) -> None:
        self.set_setting("muted", bool(muted))
        if muted:
            self.voice_out.stop()
            self.speech.stop()
            self.avatar.stop_speaking()
        elif not self.perm("voice"):
            self.set_setting("perm_voice", True)
        self.chat.refresh_controls()

    def stop_all(self) -> None:
        self._cancel.set()
        self.voice_out.stop()
        self.speech.stop()
        self.avatar.stop_speaking()

    # ── voice: engine choice, speed, previews ─────────────────────────────
    def neural_ready(self) -> bool:
        return self.addons.installed("voices") and sherpa_ok()

    def natural_ready(self) -> bool:
        return self.addons.installed("natural") and sherpa_ok() and hasattr(sherpa(), "OfflineTtsPocketModelConfig")

    def natural_voice(self) -> str:
        voices = [k for k, _l in self.natural.voices()]
        wanted = self.settings.get("natural_voice") or self.character["voice"].get("natural") or "alba"
        if wanted.lower().endswith(".wav") and ":" not in wanted:      # a recording shipped in the character pack
            folder = self.library.folders.get(self.character["id"])
            if folder is not None and (folder / wanted).is_file():
                return "file:" + str(folder / wanted)
        if wanted.startswith("file:") and Path(wanted[5:]).is_file():
            return wanted
        return wanted if wanted in voices else (voices[0] if voices else wanted)

    def voice_speed(self) -> float:
        value = self.settings.get("voice_speed")
        if value is None:
            value = self.character["voice"].get("speed", 1.1)
        try:
            return max(SPEED_RANGE[0], min(SPEED_RANGE[1], float(value)))
        except (TypeError, ValueError):
            return 1.1

    def voice_config(self) -> dict[str, Any]:
        voice = self.character["voice"]
        choice = self.settings.get("voice_engine", "auto")
        if choice == "windows" and self.speech.available:
            engine = "windows"
        elif choice == "neural" and self.neural_ready():
            engine = "neural"
        elif self.natural_ready():
            engine = "natural"
        else:
            engine = "neural" if self.neural_ready() else "windows"
        speed = self.voice_speed()
        neural_voice = self.settings.get("neural_voice") or voice.get("kokoro") or "af_heart"
        rate = int(voice.get("rate", 0)) + round(math.log(speed) / math.log(1.12))
        return {
            "engine": engine,
            "voice": neural_voice if neural_voice in KOKORO_SID else "af_heart",
            "natural": self.natural_voice() if engine == "natural" else "",
            "speed": speed,
            "sapi_voice": self.speech.pick(self.settings.get("voice_name") or "", voice.get("prefer", [])),
            "sapi_rate": max(-10, min(10, rate)),
            "sapi_pitch": int(voice.get("pitch", 0)) + int(self.settings.get("voice_pitch") or 0),
            "sapi_volume": max(0, min(100, int(voice.get("volume", 90)) + int(self.settings.get("voice_volume") or 0))),
        }

    def voice_choices(self) -> tuple[list[str], str, Callable[[str], None]]:
        """(menu labels, current label, setter) for whichever engine is active."""
        config = self.voice_config()
        if config["engine"] == "natural":
            pairs = self.natural.voices()
            labels = [label for _k, label in pairs] or ["(no voices yet)"]
            by_label = {label: key for key, label in pairs}
            current = next((label for key, label in pairs if key == config["natural"]), labels[0])
            return labels, current, lambda label: self.set_setting("natural_voice", by_label.get(label, ""))
        if config["engine"] == "neural" or (self.settings.get("voice_engine") == "neural"):
            labels = [label for _k, _s, label in KOKORO_VOICES]
            return labels, KOKORO_LABEL.get(config["voice"], labels[0]), \
                lambda label: self.set_setting("neural_voice", KOKORO_BY_LABEL.get(label, ""))
        default = "Character default"
        labels = [default] + self.speech.voices
        return labels, self.settings.get("voice_name") or default, \
            lambda label: self.set_setting("voice_name", "" if label == default else label)

    def can_speak(self) -> bool:
        return self.perm("voice") and not self.settings["muted"] and (self.speech.available or self.neural_ready()
                                                                      or self.natural_ready())

    def preview_voice(self) -> None:
        if self.settings.get("voice_engine") == "neural" and not self.neural_ready():
            self.chat.offer_addon("voices")
            return
        if self.settings.get("voice_engine") in ("auto", "natural", None) and not self.natural_ready() \
                and not self.neural_ready():
            self.chat.offer_addon("natural")
        lines = self.character["persona"]["greetings"] or ["Testing, one two."]
        self.voice_out.say(random.choice(lines))

    def voice_problem(self, message: str) -> None:
        self.ctx.set_status(f"Neural voice unavailable ({message}) — using a Windows voice.", "warning")

    def refresh_voice_ui(self) -> None:
        self.chat.refresh_controls()
        if self._built and getattr(self, "_tabs", None) is not None:
            try:
                if self._tabs.get() == "AI & Voice":
                    self._refresh_page(rebuild=True)
            except Exception:
                pass

    # ── voice add-on downloads (neural voices, voice chat) ────────────────
    def start_addon_download(self, key: str) -> None:
        if self.addons.downloading:
            return
        if not sherpa_ok():
            self.ctx.set_status("The voice engine isn't in this build — update Juno first.", "error")
            return
        self.addons.downloading = key
        cancel = self._addon_cancel = threading.Event()
        self._addon_state[key] = {"done": 0, "total": self.addons.total(key), "error": ""}
        self.update_addon_ui(key)

        def progress(done: int, total: int) -> None:
            self._addon_state[key].update(done=done, total=total)
            self.ctx.call_in_ui(self.update_addon_ui, key)

        def done(_r: Any) -> None:
            self.addons.downloading = None
            self.ctx.set_status(f"{VOICE_ADDONS[key]['name']} installed", "success")
            self.ctx.notify(f"{VOICE_ADDONS[key]['name']} ready", "Downloaded and working offline.",
                            subtitle=self.character["name"], icon="♪")
            self.chat.hide_addon()
            self.update_addon_ui(key)
            if key in ("voices", "natural") and self.settings.get("voice_engine") != "windows":
                self.set_setting("voice_engine", "auto" if key == "natural" else "neural")
            self.refresh_voice_ui()
            self._refresh_page(rebuild=True)
            if key == "listen" and self.powered and self._want_voice_chat:
                self.toggle_voice_chat()

        def failed(exc: BaseException) -> None:
            self.addons.downloading = None
            self._addon_state[key]["error"] = str(exc) if isinstance(exc, ProviderError) else f"Download failed: {exc}"
            self.ctx.set_status(self._addon_state[key]["error"], "warning" if cancel.is_set() else "error")
            self.update_addon_ui(key)

        self.ctx.run_in_thread(self.addons.download, key, progress, cancel, on_done=done, on_error=failed)

    def cancel_addon_download(self) -> None:
        if self._addon_cancel is not None:
            self._addon_cancel.set()

    def delete_addon(self, key: str) -> None:
        if self.addons.downloading == key:
            return
        if key == "listen":
            self.listener.unload()
        else:
            self.neural.unload()
        self.addons.delete(key)
        self.ctx.set_status(f"{VOICE_ADDONS[key]['name']} removed", "success")
        self.refresh_voice_ui()
        self._refresh_page(rebuild=True)

    def addon_text(self, key: str) -> str:
        st = self._addon_state.get(key, {})
        if self.addons.downloading == key:
            done, total = st.get("done", 0), max(1, st.get("total", 1))
            return f"Downloading {VOICE_ADDONS[key]['name'].lower()}…  {done / 1024 ** 2:.0f} / " \
                   f"{total / 1024 ** 2:.0f} MB ({done * 100 // total}%)"
        if st.get("error"):
            return st["error"]
        if self.addons.installed(key):
            return f"{VOICE_ADDONS[key]['name']} installed — works offline."
        return f"{VOICE_ADDONS[key]['name']}: {VOICE_ADDONS[key]['about']}"

    def update_addon_ui(self, key: str) -> None:
        st = self._addon_state.get(key, {})
        downloading = self.addons.downloading == key
        frac = st.get("done", 0) / max(1, st.get("total", 1)) if downloading else 0
        for widgets in (self.chat.addon_widgets.get(key), getattr(self, "_addon_widgets", {}).get(key)):
            if not widgets:
                continue
            try:
                widgets["label"].configure(text=self.addon_text(key))
                if downloading:
                    widgets["bar"].set(frac)
                    widgets["bar"].grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
                    widgets["button"].configure(text="Pause", command=self.cancel_addon_download, state="normal")
                else:
                    widgets["bar"].grid_remove()
                    if self.addons.installed(key):
                        widgets["button"].configure(text="Remove", command=lambda k=key: self.delete_addon(k))
                    else:
                        widgets["button"].configure(text="Resume" if st.get("error") else "Download",
                                                    command=lambda k=key: self.start_addon_download(k),
                                                    state="disabled" if self.addons.downloading else "normal")
            except Exception:
                pass

    # ── voice chat (microphone) ───────────────────────────────────────────
    _want_voice_chat = False

    def toggle_voice_chat(self) -> None:
        if self.listener.state != "off":
            self._want_voice_chat = False
            self.listener.stop()
            self.chat.set_status("Voice chat off")
            self.chat.refresh_mic()
            return
        if not self.perm("mic"):
            self.ctx.set_status("Voice chat is turned off on the Permissions page.", "warning")
            return
        self._want_voice_chat = True
        self.power_on()
        if not self.chat.is_open:
            self.chat.show()
        ok, why = self.listener.available()
        if not ok:
            if why == "download":
                self.chat.offer_addon("listen")
                self.chat.system("Voice chat needs a one-time download (about 110 MB) — press Download above. "
                                 "After that it runs entirely on your PC.")
            else:
                self.chat.system(f"Voice chat isn't available: {why}.")
            return
        if self.settings["muted"]:
            self.set_muted(False)
            self.chat.system("Voice replies switched on for voice chat.")

        def ready() -> None:
            self.chat.system("🎙 Voice chat on — just talk. I pause listening while I'm speaking. "
                             "Use “Mute me” to stop me hearing you.")
            self.chat.refresh_mic()

        def error(message: str) -> None:
            self._want_voice_chat = False
            self.chat.system(message)
            self.chat.refresh_mic()

        self.listener.muted = False
        self.listener.start(ready, error)
        self.chat.refresh_mic()

    def toggle_mic_mute(self) -> None:
        self.listener.muted = not self.listener.muted
        self.chat.refresh_mic()

    def on_heard(self, text: str) -> None:
        if not self.powered or self.listener.state == "off" or self._busy:
            return
        self.voice_out.stop()
        self.send_message(text, spoken=True)

    # ── images: screenshots, pasted, dropped or picked ────────────────────
    def add_image(self, img: Any, label: str) -> bool:
        if len(self.attachments) >= MAX_IMAGES:
            self.chat.system(f"Up to {MAX_IMAGES} images per message — remove one first.")
            return False
        try:
            media, data = encode_image(img)
            thumb = img.convert("RGB").copy()
            thumb.thumbnail((80, 56))
        except Exception as exc:
            self.ctx.set_status(f"Couldn't use that image: {exc}", "error")
            return False
        self.attachments.append({"media": media, "data": data, "thumb": thumb, "label": label})
        self.chat.show_attachments(self.attachments)
        self.chat.set_status(f"{label} attached — sent with your next message")
        self._refresh_status_bar()
        return True

    def remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self.attachments):
            self.attachments.pop(index)
        self.chat.show_attachments(self.attachments)
        self._refresh_status_bar()

    def paste_from_clipboard(self) -> bool:
        """Ctrl+V in the chat: an image (or copied image files) becomes an attachment."""
        if ImageGrab is None:
            return False
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return False
        if Image is not None and isinstance(data, Image.Image):
            return self.add_image(data, "Pasted image")
        if isinstance(data, list):
            files = [Path(f) for f in data if Path(str(f)).suffix.lower() in IMAGE_EXTENSIONS]
            added = False
            for f in files[:MAX_IMAGES]:
                added = self._add_image_file(f) or added
            return added
        return False

    def _add_image_file(self, path: Path) -> bool:
        try:
            with Image.open(path) as img:
                img.load()
                return self.add_image(img.copy(), path.name)
        except Exception as exc:
            self.ctx.set_status(f"Couldn't open {path.name}: {exc}", "error")
            return False

    def pick_image_files(self) -> None:
        from tkinter import filedialog
        names = filedialog.askopenfilenames(
            parent=self.chat.win, title="Attach images",
            filetypes=[("Images", " ".join(f"*{e}" for e in sorted(IMAGE_EXTENSIONS))), ("All files", "*.*")])
        for name in names or ():
            self._add_image_file(Path(name))

    def on_chat_drop(self, payload: Any) -> None:
        for f in payload.files:
            if f.suffix.lower() in IMAGE_EXTENSIONS:
                self._add_image_file(f)
            elif f.suffix.lower() in (".mtchar",):
                self.import_character(f)
        if payload.text and not payload.files and self.chat.win is not None:
            self.chat.entry.insert("end", payload.text)

    # ── characters ────────────────────────────────────────────────────────
    def import_character(self, src: Path) -> str:
        cid = self.library.import_file(Path(src))
        self.select_character(cid)
        name = self.library.characters[cid]["name"]
        self.ctx.set_status(f"Character installed: {name}", "success")
        return f"character {name}"

    def select_character(self, cid: str) -> None:
        if cid not in self.library.characters:
            return
        self.set_setting("character", cid)
        self.character = self.library.characters[cid]
        self.avatar.set_character(self.character, self.library.folders.get(cid),
                                  SIZES.get(self.settings["size"], 1.0))
        self.new_chat(silent=True)
        self.chat.refresh_controls()
        if self.avatar.visible:
            self.avatar.say(f"Hi, I'm {self.character['name']}.", 4)
            self.avatar.gesture("wave", 1.5)
        self._refresh_page(rebuild=True)

    # ── memory ────────────────────────────────────────────────────────────
    def _load_memories(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.memory_file.read_text(encoding="utf-8"))
            return [m for m in data if isinstance(m, dict) and m.get("text")]
        except Exception:
            return []

    def _save_memories(self) -> None:
        tmp = self.memory_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.memories, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.memory_file)

    def add_memory(self, text: str) -> None:
        text = text.strip()
        if text and self.perm("memory"):
            self.memories.append({"id": uuid.uuid4().hex[:8], "text": text[:300], "created": time.strftime("%Y-%m-%d")})
            self._save_memories()
            self.ctx.set_status("Saved to companion memory", "success")
            self._refresh_page()

    def update_memory(self, mid: str, text: str) -> None:
        for m in self.memories:
            if m["id"] == mid:
                m["text"] = text.strip()[:300]
        self.memories = [m for m in self.memories if m["text"]]
        self._save_memories()

    def delete_memory(self, mid: str | None = None) -> None:
        self.memories = [] if mid is None else [m for m in self.memories if m["id"] != mid]
        self._save_memories()
        self._refresh_page()

    # ── screen sharing ────────────────────────────────────────────────────
    def share_screen(self, open_chat: bool = False) -> None:
        if not self.perm("screen") or ImageGrab is None:
            self.ctx.set_status("Screen sharing is turned off on the Permissions page.", "warning")
            return
        was_open = self.chat.is_open
        avatar_visible = self.avatar.visible
        if self.chat.win is not None:
            self.chat.win.withdraw()
        if avatar_visible and self.avatar.win is not None:
            self.avatar.win.withdraw()

        def grab() -> None:
            try:
                img = ImageGrab.grab(all_screens=True)
                self._attach(img, "Screenshot")
            except Exception as exc:
                self.ctx.set_status(f"Screenshot failed: {exc}", "error")
            finally:
                if avatar_visible and self.avatar.win is not None:
                    self.avatar.win.deiconify()
                if was_open or open_chat:
                    self.chat.show()
        self.ctx.app.after(220, grab)

    def choose_window(self) -> None:
        if not self.perm("screen"):
            return
        windows = list_windows()
        if not windows:
            self.ctx.set_status("No other windows found to share.", "warning")
            return
        t = self.ctx.theme
        picker = ctk.CTkToplevel(self.ctx.app)
        picker.title("Share which window?")
        picker.geometry("420x420")
        try:
            picker.attributes("-topmost", True)
        except Exception:
            pass
        t.tag(ctk.CTkLabel(picker, text="Pick a window to send as a one-time screenshot:", anchor="w"),
              "muted").pack(fill="x", padx=12, pady=(12, 4))
        listing = t.tag(ctk.CTkScrollableFrame(picker), "transparent")
        listing.pack(fill="both", expand=True, padx=8, pady=8)

        def pick(hwnd: int, title: str, rect: tuple[int, int, int, int]) -> None:
            picker.destroy()
            try:
                self._attach(capture_window(hwnd, rect), f"Window: {title[:40]}")
            except Exception as exc:
                self.ctx.set_status(f"Couldn't capture that window: {exc}", "error")
            self.chat.show()

        for hwnd, title, rect in windows[:40]:
            t.tag(ctk.CTkButton(listing, text=title[:60], anchor="w",
                                command=lambda h=hwnd, ti=title, r=rect: pick(h, ti, r)), "secondary").pack(
                fill="x", pady=2)
        t.apply(picker)

    def _attach(self, img: Any, label: str) -> None:
        self.add_image(img, label)

    def clear_attachment(self) -> None:
        self.attachments = []
        if self.chat.win is not None:
            self.chat.show_attachments([])
            self.chat.set_status("Idle")

    # ── conversation ──────────────────────────────────────────────────────
    def new_chat(self, silent: bool = False) -> None:
        self._cancel.set()
        self.history.clear()
        self.clear_attachment()
        self.chat.clear()
        if not silent:
            self.chat.system("New conversation.")

    def send_message(self, text: str, spoken: bool = False) -> None:
        if self._busy:
            self.chat.system("Still answering the last message — press ■ to cancel it.")
            return
        self.power_on() if not self.powered else None
        self.voice_out.stop()
        self.speech.stop()
        self.avatar.stop_speaking()
        images = [(a["media"], a["data"]) for a in self.attachments]
        self.history.append({"role": "user", "text": text, "images": images})
        self.chat.add_user(text or "(image)", len(images), spoken)
        if images:
            self.clear_attachment()

        provider = self.settings["provider"]
        key = self.api_key()
        manifest = self.local_manifest() if provider == "local" else None
        needs_setup = (provider == "none" or (provider == "anthropic" and not key)
                       or (provider == "local" and (manifest is None or not self.engine.available())))
        if needs_setup:
            if provider == "local" and not self.engine.available():
                reply = "My AI engine is missing from Juno's bin folder — reinstalling Juno will fix it."
            elif provider == "local":
                reply = ("I need my brain first! It's a one-time download that then runs entirely on your PC — "
                         "press Download just above, and I'll be ready in a few minutes.")
                self.chat.offer_setup(self)
            elif provider == "none":
                reply = "My AI is switched off. Turn it on on the Brain page."
            else:
                reply = "I'm set to use the Claude API but there's no key yet — add one on the Brain page."
            self.chat.begin_reply()
            self.chat.stream(reply)
            self.chat.end_reply()
            self.avatar.set_expression("concerned", 4)
            self.history.append({"role": "assistant", "text": reply, "images": []})
            return

        self._busy = True
        self._cancel = threading.Event()
        cancel = self._cancel
        self.avatar.mode = "thinking"
        self.chat.set_status("Thinking…")
        self.chat.begin_reply()
        speak = self.can_speak()
        system = build_system_prompt(self.character, self.personality(), self.settings.get("instructions") or "",
                                     [m["text"] for m in self.memories], speak, self.perm("memory"))
        messages = prepare_history(self.history)
        flt = ReplyFilter()
        first = {"seen": False}

        def on_text(chunk: str) -> None:
            shown = flt.feed(chunk)
            if shown:
                self.ctx.call_in_ui(self._on_stream_text, shown, first)

        def work() -> None:
            model = self.settings.get("model") or CLAUDE_MODELS[0]
            if provider == "local":
                status = lambda text: self.ctx.call_in_ui(self.chat.set_status, text)
                for attempt in range(2):
                    base = self.engine.ensure(manifest, status)
                    status("Thinking…")
                    try:
                        stream_openai_compatible("", "local", system, messages, on_text, cancel, base,
                                                 extra=dict(LOCAL_SAMPLING), max_tokens=400)
                        self.engine.last_used = time.monotonic()
                        return
                    except ProviderError:
                        if attempt == 0 and self.engine.crashed_on_gpu() and not flt.raw:
                            self.engine.vulkan_bad = True  # GPU driver trouble: retry on the processor
                            self.engine.stop()
                            continue
                        raise
            if provider == "openai":
                stream_openai_compatible(key, model, system, messages, on_text, cancel,
                                         self.settings.get("base_url") or "https://api.openai.com/v1")
            else:
                stream_anthropic(key, model, system, messages, on_text, cancel,
                                 self.settings.get("base_url") or "https://api.anthropic.com")

        def done(_result: Any) -> None:
            rest, clean, expression, memories = flt.finish()
            if rest:
                self.chat.stream(rest)
            self.chat.end_reply()
            self._busy = False
            if cancel.is_set():
                self.avatar.mode = "idle"
                self.chat.set_status("Stopped")
                return
            self.history.append({"role": "assistant", "text": flt.raw.strip(), "images": []})
            self.avatar.mode = "idle"
            self.avatar.set_expression(expression, 6)
            if memories and self.perm("memory"):
                self.chat.offer_memories(memories)
            if not self.chat.is_open and clean:
                self.avatar.say(clean, 8)
            if speak and clean:
                self.speak(clean)
            else:
                self.chat.set_status("Idle")

        def failed(exc: BaseException) -> None:
            self._busy = False
            self.avatar.mode = "idle"
            self.chat.end_reply()
            message = str(exc) if isinstance(exc, ProviderError) else f"Something went wrong: {exc}"
            self.chat.system(message)
            self.chat.set_status("Error")
            self.avatar.set_expression("concerned", 4)
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()

        self.ctx.run_in_thread(work, on_done=done, on_error=failed)

    def _on_stream_text(self, text: str, first: dict[str, bool]) -> None:
        if not first["seen"]:
            first["seen"] = True
            self.avatar.mode = "idle"
            self.chat.set_status("Replying…")
        self.chat.stream(text)

    def speak(self, text: str) -> None:
        if not self.can_speak():
            self.chat.set_status("Idle")
            return
        self.chat.set_status("Speaking…")
        self.voice_out.say(text, on_done=lambda: self.chat.set_status("Idle"))

    # ── main-window page ──────────────────────────────────────────────────
    def get_frame(self, master: Any) -> Any:
        self._master = master
        self.page = ctk.CTkFrame(master, fg_color="transparent")
        self._build_page()
        return self.page

    def _on_drop(self, payload: Any) -> None:
        for f in payload.files:
            if f.suffix.lower() in (".mtchar", ".json"):
                try:
                    self.import_character(f)
                except Exception as exc:
                    self.ctx.set_status(f"Couldn't import {f.name}: {exc}", "error")
            else:
                self.ctx.set_status("Drop a character pack (.mtchar) here.", "warning")

    def _refresh_page(self, rebuild: bool = False) -> None:
        if not self._built:
            return
        if rebuild:
            self.ctx.app.after(10, self._build_page)
        else:
            self._refresh_status_bar()

    def _build_page(self) -> None:
        t = self.ctx.theme
        current_tab = None
        if getattr(self, "_tabs", None) is not None:
            try:
                current_tab = self._tabs.get()
            except Exception:
                current_tab = None
        for child in self.page.winfo_children():
            child.destroy()
        page = self.page
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        top = t.tag(ctk.CTkFrame(page), "card")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top.grid_columnconfigure(1, weight=1)
        self.top_status = t.tag(ctk.CTkLabel(top, text="", anchor="w", justify="left"), "text")
        self.top_status.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 6))
        bar = t.tag(ctk.CTkFrame(top, fg_color="transparent"), "transparent")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 12))
        name = self.character["name"]
        self.power_btn = t.tag(ctk.CTkButton(bar, text=f"⏻  Shut down {name}" if self.powered else f"⏻  Switch on {name}",
                                             width=170, height=34, command=self.toggle_power),
                               "danger" if self.powered else "accent")
        self.power_btn.pack(side="left")
        t.tag(ctk.CTkButton(bar, text="Open chat", width=100, height=34, command=self.open_chat), "secondary").pack(
            side="left", padx=6)
        t.tag(ctk.CTkButton(bar, text="🎙 Voice chat", width=120, height=34, command=self.toggle_voice_chat),
              "secondary").pack(side="left")
        self.show_btn = t.tag(ctk.CTkButton(bar, text="", width=110, height=34, command=self._toggle_visible), "ghost")
        if self.powered:
            self.show_btn.pack(side="left", padx=6)
            t.tag(ctk.CTkButton(bar, text="Back to corner", width=120, height=34, command=self.reset_home),
                  "ghost").pack(side="left")
        self.dnd_var = ctk.BooleanVar(value=bool(self.settings["dnd"]))
        ctk.CTkSwitch(bar, text="Do Not Disturb", variable=self.dnd_var,
                      command=lambda: self.toggle_dnd() if self.dnd_var.get() != self.settings["dnd"] else None).pack(
            side="right")

        tabs = ctk.CTkTabview(page)
        tabs.grid(row=1, column=0, sticky="nsew")
        for name in ("Character", "Personality", "AI & Voice", "Permissions", "Memory"):
            tabs.add(name)
        def scrolling(name: str) -> Any:  # tab content can be taller than the window
            box = t.tag(ctk.CTkScrollableFrame(tabs.tab(name), fg_color="transparent"), "transparent")
            box.pack(fill="both", expand=True)
            return box
        self._tab_character(scrolling("Character"))
        self._tab_personality(scrolling("Personality"))
        self._tab_ai(scrolling("AI & Voice"))
        self._tab_permissions(scrolling("Permissions"))
        self._tab_memory(tabs.tab("Memory"))  # has its own scrolling list
        self._tabs = tabs
        if current_tab:
            try:
                tabs.set(current_tab)
            except Exception:
                pass
        self._built = True
        t.apply(page)
        self._refresh_status_bar()
        self.ctx.register_drop_target(page, self._on_drop, accept=("files",))

    def _refresh_status_bar(self) -> None:
        if not self._built:
            return
        if not self.powered:
            self.top_status.configure(text=f"{self.character['name']} is switched off — nothing is running and "
                                           f"no memory is used. Switch it on whenever you want company.")
            return
        bits = [f"{self.character['name']} is " + ("on your desktop" if self.avatar.visible else "hidden (still on)")]
        active = []
        if self.attachments:
            active.append(f"{len(self.attachments)} image(s) attached")
        if self.listener.state != "off":
            active.append("voice chat on" + (" (you're muted)" if self.listener.muted else ""))
        if self.avatar.mode == "speaking":
            active.append("speaking")
        if self.settings["dnd"]:
            active.append("Do Not Disturb")
        provider = self.settings["provider"]
        if provider == "anthropic" and not self.api_key():
            active.append("no API key yet — see AI & Voice")
        if provider == "local":
            if self.local_manifest() is None:
                active.append("brain not downloaded yet — see AI & Voice")
            elif self.engine.running():
                active.append("AI loaded on " + ("graphics card" if self.engine.backend == "vulkan" else "processor"))
        if active:
            bits.append("  ·  ".join(active))
        self.top_status.configure(text="   ·   ".join(bits))
        self.show_btn.configure(text="Hide" if self.avatar.visible else "Show on desktop")

    def _toggle_visible(self) -> None:
        self.hide_avatar() if self.avatar.visible else self.show_avatar()

    def _row(self, parent: Any, row: int, label: str, widget_factory: Callable[[Any], Any], hint: str = "") -> None:
        t = self.ctx.theme
        t.tag(ctk.CTkLabel(parent, text=label, anchor="w", width=150), "text").grid(
            row=row, column=0, sticky="nw", padx=(4, 12), pady=6)
        holder = t.tag(ctk.CTkFrame(parent, fg_color="transparent"), "transparent")
        holder.grid(row=row, column=1, sticky="ew", pady=6)
        widget_factory(holder)
        if hint:
            label_ = t.tag(ctk.CTkLabel(holder, text=hint, anchor="w", justify="left", wraplength=560), "muted")
            if holder.grid_slaves():   # the row's widgets use grid — can't mix in pack
                label_.grid(row=99, column=0, columnspan=3, sticky="ew", pady=(2, 0))
            else:
                label_.pack(fill="x", pady=(2, 0))

    def _tab_character(self, tab: Any) -> None:
        t = self.ctx.theme
        tab.grid_columnconfigure(1, weight=1)
        names = {c["name"] + ("" if cid == "juno" else f"  ({cid})"): cid for cid, c in self.library.characters.items()}
        current = next(n for n, cid in names.items() if cid == self.character["id"])

        def picker(holder: Any) -> None:
            var = ctk.StringVar(value=current)
            ctk.CTkOptionMenu(holder, values=list(names), variable=var, width=240,
                              command=lambda n: self.select_character(names[n])).pack(side="left")
            ctk.CTkButton(holder, text="Import character…", width=140, command=self._ask_import).pack(side="left", padx=8)
            t.tag(ctk.CTkButton(holder, text="Export template…", width=140, command=self._ask_export),
                  "secondary").pack(side="left")
            if self.library.folders.get(self.character["id"]) is not None:
                t.tag(ctk.CTkButton(holder, text="Remove", width=80, command=self._remove_character), "ghost").pack(
                    side="left", padx=6)
        self._row(tab, 0, "Character", picker)
        c = self.character
        info = f"{c['description']}\nBy {c.get('author') or 'unknown'} · v{c.get('version', '1.0')} · " \
               f"{'custom artwork' if c['look'].get('rig') == 'sprite' else 'drawn body, ' + c['look'].get('hair_style', 'bob') + ' hair'}"
        t.tag(ctk.CTkLabel(tab, text=info, anchor="w", justify="left", wraplength=640), "muted").grid(
            row=1, column=1, sticky="ew", pady=(0, 10))

        def seg(key: str, values: list[str], labels: dict[str, str] | None = None) -> Callable[[Any], None]:
            def build(holder: Any) -> None:
                labels_ = labels or {v: v for v in values}
                inverse = {v: k for k, v in labels_.items()}
                var = ctk.StringVar(value=labels_.get(self.settings[key], values[0]))

                def changed(label: str) -> None:
                    self.set_setting(key, inverse[label])
                    if key == "size":
                        self.avatar.set_character(self.character, self.library.folders.get(self.character["id"]),
                                                  SIZES[inverse[label]])
                        if self.avatar.visible:
                            self.avatar.show()
                ctk.CTkSegmentedButton(holder, values=[labels_[v] for v in values], variable=var,
                                       command=changed).pack(anchor="w")
            return build
        self._row(tab, 2, "Size", seg("size", ["S", "M", "L"], {"S": "Small", "M": "Medium", "L": "Large"}))
        self._row(tab, 3, "Activity", seg("activity", ["quiet", "occasional", "chatty"],
                                          {"quiet": "Quiet", "occasional": "Occasional", "chatty": "Chatty"}),
                  "How often they fidget, wander and (if Chatty) pipe up with a short line.")

        def switches(holder: Any) -> None:
            for key, text in (("walk", "Stroll around the desktop in mini mode"), ("reduced_motion", "Reduced motion"),
                              ("greet", "Greet me when Juno starts")):
                var = ctk.BooleanVar(value=bool(self.settings[key]))
                ctk.CTkSwitch(holder, text=text, variable=var,
                              command=lambda k=key, v=var: self.set_setting(k, v.get())).pack(anchor="w", pady=2)
        self._row(tab, 4, "Behaviour", switches,
                  "Drag the character anywhere; click it to open chat; right-click for more.")

    def _tab_personality(self, tab: Any) -> None:
        t = self.ctx.theme
        tab.grid_columnconfigure(1, weight=1)
        values = self.personality()
        for row, (axis, left, right) in enumerate(PERSONALITY_AXES):
            def build(holder: Any, axis: str = axis, left: str = left, right: str = right) -> None:
                t.tag(ctk.CTkLabel(holder, text=left, width=80, anchor="e"), "muted").pack(side="left")
                slider = ctk.CTkSlider(holder, from_=0, to=1, number_of_steps=20, width=320,
                                       command=lambda v, a=axis: self._set_axis(a, float(v)))
                slider.set(values.get(axis, 0.5))
                slider.pack(side="left", padx=10)
                t.tag(ctk.CTkLabel(holder, text=right, width=80, anchor="w"), "muted").pack(side="left")
            self._row(tab, row, axis.capitalize(), build)

        def reset(holder: Any) -> None:
            t.tag(ctk.CTkButton(holder, text="Reset to character defaults", width=200, command=self._reset_axes),
                  "secondary").pack(anchor="w")
        self._row(tab, len(PERSONALITY_AXES), "", reset)

        def instructions(holder: Any) -> None:
            box = ctk.CTkTextbox(holder, height=110, wrap="word")
            box.insert("1.0", self.settings.get("instructions") or "")
            box.pack(fill="x")
            box.bind("<FocusOut>", lambda _e: self.set_setting("instructions", box.get("1.0", "end").strip()[:2000]))
        self._row(tab, len(PERSONALITY_AXES) + 1, "Your instructions", instructions,
                  "e.g. “Keep answers short”, “Call me Sam”, “Don't interrupt while I'm focused”. Saved when you "
                  "click away.")

    def _set_axis(self, axis: str, value: float) -> None:
        all_ = dict(self.settings.get("personality") or {})
        mine = dict(all_.get(self.character["id"], {}))
        mine[axis] = round(value, 2)
        all_[self.character["id"]] = mine
        self.set_setting("personality", all_)

    def _reset_axes(self) -> None:
        all_ = dict(self.settings.get("personality") or {})
        all_.pop(self.character["id"], None)
        self.set_setting("personality", all_)
        self._refresh_page(rebuild=True)

    def _tab_ai(self, tab: Any) -> None:
        t = self.ctx.theme
        tab.grid_columnconfigure(1, weight=1)
        providers = {"On this PC (free, private)": "local", "Claude API (online)": "anthropic",
                     "OpenAI-compatible server": "openai", "Off": "none"}
        inverse = {v: k for k, v in providers.items()}
        provider_now = self.settings["provider"]
        row = 0

        def provider(holder: Any) -> None:
            var = ctk.StringVar(value=inverse.get(provider_now, "On this PC (free, private)"))
            ctk.CTkOptionMenu(holder, values=list(providers), variable=var, width=260,
                              command=lambda n: (self.set_setting("provider", providers[n]),
                                                 self._refresh_page(rebuild=True))).pack(anchor="w")
        self._row(tab, row, "AI", provider,
                  "“On this PC” runs a small open-source model (Qwen3.5, Apache 2.0) entirely offline — no account, "
                  "no key, nothing leaves your computer." if provider_now == "local" else
                  "Online services need an account and API key and are billed by the provider.")
        row += 1

        if provider_now == "local":
            def brain(holder: Any) -> None:
                text, role = self._brain_status()
                label = t.tag(ctk.CTkLabel(holder, text=text, anchor="w", justify="left", wraplength=600), role)
                label.grid(row=0, column=0, columnspan=3, sticky="ew")
                bar = ctk.CTkProgressBar(holder, width=420)
                bar.set(0)
                bar.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
                bar.grid_remove()
                installed = self.local_manifest() is not None
                buttons = t.tag(ctk.CTkFrame(holder, fg_color="transparent"), "transparent")
                buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
                if not installed or self.models.downloading:
                    primary = ctk.CTkButton(buttons, text="Download", width=150, command=self.start_download)
                    primary.pack(side="left")
                else:
                    primary = t.tag(ctk.CTkButton(buttons, text="Talk now", width=110,
                                                  command=lambda: (self.avatar.show(), self.chat.show())), "accent")
                    primary.pack(side="left")
                    t.tag(ctk.CTkButton(buttons, text="Delete model", width=110, command=self.delete_model),
                          "ghost").pack(side="left", padx=6)
                self._dl_widgets = {"label": label, "bar": bar, "primary": primary}
                self._update_download_ui() if self.models.downloading or self._download_state.get("error") else None
            self._row(tab, row, "Brain", brain)
            row += 1

            def sizes(holder: Any) -> None:
                labels = {k: f"{v['label']} · {v['approx_gb']:.1f} GB" for k, v in LOCAL_MODELS.items()}
                inverse_l = {v: k for k, v in labels.items()}
                var = ctk.StringVar(value=labels[self.settings["local_size"]])

                def changed(label: str) -> None:
                    if self.models.downloading:
                        var.set(labels[self.settings["local_size"]])
                        return
                    self.set_setting("local_size", inverse_l[label])
                    self.ctx.run_in_thread(self.engine.stop)
                    self._refresh_page(rebuild=True)
                ctk.CTkSegmentedButton(holder, values=list(labels.values()), variable=var, command=changed).pack(
                    anchor="w")
                rec = recommended_model()
                ram = system_ram_gb()
                info = LOCAL_MODELS[self.settings["local_size"]]
                t.tag(ctk.CTkLabel(holder, anchor="w", justify="left", wraplength=600,
                                   text=f"{info['name']}: {info['about']}  ·  Your PC has {ram:.0f} GB of RAM — "
                                        f"{LOCAL_MODELS[rec]['label']} is recommended."), "muted").pack(
                    fill="x", pady=(4, 0))
            self._row(tab, row, "Model size", sizes)
            row += 1

            def backend(holder: Any) -> None:
                labels = {"auto": "Auto", "gpu": "Graphics card", "cpu": "Processor only"}
                inverse_b = {v: k for k, v in labels.items()}
                var = ctk.StringVar(value=labels.get(self.settings["local_backend"], "Auto"))

                def changed(label: str) -> None:
                    self.set_setting("local_backend", inverse_b[label])
                    self.engine.vulkan_bad = False
                    self.ctx.run_in_thread(self.engine.stop)
                ctk.CTkSegmentedButton(holder, values=list(labels.values()), variable=var, command=changed).pack(
                    anchor="w")
            self._row(tab, row, "Runs on", backend,
                      "Auto uses your graphics card when it can and falls back to the processor.")
            row += 1

            def idle(holder: Any) -> None:
                labels = {"5": "5 min", "15": "15 min", "60": "1 hour", "never": "Never"}
                inverse_i = {v: k for k, v in labels.items()}
                var = ctk.StringVar(value=labels.get(self.settings["local_idle"], "15 min"))
                ctk.CTkSegmentedButton(holder, values=list(labels.values()), variable=var,
                                       command=lambda l: self.set_setting("local_idle", inverse_i[l])).pack(anchor="w")
            self._row(tab, row, "Free memory after", idle,
                      "The model uses a few GB of RAM while loaded; it reloads in a few seconds when you chat again.")
            row += 1
        elif provider_now in ("anthropic", "openai"):
            def key(holder: Any) -> None:
                entry = ctk.CTkEntry(holder, show="•", width=360,
                                     placeholder_text="paste your API key" if not self.settings.get("api_key")
                                     else "saved — paste a new key to replace it")
                entry.pack(side="left")

                def save() -> None:
                    value = entry.get().strip()
                    if value:
                        self.set_setting("api_key", protect_secret(value))
                        entry.delete(0, "end")
                        self.ctx.set_status("API key saved (encrypted for your Windows account)", "success")
                        self._refresh_status_bar()
                ctk.CTkButton(holder, text="Save key", width=90, command=save).pack(side="left", padx=6)
                t.tag(ctk.CTkButton(holder, text="Forget key", width=90,
                                    command=lambda: (self.set_setting("api_key", ""), self._refresh_status_bar())),
                      "ghost").pack(side="left")
            self._row(tab, row, "API key", key)
            row += 1

            def model(holder: Any) -> None:
                var = ctk.StringVar(value=self.settings.get("model") or CLAUDE_MODELS[0])
                values = CLAUDE_MODELS if provider_now == "anthropic" else [var.get()]
                box = ctk.CTkComboBox(holder, values=values, variable=var, width=260,
                                      command=lambda v: self.set_setting("model", v))
                box.pack(side="left")
                box.bind("<FocusOut>", lambda _e: self.set_setting("model", var.get().strip()))
            self._row(tab, row, "Model", model)
            row += 1
            if provider_now == "openai":
                def base(holder: Any) -> None:
                    entry = ctk.CTkEntry(holder, width=360, placeholder_text="https://api.openai.com/v1")
                    entry.insert(0, self.settings.get("base_url") or "")
                    entry.pack(anchor="w")
                    entry.bind("<FocusOut>", lambda _e: self.set_setting("base_url", entry.get().strip()))
                self._row(tab, row, "Base URL", base, "Any server that speaks the OpenAI chat-completions format.")
                row += 1

        def test(holder: Any) -> None:
            t.tag(ctk.CTkButton(holder, text="Test chat", width=100, command=self._test_ai), "secondary").pack(anchor="w")
        if provider_now != "none":
            self._row(tab, row, "", test)
            row += 1

        def voice(holder: Any) -> None:
            config = self.voice_config()
            engines = {"Natural (best)": "auto", "Kokoro (fastest)": "neural", "Windows voices": "windows"}
            choice = self.settings.get("voice_engine", "auto")
            seg = ctk.CTkSegmentedButton(holder, values=list(engines),
                                         command=lambda v: (self.set_setting("voice_engine", engines[v]),
                                                            self._refresh_page(rebuild=True),
                                                            self.chat.refresh_controls()))
            seg.set({"windows": "Windows voices", "neural": "Kokoro (fastest)"}.get(choice, "Natural (best)"))
            seg.pack(anchor="w")
            wants = "natural" if choice in ("auto", "natural") else "neural" if choice == "neural" else ""
            if wants and config["engine"] != wants:
                note = (f"{'Natural' if wants == 'natural' else 'Kokoro'} voices need a one-time download below — "
                        f"until then {'Kokoro' if config['engine'] == 'neural' else 'a Windows voice'} is used."
                        if sherpa_ok() else "These voices need the voice engine from a newer Juno build.")
                t.tag(ctk.CTkLabel(holder, text=note, anchor="w"), "warning").pack(anchor="w", pady=(4, 0))
            line = t.tag(ctk.CTkFrame(holder, fg_color="transparent"), "transparent")
            line.pack(anchor="w", pady=(8, 2))
            t.tag(ctk.CTkLabel(line, text="Voice", width=60, anchor="w"), "muted").pack(side="left")
            names, selected, setter = self.voice_choices()
            self._voice_menu = ctk.CTkOptionMenu(line, values=names or ["(none)"], width=280, dynamic_resizing=False,
                                                 command=lambda v: (setter(v), self.chat.refresh_controls()))
            self._voice_menu.set(selected or (names[0] if names else "(none)"))
            self._voice_menu.pack(side="left", padx=8)
            t.tag(ctk.CTkButton(line, text="▶  Try it", width=90, command=self.preview_voice), "secondary").pack(
                side="left")

            line = t.tag(ctk.CTkFrame(holder, fg_color="transparent"), "transparent")
            line.pack(anchor="w", pady=2)
            t.tag(ctk.CTkLabel(line, text="Speed", width=60, anchor="w"), "muted").pack(side="left")
            lo, hi = SPEED_RANGE
            value = t.tag(ctk.CTkLabel(line, text=f"{self.voice_speed():.2f}×", width=50), "text")
            slider = ctk.CTkSlider(line, from_=lo, to=hi, number_of_steps=int(round((hi - lo) / 0.05)), width=280,
                                   command=lambda v: (value.configure(text=f"{float(v):.2f}×"),
                                                      self.set_setting("voice_speed", round(float(v), 2))))
            slider.set(self.voice_speed())
            slider.pack(side="left", padx=8)
            value.pack(side="left")
            t.tag(ctk.CTkButton(line, text="Reset", width=60,
                                command=lambda: (self.set_setting("voice_speed", None),
                                                 self._refresh_page(rebuild=True))), "ghost").pack(side="left", padx=6)
            if config["engine"] == "windows":
                for key_, label in (("voice_pitch", "Pitch"), ("voice_volume", "Volume")):
                    line = t.tag(ctk.CTkFrame(holder, fg_color="transparent"), "transparent")
                    line.pack(anchor="w", pady=2)
                    t.tag(ctk.CTkLabel(line, text=label, width=60, anchor="w"), "muted").pack(side="left")
                    lo_, hi_ = (-40, 40) if key_ == "voice_volume" else (-5, 5)
                    sl = ctk.CTkSlider(line, from_=lo_, to=hi_, number_of_steps=hi_ - lo_, width=280,
                                       command=lambda v, k=key_: self.set_setting(k, int(v)))
                    sl.set(int(self.settings.get(key_) or 0))
                    sl.pack(side="left", padx=8)
        self._row(tab, row, "Voice", voice,
                  "Natural voices sound the most human; Kokoro is lighter and faster. Both run offline after a "
                  "one-time download. Add your own voice: put a 5–10 second .wav of someone talking in "
                  "Juno's data folder ▸ voices (only voices you have permission to use).")
        row += 1

        self._addon_widgets = {}
        for key_ in ("natural", "voices", "listen"):
            def addon(holder: Any, key_: str = key_) -> None:
                holder.grid_columnconfigure(0, weight=1)
                installed = self.addons.installed(key_)
                label = t.tag(ctk.CTkLabel(holder, text=self.addon_text(key_), anchor="w", justify="left",
                                           wraplength=520), "success" if installed else "text")
                label.grid(row=0, column=0, sticky="ew")
                button = t.tag(ctk.CTkButton(holder, text="", width=110), "ghost" if installed else "accent")
                button.grid(row=0, column=1, sticky="e", padx=(8, 0))
                bar = ctk.CTkProgressBar(holder, width=420)
                bar.set(0)
                self._addon_widgets[key_] = {"label": label, "button": button, "bar": bar}
                self.update_addon_ui(key_)
            self._row(tab, row, VOICE_ADDONS[key_]["name"], addon,
                      "Talk out loud and Juno answers out loud. Turn it on with 🎙 in the chat." if key_ == "listen"
                      else "")
            row += 1

    def _refresh_voice_menu(self) -> None:
        menu = getattr(self, "_voice_menu", None)
        if menu is not None:
            try:
                menu.configure(values=["Character default"] + self.speech.voices)
            except Exception:
                pass
        elif self._built:
            self._refresh_page(rebuild=True)

    def _test_ai(self) -> None:
        self.avatar.show()
        self.chat.show()
        self.send_message("Quick systems check — say hi in one sentence.")

    def _tab_permissions(self, tab: Any) -> None:
        t = self.ctx.theme
        tab.grid_columnconfigure(1, weight=1)
        rows = [
            ("screen", "Screenshots & window sharing",
             "Only when you click Screenshot / Share a window. Images are sent with that one message and never saved."),
            ("memory", "Saved memory", "Lets the companion suggest facts to remember. Nothing is saved without your "
                                       "click; manage everything on the Memory tab."),
            ("voice", "Speak replies aloud", "Neural or Windows voices, both offline. Nothing leaves your PC for "
                                             "speech."),
            ("mic", "Microphone (voice chat)", "Only listens while 🎙 voice chat is on — never in the background. "
                                               "Speech is recognised on your PC; audio is never saved or sent."),
            ("proactive", "Greetings & idle lines", "Short speech-bubble lines (never spoken). Do Not Disturb "
                                                    "silences them."),
        ]
        for i, (key, title, desc) in enumerate(rows):
            def build(holder: Any, key: str = key, desc: str = desc) -> None:
                var = ctk.BooleanVar(value=self.perm(key))

                def changed() -> None:
                    self.set_setting(f"perm_{key}", var.get())
                    if key == "voice" and not var.get():
                        self.voice_out.stop()
                        self.speech.stop()
                        self.avatar.stop_speaking()
                    if key == "mic" and not var.get():
                        self.listener.stop()
                    if key == "screen" and not var.get():
                        self.clear_attachment()
                    self.chat.refresh_controls()
                ctk.CTkSwitch(holder, text="Allowed", variable=var, command=changed).pack(anchor="w")
                t.tag(ctk.CTkLabel(holder, text=desc, anchor="w", justify="left", wraplength=560), "muted").pack(
                    fill="x")
            self._row(tab, i, title, build)
        for j, (title, desc) in enumerate((
                ("Computer actions", "Not available. The companion is read-only by design: it can suggest, never act."),)):
            def build(holder: Any, desc: str = desc) -> None:
                sw = ctk.CTkSwitch(holder, text="Unavailable")
                sw.configure(state="disabled")
                sw.pack(anchor="w")
                t.tag(ctk.CTkLabel(holder, text=desc, anchor="w", justify="left", wraplength=560), "muted").pack(fill="x")
            self._row(tab, len(rows) + j, title, build)

    def _tab_memory(self, tab: Any) -> None:
        t = self.ctx.theme
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(tab, anchor="w", justify="left", wraplength=700,
                           text="Facts the companion keeps across chats. Edit any line and press Enter, or delete it. "
                                "Shared screenshots are never stored here."), "muted").grid(
            row=0, column=0, sticky="ew", pady=(4, 8))
        listing = t.tag(ctk.CTkScrollableFrame(tab, height=220), "transparent")
        listing.grid(row=1, column=0, sticky="nsew")
        listing.grid_columnconfigure(0, weight=1)
        if not self.memories:
            t.tag(ctk.CTkLabel(listing, text="Nothing saved yet.", anchor="w"), "muted").grid(row=0, column=0, sticky="w")
        for i, m in enumerate(self.memories):
            entry = ctk.CTkEntry(listing)
            entry.insert(0, m["text"])
            entry.grid(row=i, column=0, sticky="ew", pady=2, padx=(0, 6))
            entry.bind("<Return>", lambda _e, mid=m["id"], en=entry: (self.update_memory(mid, en.get()),
                                                                        self.ctx.set_status("Memory updated", "success")))
            t.tag(ctk.CTkButton(listing, text="Delete", width=70,
                                command=lambda mid=m["id"]: (self.delete_memory(mid),
                                                             self._refresh_page(rebuild=True))), "ghost").grid(
                row=i, column=1, pady=2)
        add = t.tag(ctk.CTkFrame(tab, fg_color="transparent"), "transparent")
        add.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        add.grid_columnconfigure(0, weight=1)
        new = ctk.CTkEntry(add, placeholder_text="Add something to remember…")
        new.grid(row=0, column=0, sticky="ew")

        def add_new() -> None:
            if new.get().strip():
                self.add_memory(new.get())
                self._refresh_page(rebuild=True)
        new.bind("<Return>", lambda _e: add_new())
        ctk.CTkButton(add, text="Add", width=70, command=add_new).grid(row=0, column=1, padx=6)
        t.tag(ctk.CTkButton(add, text="Clear all", width=80,
                            command=lambda: (self.delete_memory(None), self._refresh_page(rebuild=True))),
              "danger").grid(row=0, column=2)

    # ── import/export dialogs ─────────────────────────────────────────────
    def _ask_import(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askopenfilename(parent=self.ctx.app, title="Import a character pack",
                                            filetypes=[("Character packs", "*.mtchar"), ("character.json", "*.json")])
        if chosen:
            try:
                self.import_character(Path(chosen))
            except Exception as exc:
                self.ctx.set_status(f"Couldn't import character: {exc}", "error")

    def _ask_export(self) -> None:
        from tkinter import filedialog
        dest = filedialog.asksaveasfilename(parent=self.ctx.app, title="Save character template",
                                            defaultextension=".mtchar", initialfile="my-character.mtchar",
                                            filetypes=[("Character packs", "*.mtchar")])
        if dest:
            CharacterLibrary.export_template(Path(dest))
            self.ctx.set_status("Template saved — unzip it, edit character.json, zip it back as .mtchar", "success")

    def _remove_character(self) -> None:
        cid = self.character["id"]
        try:
            self.library.remove(cid)
        except Exception as exc:
            self.ctx.set_status(str(exc), "error")
            return
        self.select_character("juno")
        self.ctx.set_status("Character removed", "success")
