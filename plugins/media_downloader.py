"""
Media Downloader — Multi-Tool plugin (API v1)

A YouTube-first front end for the bundled engines:
    bin/yt-dlp.exe   extraction + download (self-updates from inside the app)
    bin/ffmpeg.exe   merging, audio conversion, thumbnails, clips, SponsorBlock cuts
    bin/deno.exe     JavaScript runtime yt-dlp needs to solve YouTube's challenges

Nothing is read from the user's system: no Python, no PATH, no yt-dlp config files.

YouTube features: videos, Shorts, live VODs, playlists, channels, YouTube Music,
quality up to 8K (SDR preferred unless HDR is allowed), H.264 "plays everywhere" mode,
subtitles, chapters, split-by-chapter, SponsorBlock removal, clip ranges, instant
thumbnails, browser cookies for age-restricted/members content.
Other sites (TikTok, X/Twitter, Instagram, Twitch, Vimeo, SoundCloud, Reddit, …) use the
same pipeline — anything yt-dlp supports.
"""

from __future__ import annotations

import ctypes
import io
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import customtkinter as ctk

try:
    from PIL import Image
except Exception:  # thumbnails are optional
    Image = None

IS_WINDOWS = sys.platform == "win32"

# ════════════════════════════════════════════════════════════════════════════
#  Options
# ════════════════════════════════════════════════════════════════════════════

FORMATS: dict[str, dict[str, str]] = {
    "MP4 · H.264 — plays everywhere": {"kind": "video", "codec": "compat", "container": "mp4"},
    "MP4 · Best quality (VP9/AV1)": {"kind": "video", "codec": "best", "container": "mp4"},
    "MKV · Best quality, any codec": {"kind": "video", "codec": "best", "container": "mkv"},
    "MP3 · Audio": {"kind": "audio", "codec": "mp3"},
    "M4A · Audio (AAC, no re-encode)": {"kind": "audio", "codec": "m4a"},
    "Opus · Audio (YouTube original)": {"kind": "audio", "codec": "opus"},
    "WAV · Audio (for editing)": {"kind": "audio", "codec": "wav"},
}
VIDEO_QUALITIES: dict[str, int | None] = {
    "Best available": None,
    "8K · 4320p": 4320,
    "4K · 2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
MP3_QUALITIES: dict[str, str] = {
    "Best (VBR ~245 kbps)": "0",
    "320 kbps": "320K",
    "192 kbps": "192K",
    "128 kbps": "128K",
}
ORIGINAL_QUALITY = "Original quality"
COOKIE_BROWSERS = ["None", "Firefox", "Chrome", "Edge", "Brave", "Opera", "Vivaldi", "Chromium"]

DEFAULTS: dict[str, Any] = {
    "format": "MP4 · H.264 — plays everywhere",
    "video_quality": "1080p",
    "mp3_quality": "Best (VBR ~245 kbps)",
    "playlist": False,
    "subs": False,
    "sub_langs": "en",
    "sponsorblock": False,
    "thumbnail": True,
    "chapters": True,
    "split_chapters": False,
    "hdr": False,
    "archive": False,
    "cookies": "None",
    "parallel": "2",
    "clipboard_watch": False,
    "auto_update": True,
    "nightly": False,
    "out_dir": "",
    "last_update_check": 0.0,
}

PP_LABELS = {
    "Merger": "Merging video + audio",
    "FFmpegMerger": "Merging video + audio",
    "FFmpegExtractAudio": "Converting audio",
    "EmbedThumbnail": "Embedding thumbnail",
    "FFmpegThumbnailsConvertor": "Preparing thumbnail",
    "FFmpegMetadata": "Writing metadata",
    "FFmpegEmbedSubtitle": "Embedding subtitles",
    "FFmpegSubtitlesConvertor": "Converting subtitles",
    "SponsorBlock": "Finding sponsor segments",
    "ModifyChapters": "Removing sponsor segments",
    "FFmpegSplitChapters": "Splitting chapters",
    "FFmpegVideoRemuxer": "Remuxing",
    "FFmpegVideoConvertor": "Converting video",
    "FFmpegFixupM3u8": "Fixing stream",
    "FFmpegFixupM4a": "Fixing audio container",
    "FFmpegFixupDuplicateMoov": "Fixing container",
    "FFmpegFixupTimestamp": "Fixing timestamps",
    "MoveFiles": "Saving",
}

# Machine-readable lines we ask yt-dlp to print (fields separated by | or TAB)
DL_TEMPLATE = (
    "download:[MT-DL] %(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(info.vcodec)s|"
    "%(info.acodec)s|%(info.playlist_index)s|%(info.playlist_count)s"
)
PP_TEMPLATE = "postprocess:[MT-PP] %(progress.status)s|%(progress.postprocessor)s"
INFO_PRINT = "video:[MT-INFO] %(id)s\t%(extractor_key)s\t%(duration_string)s\t%(thumbnail)s\t%(playlist_title)s\t%(title)s"
FILE_PRINT = "after_move:[MT-FILE] %(filepath)s"

URL_RE = re.compile(r"https?://[^\s<>\"'{}|\\^`]+", re.IGNORECASE)
YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/|v/)|youtu\.be/|"
    r"music\.youtube\.com/watch\?(?:[^#]*&)?v=)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
YT_COLLECTION_RE = re.compile(
    r"youtube\.com/(?:playlist\?|@[^/?#]+|channel/|c/|user/)", re.IGNORECASE
)
MEDIA_HOST_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|tiktok\.com|x\.com|twitter\.com|instagram\.com|"
    r"twitch\.tv|vimeo\.com|soundcloud\.com|reddit\.com|redd\.it|facebook\.com|fb\.watch|"
    r"dailymotion\.com|bandcamp\.com|bilibili\.com|kick\.com|streamable\.com)/",
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════════════════════════════
#  Pure helpers (no UI)
# ════════════════════════════════════════════════════════════════════════════

def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(").,;]'\"")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def youtube_id(url: str) -> str | None:
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def is_collection_url(url: str) -> bool:
    """Pure playlist/channel links (not a single video that happens to carry &list=)."""
    return bool(YT_COLLECTION_RE.search(url)) and not youtube_id(url)


