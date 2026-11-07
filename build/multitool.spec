# PyInstaller spec — builds dist/MultiTool/ (one-folder app with its own embedded Python).
#
#   pyinstaller --noconfirm --clean build/multitool.spec
#
# Plugins are plain .py files loaded at runtime, so PyInstaller can't see what they import.
# To make any plugin work without the user installing anything, this build ships:
#   * the ENTIRE Python standard library
#   * every third-party package listed in BUNDLED_PACKAGES (keep in sync with requirements.txt)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is injected by PyInstaller)

# Third-party packages plugins may import. Add new ones here + in requirements.txt.
BUNDLED_PACKAGES = [
    "customtkinter",
    "tkinterdnd2",   # includes the native tkdnd library for drag-and-drop
    "PIL",           # Pillow: thumbnails, screenshots, textures
    "darkdetect",    # customtkinter dependency
]
if sys.platform == "win32":
    BUNDLED_PACKAGES.append("win32com")

# ── whole standard library ──────────────────────────────────────────────────
SKIP_STDLIB = {
    "test", "tests", "idlelib", "lib2to3", "turtledemo", "ensurepip", "venv", "pydoc_data",
    "tkinter.test", "unittest.test", "distutils", "this", "antigravity", "__phello__", "_pyrepl",
    # POSIX-only modules that don't exist on Windows
    "curses", "_curses", "_curses_panel", "readline", "grp", "pwd", "termios", "tty", "pty", "fcntl",
    "resource", "posix", "syslog", "crypt", "spwd", "nis", "ossaudiodev", "_posixsubprocess",
    "_posixshmem", "_scproxy", "_gdbm", "_dbm", "dbm.gnu", "dbm.ndbm", "multiprocessing.popen_fork",
    "multiprocessing.popen_forkserver", "multiprocessing.popen_spawn_posix", "_tkinter_test",
}


def _keep(name: str) -> bool:
    parts = name.split(".")
    if any(p in ("test", "tests", "idle_test") for p in parts[1:]):
        return False
    return not any(name == s or name.startswith(s + ".") for s in SKIP_STDLIB)


hiddenimports = []
for top in sorted(sys.stdlib_module_names):
    if not _keep(top):
        continue
    try:
        hiddenimports += [m for m in collect_submodules(top, filter=_keep) if _keep(m)] or [top]
    except Exception:
        hiddenimports.append(top)

# pywin32 (Windows speech via SAPI COM, clipboard, window APIs) — its PyInstaller hooks bundle the DLLs
if sys.platform == "win32":
    hiddenimports += ["win32com.client", "pythoncom", "pywintypes", "win32api", "win32con", "win32gui",
                      "win32clipboard"]

datas, binaries = [], []
for pkg in BUNDLED_PACKAGES:
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # optional package missing from this build environment
        print(f"WARNING: could not bundle {pkg}: {exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

icon = ROOT / "assets" / "icon.ico"
if icon.is_file():
    datas.append((str(icon), "assets"))

# The exe runs launcher.py, which loads core/main.py (updatable in place) and falls back to the copy of
# main.py frozen in here. Importing main from the launcher makes PyInstaller bundle everything it uses.
a = Analysis(  # noqa: F821
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    excludes=["tkinter.test", "unittest.test", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiTool",
    console=False,                # windowed app, no console
    icon=str(icon) if icon.is_file() else None,
    upx=False,                    # UPX triggers antivirus false positives
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MultiTool",
)
