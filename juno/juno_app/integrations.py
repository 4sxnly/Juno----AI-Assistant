"""
Juno integrations — how Juno does things on your PC.

RULE: an integration only runs when you explicitly ask for something it covers ("Hey Juno, open Spotify",
"pause the music", "google cheap flights"). Ordinary chat never triggers one. The router works in three steps:

  1. Your phrases ("Blender" → Blender.exe) and every integration's command patterns are matched against what
     you said. A match runs straight away — no AI involved.
  2. If no pattern matched but you named an integration ("put some lofi on in Spotify"), the AI is shown ONLY
     that integration's actions and picks one (with its details).
  3. Otherwise it's just conversation.

ADDING YOUR OWN (no restart needed — Integrations ▸ Reload)
  • Phrases page: map a word to an app, file, folder or website. "open <phrase>" (or just "<phrase>") opens it.
  • .junoint file (JSON, no code) dropped on Juno or put in %APPDATA%\\Juno\\integrations:

        {"id": "obs", "name": "OBS Studio", "aliases": ["obs"], "description": "Recording",
         "actions": [
           {"title": "Open OBS", "phrases": ["open obs", "start obs"], "do": {"open": "obs64"}},
           {"title": "Start recording", "phrases": ["start recording"], "do": {"keys": "ctrl+shift+f9"}},
           {"title": "Search the OBS docs", "phrases": ["obs help {query}"],
            "do": {"url": "https://obsproject.com/kb/search?q={query}"}, "say": "Searching the OBS docs."}]}

     "do" kinds: open (app name from the Start menu, path, or URI), url (template), keys (hotkey such as
     "ctrl+shift+f9", "media_play_pause", "volume_up"), run (["program", "arg", ...] — no shell), say (text).
  • .py file in the same folder for anything smarter — subclass Integration:

        from juno_app.integrations import Integration, Action, Field

        class MyThing(Integration):
            id, name, aliases = "mything", "My Thing", ["my thing"]
            fields = [Field("token", "API token", kind="secret")]
            def actions(self):
                return [Action("hello", "Say hello", ["say hello to my thing"], self.hello)]
            def hello(self, api, params):
                return "Hello from my thing!"          # or ActionResult(...)

PATTERN SYNTAX: words match words; {slot} captures text; [optional words]; (a|b) alternatives.
"""

from __future__ import annotations

import ctypes
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = sys.platform == "win32"


# ════════════════════════════════════════════════════════════════════════════
#  Framework
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"        # text | path | folder | secret | bool | choice
    default: Any = ""
    help: str = ""
    choices: list[str] = field(default_factory=list)


@dataclass
class ActionResult:
    message: str                      # shown in the chat as an [Action] line
    say: str | None = None            # spoken reply (defaults to the message)
    ok: bool = True
    data: Any = None


@dataclass
class Action:
    id: str
    title: str
    patterns: list[str]
    run: Callable[["IntegrationAPI", dict[str, str]], Any]
    description: str = ""
    params: dict[str, str] = field(default_factory=dict)   # slot → what it means (for the AI fallback)
    examples: list[str] = field(default_factory=list)
    background: bool = True                                # run off the UI thread


class Integration:
    """Base class. Subclasses set id/name/aliases and return their actions."""

    id = "integration"
    name = "Integration"
    aliases: list[str] = []
    description = ""
    icon = "◆"
    category = "Apps"
    fields: list[Field] = []
    default_enabled = True
    source = "built in"

    def __init__(self) -> None:
        self.api: IntegrationAPI | None = None

    def bind(self, api: "IntegrationAPI") -> None:
        self.api = api

    def actions(self) -> list[Action]:
        return []

    def status(self) -> tuple[str, str]:
        """(text, role) shown on the integration card: success / warning / danger / muted."""
        return "Ready", "success"

    def setting(self, key: str, default: Any = None) -> Any:
        return self.api.get(self.id, key, default) if self.api else default

    def on_settings_changed(self) -> None:
        pass


class IntegrationAPI:
    """What integrations can use. Thread-safe unless noted."""

    def __init__(self, core: Any):
        self.core = core
        self.ctx = core.ctx
        self.log = core.ctx.log
        self.apps = AppIndex(self.log)

    # settings (stored per integration)
    def get(self, integration_id: str, key: str, default: Any = None) -> Any:
        return self.ctx.app.settings.get(f"integration:{integration_id}", key, default)

    def set(self, integration_id: str, key: str, value: Any) -> None:
        self.ctx.app.settings.set(f"integration:{integration_id}", key, value)

    def secret(self, integration_id: str, key: str) -> str:
        from .engine import unprotect_secret
        return unprotect_secret(self.get(integration_id, key, "") or "")

    def set_secret(self, integration_id: str, key: str, value: str) -> None:
        from .engine import protect_secret
        self.set(integration_id, key, protect_secret(value) if value else "")

    @property
    def data_dir(self) -> Path:
        return self.ctx.data_dir

    # doing things
    def open(self, target: str) -> str:
        """Open an app (Start-menu name, exe path, file, folder, URL or URI). Returns what was opened."""
        target = os.path.expandvars(os.path.expanduser(str(target).strip().strip('"')))
        if re.match(r"^[a-z][a-z0-9+.-]*:(?!\\)", target, re.I) and not re.match(r"^[a-z]:[\\/]", target, re.I):
            self.open_uri(target)
            return target
        path = Path(target)
        if path.exists():
            _startfile(str(path))
            return path.name
        found = self.apps.find(target)
        if found is not None:
            _startfile(str(found))
            return found.stem
        exe = shutil.which(target)
        if exe:
            subprocess.Popen([exe], **_detached())
            return Path(exe).stem
        raise LookupError(f"couldn't find “{target}”")

    def open_uri(self, uri: str) -> None:
        if IS_WINDOWS:
            _startfile(uri)
        else:
            webbrowser.open(uri)

    def open_url(self, url: str) -> None:
        webbrowser.open(url, new=2)

    def run(self, argv: list[str]) -> None:
        subprocess.Popen([str(a) for a in argv], **_detached())

    def keys(self, combo: str) -> None:
        send_keys(combo)

    def notify(self, title: str, body: str = "", **kw: Any) -> None:
        self.ctx.notify(title, body, **kw)

    def say(self, text: str) -> None:
        self.ctx.call_in_ui(self.core.speak, text)

    def after(self, seconds: float, fn: Callable[[], Any]) -> None:
        self.ctx.call_in_ui(lambda: self.ctx.app.after(int(seconds * 1000), fn))


