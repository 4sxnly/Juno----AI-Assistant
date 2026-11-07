# PyInstaller spec — builds dist/Juno/ (one-folder app with its own embedded Python).
#
#   pyinstaller --noconfirm --clean juno/build/juno.spec
#
# The exe runs juno/launcher.py. The app itself (juno_app) is NOT compiled into the exe: it's shipped as
# plain files (a fallback copy inside the bundle, the live copy in <app>\core\juno_app) so Settings ▸ Updates
# can replace it without a new installer. That's why the whole standard library and every package Juno or
# an integration may import is bundled here.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821  -> juno/

BUNDLED_PACKAGES = ["customtkinter", "tkinterdnd2", "PIL", "darkdetect", "numpy", "sounddevice",
                    "_sounddevice_data", "sherpa_onnx"]
if sys.platform == "win32":
    BUNDLED_PACKAGES.append("win32com")

SKIP_STDLIB = {
    "test", "tests", "idlelib", "lib2to3", "turtledemo", "ensurepip", "venv", "pydoc_data",
    "tkinter.test", "unittest.test", "distutils", "this", "antigravity", "__phello__", "_pyrepl",
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
if sys.platform == "win32":
    hiddenimports += ["win32com.client", "pythoncom", "pywintypes", "win32api", "win32con", "win32gui",
                      "win32clipboard"]

datas, binaries = [], []
for pkg in BUNDLED_PACKAGES:
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:
        print(f"WARNING: could not bundle {pkg}: {exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

# the app's own code, as plain files (fallback copy)
for src in (ROOT / "juno_app").rglob("*.py"):
    datas.append((str(src), str(Path("fallback") / src.parent.relative_to(ROOT))))
icon = ROOT / "assets" / "icon.ico"
if icon.is_file():
    datas.append((str(icon), "assets"))

a = Analysis(  # noqa: F821
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    excludes=["juno_app", "tkinter.test", "unittest.test", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name="Juno", console=False,
    icon=str(icon) if icon.is_file() else None, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Juno")  # noqa: F821