def parse_timestamp(text: str) -> float | None:
    """'90', '1:30', '01:02:03.5' -> seconds. Blank -> None. Raises ValueError if invalid."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    seconds = 0.0
    for part in parts:
        value = float(part)
        if value < 0:
            raise ValueError(text)
        seconds = seconds * 60 + value
    return seconds


def _num(value: str) -> float | None:
    if value in ("", "NA", "None", "none", None):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def fmt_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def default_download_dir() -> Path:
    """The real Downloads folder (users often move it to another drive)."""
    if IS_WINDOWS:
        try:
            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
            folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                             (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            buf = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buf)) == 0:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if path:
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Downloads"


@dataclass
class Job:
    id: int
    url: str
    opts: dict[str, Any]
    state: str = "queued"     # confirm | queued | running | done | partial | error | cancelled
    title: str = ""
    item_title: str = ""
    detail: str = "Waiting in queue…"
    progress: float = 0.0
    video_id: str | None = None
    thumb_url: str | None = None
    site: str = ""
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    proc: Any = None
    cancel: bool = False
    row: Any = None

    def reset(self) -> None:
        self.state, self.detail, self.progress = "queued", "Waiting in queue…", 0.0
        self.files, self.errors, self.warnings, self.log = [], [], [], []
        self.proc, self.cancel, self.item_title = None, False, ""


def build_command(url: str, o: dict[str, Any], ytdlp: Path, ffmpeg: Path | None, deno: Path | None,
                  temp_dir: Path, archive_file: Path) -> list[str]:
    fmt = FORMATS.get(o["format"], FORMATS[DEFAULTS["format"]])
    cmd = [
        str(ytdlp), "--ignore-config", "--color", "never", "--newline", "--progress", "--no-simulate",
        "--progress-template", DL_TEMPLATE, "--progress-template", PP_TEMPLATE,
        "--print", INFO_PRINT, "--print", FILE_PRINT,
        "--retries", "10", "--fragment-retries", "10", "--concurrent-fragments", "4",
        "--trim-filenames", "160", "--no-mtime",
        "-P", str(o["out_dir"]), "-P", f"temp:{temp_dir}",
    ]
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(ffmpeg)]
    if deno:
        cmd += ["--js-runtimes", f"deno:{deno}"]

    cmd.append("--yes-playlist" if o["playlist"] else "--no-playlist")
    name = "%(title)s [%(id)s]"
    if o.get("clip"):
        name += " (clip %(section_start)s-%(section_end)s)"
    if o["playlist"]:
        template = "%(playlist_title,playlist_id|Playlist)s/%(playlist_index|0)s - " + name + ".%(ext)s"
    else:
        template = name + ".%(ext)s"
    cmd += ["-o", template]

    if fmt["kind"] == "video":
        height = VIDEO_QUALITIES.get(o["quality"])
        res = f"res:{height}" if height else "res"
        if fmt["codec"] == "compat":
            sort = f"{res},vcodec:h264,acodec:aac"   # H.264/AAC whenever YouTube offers it
        else:
            sort = res if o["hdr"] else f"{res},+hdr"  # prefer SDR: HDR looks washed out on SDR players
        cmd += ["-f", "bv*+ba/b", "-S", sort, "--merge-output-format", fmt["container"]]
        if o["subs"]:
            langs = [l.strip() for l in re.split(r"[,\s]+", o.get("sub_langs") or "en") if l.strip()]
            spec = ",".join(f"{l}.*" for l in langs) + ",-live_chat"
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", spec, "--embed-subs"]
    else:
        codec = fmt["codec"]
        if codec == "m4a":
            cmd += ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]
        elif codec == "opus":
            cmd += ["-f", "ba[acodec=opus]/ba/b", "-x", "--audio-format", "opus"]
        elif codec == "mp3":
            cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3",
                    "--audio-quality", MP3_QUALITIES.get(o["quality"], "0")]
        else:
            cmd += ["-f", "ba/b", "-x", "--audio-format", "wav"]

    is_wav = fmt.get("codec") == "wav"
    if o["thumbnail"] and not is_wav:
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    if not is_wav:
        cmd += ["--embed-metadata"]
    if o["chapters"] and (fmt["kind"] == "video" or fmt["codec"] == "m4a"):
        cmd += ["--embed-chapters"]
    if o["sponsorblock"]:
        cmd += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
    if o["split_chapters"]:
        cmd += ["--split-chapters",
                "-o", "chapter:%(title)s [%(id)s]/%(section_number)02d - %(section_title)s.%(ext)s"]
    if o.get("clip"):
        start, end = o["clip"]
        cmd += ["--download-sections", f"*{start:g}-{'inf' if end is None else format(end, 'g')}",
                "--force-keyframes-at-cuts"]
    if o["cookies"] and o["cookies"] != "None":
        cmd += ["--cookies-from-browser", o["cookies"].lower()]
    if o["archive"]:
        cmd += ["--download-archive", str(archive_file)]
    cmd += ["--", url]
    return cmd


def apply_output_line(job: Job, line: str) -> bool:
    """Update `job` from one line of yt-dlp output. Returns True if the UI should refresh."""
    line = line.rstrip("\r\n")
    if not line:
        return False
    job.log.append(line)
    if len(job.log) > 400:
        del job.log[:100]

    if line.startswith("[MT-DL] "):
        fields = line[8:].split("|")
        if len(fields) < 10:
            return False
        status = fields[0]
        downloaded, total, estimate, speed, eta = (_num(v) for v in fields[1:6])
        vcodec, acodec = fields[6], fields[7]
        p_index, p_count = _num(fields[8]), _num(fields[9])
        total = total or estimate
        frac = 1.0 if status == "finished" else (min(downloaded / total, 1.0) if downloaded is not None and total else None)

        stream_is_audio = vcodec in ("none", "NA", "") and acodec not in ("none", "NA", "")
        stream_is_video = acodec in ("none", "NA", "") and vcodec not in ("none", "NA", "")
        if job.opts.get("kind") == "video" and stream_is_video:
            lo, hi, label = 0.0, 0.85, "Video"
        elif job.opts.get("kind") == "video" and stream_is_audio:
            lo, hi, label = 0.85, 1.0, "Audio"
        else:
            lo, hi, label = 0.0, 1.0, "Downloading"
        item = lo + (frac or 0.0) * (hi - lo)

        prefix = ""
        if p_index and p_count:
            job.progress = min(((p_index - 1) + item) / p_count, 1.0)
            prefix = f"Item {int(p_index)}/{int(p_count)} · "
            if job.item_title:
                prefix += (job.item_title[:38] + "…" if len(job.item_title) > 40 else job.item_title) + " · "
        else:
            job.progress = item
        parts = [f"{prefix}{label} {frac * 100:.0f}%" if frac is not None else f"{prefix}{label}…"]
        if total:
            parts.append(fmt_bytes(total))
        if speed and status != "finished":
            parts.append(f"{fmt_bytes(speed)}/s")
        if eta is not None and status != "finished":
            parts.append(f"ETA {fmt_eta(eta)}")
        job.detail = " · ".join(parts)
        return True

    if line.startswith("[MT-PP] "):
        status, _, name = line[8:].partition("|")
        if status == "started":
            job.detail = PP_LABELS.get(name.strip(), "Processing") + "…"
            return True
        return False

    if line.startswith("[MT-INFO] "):
        parts = line[10:].split("\t", 5)
        if len(parts) == 6:
            vid, extractor, _duration, thumb, playlist_title, title = (p if p != "NA" else "" for p in parts)
            job.site = extractor
            if extractor.lower().startswith("youtube") and vid:
                job.video_id = job.video_id or vid
            if thumb and not job.thumb_url:
                job.thumb_url = thumb
            if job.opts.get("playlist") and playlist_title:
                job.title, job.item_title = playlist_title, title
            else:
                job.title = title or job.title
            return True
        return False

    if line.startswith("[MT-FILE] "):
        path = line[10:].strip()
        if path and path not in job.files:
            job.files.append(path)
        return True

    if line.startswith("ERROR:"):
        job.errors.append(line[6:].strip())
        return True
    if line.startswith("WARNING:"):
        job.warnings.append(line[8:].strip())
    return False


def friendly_error(job: Job) -> str:
    """Turn yt-dlp's last error into something a person can act on."""
    text = " ".join(job.errors[-3:]) or (job.log[-1] if job.log else "Unknown error")
    low = text.lower()
    if "not a bot" in low or ("sign in to confirm" in low and "age" not in low):
        return ("YouTube wants to confirm you're not a bot. Wait a bit and retry, or set "
                "“Use cookies from” (More options) to a browser where you're signed in to YouTube.")
    if "confirm your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        return "Age-restricted video. Set “Use cookies from” to a browser where you're signed in, then retry."
    if "members-only" in low or "join this channel" in low:
        return "Members-only video. Use cookies from a browser signed in to an account with access."
    if "private video" in low:
        return "This video is private."
    if "video unavailable" in low or "this video has been removed" in low:
        return "Video unavailable (removed, region-locked or private)."
    if "requested format is not available" in low:
        return "That quality/format isn't offered for this video. Try “Best available”."
    if "http error 403" in low or "403: forbidden" in low:
        return "The site refused the download (403). Click “Update yt-dlp” below, then retry."
    if "unsupported url" in low:
        return "This website or link type isn't supported."
    if "cookie" in low and ("could not copy" in low or "failed to decrypt" in low):
        return ("Couldn't read that browser's cookies. Close the browser completely and retry, "
                "or use Firefox for cookies.")
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg is missing from the app's bin folder — reinstall Multi-Tool."
    if "unable to download webpage" in low or "getaddrinfo" in low or "timed out" in low:
        return "Network problem — check your internet connection and retry."
    if "no space left" in low:
        return "The destination drive is full."
    return text[:300]


