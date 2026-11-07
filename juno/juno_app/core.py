"""
Juno core — the assistant's brain stem.

JunoCore builds on the engine (brain, voices, voice chat, characters, memory) and adds what turns a
companion into an assistant: integrations that run only when asked, listening modes (talk, "Hey Juno",
one-shot hotkey), an activity log, and avatars that live both in the main window and on the desktop.
"""

from __future__ import annotations

import json
import re
import threading
import time
import tkinter as tk
from collections import deque
from typing import Any, Callable

import customtkinter as ctk

from . import engine as E
from .base import ThemeHandle, find_binary, hidden_subprocess_kwargs
from .integrations import ActionResult, Integration, Match, Registry, clean_command
from .orb import ORB_STYLES, OrbOverlay, StageOrb

JUNO_DEFAULTS: dict[str, Any] = {
    "avatar_style": "orb",        # orb | core | pulse | character
    "listen_mode": "off",         # off | talk | wake
    "wake_word": "juno",
    "hotkey": True,               # Ctrl+Alt+J: listen for one command / bring Juno up
    "close_action": "mini",       # mini | quit
    "start_mini": False,
    "boot": True,
    "speak_actions": True,
}
LISTEN_MODES = {"off": "Off", "talk": "Talk", "wake": "Hey Juno"}


