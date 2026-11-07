# Multi-Tool — Desktop Utility Belt

A plugin-based Windows toolbox. The installer contains everything: its own Python runtime,
all libraries, and the engines (yt-dlp, ffmpeg, Deno). Users install nothing else.

This repo also holds **Juno**, the desktop assistant (its own app, in `juno/` — see `juno/README.md`).
Multi-Tool itself contains no AI assistant.

## How updates work (no new installer for most changes)

| You change… | What happens |
|---|---|
| `main.py`, anything in `plugins/`, `extras/themes/*.mttheme` | Just commit it. The installed app finds it in **Settings ▸ Updates** (it also checks every 6 hours) and downloads only the changed files. A `main.py` change restarts the app. |
| `requirements.txt`, `launcher.py`, `build/**`, `assets/**`, `.github/workflows/build.yml` | GitHub builds a new installer automatically and publishes it as a Release. The app's **Settings ▸ Updates** offers **Download & install**, which runs it silently and reopens Multi-Tool. |

The exe runs `launcher.py`, which loads `core/main.py` next to the exe (the updatable copy). If that file is
ever broken, it falls back to the copy frozen into the exe, so a bad update can't stop the app from starting.

## Build the installer by hand

Repo ▸ **Actions** ▸ **Build Multi-Tool installer** (or **Build Juno installer**) ▸ **Run workflow**. When it finishes, the installer is on the
repo's **Releases** page (and in the run's *Artifacts*).

## Layout

| Path | What |
|---|---|
| `main.py` | Core shell: window, theme engine (effects + sounds), plugin loader, pop-up notifications, updater |
| `launcher.py` | Tiny frozen entry point that loads `core/main.py` |
| `plugins/` | One file (or folder) per tool |
| `extras/` | Theme packs and character packs |
| `build/multitool.spec` | PyInstaller: bundles Python + whole stdlib + `BUNDLED_PACKAGES` |
| `build/fetch_engines.ps1` | Downloads + verifies yt-dlp, Deno, ffmpeg (Multi-Tool) or llama.cpp (Juno) at build time |
| `build/installer.iss` | Inno Setup: per-user install, no admin, tool checkboxes |
| `.github/workflows/build.yml` | Builds, smoke-tests and publishes Multi-Tool (releases `build-N`) |
| `.github/workflows/build-juno.yml` | Builds, smoke-tests and publishes Juno (releases `juno-build-N`) |
| `juno/` | Juno, the desktop assistant app |

## Plugins

| Plugin | What it does |
|---|---|
| Media Downloader | YouTube (and 1,000+ sites) to MP4/MP3, queue, playlists, pop-up when done |
| Discord Heads-Up | Themed pop-ups for Discord messages (avatar, name, server, text, your notes). Reads Windows' notification list; no Discord login |
| Drop Zone (Demo) | Reference plugin for writing your own |

## Themes

Built in: Default Dark, Cyberpunk Neon. Packs: Overdrive 2099, Phosphor CRT, Amber Lab 86.
A `.mttheme` is JSON or a zip with `theme.json` (+ banner PNG, + `sounds/*.wav`). `effects` switch on shell features:
`card_echo` (stacked 3D panel), `corner_brackets`, `title_shadow`, `title_decrypt`, `glitch_titles`, `kicker`,
`hex_ticker`, `scan_sweep`, `boot_sequence` + `boot_lines`, `status_clock`, `status_telemetry`, `blink_cursor`,
`body_font`, `synth_sounds` (`neon`, `hazard`, `phosphor`, `amber`, `soft` — built-in sounds, no files needed).
Full list: `EFFECT_DEFAULTS` in `main.py`.

## Installing add-ons by hand

Settings → **＋ Install…**, or drop the file anywhere on the Settings page:

| File | What it is |
|---|---|
| `.py` / `.zip` | Plugin |
| `.mttheme` | Theme pack |
