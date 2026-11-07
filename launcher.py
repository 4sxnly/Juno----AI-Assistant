"""Multi-Tool launcher — the tiny entry point that PyInstaller freezes into MultiTool.exe.

The real app lives in ``core/main.py`` next to the exe, as a plain file. That lets the app update
itself from GitHub (Settings ▸ Updates) without a new installer: only this launcher, the embedded
Python runtime and the bundled libraries/engines are frozen.

If ``core/main.py`` is missing or broken, the copy of main.py frozen into the exe at build time is used
instead, so a bad update can never stop the app from starting.
"""

from __future__ import annotations

import importlib.util
import multiprocessing
import sys
import traceback
from pathlib import Path


def _bundled():
    # A real import statement so PyInstaller analyses main.py and bundles everything it needs.
    import main  # noqa: PLC0415
    return main


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _load_core():
    core = _app_dir() / "core" / "main.py"
    if not core.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("main", core)
        module = importlib.util.module_from_spec(spec)
        sys.modules["main"] = module          # plugins that `import main` get this copy
        spec.loader.exec_module(module)
        if not callable(getattr(module, "main", None)):
            raise ImportError("core/main.py has no main()")
        return module
    except Exception:
        sys.modules.pop("main", None)
        _log_fallback(traceback.format_exc())
        return None


def _log_fallback(details: str) -> None:
    try:
        import os
        base = Path(os.environ.get("APPDATA") or Path.home()) / "MultiTool" / "logs"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "launcher.log", "a", encoding="utf-8") as fh:
            fh.write("core/main.py failed to load — using the built-in copy instead.\n" + details + "\n")
    except Exception:
        pass


def _fatal(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Multi-Tool", 0x10)
    except Exception:
        sys.stderr.write(message + "\n")


def run() -> int:
    module = _load_core() or _bundled()
    return int(module.main() or 0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception:
        _fatal("Multi-Tool could not start.\n\n" + traceback.format_exc(limit=4))
        sys.exit(1)
