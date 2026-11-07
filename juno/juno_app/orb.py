"""
Static-style avatars: simple forms that still feel alive.

  orb    — a rotating wireframe globe inside tilted orbit rings with travelling sparks
  core   — an arc-reactor: counter-rotating segmented rings around a pulsing hex core
  pulse  — a circular equaliser ring that breathes when idle and dances when Juno talks

They are drawn with Pillow (anti-glare glow + CRT scanlines in the main window; crisp strokes on a
transparent key colour for the desktop overlay) and react to Juno's state: idle, listening,
thinking and speaking (mouth level comes from the speech envelope).
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk
from typing import Any

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
except Exception:  # pragma: no cover - Pillow is bundled
    Image = None

ORB_STYLES = {
    "orb": ("Orb", "Wireframe globe in orbit rings"),
    "core": ("Core", "Arc-reactor rings around a pulsing core"),
    "pulse": ("Pulse", "Circular equaliser that moves with her voice"),
}
OVERLAY_KEY = "#fe00fe"


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


class OrbRenderer:
    """Draws one frame of a static-style avatar."""

    def __init__(self, style: str, size: int, palette: dict[str, Any]):
        self.style = style if style in ORB_STYLES else "orb"
        self.size = int(size)
        self.set_palette(palette)
        self._scan: Any = None
        self._bars = [random.random() for _ in range(72)]
        self._spark_seed = [random.random() * math.tau for _ in range(6)]

    def set_palette(self, palette: dict[str, Any]) -> None:
        self.bright = _hex(palette.get("accent", "#39ff7a"))
        self.mid = _hex(palette.get("glow", "#1c7a3e"))
        self.dim = _hex(palette.get("dim", "#0a2a15"))
        self.alt = _hex(palette.get("accent2", "#c8ff4d"))
        self.warn = _hex(palette.get("warning", "#ffcc33"))
        self.bg = _hex(palette.get("card", "#03110a"))
        self.core_fill = _hex(palette.get("bg", "#010805"))

    # ── public ───────────────────────────────────────────────────────────
    def render(self, t: float, state: str, level: float, glow: bool = True,
               background: str | None = None) -> Any:
        """state: idle | listening | hearing | thinking | speaking. level: 0..1 speech loudness."""
        size = self.size
        overlay = background is not None
        bg = _hex(background) if overlay else (0, 0, 0)
        layer = Image.new("RGB", (size, size), bg)
        draw = ImageDraw.Draw(layer)
        accent = self.alt if state in ("listening", "hearing") else self.bright
        if state == "thinking":
            accent = _mix(self.bright, self.alt, 0.5 + 0.5 * math.sin(t * 5))
        speed = {"thinking": 2.6, "speaking": 1.5, "hearing": 1.4}.get(state, 1.0)
        if overlay:  # a dark puck so the form reads on any wallpaper
            r = size * 0.47
            c = size / 2
            draw.ellipse((c - r, c - r, c + r, c + r), fill=self.core_fill, outline=self.mid, width=2)
        getattr(self, f"_draw_{self.style}")(draw, t, speed, accent, level, state)
        if overlay or not glow:
            return layer
        halo = layer.filter(ImageFilter.GaussianBlur(radius=max(3, size // 70)))
        base = Image.new("RGB", (size, size), self.bg)
        out = ImageChops.add(ImageChops.add(base, halo), layer)
        return ImageChops.multiply(out, self._scanlines())

    # ── shared bits ──────────────────────────────────────────────────────
    def _scanlines(self) -> Any:
        if self._scan is None or self._scan.size[0] != self.size:
            scan = Image.new("RGB", (self.size, self.size), (255, 255, 255))
            d = ImageDraw.Draw(scan)
            for y in range(0, self.size, 3):
                d.line((0, y, self.size, y), fill=(190, 205, 195))
            self._scan = scan
        return self._scan

    def _ticks(self, draw: Any, radius: float, count: int, length: float, color: tuple, rotate: float = 0) -> None:
        c = self.size / 2
        for i in range(count):
            a = rotate + i * math.tau / count
            long = length * (2.2 if i % (count // 8 or 1) == 0 else 1)
            x0, y0 = c + math.cos(a) * radius, c + math.sin(a) * radius
            x1, y1 = c + math.cos(a) * (radius + long), c + math.sin(a) * (radius + long)
            draw.line((x0, y0, x1, y1), fill=color, width=1)

    def _arc_ring(self, draw: Any, radius: float, segments: int, gap: float, rotate: float, color: tuple,
                  width: int) -> None:
        c = self.size / 2
        box = (c - radius, c - radius, c + radius, c + radius)
        span = 360 / segments
        for i in range(segments):
            start = math.degrees(rotate) + i * span
            draw.arc(box, start, start + span * (1 - gap), fill=color, width=width)

    def _spikes(self, draw: Any, radius: float, level: float, t: float, color: tuple, count: int = 64) -> None:
        if level <= 0.02:
            return
        c = self.size / 2
        for i in range(count):
            a = i * math.tau / count
            wobble = 0.55 + 0.45 * math.sin(i * 1.7 + t * 9) * math.sin(i * 0.6 - t * 5)
            length = self.size * 0.07 * level * max(0.15, wobble)
            x0, y0 = c + math.cos(a) * radius, c + math.sin(a) * radius
            x1, y1 = c + math.cos(a) * (radius + length), c + math.sin(a) * (radius + length)
            draw.line((x0, y0, x1, y1), fill=color, width=2)

    # ── orb ──────────────────────────────────────────────────────────────
    def _draw_orb(self, draw: Any, t: float, speed: float, accent: tuple, level: float, state: str) -> None:
        size = self.size
        c = size / 2
        r = size * 0.27 * (1 + 0.05 * level)
        spin = t * 0.35 * speed
        tilt = 0.38
        ct, st = math.cos(tilt), math.sin(tilt)

        def project(lat: float, lon: float) -> tuple[float, float, float]:
            x = math.cos(lat) * math.cos(lon + spin)
            y = math.sin(lat)
            z = math.cos(lat) * math.sin(lon + spin)
            y, z = y * ct - z * st, y * st + z * ct
            return c + r * x, c - r * y, z

        def seg(p: tuple, q: tuple) -> None:
            depth = (p[2] + q[2]) / 2
            color = _mix(self.dim, accent, 0.25 + 0.75 * (depth * 0.5 + 0.5)) if depth > -0.1 else self.dim
            draw.line((p[0], p[1], q[0], q[1]), fill=color, width=1 if depth < 0.3 else 2)

        for m in range(12):  # meridians
            lon = m * math.pi / 6
            pts = [project(-math.pi / 2 + k * math.pi / 20, lon) for k in range(21)]
            for a, b in zip(pts, pts[1:]):
                seg(a, b)
        for p in range(1, 8):  # parallels
            lat = -math.pi / 2 + p * math.pi / 8
            pts = [project(lat, k * math.tau / 36) for k in range(37)]
            for a, b in zip(pts, pts[1:]):
                seg(a, b)

        # orbit rings with travelling sparks
        for idx, (radius, tilt_r, rate) in enumerate(((1.55, 0.35, 0.6), (1.85, -0.22, -0.4), (1.25, 1.2, 0.9))):
            cr, sr = math.cos(tilt_r), math.sin(tilt_r)
            pts = []
            for k in range(73):
                a = k * math.tau / 72
                x, z = math.cos(a) * radius, math.sin(a) * radius
                y = -z * sr
                z = z * cr
                pts.append((c + r * x, c - r * y * 0.35 - r * z * 0.12, z))
            for a, b in zip(pts, pts[1:]):
                behind = a[2] < 0 and abs(a[0] - c) < r
                draw.line((a[0], a[1], b[0], b[1]), fill=self.dim if behind else _mix(self.mid, accent, 0.35),
                          width=1)
            for s in range(2):
                a = t * rate * speed + self._spark_seed[idx * 2 + s]
                x, z = math.cos(a) * radius, math.sin(a) * radius
                y = -z * sr
                z2 = z * cr
                px, py = c + r * x, c - r * y * 0.35 - r * z2 * 0.12
                rad = 3.2 if z2 > 0 else 2
                draw.ellipse((px - rad, py - rad, px + rad, py + rad), fill=accent if z2 > 0 else self.mid)

        # outer HUD
        self._arc_ring(draw, size * 0.44, 24, 0.35, -t * 0.15 * speed, self.mid, 2)
        self._ticks(draw, size * 0.465, 96, 3, self.dim)
        if state == "thinking":
            self._arc_ring(draw, size * 0.40, 3, 0.75, t * 3.2, accent, 3)
        if state in ("listening", "hearing"):
            pulse = 0.5 + 0.5 * math.sin(t * (7 if state == "hearing" else 3))
            rr = size * (0.31 + 0.03 * pulse)
            draw.ellipse((c - rr, c - rr, c + rr, c + rr), outline=_mix(self.mid, accent, pulse), width=2)
        self._spikes(draw, r * 1.08, level, t, accent)

    # ── core ─────────────────────────────────────────────────────────────
    def _draw_core(self, draw: Any, t: float, speed: float, accent: tuple, level: float, state: str) -> None:
        size = self.size
        c = size / 2
        rings = ((0.42, 36, 0.3, 0.12, 2), (0.36, 12, 0.18, -0.25, 3), (0.29, 6, 0.4, 0.4, 4),
                 (0.22, 18, 0.5, -0.6, 2))
        for i, (radius, segs, gap, rate, width) in enumerate(rings):
            color = _mix(self.mid, accent, 0.4 + 0.15 * i + 0.4 * level)
            self._arc_ring(draw, size * radius, segs, gap, t * rate * speed, color, width)
        self._ticks(draw, size * 0.455, 120, 2, self.dim, rotate=t * 0.02)
        # spokes
        for i in range(6):
            a = i * math.tau / 6 + t * 0.2 * speed
            x0, y0 = c + math.cos(a) * size * 0.12, c + math.sin(a) * size * 0.12
            x1, y1 = c + math.cos(a) * size * 0.2, c + math.sin(a) * size * 0.2
            draw.line((x0, y0, x1, y1), fill=self.mid, width=2)
        # hex core
        beat = 0.5 + 0.5 * math.sin(t * (6 if state == "thinking" else 2))
        hr = size * (0.1 + 0.015 * beat + 0.04 * level)
        hexagon = [(c + math.cos(k * math.pi / 3 + math.pi / 6) * hr, c + math.sin(k * math.pi / 3 + math.pi / 6) * hr)
                   for k in range(6)]
        draw.polygon(hexagon, outline=accent, fill=_mix(self.core_fill, accent, 0.18 + 0.3 * level))
        inner = hr * 0.45
        draw.ellipse((c - inner, c - inner, c + inner, c + inner), fill=_mix(self.mid, accent, 0.5 + 0.5 * beat))
        if state in ("listening", "hearing"):
            self._arc_ring(draw, size * 0.475, 4, 0.6, -t * 2, accent, 2)
        self._spikes(draw, size * 0.43, level, t, accent, count=48)

    # ── pulse ────────────────────────────────────────────────────────────
    def _draw_pulse(self, draw: Any, t: float, speed: float, accent: tuple, level: float, state: str) -> None:
        size = self.size
        c = size / 2
        base_r = size * 0.24
        count = len(self._bars)
        breathe = 0.5 + 0.5 * math.sin(t * 1.4)
        for i in range(count):
            a = i * math.tau / count - math.pi / 2
            target = 0.12 + 0.12 * breathe * (0.5 + 0.5 * math.sin(i * 0.5 + t * 2))
            if level > 0.02:
                target += level * (0.4 + 0.6 * abs(math.sin(i * 1.3 + t * 11)))
            if state == "thinking":
                target += 0.35 * max(0.0, math.cos(a - t * 4)) ** 6
            if state in ("listening", "hearing"):
                target += 0.12 * (1 if state == "hearing" else 0.5)
            self._bars[i] += (target - self._bars[i]) * 0.35
            length = size * 0.2 * self._bars[i]
            x0, y0 = c + math.cos(a) * base_r, c + math.sin(a) * base_r
            x1, y1 = c + math.cos(a) * (base_r + length), c + math.sin(a) * (base_r + length)
            draw.line((x0, y0, x1, y1), fill=_mix(self.mid, accent, 0.35 + self._bars[i]), width=3)
        rr = base_r * 0.86
        draw.ellipse((c - rr, c - rr, c + rr, c + rr), outline=self.mid, width=1)
        dot = size * (0.05 + 0.02 * breathe + 0.05 * level)
        draw.ellipse((c - dot, c - dot, c + dot, c + dot), fill=accent)
        self._arc_ring(draw, size * 0.465, 48, 0.5, t * 0.05, self.dim, 1)


class _ReactiveMixin:
    """Avatar-like reactions shared by the stage and the desktop overlay (same API as the character avatar)."""

    def _init_reactive(self) -> None:
        self.mode = "idle"
        self.speech: tuple[list[float], float, float] | None = None
        self.bubble: tuple[str, float] | None = None
        self.stroll = None
        self.panel_side = 0
        self._level = 0.0

    def start_speaking(self, envelope: list[float], duration: float) -> None:
        self.speech = (envelope, time.monotonic(), duration)
        self.mode = "speaking"

    def stop_speaking(self) -> None:
        self.speech = None
        if self.mode == "speaking":
            self.mode = "idle"

    def set_expression(self, mood: str, seconds: float = 6.0) -> None:
        pass

    def gesture(self, pose: str, seconds: float, arm: str = "r") -> None:
        pass

    def hop(self) -> None:
        pass

    def say(self, text: str, seconds: float = 6.0) -> None:
        text = " ".join(text.split())
        self.bubble = (text if len(text) <= 140 else text[:138] + "…", time.monotonic() + seconds)

    def set_character(self, *_a: Any, **_k: Any) -> None:
        pass

    def _speech_level(self) -> float:
        target = 0.0
        if self.speech is not None:
            env, start, duration = self.speech
            elapsed = time.monotonic() - start
            if elapsed > duration + 0.2:
                self.speech = None
            elif env:
                target = env[min(len(env) - 1, max(0, int(elapsed / 0.03)))]
        self._level += (target - self._level) * 0.5
        return self._level

    def _state(self, listening: str) -> str:
        if self.mode in ("thinking", "speaking"):
            return self.mode
        return listening if listening in ("listening", "hearing") else "idle"


class StageOrb(_ReactiveMixin):
    """The big animated avatar in the main window (a canvas the shell places)."""

    def __init__(self, core: Any, parent: Any, style: str, size: int = 340):
        self._init_reactive()
        self.core = core
        self.app = core.ctx.app
        palette = core.ctx.theme.palette
        self.renderer = OrbRenderer(style, size, palette)
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=palette["card"], highlightthickness=0, bd=0)
        self.canvas._mt_role = "skip"
        self.win = self.canvas
        self.visible = False
        self._photo: Any = None
        self._image_item: Any = None
        self._after: Any = None
        self._t0 = time.monotonic()

    def show(self) -> None:
        self.visible = True
        if self._after is None:
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
        try:
            self.canvas.destroy()
        except Exception:
            pass

    def tick(self) -> None:
        self._after = None
        if not self.visible:
            return
        try:
            if self.canvas.winfo_ismapped() and self.app.state() != "withdrawn":
                self.draw()
        except Exception:
            self.core.ctx.log.exception("Stage orb frame failed")
        reduce = bool(self.core.settings.get("reduced_motion"))
        self._after = self.app.after(66 if reduce else 33, self.tick)

    def draw(self) -> None:
        if Image is None:
            return
        t = time.monotonic() - self._t0
        if self.core.settings.get("reduced_motion"):
            t *= 0.35
        image = self.renderer.render(t, self._state(self.core.listener.state), self._speech_level(), glow=True)
        if self._photo is None or self._photo.width() != image.width:
            self._photo = ImageTk.PhotoImage(image, master=self.canvas)
            self.canvas.delete("orb")
            self._image_item = self.canvas.create_image(0, 0, image=self._photo, anchor="nw", tags="orb")
            self.canvas.tag_lower("orb")
        else:
            self._photo.paste(image)


class OrbOverlay(_ReactiveMixin):
    """The orb floating on the desktop in mini mode (transparent, always on top, draggable)."""

    SIZE = 150

    def __init__(self, core: Any, style: str):
        self._init_reactive()
        self.core = core
        self.app = core.ctx.app
        self.renderer = OrbRenderer(style, self.SIZE, core.ctx.theme.palette)
        self.win: Any = None
        self.canvas: Any = None
        self.visible = False
        self.x = self.y = 0
        self._photo: Any = None
        self._after: Any = None
        self._drag: dict[str, Any] | None = None
        self._t0 = time.monotonic()

    @property
    def size(self) -> tuple[int, int]:
        return self.SIZE + 220, self.SIZE + 70   # room for the speech bubble beside it

    def build(self) -> None:
        if self.win is not None:
            return
        self.win = tk.Toplevel(self.app)
        self.win._mt_role = "skip"
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.configure(bg=OVERLAY_KEY)
            self.win.attributes("-transparentcolor", OVERLAY_KEY)
        except Exception:
            pass
        w, h = self.size
        self.canvas = tk.Canvas(self.win, width=w, height=h, bg=OVERLAY_KEY, highlightthickness=0, bd=0)
        self.canvas._mt_role = "skip"
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.core.app.exit_mini())
        self.canvas.bind("<Button-3>", self._menu)

    def home_position(self) -> tuple[int, int]:
        from .base import work_area
        left, top, right, bottom = work_area(self.app)
        home = self.core.settings.get("orb_home")
        w, h = self.size
        if isinstance(home, list) and len(home) == 2:
            return max(left - w // 2, min(int(home[0]), right - w // 2)), max(top, min(int(home[1]), bottom - h // 2))
        return right - w - 10, bottom - h - 10

    def show(self) -> None:
        self.build()
        self.x, self.y = self.home_position()
        w, h = self.size
        self.win.geometry(f"{w}x{h}+{self.x}+{self.y}")
        self.win.deiconify()
        self.visible = True
        if self._after is None:
            self._after = self.app.after(40, self.tick)

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

    def tick(self) -> None:
        self._after = None
        if not self.visible or self.win is None:
            return
        try:
            self.draw()
        except Exception:
            self.core.ctx.log.exception("Orb overlay frame failed")
        self._after = self.app.after(40, self.tick)

    def draw(self) -> None:
        if Image is None:
            return
        t = time.monotonic() - self._t0
        image = self.renderer.render(t, self._state(self.core.listener.state), self._speech_level(),
                                     glow=False, background=OVERLAY_KEY)
        c = self.canvas
        w, h = self.size
        ox, oy = w - self.SIZE - 6, h - self.SIZE - 6
        if self._photo is None:
            self._photo = ImageTk.PhotoImage(image, master=c)
            c.create_image(ox, oy, image=self._photo, anchor="nw", tags="orb")
        else:
            self._photo.paste(image)
        c.delete("bubble")
        if self.bubble and time.monotonic() < self.bubble[1]:
            p = self.core.ctx.theme.palette
            item = c.create_text(ox - 14, oy + 20, text=self.bubble[0], width=190, anchor="ne", justify="left",
                                 fill=p["text"], font=(p["mono_font"], 10), tags="bubble")
            x0, y0, x1, y1 = c.bbox(item)
            box = c.create_rectangle(x0 - 8, y0 - 6, x1 + 8, y1 + 6, fill=p["card"], outline=p["accent"],
                                     width=1, tags="bubble")
            c.tag_lower(box, item)
        elif self.bubble:
            self.bubble = None

    # ── input ────────────────────────────────────────────────────────────
    def _press(self, event: Any) -> None:
        self._drag = {"x": event.x_root, "y": event.y_root, "ox": self.x, "oy": self.y, "moved": False}

    def _move(self, event: Any) -> None:
        if self._drag is None:
            return
        dx, dy = event.x_root - self._drag["x"], event.y_root - self._drag["y"]
        if abs(dx) + abs(dy) > 4:
            self._drag["moved"] = True
        self.x, self.y = self._drag["ox"] + dx, self._drag["oy"] + dy
        self.win.geometry(f"+{self.x}+{self.y}")
        self.core.chat.reposition()

    def _release(self, _event: Any) -> None:
        drag, self._drag = self._drag, None
        if drag and drag["moved"]:
            self.core.set_setting("orb_home", [self.x, self.y])
        else:
            self.core.chat.toggle()

    def _menu(self, event: Any) -> None:
        self.core.app.mini_menu(event)
