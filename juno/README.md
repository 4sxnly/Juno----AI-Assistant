# Juno — desktop assistant

A green-phosphor "command center" for your PC: an AI that runs on your own computer, talks and listens,
and carries out tasks through integrations — but only when you ask.

## What it does

* **Command Center** — live dashboard around Juno's avatar: core status, live activity feed, integrations,
  recent commands, quick commands, system monitor, memory and brain status.
* **Talk to it** — type in the chat or the command bar (Ctrl+K), or speak:
  * **Talk** mode: everything you say goes to Juno.
  * **Hey Juno** mode: always listening, but only acts when you start with her name (wake word is editable).
  * **Ctrl+Alt+J** anywhere: listen for one command.
  Speech is recognised on your PC (Moonshine) and never saved. Voices: 28 natural Kokoro voices or Windows voices.
* **Integrations** (only run when you explicitly ask):

  | Integration | Say things like |
  |---|---|
  | Apps | "open Notepad", "close Spotify" — any Start-menu app by name |
  | Phrases | your own words → apps, files, folders, sites ("open Blender", "game folder") |
  | System | "volume up by 20", "set the volume to 35", "mute", "lock my PC", "take a screenshot", "set a timer for 5 minutes", "what time is it" |
  | Media keys | "pause the music", "next song", "previous track" |
  | Spotify | "open Spotify", "play lofi beats on Spotify", "what's playing" (connect a Premium account for full control) |
  | Google & web | "google cheap flights", "search YouTube for…", "open Gmail", "directions to…", "go to example.com" |
  | Discord | "open Discord", "any new Discord messages" |
  | Blender | "open Blender", "open my last Blender file", "open the Blender file train" |
  | Roblox Studio | "open Roblox Studio", "open my lobby place in Studio" (local files only; never touches your account) |

  If you name an integration but phrase it differently ("put some lofi on in Spotify"), the AI picks the matching
  action — it is only ever shown that one integration's actions.
* **Add your own** — Integrations ▸ Create template. `.junoint` files are JSON (no code): phrases with `{slots}`
  that open apps/URLs, press hotkeys or run programs. `.py` files can do anything. Drop either onto Juno's window.
  Full format: top of `juno_app/integrations.py`.
* **Avatars** — static styles (Orb, Core, Pulse — animated rings, sparks and voice-reactive bars) or an animated
  character (Juno, Vex, or one you import as `.mtchar`).
* **Mini mode** — hides the window; the avatar stays on the desktop. Click it to chat, double-click to open
  Juno, right-click for listening mode, mute and quit. The ✕ button goes to mini mode (changeable in Settings).

## Files

| Path | What |
|---|---|
| `juno_app/` | the app (updated in place by Settings ▸ Updates) |
| `juno_app/base.py` | shared plumbing generated from Multi-Tool's core (window services, theme engine, pop-ups, updater) |
| `juno_app/engine.py` | brain (llama.cpp / Claude / OpenAI-compatible), voices, voice chat, characters, memory |
| `juno_app/core.py` | the assistant: command router, listening modes, avatars, activity |
| `juno_app/integrations.py` | integration framework + built-ins |
| `juno_app/orb.py` | static-style avatars |
| `juno_app/app.py` | the command-center window, mini mode, hotkey, start-up |
| `launcher.py` | frozen entry point |
| `build/` | PyInstaller spec + Inno Setup script |
| `integrations/` | integrations shipped with Juno (yours live in `%APPDATA%\Juno\integrations`) |

Built by `.github/workflows/build-juno.yml`; releases are tagged `juno-build-N`.