def kill_process_tree(proc: subprocess.Popen, hidden: dict[str, Any]) -> None:
    """yt-dlp spawns ffmpeg; killing only the parent would leave ffmpeg running."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **hidden)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Queue row widget
# ════════════════════════════════════════════════════════════════════════════

STATE_BUTTONS: dict[str, list[tuple[str, str, str]]] = {
    "queued": [("Cancel", "ghost", "cancel")],
    "confirm": [("Download all", "accent", "confirm"), ("Remove", "ghost", "remove")],
    "running": [("Cancel", "secondary", "cancel")],
    "done": [("Open", "accent", "open"), ("Folder", "secondary", "reveal"), ("✕", "ghost", "remove")],
    "partial": [("Folder", "accent", "reveal"), ("Copy log", "secondary", "copy_log"), ("✕", "ghost", "remove")],
    "error": [("Retry", "accent", "retry"), ("Copy log", "secondary", "copy_log"), ("✕", "ghost", "remove")],
    "cancelled": [("Retry", "secondary", "retry"), ("✕", "ghost", "remove")],
}
STATE_DETAIL_ROLE = {"done": "success", "partial": "warning", "error": "danger", "cancelled": "muted",
                     "confirm": "warning"}


class JobRow:
    THUMB_W, THUMB_H = 112, 63

    def __init__(self, plugin: "Plugin", parent: Any, job: Job):
        self.plugin = plugin
        self.job = job
        t = plugin.ctx.theme
        pal = t.palette
        self._shown_state: str | None = None
        self._image = None

        self.frame = t.tag(ctk.CTkFrame(parent), "card")
        self.frame.grid_columnconfigure(1, weight=1)

        self.thumb = ctk.CTkLabel(self.frame, text="▶", width=self.THUMB_W, height=self.THUMB_H,
                                  fg_color=pal["control"], corner_radius=4)
        self.thumb.grid(row=0, column=0, rowspan=3, padx=(10, 12), pady=10)
        self.title = t.tag(ctk.CTkLabel(self.frame, text="", anchor="w", font=t.font(13, "bold")), "text")
        self.title.grid(row=0, column=1, sticky="ew", pady=(10, 0))
        self.detail = t.tag(ctk.CTkLabel(self.frame, text="", anchor="w"), "muted")
        self.detail.grid(row=1, column=1, sticky="ew")
        self.bar = ctk.CTkProgressBar(self.frame, height=6)
        self.bar.set(0)
        self.bar.grid(row=2, column=1, sticky="ew", pady=(2, 12))

        box = t.tag(ctk.CTkFrame(self.frame, fg_color="transparent"), "transparent")
        box.grid(row=0, column=2, rowspan=3, padx=10)
        self.buttons = [ctk.CTkButton(box, text="", width=84, height=28) for _ in range(3)]
        for i, b in enumerate(self.buttons):
            b.grid(row=0, column=i, padx=2)
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        title = job.title or job.url
        self.title.configure(text=title if len(title) <= 90 else title[:88] + "…")
        detail = job.detail if len(job.detail) <= 130 else job.detail[:128] + "…"
        self.detail.configure(text=detail)
        self.bar.set(max(0.0, min(job.progress, 1.0)))
        if job.state != self._shown_state:
            self._shown_state = job.state
            t = self.plugin.ctx.theme
            t.tag(self.detail, STATE_DETAIL_ROLE.get(job.state, "muted"))
            t.apply(self.detail)
            specs = STATE_BUTTONS.get(job.state, [])
            for i, btn in enumerate(self.buttons):
                if i < len(specs):
                    text, role, action = specs[i]
                    btn.configure(text=text, width=34 if text == "✕" else 84,
                                  command=lambda a=action: self.plugin.job_action(self.job, a))
                    t.tag(btn, role)
                    t.apply(btn)
                    btn.grid()
                else:
                    btn.grid_remove()

    def set_thumbnail(self, image: Any) -> None:
        self._image = image
        self.thumb.configure(image=image, text="")

    def destroy(self) -> None:
        self.frame.destroy()


# ════════════════════════════════════════════════════════════════════════════
#  Plugin
# ════════════════════════════════════════════════════════════════════════════

class Plugin:
    meta_title = "Media Downloader"
    meta_description = ("Download YouTube videos, Shorts, playlists and channels — plus TikTok, X/Twitter and "
                        "1,000+ other sites — as MP4 or MP3. Paste a link, or drag one straight out of your browser.")
    meta_icon = "⬇"
    meta_order = 10
    meta_api_version = 1

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.settings = {k: ctx.get_setting(k, v) for k, v in DEFAULTS.items()}
        if not self.settings["out_dir"]:
            self.settings["out_dir"] = str(default_download_dir() / "Multi-Tool")
        self.ytdlp = ctx.bin_path("yt-dlp")
        self.ffmpeg = ctx.bin_path("ffmpeg")
        self.deno = ctx.bin_path("deno")
        self.temp_dir = ctx.data_dir / "tmp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_file = ctx.data_dir / "download-archive.txt"
        self._cleanup_temp()

        self.jobs: list[Job] = []
        self._next_id = 1
        self._updating = False
        self._ytdlp_version: str | None = None
        self._closing = False
        self._built = False
        self._thumb_cache: dict[str, Any] = {}
        self._clip_after = None
        self._clip_last: str | None = None

        if self.ytdlp:
            due = time.time() - float(self.settings["last_update_check"] or 0) > 20 * 3600
            if self.settings["auto_update"] and due:
                self.update_ytdlp(auto=True)
            else:
                ctx.run_in_thread(self._read_version, on_done=self._set_version)
        if self.settings["clipboard_watch"]:
            self._start_clipboard_watch()

    # ── UI construction ───────────────────────────────────────────────────
    def get_frame(self, master: Any) -> Any:
        t = self.ctx.theme
        s = self.settings
        root = ctk.CTkFrame(master, fg_color="transparent")
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(3, weight=1)
        self.root = root

        # ── input card ──
        card = t.tag(ctk.CTkFrame(root), "card")
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        top = t.tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        top.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(top, height=40, font=t.font(14),
                                  placeholder_text="Paste or drop a YouTube link — video, Short, playlist or channel")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _e: self._download_from_entry())
        t.tag(ctk.CTkButton(top, text="Paste & go", width=110, height=40, command=self._paste_and_go),
              "secondary").grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(top, text="⬇  Download", width=140, height=40, font=t.font(14, "bold"),
                      command=self._download_from_entry).grid(row=0, column=2, padx=(8, 0))

        opts = t.tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        opts.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.v_format = ctk.StringVar(value=s["format"] if s["format"] in FORMATS else DEFAULTS["format"])
        self.v_quality = ctk.StringVar()
        self.v_playlist = ctk.BooleanVar(value=bool(s["playlist"]))
        self.v_subs = ctk.BooleanVar(value=bool(s["subs"]))
        self.v_sponsor = ctk.BooleanVar(value=bool(s["sponsorblock"]))
        ctk.CTkOptionMenu(opts, values=list(FORMATS), variable=self.v_format, width=270,
                          command=lambda _v: self._on_format_changed()).pack(side="left")
        self.quality_menu = ctk.CTkOptionMenu(opts, values=list(VIDEO_QUALITIES), variable=self.v_quality,
                                              width=190, command=lambda _v: self._save())
        self.quality_menu.pack(side="left", padx=(8, 16))
        for text, var in (("Whole playlists", self.v_playlist), ("Subtitles", self.v_subs),
                          ("Skip sponsors", self.v_sponsor)):
            ctk.CTkCheckBox(opts, text=text, variable=var, command=self._save,
                            checkbox_width=18, checkbox_height=18).pack(side="left", padx=(0, 14))

        dest = t.tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        dest.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))
        dest.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(dest, text="Save to"), "muted").grid(row=0, column=0, padx=(0, 8))
        self.dest_label = t.tag(ctk.CTkLabel(dest, text=s["out_dir"], anchor="w"), "text")
        self.dest_label.grid(row=0, column=1, sticky="ew")
        t.tag(ctk.CTkButton(dest, text="Change…", width=84, height=28, command=self._choose_dir),
              "ghost").grid(row=0, column=2)
        t.tag(ctk.CTkButton(dest, text="Open", width=64, height=28,
                            command=lambda: self.ctx.open_path(self.settings["out_dir"])),
              "ghost").grid(row=0, column=3)
        self.more_btn = t.tag(ctk.CTkButton(dest, text="More options ▾", width=130, height=28,
                                            command=self._toggle_more), "secondary")
        self.more_btn.grid(row=0, column=4, padx=(8, 0))

        self.more = self._build_more(card)
        self._on_format_changed(save=False)

        # ── queue ──
        head = t.tag(ctk.CTkFrame(root, fg_color="transparent"), "transparent")
        head.grid(row=2, column=0, sticky="ew", pady=(16, 6))
        head.grid_columnconfigure(1, weight=1)
        t.tag(ctk.CTkLabel(head, text="Queue"), "title", font_size=15).grid(row=0, column=0, padx=(2, 10))
        self.counts = t.tag(ctk.CTkLabel(head, text="", anchor="w"), "muted")
        self.counts.grid(row=0, column=1, sticky="ew")
        t.tag(ctk.CTkButton(head, text="Clear finished", width=120, height=28, command=self._clear_finished),
              "ghost").grid(row=0, column=2)

        self.queue = t.tag(ctk.CTkScrollableFrame(root, corner_radius=0), "transparent")
        self.queue.grid(row=3, column=0, sticky="nsew")
        self.queue.grid_columnconfigure(0, weight=1)
        self.placeholder = t.tag(ctk.CTkLabel(
            self.queue, text="Drag a link from your browser anywhere onto this page,\n"
                             "or paste one above. Multiple links at once are fine.",
            justify="center", font=t.font(13)), "muted")

        # ── engine bar ──
        bar = t.tag(ctk.CTkFrame(root, fg_color="transparent"), "transparent")
        bar.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.engine_label = t.tag(ctk.CTkLabel(bar, text="", anchor="w"), "muted")
        self.engine_label.grid(row=0, column=0, sticky="ew")
        self.update_btn = t.tag(ctk.CTkButton(bar, text="Update yt-dlp", width=130, height=28,
                                              command=lambda: self.update_ytdlp(auto=False)), "secondary")
        self.update_btn.grid(row=0, column=1)

        self._built = True
        for job in self.jobs:
            self._make_row(job)
        self._refresh_counts()
        self._refresh_engine_label()
        if not self.ctx.register_drop_target(root, self._on_drop):
            self.ctx.log.warning("Drag & drop unavailable for Media Downloader")
        return root

    def _build_more(self, card: Any) -> Any:
        t = self.ctx.theme
        s = self.settings
        more = t.tag(ctk.CTkFrame(card, fg_color="transparent"), "transparent")
        more.grid_columnconfigure((1, 3), weight=1)

        def label(text: str, row: int, col: int) -> None:
            t.tag(ctk.CTkLabel(more, text=text, anchor="w"), "muted").grid(
                row=row, column=col, sticky="w", padx=(0, 8), pady=4)

        # clip range
        label("Clip", 0, 0)
        clip = t.tag(ctk.CTkFrame(more, fg_color="transparent"), "transparent")
        clip.grid(row=0, column=1, sticky="w")
        self.clip_start = ctk.CTkEntry(clip, width=90, placeholder_text="start 1:30")
        self.clip_start.pack(side="left")
        t.tag(ctk.CTkLabel(clip, text="→"), "muted").pack(side="left", padx=6)
        self.clip_end = ctk.CTkEntry(clip, width=90, placeholder_text="end 2:45")
        self.clip_end.pack(side="left")
        t.tag(ctk.CTkLabel(clip, text="blank = whole video"), "muted").pack(side="left", padx=8)

        label("Subtitle languages", 0, 2)
        self.sub_langs = ctk.CTkEntry(more, width=140, placeholder_text="en, es")
        self.sub_langs.insert(0, s["sub_langs"])
        self.sub_langs.grid(row=0, column=3, sticky="w")
        self.sub_langs.bind("<FocusOut>", lambda _e: self._save())

        self.v_thumb = ctk.BooleanVar(value=bool(s["thumbnail"]))
        self.v_chapters = ctk.BooleanVar(value=bool(s["chapters"]))
        self.v_split = ctk.BooleanVar(value=bool(s["split_chapters"]))
        self.v_hdr = ctk.BooleanVar(value=bool(s["hdr"]))
        self.v_archive = ctk.BooleanVar(value=bool(s["archive"]))
        checks = t.tag(ctk.CTkFrame(more, fg_color="transparent"), "transparent")
        checks.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        for text, var in (("Embed thumbnail", self.v_thumb), ("Embed chapters", self.v_chapters),
                          ("Split by chapters", self.v_split), ("Allow HDR", self.v_hdr),
                          ("Skip videos already downloaded", self.v_archive)):
            ctk.CTkCheckBox(checks, text=text, variable=var, command=self._save,
                            checkbox_width=18, checkbox_height=18).pack(side="left", padx=(0, 16))

        label("Use cookies from", 2, 0)
        self.v_cookies = ctk.StringVar(value=s["cookies"] if s["cookies"] in COOKIE_BROWSERS else "None")
        ctk.CTkOptionMenu(more, values=COOKIE_BROWSERS, variable=self.v_cookies, width=140,
                          command=lambda _v: self._save()).grid(row=2, column=1, sticky="w")
        label("Parallel downloads", 2, 2)
        self.v_parallel = ctk.StringVar(value=str(s["parallel"]))
        ctk.CTkSegmentedButton(more, values=["1", "2", "3", "4"], variable=self.v_parallel,
                               command=lambda _v: (self._save(), self._schedule())).grid(row=2, column=3, sticky="w")
        t.tag(ctk.CTkLabel(more, anchor="w", justify="left", wraplength=760,
                           text="Cookies are only needed for age-restricted, members-only or “confirm you're not a bot” "
                                "videos. Firefox is the most reliable; Chrome/Edge cookies often can't be read on "
                                "Windows while the browser is open."), "muted").grid(
            row=3, column=0, columnspan=4, sticky="ew", pady=(2, 6))

        self.v_clip = ctk.BooleanVar(value=bool(s["clipboard_watch"]))
        self.v_auto = ctk.BooleanVar(value=bool(s["auto_update"]))
        self.v_nightly = ctk.BooleanVar(value=bool(s["nightly"]))
        switches = t.tag(ctk.CTkFrame(more, fg_color="transparent"), "transparent")
        switches.grid(row=4, column=0, columnspan=4, sticky="w", pady=(0, 4))
        ctk.CTkSwitch(switches, text="Grab video links I copy (YouTube, TikTok, X…)", variable=self.v_clip,
                      command=self._on_clipboard_toggle).pack(side="left", padx=(0, 20))
        ctk.CTkSwitch(switches, text="Keep yt-dlp updated (daily)", variable=self.v_auto,
                      command=self._save).pack(side="left", padx=(0, 20))
        ctk.CTkSwitch(switches, text="Nightly builds (faster YouTube fixes)", variable=self.v_nightly,
                      command=self._save).pack(side="left")
        return more

    # ── option handling ───────────────────────────────────────────────────
    def _qualities_for(self, fmt_name: str) -> list[str]:
        fmt = FORMATS[fmt_name]
        if fmt["kind"] == "video":
            return list(VIDEO_QUALITIES)
        if fmt["codec"] == "mp3":
            return list(MP3_QUALITIES)
        return [ORIGINAL_QUALITY]

    def _on_format_changed(self, save: bool = True) -> None:
        values = self._qualities_for(self.v_format.get())
        self.quality_menu.configure(values=values)
        kind = FORMATS[self.v_format.get()]
        saved = self.settings["video_quality"] if kind["kind"] == "video" else self.settings["mp3_quality"]
        self.v_quality.set(saved if saved in values else values[0])
        self.quality_menu.configure(state="normal" if len(values) > 1 else "disabled")
        if save:
            self._save()

    def _save(self) -> None:
        if not self._built:
            return
        s = self.settings
        s["format"] = self.v_format.get()
        quality = self.v_quality.get()
        if quality in VIDEO_QUALITIES:
            s["video_quality"] = quality
        elif quality in MP3_QUALITIES:
            s["mp3_quality"] = quality
        s.update(playlist=self.v_playlist.get(), subs=self.v_subs.get(), sponsorblock=self.v_sponsor.get(),
                 sub_langs=self.sub_langs.get().strip() or "en", thumbnail=self.v_thumb.get(),
                 chapters=self.v_chapters.get(), split_chapters=self.v_split.get(), hdr=self.v_hdr.get(),
                 archive=self.v_archive.get(), cookies=self.v_cookies.get(), parallel=self.v_parallel.get(),
                 clipboard_watch=self.v_clip.get(), auto_update=self.v_auto.get(), nightly=self.v_nightly.get())
        for key, value in s.items():
            if self.ctx.get_setting(key, None) != value:
                self.ctx.set_setting(key, value)

    def _toggle_more(self) -> None:
        if self.more.winfo_ismapped():
            self.more.grid_remove()
            self.more_btn.configure(text="More options ▾")
        else:
            self.more.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))
            self.more_btn.configure(text="More options ▴")

    def _choose_dir(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.ctx.app, initialdir=self.settings["out_dir"],
                                         title="Where should downloads be saved?")
        if chosen:
            self.settings["out_dir"] = str(Path(chosen))
            self.ctx.set_setting("out_dir", self.settings["out_dir"])
            self.dest_label.configure(text=self.settings["out_dir"])

    def _snapshot_options(self) -> dict[str, Any] | None:
        """Options are frozen per job, so changing settings never affects queued items."""
        self._save()
        s = dict(self.settings)
        clip = None
        if self._built:
            try:
                start = parse_timestamp(self.clip_start.get())
                end = parse_timestamp(self.clip_end.get())
            except ValueError:
                self.ctx.set_status("Clip times must look like 90, 1:30 or 1:02:03.", "error")
                return None
            if start is not None or end is not None:
                start = start or 0.0
                if end is not None and end <= start:
                    self.ctx.set_status("Clip end must be after the start.", "error")
                    return None
                clip = (start, end)
        fmt = FORMATS[s["format"]]
        s["kind"] = fmt["kind"]
        s["quality"] = s["video_quality"] if fmt["kind"] == "video" else (
            s["mp3_quality"] if fmt["codec"] == "mp3" else ORIGINAL_QUALITY)
        s["clip"] = clip
        return s

    # ── adding work ───────────────────────────────────────────────────────
    def _download_from_entry(self) -> None:
        text = self.entry.get()
        urls = extract_urls(text)
        if not urls and text.strip():
            candidate = text.strip()
            if re.match(r"^[\w.-]+\.[a-z]{2,}/", candidate, re.I):
                urls = ["https://" + candidate]
        if not urls:
            self.ctx.set_status("Paste a link first (it should start with https://).", "warning")
            return
        if self.add_urls(urls):
            self.entry.delete(0, "end")

    def _paste_and_go(self) -> None:
        try:
            text = self.ctx.app.clipboard_get()
        except Exception:
            text = ""
        urls = extract_urls(text)
        if not urls:
            self.ctx.set_status("The clipboard doesn't contain a link.", "warning")
            return
        self.add_urls(urls)

    def _on_drop(self, payload: Any) -> None:
        urls = list(payload.urls)
        if not urls and payload.text:
            urls = extract_urls(payload.text)
        if urls:
            self.add_urls(urls)
        elif payload.files:
            self.ctx.set_status("Drop links from your browser here — local files go to the Audio Extractor.",
                                "warning")
        else:
            self.ctx.set_status("No link found in what was dropped.", "warning")

    def add_urls(self, urls: list[str]) -> bool:
        opts = self._snapshot_options()
        if opts is None:
            return False
        for url in urls:
            job = Job(id=self._next_id, url=url, opts=dict(opts))
            self._next_id += 1
            job.video_id = youtube_id(url)
            if job.video_id:
                job.thumb_url = f"https://i.ytimg.com/vi/{job.video_id}/mqdefault.jpg"
            if is_collection_url(url) and not opts["playlist"]:
                job.state = "confirm"
                job.detail = "This is a playlist/channel link and may contain many videos."
            self.jobs.append(job)
            self._make_row(job)
            if job.thumb_url:
                self._load_thumbnail(job)
        self._refresh_counts()
        self._schedule()
        self.ctx.set_status(f"Added {len(urls)} link{'s' if len(urls) != 1 else ''} to the queue", "success")
        return True

    def _make_row(self, job: Job) -> None:
        if not self._built or job.row is not None:
            return
        self.placeholder.grid_remove()
        job.row = JobRow(self, self.queue, job)
        job.row.frame.grid(row=job.id, column=0, sticky="ew", pady=(0, 8), padx=(0, 6))
        self.ctx.theme.apply(job.row.frame)
        self.ctx.register_drop_target(job.row.frame, self._on_drop)
        if job.id in self._thumb_cache:
            job.row.set_thumbnail(self._thumb_cache[job.id])

    # ── scheduling & running ──────────────────────────────────────────────
    def _schedule(self) -> None:
        if self._updating or self._closing:
            return
        limit = max(1, int(self.settings.get("parallel") or 2))
        running = sum(1 for j in self.jobs if j.state == "running")
        for job in self.jobs:
            if running >= limit:
                break
            if job.state == "queued":
                self._start(job)
                running += 1
        self._refresh_counts()

    def _start(self, job: Job) -> None:
        if not self.ytdlp:
            job.state, job.detail = "error", "yt-dlp is missing from the app's bin folder — reinstall Multi-Tool."
            self._refresh(job)
            return
        try:
            Path(job.opts["out_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            job.state, job.detail = "error", f"Can't use the download folder: {exc}"
            self._refresh(job)
            return
        job.state, job.detail = "running", "Starting…"
        self._refresh(job)
        threading.Thread(target=self._run, args=(job,), name=f"ytdlp-{job.id}", daemon=True).start()

    def _run(self, job: Job) -> None:
        cmd = build_command(job.url, job.opts, self.ytdlp, self.ffmpeg, self.deno, self.temp_dir, self.archive_file)
        self.ctx.log.info("Job %d: %s", job.id, subprocess.list2cmdline(cmd))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1, env=env, cwd=str(self.temp_dir),
                                        **self.ctx.subprocess_kwargs())
        except OSError as exc:
            job.errors.append(f"Could not start yt-dlp: {exc}")
            self.ctx.call_in_ui(self._finished, job, -1)
            return
        if job.cancel:  # Cancel was pressed while the process was starting
            kill_process_tree(job.proc, self.ctx.subprocess_kwargs())
        last_push = 0.0
        for line in job.proc.stdout:
            urgent = line.startswith(("[MT-INFO]", "[MT-FILE]", "ERROR:", "[MT-PP]"))
            if apply_output_line(job, line):
                now = time.monotonic()
                if urgent or now - last_push > 0.15:
                    last_push = now
                    self.ctx.call_in_ui(self._refresh, job)
                    if line.startswith("[MT-INFO]") and job.thumb_url and job.id not in self._thumb_cache:
                        self.ctx.call_in_ui(self._load_thumbnail, job)
        rc = job.proc.wait()
        self.ctx.call_in_ui(self._finished, job, rc)

    def _finished(self, job: Job, rc: int) -> None:
        job.proc = None
        if job.cancel:
            job.state, job.detail = "cancelled", "Cancelled"
        elif rc == 0 and not job.errors:
            job.state, job.progress = "done", 1.0
            if job.files:
                name = Path(job.files[-1]).name
                extra = f" (+{len(job.files) - 1} more)" if len(job.files) > 1 else ""
                job.detail = f"Saved · {name}{extra}"
            else:
                job.detail = "Already downloaded — nothing new to fetch."
            self.ctx.set_status(f"Downloaded: {job.title or job.url}", "success")
            self._notify_done(job)
        elif job.files:
            job.state, job.progress = "partial", 1.0
            job.detail = f"{len(job.files)} saved, some items failed: {friendly_error(job)}"
        else:
            job.state = "error"
            job.detail = friendly_error(job)
            if any("javascript runtime" in w.lower() for w in job.warnings) and not self.deno:
                job.detail += "  (deno.exe is missing from bin — YouTube needs it.)"
            self.ctx.log.warning("Job %d failed:\n%s", job.id, "\n".join(job.log[-25:]))
        self._refresh(job)
        self._schedule()

    def _notify_done(self, job: Job) -> None:
        """Heads-up pop-up when a download finishes while you're doing something else."""
        notify = getattr(self.ctx, "notify", None)
        if notify is None or not job.files:
            return
        try:
            if self.ctx.app.focus_displayof() is not None:
                return  # Multi-Tool is in front — the queue already shows it
        except Exception:
            pass
        folder = Path(job.files[-1]).parent
        count = len(job.files)
        notify("Download finished", job.title or Path(job.files[-1]).name,
               subtitle=f"{count} file{'s' if count != 1 else ''} · click to open the folder",
               image=getattr(self._thumb_cache.get(job.id), "_light_image", None),
               on_click=lambda: self.ctx.open_path(folder), icon="↓")

    def _refresh(self, job: Job) -> None:
        if job.row is not None:
            job.row.refresh()
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        if not self._built:
            return
        running = sum(j.state == "running" for j in self.jobs)
        queued = sum(j.state in ("queued", "confirm") for j in self.jobs)
        done = sum(j.state in ("done", "partial") for j in self.jobs)
        failed = sum(j.state == "error" for j in self.jobs)
        parts = []
        if running:
            parts.append(f"{running} downloading")
        if queued:
            parts.append(f"{queued} waiting")
        if done:
            parts.append(f"{done} done")
        if failed:
            parts.append(f"{failed} failed")
        if self._updating:
            parts.append("paused while yt-dlp updates")
        self.counts.configure(text="  ·  ".join(parts))
        if not self.jobs:
            self.placeholder.grid(row=0, column=0, pady=60)

    # ── row actions ───────────────────────────────────────────────────────
    def job_action(self, job: Job, action: str) -> None:
        if action == "cancel":
            job.cancel = True
            if job.state == "running":
                job.detail = "Cancelling…"
                threading.Thread(target=kill_process_tree, args=(job.proc, self.ctx.subprocess_kwargs()),
                                 daemon=True).start()
            else:
                job.state, job.detail = "cancelled", "Cancelled"
            self._refresh(job)
            self._schedule()
        elif action == "confirm":
            job.opts["playlist"] = True
            job.state, job.detail = "queued", "Waiting in queue…"
            self._refresh(job)
            self._schedule()
        elif action == "retry":
            job.reset()
            self._refresh(job)
            self._schedule()
        elif action == "remove":
            if job.state == "running":
                return
            if job.row is not None:
                job.row.destroy()
                job.row = None
            self.jobs.remove(job)
            self._refresh_counts()
        elif action == "open" and job.files:
            self.ctx.open_path(job.files[-1])
        elif action == "reveal":
            self._reveal(job.files[-1] if job.files else job.opts["out_dir"])
        elif action == "copy_log":
            self.ctx.copy_to_clipboard(f"URL: {job.url}\n" + "\n".join(job.log[-80:]))
            self.ctx.set_status("Log copied to clipboard", "success")

    def _clear_finished(self) -> None:
        for job in [j for j in self.jobs if j.state in ("done", "partial", "cancelled", "error")]:
            self.job_action(job, "remove")

    def _reveal(self, path: str) -> None:
        p = Path(path)
        if IS_WINDOWS and p.is_file():
            subprocess.Popen(f'explorer /select,"{p}"')
        else:
            self.ctx.open_path(p if p.is_dir() else p.parent)

    # ── thumbnails ────────────────────────────────────────────────────────
    def _load_thumbnail(self, job: Job) -> None:
        if Image is None or not job.thumb_url or job.id in self._thumb_cache:
            return
        self._thumb_cache[job.id] = None  # in flight
        url = job.thumb_url

        def fetch() -> Any:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(3_000_000)
            img = Image.open(io.BytesIO(data)).convert("RGB")
            w, h = img.size  # crop to 16:9 so Shorts/TikTok portraits don't distort
            target = 16 / 9
            if w / h > target:
                nw = int(h * target)
                img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
            else:
                nh = int(w / target)
                img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
            return img.resize((JobRow.THUMB_W * 2, JobRow.THUMB_H * 2))

        def done(img: Any) -> None:
            image = ctk.CTkImage(light_image=img, dark_image=img, size=(JobRow.THUMB_W, JobRow.THUMB_H))
            self._thumb_cache[job.id] = image
            if job.row is not None:
                job.row.set_thumbnail(image)

        def failed(_exc: BaseException) -> None:
            self._thumb_cache.pop(job.id, None)

        self.ctx.run_in_thread(fetch, on_done=done, on_error=failed)

    # ── yt-dlp version / self-update ──────────────────────────────────────
    def _read_version(self) -> str:
        out = subprocess.run([str(self.ytdlp), "--version"], capture_output=True, text=True, timeout=60,
                             **self.ctx.subprocess_kwargs())
        return out.stdout.strip() or "?"

    def _set_version(self, version: str) -> None:
        self._ytdlp_version = version
        self._refresh_engine_label()

    def update_ytdlp(self, auto: bool = False) -> None:
        if not self.ytdlp or self._updating:
            return
        if any(j.state == "running" for j in self.jobs):
            if not auto:
                self.ctx.set_status("Wait for running downloads to finish before updating yt-dlp.", "warning")
            return
        self._updating = True
        self._refresh_engine_label()
        self._refresh_counts()
        channel = "nightly" if self.settings.get("nightly") else "stable"

        def work() -> tuple[int, str]:
            proc = subprocess.run([str(self.ytdlp), "--update-to", channel], capture_output=True, text=True,
                                  timeout=180, **self.ctx.subprocess_kwargs())
            text = (proc.stdout + "\n" + proc.stderr).strip()
            return proc.returncode, text

        def done(result: tuple[int, str]) -> None:
            rc, text = result
            self._updating = False
            self.settings["last_update_check"] = time.time()
            self.ctx.set_setting("last_update_check", self.settings["last_update_check"])
            lines = [l for l in text.splitlines() if l.strip()]
            summary = lines[-1] if lines else "No output"
            self.ctx.log.info("yt-dlp update (%s): rc=%s %s", channel, rc, text)
            if rc == 0:
                if not auto or "updated" in summary.lower():
                    self.ctx.set_status(f"yt-dlp: {summary}", "success")
            else:
                self.ctx.set_status(f"yt-dlp update failed: {summary}", "error")
            self.ctx.run_in_thread(self._read_version, on_done=self._set_version)
            self._schedule()

        def failed(exc: BaseException) -> None:
            self._updating = False
            self.ctx.set_status(f"yt-dlp update failed: {exc}", "error")
            self._refresh_engine_label()
            self._schedule()

        self.ctx.run_in_thread(work, on_done=done, on_error=failed)

    def _refresh_engine_label(self) -> None:
        if not self._built:
            return
        if not self.ytdlp:
            text, role = "yt-dlp.exe is missing from the bin folder — reinstall Multi-Tool.", "danger"
        else:
            version = "updating…" if self._updating else (self._ytdlp_version or "…")
            parts = [f"yt-dlp {version}",
                     "ffmpeg ✓" if self.ffmpeg else "ffmpeg ✗ (merging/MP3 won't work)",
                     "deno ✓" if self.deno else "deno ✗ (YouTube may fail)"]
            text = "  ·  ".join(parts)
            role = "muted" if self.ffmpeg and self.deno else "warning"
        self.engine_label.configure(text=text)
        self.ctx.theme.tag(self.engine_label, role)
        self.ctx.theme.apply(self.engine_label)
        self.update_btn.configure(state="disabled" if self._updating or not self.ytdlp else "normal")

    # ── clipboard watcher ─────────────────────────────────────────────────
    def _on_clipboard_toggle(self) -> None:
        self._save()
        if self.v_clip.get():
            self._start_clipboard_watch()
            self.ctx.set_status("Watching the clipboard for video links", "success")
        else:
            self._stop_clipboard_watch()

    def _start_clipboard_watch(self) -> None:
        self._stop_clipboard_watch()
        try:
            self._clip_last = self.ctx.app.clipboard_get()  # ignore whatever was copied before
        except Exception:
            self._clip_last = ""
        self._clip_after = self.ctx.app.after(1000, self._clip_tick)

    def _stop_clipboard_watch(self) -> None:
        if self._clip_after is not None:
            try:
                self.ctx.app.after_cancel(self._clip_after)
            except Exception:
                pass
            self._clip_after = None

    def _clip_tick(self) -> None:
        self._clip_after = None
        if self._closing or not self.settings.get("clipboard_watch"):
            return
        try:
            text = self.ctx.app.clipboard_get()
        except Exception:
            text = ""
        if text and text != self._clip_last:
            self._clip_last = text
            urls = [u for u in extract_urls(text) if MEDIA_HOST_RE.match(u)]
            known = {j.url for j in self.jobs}
            urls = [u for u in urls if u not in known]
            if urls:
                self.add_urls(urls)
        self._clip_after = self.ctx.app.after(1000, self._clip_tick)

    # ── housekeeping ──────────────────────────────────────────────────────
    def _cleanup_temp(self) -> None:
        cutoff = time.time() - 2 * 86400
        for item in self.temp_dir.glob("*"):
            try:
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                pass

    def on_theme_changed(self, palette: dict[str, Any]) -> None:
        for job in self.jobs:
            if job.row is not None and job.row._image is None:
                job.row.thumb.configure(fg_color=palette["control"])

    def shutdown(self) -> None:
        self._closing = True
        self._stop_clipboard_watch()
        hidden = self.ctx.subprocess_kwargs()
        for job in self.jobs:
            if job.proc is not None:
                job.cancel = True
                kill_process_tree(job.proc, hidden)
