"""Juno launcher — the tiny entry point PyInstaller freezes into Juno.exe.

The app itself is the juno_app package, kept as plain files so Settings ▸ Updates can replace it without a
new installer:
  1. <app folder>\\core\\juno_app      — updated copy (written by the in-app updater)
  2. <exe bundle>\\fallback\\juno_app  — the copy shipped inside this build
If the updated copy fails to load, the shipped copy is used, so a bad update can't stop Juno from starting.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _candidates() -> list[Path]:
    out = [_app_dir() / "core"]
    if getattr(sys, "frozen", False):
        out.append(Path(getattr(sys, "_MEIPASS", _app_dir())) / "fallback")
    else:
        out.append(_app_dir())
    return [p for p in out if (p / "juno_app" / "__init__.py").is_file()]


def _forget() -> None:
    for name in [m for m in sys.modules if m == "juno_app" or m.startswith("juno_app.")]:
        del sys.modules[name]


def _log(details: str) -> None:
    try:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "Juno" / "logs"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "launcher.log", "a", encoding="utf-8") as fh:
            fh.write(details + "\n")
    except Exception:
        pass


def _fatal(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Juno", 0x10)
    except Exception:
        sys.stderr.write(message + "\n")


def run() -> int:
    last_error = "no copy of juno_app found"
    for folder in _candidates():
        sys.path.insert(0, str(folder))
        try:
            import juno_app.app as app  # noqa: PLC0415
        except Exception:
            last_error = traceback.format_exc()
            _log(f"{folder} failed to load — trying the next copy.\n{last_error}")
            sys.path.remove(str(folder))
            _forget()
            continue
        return int(app.main() or 0)
    _fatal("Juno could not start.\n\n" + last_error[-1500:])
    return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception:
        _fatal("Juno could not start.\n\n" + traceback.format_exc(limit=4))
        sys.exit(1)