class JunoContext:
    """The services the engine expects (formerly Multi-Tool's plugin context)."""

    plugin_id = "juno"

    def __init__(self, app: Any):
        self.app = app
        self.paths = app.paths
        self.log = app.log
        self.data_dir = app.paths.data_dir
        self.models_dir = app.models_dir
        self.theme = ThemeHandle(app.theme, "juno")
        self.dnd_available = app.dnd_available

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.app.settings.get("juno", key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.app.settings.set("juno", key, value)

    def bin_path(self, name: str) -> Any:
        return find_binary(self.paths.bin_dir, name)

    def subprocess_kwargs(self) -> dict[str, Any]:
        return hidden_subprocess_kwargs()

    def register_drop_target(self, widget: Any, callback: Callable[..., Any],
                             accept: tuple[str, ...] = ("files", "text")) -> bool:
        return self.app.register_drop_target(widget, callback, accept, owner="juno")

    def register_file_type(self, extension: str, description: str, handler: Callable[..., Any]) -> None:
        self.app.file_types[extension.lower()] = (description, handler)

    def call_in_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.app.call_in_ui(fn, *args, **kwargs)

    def run_in_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self.app.run_in_thread(fn, *args, **kwargs)

    def copy_to_clipboard(self, text: str) -> None:
        self.app.copy_to_clipboard(text)

    def set_status(self, text: str, level: str = "info") -> None:
        self.app.call_in_ui(self.app.set_status, text, level)

    def open_path(self, path: Any) -> None:
        self.app.open_path(path)

    def notify(self, title: str, body: str = "", **kw: Any) -> None:
        self.app.call_in_ui(self.app.notify, title, body, **kw)


# ════════════════════════════════════════════════════════════════════════════
#  Avatars: one on the stage (main window), one on the desktop (mini mode)
# ════════════════════════════════════════════════════════════════════════════

class StageCharacter(E.Avatar):
    """The animated character drawn large in the main window (no wandering, no speech bubble)."""

    BUBBLE_H = 4

    def __init__(self, core: "JunoCore", parent: Any, bg: str):
        super().__init__(core)
        self.parent = parent
        self.bg = bg

    def build(self) -> None:
        if self.canvas is not None:
            return
        w, h = self.size
        self.canvas = tk.Canvas(self.parent, width=w, height=h, bg=self.bg, highlightthickness=0, bd=0)
        self.canvas._mt_role = "skip"
        self.win = self.canvas

    def set_character(self, character: dict[str, Any], folder: Any, size_factor: float) -> None:
        super().set_character(character, folder, size_factor * 1.45)

    def place(self, x: int, y: int, clamp: bool = False) -> None:
        pass

    def show(self) -> None:
        self.build()
        self.visible = True
        if self._after is None:
            self._last = time.monotonic()
            self._after = self.app.after(33, self.tick)

    def hide(self) -> None:
        self.visible = False
        if self._after is not None:
            try:
                self.app.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def destroy(self) -> None:
        self.hide()
        if self.canvas is not None:
            try:
                self.canvas.destroy()
            except Exception:
                pass
        self.canvas = self.win = None

    def say(self, text: str, seconds: float = 6.0) -> None:
        pass

    def _start_action(self, name: str, now: float) -> None:
        if name != "stroll":
            super()._start_action(name, now)

    def tick(self) -> None:
        try:
            if self.app.state() == "withdrawn" or not self.canvas.winfo_ismapped():
                self._after = self.app.after(200, self.tick)
                return
            self.x, self.y = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        except Exception:
            pass
        super().tick()


class AvatarHub:
    """Looks like one avatar to the engine; forwards to the stage avatar and (in mini mode) the desktop one."""

    BROADCAST = ("set_expression", "gesture", "start_speaking", "stop_speaking", "hop")

    def __init__(self, core: "JunoCore"):
        self.core = core
        self.stage: Any = None
        self.desk: Any = None
        self._mode = "idle"
        self.bubble = None
        self.stroll = None

    def parts(self) -> list[Any]:
        return [p for p in (self.stage, self.desk) if p is not None]

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value
        for part in self.parts():
            part.mode = value

    @property
    def visible(self) -> bool:
        return bool(self.desk is not None and self.desk.visible) or not self.core.app.mini

    @property
    def win(self) -> Any:
        return self.desk.win if self.desk is not None else None

    @property
    def panel_side(self) -> int:
        return getattr(self.desk, "panel_side", 0)

    @panel_side.setter
    def panel_side(self, value: int) -> None:
        if self.desk is not None:
            self.desk.panel_side = value

    def __getattr__(self, name: str) -> Any:
        if name in AvatarHub.BROADCAST:
            def fan(*args: Any, **kwargs: Any) -> None:
                for part in self.parts():
                    getattr(part, name)(*args, **kwargs)
            return fan
        target = self.desk or self.stage
        if target is None:
            raise AttributeError(name)
        return getattr(target, name)

    def say(self, text: str, seconds: float = 6.0) -> None:
        if self.desk is not None and self.desk.visible:
            self.desk.say(text, seconds)

    def show(self) -> None:
        if self.core.app.mini and self.desk is not None:
            self.desk.show()

    def hide(self) -> None:
        if self.desk is not None:
            self.desk.hide()

    def set_character(self, character: dict[str, Any], folder: Any, size: float) -> None:
        for part in self.parts():
            part.set_character(character, folder, size)

    def destroy(self) -> None:
        for part in self.parts():
            part.destroy()


# ════════════════════════════════════════════════════════════════════════════
#  Chat: the same conversation in the main window and in mini mode's panel
# ════════════════════════════════════════════════════════════════════════════

class EmbeddedChat(E.ChatPanel):
    """The engine's chat panel built inside the main window instead of its own window."""

    def __init__(self, core: "JunoCore"):
        super().__init__(core)
        self.container: Any = None

    @property
    def is_open(self) -> bool:  # type: ignore[override]
        app = self.p.app
        return bool(self.win is not None and not app.mini and app.current_page == "chat"
                    and app.state() != "withdrawn")

    @is_open.setter
    def is_open(self, _value: bool) -> None:
        pass

    def _build(self) -> None:
        if self.container is None:
            self.container = ctk.CTkFrame(self.p.app, fg_color="transparent")  # replaced when the page is built
        frame = self.p.ctx.theme.tag(ctk.CTkFrame(self.container, fg_color="transparent"), "transparent")
        frame.pack(fill="both", expand=True)
        self.win = frame
        self._build_body(frame)

    def show(self) -> None:
        self.p.app.exit_mini() if self.p.app.mini else None
        self.p.app.show_page("chat")
        try:
            self.entry.focus_set()
        except Exception:
            pass
        self.p.on_chat_opened()

    def hide(self) -> None:
        pass

    def reposition(self) -> None:
        pass


class ChatHub:
    WRITE = ("add_user", "begin_reply", "stream", "end_reply", "system", "clear", "set_status",
             "show_attachments", "offer_setup", "update_setup_progress", "hide_setup", "refresh_controls",
             "refresh_mic", "offer_addon", "hide_addon", "apply_theme")

    def __init__(self, core: "JunoCore", main: EmbeddedChat, mini: E.ChatPanel):
        self.core = core
        self.main = main
        self.mini = mini
        self.skip_user_echo = False

    def active(self) -> Any:
        return self.mini if self.core.app.mini else self.main

    def __getattr__(self, name: str) -> Any:
        if name in ChatHub.WRITE:
            def fan(*args: Any, **kwargs: Any) -> None:
                if name == "add_user" and self.skip_user_echo:
                    self.skip_user_echo = False
                    return
                for view in (self.main, self.mini):
                    if view.win is None and name in ("set_status", "refresh_controls", "refresh_mic",
                                                     "apply_theme", "hide_setup", "hide_addon"):
                        continue
                    try:
                        getattr(view, name)(*args, **kwargs)
                    except Exception:
                        self.core.ctx.log.exception("chat view %s failed", name)
            return fan
        return getattr(self.active(), name)

    @property
    def is_open(self) -> bool:
        return self.main.is_open or self.mini.is_open

    def offer_memories(self, memories: list[str]) -> None:
        self.active().offer_memories(memories)

    def show(self) -> None:
        self.active().show()

    def hide(self) -> None:
        self.mini.hide()

    def toggle(self) -> None:
        if self.core.app.mini:
            self.mini.toggle()
        else:
            self.main.show()

    def reposition(self) -> None:
        if self.mini.win is not None:
            self.mini.reposition()

    @property
    def addon_widgets(self) -> dict[str, Any]:
        return self.active().addon_widgets


# ════════════════════════════════════════════════════════════════════════════
#  The core
# ════════════════════════════════════════════════════════════════════════════

class JunoCore(E.Plugin):

    def __init__(self, ctx: JunoContext, app: Any):
        self.app = app
        super().__init__(ctx)
        for key, value in JUNO_DEFAULTS.items():
            self.settings.setdefault(key, ctx.get_setting(key, value))
        if self.settings["avatar_style"] not in (*ORB_STYLES, "character"):
            self.settings["avatar_style"] = "orb"
        self.powered = True
        self.char_overlay = self.avatar
        self.avatar = AvatarHub(self)
        self.mini_chat = self.chat
        self.main_chat = EmbeddedChat(self)
        self.chat = ChatHub(self, self.main_chat, self.mini_chat)
        self.integrations = Registry(self)
        self.integrations.load()
        self.activity_file = ctx.data_dir / "activity.json"
        self.activity: deque[dict[str, Any]] = deque(self._load_activity(), maxlen=200)
        self._activity_save: Any = None
        self._follow_until = 0.0
        self._once = False
        self._once_timer: Any = None
        self.stage_parent: Any = None

    # ── settings / lifecycle ──────────────────────────────────────────────
    def wake_word(self) -> str:
        return (self.settings.get("wake_word") or "juno").strip().lower() or "juno"

    def power_on(self) -> None:
        self.powered = True
        self.avatar.show()

    def power_off(self) -> None:
        self.app.quit_app()

    def toggle_power(self) -> None:
        self.app.quit_app()

    def open_chat(self) -> None:
        self.chat.show()

    def show_avatar(self) -> None:
        self.avatar.show()

    def hide_avatar(self) -> None:
        if self.app.mini:
            self.app.exit_mini()

    def open_settings_page(self) -> None:
        self.app.exit_mini()
        self.app.show_page("avatar")

    def _refresh_page(self, rebuild: bool = False) -> None:
        self.app.refresh_page(rebuild)

    def _refresh_status_bar(self) -> None:
        self.app.refresh_status()

    def _startup(self) -> None:
        if self.settings["greet"] and not self.settings["dnd"]:
            greetings = self.character["persona"]["greetings"]
            if greetings:
                line = E.random.choice(greetings)
                self.ctx.app.after(1500, lambda: (self.avatar.say(line, 7), self.avatar.gesture("wave", 1.8),
                                                  self.log_activity("Online", line, "ok")))
        self._schedule_quip()

    def shutdown(self) -> None:
        if self._activity_save is not None:
            self._save_activity()
        super().shutdown()

    # ── avatars ───────────────────────────────────────────────────────────
    def style(self) -> str:
        return self.settings["avatar_style"]

    def build_stage(self, parent: Any) -> Any:
        """(Re)create the main-window avatar inside parent; returns its canvas."""
        self.stage_parent = parent
        old = self.avatar.stage
        if old is not None:
            old.destroy()
        p = self.ctx.theme.palette
        if self.style() == "character":
            stage = StageCharacter(self, parent, p["card"])
            stage.set_character(self.character, self.library.folders.get(self.character["id"]),
                                E.SIZES.get(self.settings["size"], 1.0))
            stage.build()
        else:
            stage = StageOrb(self, parent, self.style())
        stage.mode = self.avatar.mode
        self.avatar.stage = stage
        stage.show()
        return stage.canvas

    def make_desk(self) -> Any:
        old = self.avatar.desk
        if old is not None:
            if old is self.char_overlay:
                old.hide()
            else:
                old.destroy()
        if self.style() == "character":
            desk = self.char_overlay
            desk.set_character(self.character, self.library.folders.get(self.character["id"]),
                               E.SIZES.get(self.settings["size"], 1.0))
        else:
            desk = OrbOverlay(self, self.style())
        desk.mode = self.avatar.mode
        self.avatar.desk = desk
        return desk

    def set_avatar_style(self, style: str) -> None:
        if style == self.style():
            return
        self.set_setting("avatar_style", style)
        if self.stage_parent is not None:
            self.app.on_stage_rebuilt(self.build_stage(self.stage_parent))
        if self.app.mini:
            self.make_desk().show()
        self.log_activity("Avatar", ORB_STYLES.get(style, ("Character",))[0] if style != "character"
                          else self.character["name"], "ok")

    def select_character(self, cid: str) -> None:
        super().select_character(cid)
        if self.style() == "character" and self.stage_parent is not None:
            self.app.on_stage_rebuilt(self.build_stage(self.stage_parent))

    # ── activity log ──────────────────────────────────────────────────────
    def _load_activity(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.activity_file.read_text(encoding="utf-8"))
            return [d for d in data if isinstance(d, dict)][-200:]
        except Exception:
            return []

    def _save_activity(self) -> None:
        self._activity_save = None
        try:
            tmp = self.activity_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.activity)[-200:], ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.activity_file)
        except Exception:
            pass

    def log_activity(self, title: str, detail: str = "", level: str = "info", integration: str = "") -> None:
        """level: ok | info | warn | error. Shows in the live feed and the Activity page."""
        entry = {"t": time.time(), "title": title[:80], "detail": detail[:240], "level": level,
                 "integration": integration}
        self.activity.append(entry)
        if self._activity_save is None:
            self._activity_save = self.app.after(1500, self._save_activity)
        self.app.on_activity(entry)

    # ── the command router ────────────────────────────────────────────────
    def send_message(self, text: str, spoken: bool = False) -> None:
        if text and not self.attachments and self.try_command(text, spoken):
            return
        super().send_message(text, spoken)

    def try_command(self, text: str, spoken: bool = False) -> bool:
        match = self.integrations.match(text)
        if match is not None:
            self.run_action(match, text, spoken)
            return True
        integ = self.integrations.mentioned(text)
        if integ is not None and self._brain_ready():
            self.ai_pick_action(integ, text, spoken)
            return True
        return False

    def _echo_user(self, text: str, spoken: bool) -> None:
        self.voice_out.stop()
        self.avatar.stop_speaking()
        self.chat.add_user(text, 0, spoken)
        self.history.append({"role": "user", "text": text, "images": []})

    def run_action(self, match: Match, text: str, spoken: bool = False, echo: bool = True) -> None:
        if self._busy:
            self.chat.system("Still working on the last request — press ■ to cancel it.")
            return
        if echo:
            self._echo_user(text, spoken)
        action, integ = match.action, match.integration
        self.chat.set_status(f"{action.title}…")
        self.avatar.mode = "thinking"
        api = self.integrations.api

        def work() -> Any:
            return action.run(api, dict(match.params))

        def done(result: Any) -> None:
            if not isinstance(result, ActionResult):
                result = ActionResult(str(result) if result else "Done.")
            self.finish_action(integ, action.title, result, spoken)

        def failed(exc: BaseException) -> None:
            message = str(exc) or exc.__class__.__name__
            self.finish_action(integ, action.title, ActionResult(f"Couldn't do that: {message}", ok=False), spoken)

        if action.background:
            self.ctx.run_in_thread(work, on_done=done, on_error=failed, name=f"action-{integ.id}")
        else:
            try:
                done(work())
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.exception("Action %s failed", action.id)
                failed(exc)

    def finish_action(self, integ: Integration, title: str, result: ActionResult, spoken: bool) -> None:
        self.avatar.mode = "idle"
        self.chat.begin_reply()
        self.chat.stream(f"▸ {result.message}")
        self.chat.end_reply()
        self.history.append({"role": "assistant", "text": f"[Action] {result.message}", "images": []})
        self.avatar.set_expression("pleased" if result.ok else "concerned", 3)
        self.log_activity(title, result.message, "ok" if result.ok else "warn", integ.id)
        say = result.message if result.say is None else result.say
        if say and self.settings.get("speak_actions", True) and self.can_speak():
            self.speak(say.split("\n")[0])
        else:
            self.chat.set_status("Idle")
        if self.app.mini and not self.chat.is_open:
            self.avatar.say(result.message, 6)
        if not result.ok:
            self.ctx.theme.play("error")

    def _brain_ready(self) -> bool:
        provider = self.settings["provider"]
        if provider == "local":
            return self.local_manifest() is not None and self.engine.available()
        if provider == "anthropic":
            return bool(self.api_key())
        return provider == "openai"

    def ai_pick_action(self, integ: Integration, text: str, spoken: bool) -> None:
        """You named an integration but no command pattern fit: let the brain pick ONE of its actions."""
        if self._busy:
            self.chat.system("Still working on the last request — press ■ to cancel it.")
            return
        actions = {a.id: a for a in integ.actions()}
        lines = []
        for a in actions.values():
            params = ", ".join(f"{k} ({v})" for k, v in a.params.items()) or "none"
            lines.append(f"- {a.id}: {a.title}. {a.description} Params: {params}. "
                         f"Example: {(a.examples or a.patterns)[0]}")
        system = (f"You map a user's request onto ONE action of the '{integ.name}' integration. Reply with ONLY a "
                  f"JSON object like {{\"action\": \"id\", \"params\": {{\"name\": \"value\"}}}}. If the request "
                  f"isn't really asking for one of these actions, reply {{\"action\": \"none\"}}.\nActions:\n"
                  + "\n".join(lines))
        self._echo_user(text, spoken)
        self._busy = True
        self.avatar.mode = "thinking"
        self.chat.set_status("Working out what you want…")
        manifest = self.local_manifest() if self.settings["provider"] == "local" else None

        def work() -> str:
            return self.complete(system, [{"role": "user", "text": clean_command(text, self.wake_word()),
                                           "images": []}], manifest)

        def done(reply: str) -> None:
            self._busy = False
            self.avatar.mode = "idle"
            choice = parse_json_object(reply)
            action = actions.get(str(choice.get("action", ""))) if choice else None
            if action is None:
                self.history.pop()   # not a command after all — answer as normal chat
                self.chat.skip_user_echo = True
                E.Plugin.send_message(self, text, spoken)
                return
            params = {k: str(v) for k, v in (choice.get("params") or {}).items() if v not in (None, "")}
            if any(k not in params for k in required_slots(action)):
                self.history.pop()   # the brain couldn't fill in what the action needs — just talk
                self.chat.skip_user_echo = True
                E.Plugin.send_message(self, text, spoken)
                return
            params["_text"] = clean_command(text, self.wake_word())
            self.run_action(Match(integ, action, params, "ai"), text, spoken, echo=False)

        def failed(exc: BaseException) -> None:
            self._busy = False
            self.avatar.mode = "idle"
            self.chat.system(f"Couldn't work that out: {exc}")
            self.chat.set_status("Idle")

        self.ctx.run_in_thread(work, on_done=done, on_error=failed, name="ai-action")

    def complete(self, system: str, messages: list[dict[str, Any]], manifest: Any = None,
                 max_tokens: int = 200) -> str:
        """Blocking one-shot answer from the configured brain (call from a worker thread)."""
        provider, key, out = self.settings["provider"], self.api_key(), []
        cancel = threading.Event()
        on_text = out.append
        if provider == "local":
            base = self.engine.ensure(manifest, lambda t: self.ctx.call_in_ui(self.chat.set_status, t))
            E.stream_openai_compatible("", "local", system, messages, on_text, cancel, base,
                                       extra=dict(E.LOCAL_SAMPLING, temperature=0.1), max_tokens=max_tokens)
            self.engine.last_used = time.monotonic()
        elif provider == "openai":
            E.stream_openai_compatible(key, self.settings.get("model") or "gpt-4o-mini", system, messages, on_text,
                                       cancel, self.settings.get("base_url") or "https://api.openai.com/v1")
        elif provider == "anthropic":
            E.stream_anthropic(key, self.settings.get("model") or E.CLAUDE_MODELS[0], system, messages, on_text,
                               cancel, self.settings.get("base_url") or "https://api.anthropic.com")
        else:
            raise E.ProviderError("the AI is switched off")
        return "".join(out)

    # ── listening: off / talk / "Hey Juno" / one-shot hotkey ──────────────
    def toggle_voice_chat(self) -> None:
        self.set_listen_mode("off" if self.listener.state != "off" else "talk")

    def set_listen_mode(self, mode: str) -> None:
        mode = mode if mode in LISTEN_MODES else "off"
        self.set_setting("listen_mode", mode)
        self._once = False
        if mode == "off":
            self.listener.stop()
            self.chat.set_status("Idle")
        else:
            self.start_listening()
        self.chat.refresh_mic()
        self.app.refresh_voice()

    def start_listening(self) -> bool:
        if self.listener.state != "off":
            return True
        if not self.perm("mic"):
            self.ctx.set_status("The microphone is turned off on the Permissions page.", "warning")
            return False
        ok, why = self.listener.available()
        if not ok:
            if why == "download":
                self._want_voice_chat = True
                self.chat.offer_addon("listen")
                self.chat.system("Listening needs a one-time download (about 110 MB) — press Download above. "
                                 "After that, speech is recognised entirely on your PC.")
                self.chat.show()
            else:
                self.chat.system(f"Listening isn't available: {why}.")
            self.set_setting("listen_mode", "off")
            return False
        if self.settings["muted"]:
            self.set_muted(False)

        def ready() -> None:
            mode = self.settings.get("listen_mode")
            if self._once:
                text = "Listening for one command…"
            elif mode == "wake":
                text = f"Listening for “Hey {self.wake_word().title()}”"
            else:
                text = "Listening — just talk"
            self.chat.set_status(text)
            self.chat.refresh_mic()
            self.app.refresh_voice()

        def error(message: str) -> None:
            self.chat.system(message)
            self.set_setting("listen_mode", "off")
            self.chat.refresh_mic()
            self.app.refresh_voice()

        self.listener.muted = False
        self.listener.start(ready, error)
        return True

    def listen_once(self) -> None:
        """Hotkey: take one command, then go back to whatever mode was on before."""
        self.ctx.theme.play("open")
        if self.listener.state != "off" and self.settings.get("listen_mode") == "talk":
            self.voice_out.stop()
            return
        if self.settings.get("listen_mode") == "wake" and self.listener.state != "off":
            self._follow_until = time.monotonic() + 8
            self.chat.set_status("Listening…")
            return
        self._once = True
        if self.start_listening():
            if self._once_timer is not None:
                self.app.after_cancel(self._once_timer)
            self._once_timer = self.app.after(12_000, self._end_once)

    def _end_once(self) -> None:
        self._once_timer = None
        if self._once:
            self._once = False
            if self.settings.get("listen_mode") == "off":
                self.listener.stop()
                self.chat.set_status("Idle")
            self.app.refresh_voice()

    def on_heard(self, text: str) -> None:
        if self._once:
            self.log_activity("Heard", text, "info")
            super().on_heard(text)   # hand the command over first, then close the mic
            self._end_once()
            return
        if self.settings.get("listen_mode") == "wake":
            wake = re.escape(self.wake_word())
            m = re.match(rf"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?{wake}\b\W*(.*)$", text.strip(), re.I)
            if m:
                rest = m.group(1).strip()
                if not rest:
                    self._follow_until = time.monotonic() + 8
                    self.ctx.theme.play("open")
                    self.chat.set_status("Yes?")
                    if self.can_speak():
                        self.speak("Yes?")
                    return
                text = rest
            elif time.monotonic() > self._follow_until:
                return  # not for Juno
            self._follow_until = 0.0
        self.log_activity("Heard", text, "info")
        super().on_heard(text)

    def toggle_mic_mute(self) -> None:
        super().toggle_mic_mute()
        self.app.refresh_voice()


def required_slots(action: Any) -> set[str]:
    """Slots every pattern of an action needs (not inside [optional] parts)."""
    sets = [set(re.findall(r"\{(\w+)", re.sub(r"\[[^\]]*\]", "", p))) for p in action.patterns]
    return set.intersection(*sets) if sets else set()


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                        return value if isinstance(value, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None