def _startfile(target: str) -> None:
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target], **_detached())


def _detached() -> dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": 0x00000008 | 0x00000200, "close_fds": True}  # DETACHED | NEW_GROUP
    return {"start_new_session": True}


# ── keyboard (media keys, volume, hotkeys) ──────────────────────────────────
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B,
      "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
      "printscreen": 0x2C, "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
      "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
EXTENDED = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0x5B}


def _vk(name: str) -> int:
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"unknown key “{name}”")


def send_keys(combo: str, repeat: int = 1) -> None:
    """Press a key combination, e.g. "ctrl+shift+f9" or "volume_up" (Windows)."""
    codes = [_vk(k) for k in combo.split("+") if k.strip()]
    if not IS_WINDOWS:
        raise RuntimeError("keyboard actions need Windows")
    user32 = ctypes.windll.user32
    for _ in range(max(1, repeat)):
        for code in codes:
            user32.keybd_event(code, 0, 0x1 if code in EXTENDED else 0, 0)
        for code in reversed(codes):
            user32.keybd_event(code, 0, (0x1 if code in EXTENDED else 0) | 0x2, 0)
        if repeat > 1:
            time.sleep(0.008)


# ── installed apps (Start menu) ─────────────────────────────────────────────
class AppIndex:
    """Start-menu shortcuts, so "open Blender" works without any setup."""

    SKIP = re.compile(r"uninstall|readme|help|license|website|release notes|documentation|manual", re.I)

    def __init__(self, log: Any):
        self.log = log
        self._apps: dict[str, Path] | None = None
        self._lock = threading.Lock()

    def folders(self) -> list[Path]:
        out = []
        for env in ("PROGRAMDATA", "APPDATA"):
            base = os.environ.get(env)
            if base:
                out.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        extra = os.environ.get("JUNO_APPS_DIR")  # tests / portable setups
        if extra:
            out.append(Path(extra))
        return out

    def apps(self) -> dict[str, Path]:
        with self._lock:
            if self._apps is None:
                found: dict[str, Path] = {}
                for folder in self.folders():
                    if not folder.is_dir():
                        continue
                    for path in folder.rglob("*"):
                        if path.suffix.lower() in (".lnk", ".url", ".appref-ms") and not self.SKIP.search(path.stem):
                            found.setdefault(path.stem.lower(), path)
                self._apps = found
            return self._apps

    def refresh(self) -> None:
        with self._lock:
            self._apps = None

    def names(self) -> list[str]:
        return sorted({p.stem for p in self.apps().values()}, key=str.lower)

    def find(self, name: str) -> Path | None:
        name = normalize(name)
        if not name:
            return None
        apps = self.apps()
        if name in apps:
            return apps[name]
        starts = sorted((k for k in apps if k.startswith(name + " ") or k.split(" ")[0] == name), key=len)
        if starts:
            return apps[starts[0]]
        words = sorted((k for k in apps if re.search(rf"\b{re.escape(name)}\b", k)), key=len)
        if words:
            return apps[words[0]]
        close = difflib.get_close_matches(name, list(apps), n=1, cutoff=0.86)
        return apps[close[0]] if close else None


# ── phrase patterns ─────────────────────────────────────────────────────────
FILLER_RE = re.compile(r"^(?:(?:hey|hi|ok|okay|yo)\s+)?(?:{wake})\b[\s,.:!-]*", re.I)
POLITE_RE = re.compile(r"^(?:(?:please|can you|could you|would you|will you|can u|go ahead and|i want you to|"
                       r"i'd like you to|i need you to|just)\s+)+", re.I)
TAIL_RE = re.compile(r"\s+(?:please|for me|thanks|thank you|now|right now)$", re.I)


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"(\d)\s*%", r"\1 percent", text)
    text = re.sub(r"[“”\"!?]+", " ", text)
    text = re.sub(r"[.,;:]+(\s|$)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_command(text: str, wake: str = "juno") -> str:
    """'Hey Juno, could you please open Spotify for me?' → 'open spotify'."""
    text = normalize(text)
    text = re.sub(FILLER_RE.pattern.replace("{wake}", re.escape(wake.lower())), "", text, flags=re.I).strip()
    for _ in range(3):
        text = POLITE_RE.sub("", text).strip()
        text = TAIL_RE.sub("", text).strip()
    return text


SLOT_TYPES = {"url": r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?", "num": r"\d+(?:\.\d+)?",
              "word": r"\S+"}


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """'play {query} on spotify' · '[please] (open|launch) spotify' · 'volume up [by {amount:num}]' → regex.
    Words match words, {slot} / {slot:url|num|word} capture, [x] is optional, (a|b) picks one."""
    return re.compile(rf"^{_compile(normalize_pattern(pattern))}$", re.I)


def normalize_pattern(pattern: str) -> str:
    return re.sub(r"\s+", " ", pattern.lower().replace("’", "'")).strip()


def _split_top(text: str, sep: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _close(text: str, i: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    depth = 0
    for j in range(i, len(text)):
        if text[j] in pairs:
            depth += 1
        elif text[j] in pairs.values():
            depth -= 1
            if depth == 0:
                return j
    raise ValueError(f"unclosed bracket in “{text}”")


def _compile(text: str) -> str:
    out, need_sep = "", False
    for tok in [t for t in _split_top(text.strip(), " ") if t]:
        if tok.startswith("[") and _close(tok, 0) == len(tok) - 1:
            body = _alternatives(tok[1:-1])
            out += rf"(?:\s+{body})?" if need_sep else rf"(?:{body}\s+)?"
            continue
        out += (r"\s+" if need_sep else "") + _token(tok)
        need_sep = True
    return out


def _alternatives(text: str) -> str:
    return "(?:" + "|".join(_compile(alt) for alt in _split_top(text, "|")) + ")"


def _token(tok: str) -> str:
    out, i = "", 0
    while i < len(tok):
        ch = tok[i]
        if ch in "({[":
            j = _close(tok, i)
            inner = tok[i + 1:j]
            if ch == "{":
                name, _, kind = inner.partition(":")
                out += f"(?P<{name.strip()}>{SLOT_TYPES.get(kind.strip(), '.+?')})"
            elif ch == "(":
                out += _alternatives(inner)
            else:
                out += _alternatives(inner) + "?"
            i = j + 1
        else:
            out += re.escape(ch)
            i += 1
    return out


def example_of(pattern: str) -> str:
    """Readable sample sentence: first choice of every (a|b), optional [parts] dropped, {slot} → <slot>."""
    text = pattern
    for _ in range(4):
        text = re.sub(r"\[[^\[\]]*\]", "", text)
    for _ in range(4):
        text = re.sub(r"\(([^()]*)\)", lambda m: m.group(1).split("|")[0], text)
    text = re.sub(r"\{(\w+)(?::\w+)?\}", r"<\1>", text)
    return re.sub(r"\s+", " ", text).strip()


def specificity(pattern: str) -> int:
    """Longer fixed wording wins; alternatives count as their shortest choice."""
    text = re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", pattern)
    text = re.sub(r"\(([^()]*)\)", lambda m: min(m.group(1).split("|"), key=len), text)
    return len(text.replace(" ", ""))


@dataclass
class Match:
    integration: Integration
    action: Action
    params: dict[str, str]
    via: str = "pattern"          # pattern | phrase | ai


# ════════════════════════════════════════════════════════════════════════════
#  Registry + router
# ════════════════════════════════════════════════════════════════════════════

class Registry:
    def __init__(self, core: Any):
        self.core = core
        self.api = IntegrationAPI(core)
        self.items: dict[str, Integration] = {}
        self.errors: dict[str, str] = {}
        self._compiled: list[tuple[tuple[int, int], re.Pattern[str], Integration, Action]] = []

    @property
    def user_dir(self) -> Path:
        d = self.core.ctx.data_dir / "integrations"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def load(self) -> None:
        self.items.clear()
        self.errors.clear()
        for cls in BUILTINS:
            self._add(cls())
        for folder in (self.core.ctx.paths.plugins_dir, self.user_dir):   # shipped extras, then yours
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    if path.name.startswith(("_", ".")):
                        continue
                    try:
                        if path.suffix.lower() == ".junoint":
                            self._add(PackIntegration(json.loads(path.read_text(encoding="utf-8")), path))
                        elif path.suffix.lower() == ".py":
                            for integ in load_python_integrations(path):
                                self._add(integ)
                    except Exception as exc:
                        self.errors[path.name] = str(exc)
                        self.core.ctx.log.warning("Integration %s failed to load: %s", path.name, exc)
        self.compile()

    def _add(self, integ: Integration) -> None:
        integ.bind(self.api)
        if integ.id in self.items and integ.source == "built in":
            return
        self.items[integ.id] = integ

    def enabled(self, integ: Integration) -> bool:
        return bool(self.api.get(integ.id, "_enabled", integ.default_enabled))

    def set_enabled(self, integ_id: str, value: bool) -> None:
        self.api.set(integ_id, "_enabled", bool(value))
        self.compile()

    PRIORITY = {"phrases": 3, "apps": 0}

    def compile(self) -> None:
        compiled = []
        for integ in self.items.values():
            if not self.enabled(integ):
                continue
            try:
                actions = integ.actions()
            except Exception as exc:
                self.errors[integ.id] = str(exc)
                continue
            for action in actions:
                for pattern in action.patterns:
                    try:
                        compiled.append(((self.PRIORITY.get(integ.id, 2), specificity(pattern)),
                                         compile_pattern(pattern), integ, action))
                    except Exception as exc:
                        self.core.ctx.log.warning("Bad pattern %r in %s: %s", pattern, integ.id, exc)
        compiled.sort(key=lambda c: (-c[0][0], -c[0][1]))
        self._compiled = compiled

    def match(self, text: str, skip: int = 0) -> Match | None:
        """Best command match for what the user said (skip=n gives the n-th best, for fall-through)."""
        command = clean_command(text, self.core.wake_word())
        if not command or len(command) > 160:
            return None
        seen = 0
        for _score, regex, integ, action in self._compiled:
            m = regex.match(command)
            if m:
                if seen < skip:
                    seen += 1
                    continue
                params = {k: v.strip() for k, v in m.groupdict().items() if v}
                params["_text"] = command
                return Match(integ, action, params, "phrase" if integ.id == "phrases" else "pattern")
        return None

    def mentioned(self, text: str) -> Integration | None:
        """An enabled integration the user named explicitly (for the AI fallback)."""
        low = " " + normalize(text) + " "
        for integ in self.items.values():
            if integ.id in ("phrases", "apps", "system", "media") or not self.enabled(integ):
                continue
            for alias in [integ.name.lower(), *[a.lower() for a in integ.aliases]]:
                if alias and re.search(rf"[^a-z0-9]{re.escape(alias)}[^a-z0-9]", low):
                    return integ
        return None


def load_python_integrations(path: Path) -> list[Integration]:
    name = "juno_user_" + re.sub(r"\W", "_", path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    found = []
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, Integration) and obj is not Integration \
                and obj.__module__ == module.__name__:
            integ = obj()
            integ.source = path.name
            found.append(integ)
    if not found:
        raise ValueError("no Integration subclass in the file")
    return found


# ════════════════════════════════════════════════════════════════════════════
#  No-code packs (.junoint)
# ════════════════════════════════════════════════════════════════════════════

class PackIntegration(Integration):
    category = "Custom"

    def __init__(self, data: dict[str, Any], path: Path):
        super().__init__()
        if not isinstance(data, dict) or not data.get("actions"):
            raise ValueError("a .junoint needs an \"actions\" list")
        self.data = data
        self.id = re.sub(r"[^a-z0-9_]+", "_", str(data.get("id") or path.stem).lower())
        self.name = str(data.get("name") or path.stem)[:40]
        self.aliases = [str(a).lower() for a in data.get("aliases", [])][:8]
        self.description = str(data.get("description", ""))[:200]
        self.icon = str(data.get("icon", "◇"))[:2]
        self.source = path.name

    def actions(self) -> list[Action]:
        out = []
        for i, spec in enumerate(self.data["actions"]):
            phrases = [str(p) for p in spec.get("phrases", []) if str(p).strip()]
            do = spec.get("do") or {}
            if not phrases or not isinstance(do, dict):
                continue
            slots = sorted({s for p in phrases for s in re.findall(r"\{(\w+)\}", p)})
            out.append(Action(f"a{i}", str(spec.get("title") or phrases[0]), phrases,
                              lambda api, params, do=do, spec=spec: self._run(api, params, do, spec),
                              description=str(spec.get("description", spec.get("title", ""))),
                              params={s: s for s in slots}, examples=phrases[:2]))
        return out

    @staticmethod
    def _fill(template: str, params: dict[str, str], quote: bool = False) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: urllib.parse.quote_plus(params.get(m.group(1), "")) if quote
                      else params.get(m.group(1), ""), template)

    def _run(self, api: IntegrationAPI, params: dict[str, str], do: dict[str, Any], spec: dict[str, Any]) -> Any:
        done = []
        if do.get("open"):
            done.append(f"Opened {api.open(self._fill(str(do['open']), params))}.")
        if do.get("url"):
            api.open_url(self._fill(str(do["url"]), params, quote=True))
            done.append("Opened it in your browser.")
        if do.get("keys"):
            api.keys(str(do["keys"]))
            done.append("Done.")
        if do.get("run"):
            argv = do["run"] if isinstance(do["run"], list) else [do["run"]]
            api.run([self._fill(str(a), params) for a in argv])
            done.append("Started it.")
        message = str(spec.get("say") or do.get("say") or " ".join(done) or "Done.")
        return ActionResult(self._fill(message, params))


# ════════════════════════════════════════════════════════════════════════════
#  Built-in integrations
# ════════════════════════════════════════════════════════════════════════════

OPEN = "(open|launch|start|run|load|fire up|bring up|pull up)"


class Phrases(Integration):
    """Your own words → apps, files, folders, sites. Edited on the Phrases page."""

    id, name, icon, category = "phrases", "Phrases", "❝", "Core"
    description = "Your own words for apps, files, folders and websites. “Blender” → Blender."

    def entries(self) -> list[dict[str, str]]:
        return [e for e in (self.setting("entries", []) or []) if e.get("phrase") and e.get("target")]

    def actions(self) -> list[Action]:
        out = []
        for i, entry in enumerate(self.entries()):
            words = normalize(entry["phrase"])
            safe = re.sub(r"[{}\[\]()|]", "", words)
            patterns = [f"{OPEN} [the|my] {safe}", safe]
            if entry.get("kind") == "url":
                patterns.append(f"(go to|show me) {safe}")
            out.append(Action(f"p{i}", f"Open {entry['phrase']}", patterns,
                              lambda api, _p, e=entry: self._open(api, e), examples=[f"open {entry['phrase']}"]))
        return out

    def _open(self, api: IntegrationAPI, entry: dict[str, str]) -> ActionResult:
        target = entry["target"]
        if entry.get("kind") == "url" or re.match(r"^https?://", target, re.I):
            api.open_url(target if "://" in target else "https://" + target)
        else:
            api.open(target)
        return ActionResult(f"Opened {entry['phrase']}.")

    def status(self) -> tuple[str, str]:
        n = len(self.entries())
        return (f"{n} phrase{'s' if n != 1 else ''}", "success") if n else ("No phrases yet", "muted")


class Apps(Integration):
    id, name, icon, category = "apps", "Apps", "▣", "Core"
    description = "Open or close any app from your Start menu by name — “open Notepad”, “close Notepad”."

    def actions(self) -> list[Action]:
        return [Action("open", "Open an app", [f"{OPEN} [the|my] {{app}} [app|application|program]"], self.open_app,
                       params={"app": "app name"}, examples=["open notepad"]),
                Action("close", "Close an app", ["(close|quit|exit|kill) [the|my] {app} [app|application|program]"],
                       self.close_app, params={"app": "app name"}, examples=["close notepad"])]

    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params["app"]
        if len(name.split()) > 4:
            raise LookupError("that doesn't look like an app name")
        found = api.apps.find(name)
        if found is None:
            return ActionResult(f"I couldn't find an app called “{name}”. Add it on the Phrases page and I'll "
                                f"remember it.", ok=False)
        _startfile(str(found))
        return ActionResult(f"Opened {found.stem}.")

    def close_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = params["app"].lower()
        exe = KNOWN_EXES.get(name)
        if exe is None:
            found = api.apps.find(name)
            target = resolve_shortcut(found) if found else None
            exe = Path(target).name if target and str(target).lower().endswith(".exe") else None
        if not exe or not IS_WINDOWS:
            return ActionResult(f"I'm not sure which program “{params['app']}” is.", ok=False)
        result = subprocess.run(["taskkill", "/IM", exe], capture_output=True, text=True,
                                creationflags=0x08000000)
        if result.returncode != 0:
            return ActionResult(f"{params['app'].title()} doesn't seem to be running.", ok=False)
        return ActionResult(f"Closed {params['app'].title()}.")


KNOWN_EXES = {"spotify": "Spotify.exe", "discord": "Discord.exe", "blender": "blender.exe",
              "roblox studio": "RobloxStudioBeta.exe", "studio": "RobloxStudioBeta.exe", "chrome": "chrome.exe",
              "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe", "obs": "obs64.exe",
              "steam": "steam.exe", "notepad": "notepad.exe", "obsidian": "Obsidian.exe"}


def resolve_shortcut(path: Path | None) -> str | None:
    if path is None or path.suffix.lower() != ".lnk" or not IS_WINDOWS:
        return None
    try:
        import win32com.client
        return win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath
    except Exception:
        return None


class System(Integration):
    id, name, icon, category = "system", "System", "⚙", "Core"
    description = "Volume, mute, lock, screenshots, timers, time and date."

    def actions(self) -> list[Action]:
        return [
            Action("vol_up", "Volume up", ["(turn|crank) [the] volume up [by {amount:num} [percent|%]]",
                                           "volume up [by {amount:num} [percent|%]]",
                                           "(louder|turn it up)", "raise [the] volume [by {amount:num} [percent|%]]"],
                   self.vol_up, params={"amount": "percent, optional"}, background=False),
            Action("vol_down", "Volume down", ["(turn|bring) [the] volume down [by {amount:num} [percent|%]]",
                                               "volume down [by {amount:num} [percent|%]]", "(quieter|turn it down)",
                                               "lower [the] volume [by {amount:num} [percent|%]]"],
                   self.vol_down, params={"amount": "percent, optional"}, background=False),
            Action("vol_set", "Set the volume", ["(set|put|change|turn) [the] volume to {amount:num} [percent|%]",
                                                 "volume {amount:num} [percent|%]", "volume to {amount:num}"],
                   self.vol_set, params={"amount": "0-100"}),
            Action("mute", "Mute / unmute", ["(mute|unmute) [the] (sound|volume|audio|computer|pc)", "(mute|unmute)"],
                   lambda api, p: (api.keys("volume_mute"), ActionResult("Toggled mute."))[1], background=False),
            Action("lock", "Lock the PC", ["lock [the|my] (pc|computer|screen|laptop)"], self.lock),
            Action("screenshot", "Take a screenshot", ["(take|grab) a screenshot", "screenshot"], self.screenshot),
            Action("timer", "Set a timer", ["(set|start) a timer for {duration}", "timer {duration}",
                                            "remind me in {duration} [to {what}]"],
                   self.timer, params={"duration": "like '5 minutes'", "what": "optional reminder"}, background=False),
            Action("time", "Tell the time", ["what time is it", "what's the time", "time"], self.time_now,
                   background=False),
            Action("date", "Tell the date", ["what's the date [today]", "what day is it [today]",
                                             "what's today's date"], self.date_now, background=False),
            Action("settings", "Open Windows settings", [f"{OPEN} [windows] settings"],
                   lambda api, p: (api.open_uri("ms-settings:"), ActionResult("Opened Settings."))[1]),
        ]

    @staticmethod
    def _amount(params: dict[str, str], default: int = 10) -> int:
        m = re.search(r"\d+", params.get("amount", ""))
        return max(2, min(100, int(m.group()))) if m else default

    def vol_up(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        amount = self._amount(params)
        send_keys("volume_up", repeat=amount // 2)
        return ActionResult(f"Volume up {amount}%.", say="")

    def vol_down(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        amount = self._amount(params)
        send_keys("volume_down", repeat=amount // 2)
        return ActionResult(f"Volume down {amount}%.", say="")

    def vol_set(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        m = re.search(r"\d+", params.get("amount", ""))
        if not m:
            return ActionResult("Tell me a number between 0 and 100.", ok=False)
        level = max(0, min(100, int(m.group())))
        send_keys("volume_down", repeat=50)       # Windows volume keys move in 2% steps
        if level:
            send_keys("volume_up", repeat=level // 2)
        return ActionResult(f"Volume set to {level}%.", say="")

    def lock(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if IS_WINDOWS:
            ctypes.windll.user32.LockWorkStation()
        return ActionResult("Locked.", say="")

    def screenshot(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        from PIL import ImageGrab
        folder = Path.home() / "Pictures" / "Screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / time.strftime("Juno %Y-%m-%d %H.%M.%S.png")
        ImageGrab.grab(all_screens=True).save(target)
        return ActionResult(f"Screenshot saved to Pictures ▸ Screenshots ({target.name}).", say="Screenshot saved.")

    @staticmethod
    def parse_duration(text: str) -> int | None:
        text = text.lower().replace("an hour", "1 hour").replace("a minute", "1 minute").replace("half an hour",
                                                                                                    "30 minutes")
        total = 0
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hour|m|min|minute|s|sec|second)s?\b", text):
            total += float(num) * {"h": 3600, "hr": 3600, "hour": 3600, "m": 60, "min": 60, "minute": 60}.get(unit, 1)
        if not total:
            m = re.fullmatch(r"\s*(\d+)\s*", text)
            total = int(m.group(1)) * 60 if m else 0
        return int(total) or None

    def timer(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        seconds = self.parse_duration(params.get("duration", ""))
        if not seconds:
            return ActionResult("How long? Try “set a timer for 5 minutes”.", ok=False)
        what = params.get("what", "").strip()

        def ring() -> None:
            api.notify("Timer", what or "Time's up!", subtitle="Juno", icon="⏰", duration=20)
            api.core.speak(f"Time's up{': ' + what if what else ''}!")
            api.core.log_activity("Timer finished", what or params.get("duration", ""), "warn")
        api.ctx.app.after(seconds * 1000, ring)
        label = params.get("duration", "")
        return ActionResult(f"Timer set for {label}." + (f" I'll remind you to {what}." if what else ""))

    def time_now(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        return ActionResult(f"It's {time.strftime('%I:%M %p').lstrip('0')}.")

    def date_now(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        return ActionResult(f"It's {time.strftime('%A, %d %B %Y')}.")


class Media(Integration):
    id, name, icon, category = "media", "Media keys", "▶", "Core"
    description = "Play, pause, skip — works with Spotify, YouTube in your browser and most players."

    def actions(self) -> list[Action]:
        k = lambda combo, msg: (lambda api, p: (api.keys(combo), ActionResult(msg, say=""))[1])  # noqa: E731
        return [
            Action("play_pause", "Play / pause", ["(pause|resume|unpause) [the] (music|song|video|track|playback)",
                                                   "(pause|resume|unpause)", "play [the] music", "play some music",
                                                   "stop [the] music"], k("media_play_pause", "Play/pause."),
                   background=False),
            Action("next", "Next track", ["(next|skip) [this] [song|track]", "skip it", "play the next song"],
                   k("media_next", "Skipped."), background=False),
            Action("prev", "Previous track", ["(previous|last|go back a) (song|track)", "play the previous song"],
                   k("media_prev", "Previous track."), background=False),
        ]


class Spotify(Integration):
    id, name, icon, category = "spotify", "Spotify", "♫", "Music"
    aliases = ["spotify"]
    description = ("Open Spotify, play / pause / skip. Connect your account (Premium) to play any song, artist or "
                   "playlist by name and set Spotify's own volume.")
    fields = [Field("client_id", "Spotify app Client ID", help="developer.spotify.com ▸ Dashboard ▸ Create app. "
                    "Redirect URI: http://127.0.0.1:8737/callback (Web API). Paste the Client ID here, then Connect.")]
    REDIRECT = "http://127.0.0.1:8737/callback"
    SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"

    def actions(self) -> list[Action]:
        return [
            Action("open", "Open Spotify", [f"{OPEN} spotify"], self.open_app),
            Action("play", "Play something", ["play {query} on spotify", "spotify play {query}",
                                               "play {query} (in|from|with) spotify",
                                               "put on {query} (on|in) spotify"], self.play,
                   params={"query": "song, artist, album or playlist"}, examples=["play lofi beats on spotify"]),
            Action("resume", "Play / pause Spotify", ["(pause|resume|play|unpause) spotify",
                                                       "spotify (pause|resume|play)"], self.toggle),
            Action("next", "Next song", ["(next|skip) [song|track] on spotify", "spotify (next|skip)"], self.next),
            Action("prev", "Previous song", ["previous [song|track] on spotify", "spotify (previous|back)"],
                   self.prev),
            Action("volume", "Spotify volume", ["(set|put|turn) spotify volume to {amount:num} [percent|%]",
                                                 "spotify volume [to] {amount:num} [percent|%]"], self.volume,
                   params={"amount": "0-100"}),
            Action("current", "What's playing", ["what's playing on spotify", "what song is this",
                                                  "what's this song", "what's playing"], self.current),
        ]

    # ── account (PKCE, no secret needed) ─────────────────────────────────
    def connected(self) -> bool:
        return bool(self.api and self.api.secret(self.id, "refresh_token"))

    def status(self) -> tuple[str, str]:
        if self.connected():
            return "Connected", "success"
        return ("Ready · account not connected", "muted") if self.setting("client_id") else ("Ready", "success")

    def connect(self, on_done: Callable[[str | None], None]) -> None:
        """Opens the Spotify login in the browser and waits for the redirect. Calls on_done(error or None)."""
        import base64
        import hashlib
        import http.server
        import secrets
        client_id = (self.setting("client_id") or "").strip()
        if not client_id:
            on_done("Paste your Spotify Client ID first.")
            return
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("state", [""])[0] == state:
                    result.update({k: v[0] for k, v in query.items()})
                body = ("<h2 style='font-family:sans-serif'>Juno is connected to Spotify — you can close this tab."
                        "</h2>" if "code" in result else "<h2>Spotify login didn't finish.</h2>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

        def work() -> None:
            try:
                server = http.server.HTTPServer(("127.0.0.1", 8737), Handler)
            except OSError as exc:
                self.api.ctx.call_in_ui(on_done, f"Port 8737 is busy ({exc}).")
                return
            server.timeout = 1
            webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
                "client_id": client_id, "response_type": "code", "redirect_uri": self.REDIRECT, "scope": self.SCOPES,
                "code_challenge_method": "S256", "code_challenge": challenge, "state": state}))
            end = time.time() + 180
            while time.time() < end and "code" not in result and "error" not in result:
                server.handle_request()
            server.server_close()
            if "code" not in result:
                self.api.ctx.call_in_ui(on_done, result.get("error", "Timed out waiting for Spotify."))
                return
            try:
                self._token({"grant_type": "authorization_code", "code": result["code"],
                             "redirect_uri": self.REDIRECT, "client_id": client_id, "code_verifier": verifier})
                self.api.ctx.call_in_ui(on_done, None)
            except Exception as exc:
                self.api.ctx.call_in_ui(on_done, str(exc))
        threading.Thread(target=work, daemon=True, name="spotify-login").start()

    def disconnect(self) -> None:
        self.api.set_secret(self.id, "refresh_token", "")
        self._access = None

    _access: tuple[str, float] | None = None

    def _token(self, form: dict[str, str]) -> str:
        req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                     data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("refresh_token"):
            self.api.set_secret(self.id, "refresh_token", data["refresh_token"])
        self._access = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return self._access[0]

    def _bearer(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = self.api.secret(self.id, "refresh_token")
        if not refresh:
            raise PermissionError("not connected")
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh,
                            "client_id": (self.setting("client_id") or "").strip()})

    def _call(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = "https://api.spotify.com/v1" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._bearer(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError("Spotify isn't playing on any device — open Spotify first.") from None
            if exc.code == 403:
                raise RuntimeError("Spotify only lets Premium accounts be controlled this way.") from None
            raise RuntimeError(f"Spotify said {exc.code}.") from None

    # ── actions ──────────────────────────────────────────────────────────
    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_uri("spotify:")
        return ActionResult("Opened Spotify.")

    def play(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        query = params.get("query", "").strip()
        if not self.connected():
            api.open_uri("spotify:search:" + urllib.parse.quote(query))
            return ActionResult(f"Searched Spotify for “{query}”. Connect your Spotify account on the Integrations "
                                f"page and I'll press play for you too.", say=f"Here's {query} on Spotify.")
        kind = "playlist" if "playlist" in query.lower() else "album" if " album" in query.lower() else None
        clean = re.sub(r"\b(the )?(playlist|album|song|track)\b", "", query, flags=re.I).strip() or query
        found = self._call("GET", "/search", query={"q": clean, "type": kind or "track,artist,playlist", "limit": 1})
        if kind:
            items = (found.get(kind + "s") or {}).get("items") or []
            item = next((i for i in items if i), None)
            body = {"context_uri": item["uri"]} if item else None
        else:
            track = next((i for i in (found.get("tracks") or {}).get("items") or [] if i), None)
            artist = next((i for i in (found.get("artists") or {}).get("items") or [] if i), None)
            if artist and artist["name"].lower() == clean.lower():
                item, body = artist, {"context_uri": artist["uri"]}
            elif track:
                item, body = track, {"uris": [track["uri"]]}
            else:
                pl = next((i for i in (found.get("playlists") or {}).get("items") or [] if i), None)
                item, body = pl, ({"context_uri": pl["uri"]} if pl else None)
        if not body:
            return ActionResult(f"Spotify found nothing for “{query}”.", ok=False)
        self._call("PUT", "/me/player/play", body)
        name = item.get("name", query)
        by = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        return ActionResult(f"Playing {name}" + (f" by {by}" if by else "") + " on Spotify.")

    def toggle(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if self.connected():
            state = self._call("GET", "/me/player") or {}
            self._call("PUT", "/me/player/pause" if state.get("is_playing") else "/me/player/play")
            return ActionResult("Paused Spotify." if state.get("is_playing") else "Resumed Spotify.", say="")
        api.keys("media_play_pause")
        return ActionResult("Play/pause.", say="")

    def next(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        self._call("POST", "/me/player/next") if self.connected() else api.keys("media_next")
        return ActionResult("Skipped.", say="")

    def prev(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        self._call("POST", "/me/player/previous") if self.connected() else api.keys("media_prev")
        return ActionResult("Previous track.", say="")

    def volume(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account first (Integrations page) — or say “set volume to "
                                "40%” for the PC volume.", ok=False)
        m = re.search(r"\d+", params.get("amount", ""))
        level = max(0, min(100, int(m.group()))) if m else 50
        self._call("PUT", "/me/player/volume", query={"volume_percent": level})
        return ActionResult(f"Spotify volume {level}%.", say="")

    def current(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        if not self.connected():
            return ActionResult("Connect your Spotify account (Integrations page) and I can tell you.", ok=False)
        now = self._call("GET", "/me/player/currently-playing") or {}
        item = now.get("item")
        if not item:
            return ActionResult("Nothing's playing right now.")
        by = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        return ActionResult(f"That's {item['name']}" + (f" by {by}." if by else "."))


class Google(Integration):
    id, name, icon, category = "google", "Google & web", "G", "Web"
    aliases = ["google", "youtube", "gmail", "google maps", "google drive"]
    description = "Search Google or YouTube, open Gmail, Drive, Maps, Calendar, Docs, Translate, or any website."
    SITES = {"gmail": "https://mail.google.com", "google drive": "https://drive.google.com",
             "drive": "https://drive.google.com", "google maps": "https://maps.google.com",
             "maps": "https://maps.google.com", "google calendar": "https://calendar.google.com",
             "calendar": "https://calendar.google.com", "google docs": "https://docs.google.com",
             "youtube": "https://www.youtube.com", "google": "https://www.google.com",
             "google translate": "https://translate.google.com", "google photos": "https://photos.google.com"}

    def actions(self) -> list[Action]:
        sites = "|".join(sorted(self.SITES, key=len, reverse=True))
        return [
            Action("search", "Google search", ["google {query}", "(search|look up|look for) {query} on google",
                                                "(search google|search the web|search online) for {query}",
                                                "search for {query}", "look up {query}"], self.search,
                   params={"query": "what to search"}, examples=["google best budget gpu"]),
            Action("youtube", "YouTube search", ["[search] youtube for {query}", "youtube {query}",
                                                  "(play|find|search|look up) {query} on youtube"], self.youtube,
                   params={"query": "what to find"}),
            Action("site", "Open a Google app", [f"{OPEN} [my] ({sites})", f"(go to|show me) [my] ({sites})"],
                   self.site),
            Action("maps", "Directions", ["(directions|navigate|route) to {place}", "how do i get to {place}",
                                          "where is {place} on the map", "show {place} on (google maps|the map)"],
                   self.maps, params={"place": "destination"}),
            Action("translate", "Translate", ["translate {text} (to|into) {language}"], self.translate,
                   params={"text": "text", "language": "target language"}),
            Action("website", "Open a website", ["(go to|open|visit) {site:url}"], self.website,
                   params={"site": "like example.com"}),
        ]

    def search(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_url("https://www.google.com/search?q=" + urllib.parse.quote_plus(params["query"]))
        return ActionResult(f"Googled “{params['query']}”.", say="Here you go.")

    def youtube(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(params["query"]))
        return ActionResult(f"Searched YouTube for “{params['query']}”.", say="Here's YouTube.")

    def site(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        text = normalize(params.get("_text", ""))
        name = next((k for k in sorted(self.SITES, key=len, reverse=True) if k in text), "google")
        api.open_url(self.SITES[name])
        return ActionResult(f"Opened {name.title()}.")

    def maps(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_url("https://www.google.com/maps/dir/?api=1&destination=" + urllib.parse.quote_plus(params["place"]))
        return ActionResult(f"Directions to {params['place']}.")

    def translate(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        api.open_url("https://translate.google.com/?sl=auto&tl=" + urllib.parse.quote(params["language"][:20])
                     + "&text=" + urllib.parse.quote(params["text"]) + "&op=translate")
        return ActionResult(f"Translating into {params['language']}.")

    def website(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        site = params["site"].strip().replace(" dot ", ".")
        if not re.fullmatch(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?", site):
            raise LookupError("not a website")   # let other commands (e.g. "open notepad") handle it
        api.open_url(site if "://" in site else "https://" + site)
        return ActionResult(f"Opened {site}.")


class Discord(Integration):
    id, name, icon, category = "discord", "Discord", "✉", "Social"
    aliases = ["discord"]
    description = "Open Discord or your DMs, and read out your latest Discord messages (from Windows notifications)."

    def actions(self) -> list[Action]:
        return [
            Action("open", "Open Discord", [f"{OPEN} discord"],
                   lambda api, p: (api.open_uri("discord://-/channels/@me"), ActionResult("Opened Discord."))[1]),
            Action("dms", "Open my DMs", [f"{OPEN} [my] (discord dms|discord messages|dms)", "go to my dms"],
                   lambda api, p: (api.open_uri("discord://-/channels/@me"), ActionResult("Opened your DMs."))[1]),
            Action("unread", "Read my latest messages", ["(any|do i have) (new|unread) discord messages",
                                                         "read my discord messages", "check discord",
                                                         "who messaged me on discord", "what's new on discord"],
                   self.latest),
        ]

    def latest(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        messages = recent_discord_notifications(5)
        if messages is None:
            return ActionResult("I can't read Windows' notification list on this PC.", ok=False)
        if not messages:
            return ActionResult("No recent Discord messages.")
        lines = [f"{who}: {what}" for who, what in messages]
        spoken = "; ".join(f"{who} said {what}" for who, what in messages[:3])
        return ActionResult("Latest Discord messages —\n" + "\n".join(lines), say=spoken)


def recent_discord_notifications(limit: int = 5) -> list[tuple[str, str]] | None:
    import sqlite3
    import xml.etree.ElementTree as ET
    db = os.environ.get("MT_WPN_DB") or (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" /
                                         "Notifications" / "wpndatabase.db")
    if not Path(db).exists():
        return None
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute("SELECT n.Payload FROM Notification n JOIN NotificationHandler h ON n.HandlerId = "
                           "h.RecordId WHERE n.Type='toast' AND lower(h.PrimaryId) LIKE '%discord%' "
                           "ORDER BY n.Id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    out = []
    for (payload,) in rows:
        try:
            root = ET.fromstring(payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload)
            texts = [(t.text or "").strip() for t in root.iter("text") if t.get("placement") != "attribution"]
        except Exception:
            continue
        if texts:
            who = re.sub(r"\s+\(.*\)$", "", texts[0])
            out.append((who, " ".join(texts[1:])[:160] or "(attachment)"))
    return out


class Blender(Integration):
    id, name, icon, category = "blender", "Blender", "◭", "Creative"
    aliases = ["blender"]
    description = "Open Blender, your last .blend file, or a recent file by name."
    fields = [Field("exe", "blender.exe (optional)", kind="path", help="Leave empty to find Blender automatically.")]

    def actions(self) -> list[Action]:
        return [
            Action("open", "Open Blender", [f"{OPEN} blender"], self.open_app),
            Action("last", "Open my last file", [f"{OPEN} [my] (last|latest|recent|previous) (blender file|blend "
                                                 f"file|blend|blender project)", "continue in blender",
                                                 "(continue|resume) my blender (project|file)"], self.last),
            Action("file", "Open a recent file", [f"{OPEN} [the] blender (file|project) {{name}}",
                                                  f"{OPEN} {{name}} in blender", f"{OPEN} {{name}}.blend"],
                   self.named, params={"name": "part of the file name"}),
        ]

    def exe(self, api: IntegrationAPI) -> str | None:
        custom = self.setting("exe")
        if custom and Path(custom).exists():
            return custom
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            hits = sorted(Path(base, "Blender Foundation").glob("Blender*/blender.exe"), reverse=True) \
                if Path(base, "Blender Foundation").is_dir() else []
            if hits:
                return str(hits[0])
        found = api.apps.find("blender")
        return str(found) if found else None

    def recent(self) -> list[Path]:
        base = Path(os.environ.get("APPDATA", "")) / "Blender Foundation" / "Blender"
        files: list[Path] = []
        if base.is_dir():
            for version in sorted(base.iterdir(), reverse=True):
                recent = version / "config" / "recent-files.txt"
                if recent.is_file():
                    for line in recent.read_text(encoding="utf-8", errors="replace").splitlines():
                        p = Path(line.strip())
                        if line.strip() and p not in files and p.exists():
                            files.append(p)
        return files

    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        exe = self.exe(api)
        if not exe:
            return ActionResult("I can't find Blender — set its location on the Integrations page.", ok=False)
        _startfile(exe)
        return ActionResult("Opened Blender.")

    def last(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        files = self.recent()
        if not files:
            return ActionResult("Blender has no recent files I can see.", ok=False)
        _startfile(str(files[0]))
        return ActionResult(f"Opened {files[0].name} in Blender.")

    def named(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = normalize(params.get("name", "")).replace(".blend", "")
        files = self.recent()
        stems = {normalize(f.stem): f for f in files}
        hit = next((f for s, f in stems.items() if name and name in s), None)
        if hit is None:
            close = difflib.get_close_matches(name, list(stems), n=1, cutoff=0.6)
            hit = stems[close[0]] if close else None
        if hit is None:
            return ActionResult(f"No recent Blender file called “{params.get('name')}”.", ok=False)
        _startfile(str(hit))
        return ActionResult(f"Opened {hit.name} in Blender.")


class RobloxStudio(Integration):
    id, name, icon, category = "roblox_studio", "Roblox Studio", "◰", "Creative"
    aliases = ["roblox studio", "studio"]
    description = ("Open Roblox Studio or a local place file (.rbxl). Launches things on your PC only — it never "
                   "signs in or touches your Roblox account.")
    fields = [Field("places", "Folder with your place files", kind="folder",
                    help="Juno searches here (and Documents\\ROBLOX) for .rbxl / .rbxlx files.")]

    def actions(self) -> list[Action]:
        return [
            Action("open", "Open Roblox Studio", [f"{OPEN} [roblox] studio", f"{OPEN} roblox studio"], self.open_app),
            Action("place", "Open a place file", [f"{OPEN} [the] (place|map|game) {{name}} in [roblox] studio",
                                                  f"{OPEN} [my] {{name}} (place|map) [in studio]",
                                                  f"{OPEN} {{name}}.rbxl"], self.place,
                   params={"name": "part of the place file name"}),
        ]

    def open_app(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        exe = self.exe()
        if exe:
            _startfile(exe)
        else:
            api.open_uri("roblox-studio:")
        return ActionResult("Opened Roblox Studio.")

    @staticmethod
    def exe() -> str | None:
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
        hits = sorted(base.glob("*/RobloxStudioBeta.exe"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if base.is_dir() else []
        return str(hits[0]) if hits else None

    def places(self) -> list[Path]:
        roots = [Path(self.setting("places"))] if self.setting("places") else []
        roots.append(Path.home() / "Documents" / "ROBLOX")
        found: list[Path] = []
        for root in roots:
            if root.is_dir():
                for pattern in ("*.rbxl", "*.rbxlx"):
                    found += list(root.rglob(pattern))[:500]
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    def place(self, api: IntegrationAPI, params: dict[str, str]) -> ActionResult:
        name = normalize(params.get("name", "")).replace(".rbxl", "")
        files = {normalize(f.stem): f for f in self.places()}
        hit = next((f for s, f in files.items() if name and name in s), None)
        if hit is None:
            close = difflib.get_close_matches(name, list(files), n=1, cutoff=0.6)
            hit = files[close[0]] if close else None
        if hit is None:
            return ActionResult(f"No place file called “{params.get('name')}” — set your places folder on the "
                                f"Integrations page.", ok=False)
        _startfile(str(hit))
        return ActionResult(f"Opened {hit.name} in Roblox Studio.")


BUILTINS: list[type[Integration]] = [Phrases, Apps, System, Media, Spotify, Google, Discord, Blender, RobloxStudio]


# ════════════════════════════════════════════════════════════════════════════
#  Templates written by "Create template…"
# ════════════════════════════════════════════════════════════════════════════

TEMPLATE_PACK = {
    "id": "my_tools", "name": "My Tools", "aliases": ["my tools"], "icon": "★",
    "description": "Example pack — edit me. Phrases can contain {slots}.",
    "actions": [
        {"title": "Open Notepad", "phrases": ["open my notes", "notes please"], "do": {"open": "notepad"}},
        {"title": "Search Wikipedia", "phrases": ["wiki {query}", "look up {query} on wikipedia"],
         "do": {"url": "https://en.wikipedia.org/w/index.php?search={query}"}, "say": "Here's {query} on Wikipedia."},
        {"title": "Mute my mic in Discord", "phrases": ["mute my mic"], "do": {"keys": "ctrl+shift+m"},
         "say": "Mic toggled."},
    ],
}

TEMPLATE_PY = '''"""My integration — copy, rename and edit. Juno loads every .py in this folder."""
from juno_app.integrations import Action, ActionResult, Field, Integration


class CoinFlip(Integration):
    id = "coin_flip"
    name = "Coin flip"
    aliases = ["coin"]
    description = "Flips a coin."
    icon = "¢"
    fields = [Field("heads_word", "Word for heads", default="Heads")]

    def actions(self):
        return [Action("flip", "Flip a coin", ["flip a coin", "(heads|tails) or (heads|tails)"], self.flip)]

    def flip(self, api, params):
        import random
        side = self.setting("heads_word", "Heads") if random.random() < 0.5 else "Tails"
        return ActionResult(f"{side}!")
'''
