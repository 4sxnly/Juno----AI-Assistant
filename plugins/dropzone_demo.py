"""
Drop Zone (Demo) — reference plugin for the Multi-Tool plugin contract.

Shows every core service a real module will use:
  * drag-and-drop of files / browser links / text  (ctx.register_drop_target)
  * theme roles                                    (ctx.theme.tag)
  * background work with safe UI updates           (ctx.run_in_thread / ctx.call_in_ui)
  * bundled engine lookup                          (ctx.bin_path)
  * per-plugin settings                            (ctx.get_setting / ctx.set_setting)

Disable it in Settings (or delete it) once the real modules exist.
"""

import threading
import time

import customtkinter as ctk


class Plugin:
    meta_title = "Drop Zone (Demo)"
    meta_description = ("Reference plugin: drag files or a link from your browser onto the panel, "
                        "switch themes, and run a background task without freezing the window.")
    meta_icon = "◎"
    meta_order = 999
    meta_api_version = 1

    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._last_item = ""
        self._drops = int(ctx.get_setting("drop_count", 0))

    # ── UI ────────────────────────────────────────────────────────────────
    def get_frame(self, master):
        t = self.ctx.theme
        root = ctk.CTkFrame(master, fg_color="transparent")
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        zone = t.tag(ctk.CTkFrame(root), "card")
        zone.grid(row=0, column=0, sticky="ew")
        zone.grid_columnconfigure(0, weight=1)
        t.tag(ctk.CTkLabel(zone, text="Drop files or a browser link here"), "title", font_size=17).grid(
            row=0, column=0, pady=(30, 4))
        self._hint = t.tag(ctk.CTkLabel(zone, text=f"{self._drops} drop(s) so far"), "muted")
        self._hint.grid(row=1, column=0, pady=(0, 30))

        if not self.ctx.register_drop_target(zone, self._on_drop):
            self._hint.configure(text="Drag & drop is unavailable — see Settings ▸ Bundled engines.")
            t.tag(self._hint, "warning")

        self._log = ctk.CTkTextbox(root, wrap="word", font=t.font(12, mono=True))
        self._log.grid(row=1, column=0, sticky="nsew", pady=12)
        self._log.configure(state="disabled")

        bar = t.tag(ctk.CTkFrame(root, fg_color="transparent"), "transparent")
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(4, weight=1)
        self._task_btn = ctk.CTkButton(bar, text="Run background task", width=170, command=self._start_task)
        self._task_btn.grid(row=0, column=0)
        t.tag(ctk.CTkButton(bar, text="Copy last item", width=130, command=self._copy_last),
              "secondary").grid(row=0, column=1, padx=(8, 0))
        t.tag(ctk.CTkButton(bar, text="Clear", width=80, command=self._clear), "ghost").grid(
            row=0, column=2, padx=(8, 0))
        t.tag(ctk.CTkButton(bar, text="Test error", width=100,
                            command=lambda: self.ctx.set_status("Sample error from the demo plugin", "error")),
              "danger").grid(row=0, column=3, padx=(8, 0))
        self._progress = ctk.CTkProgressBar(bar)
        self._progress.set(0)
        self._progress.grid(row=0, column=4, sticky="ew", padx=(16, 0))

        for engine in ("ffmpeg", "yt-dlp", "tesseract"):
            path = self.ctx.bin_path(engine)
            self._append(f"[engine] {engine:<10} {path if path else 'not found in bin/'}")
        self._append(f"[theme]  active: {self.ctx.theme.name}")
        return root

    # ── behaviour ─────────────────────────────────────────────────────────
    def _on_drop(self, payload):
        for f in payload.files:
            self._append(f"[file]   {f}")
        for url in payload.urls:
            self._append(f"[link]   {url}")
        if not payload.files and not payload.urls:
            self._append(f"[text]   {payload.text[:300]}")
        self._last_item = str(payload.files[0]) if payload.files else (
            payload.urls[0] if payload.urls else payload.text)
        self._drops += 1
        self.ctx.set_setting("drop_count", self._drops)
        self._hint.configure(text=f"{self._drops} drop(s) so far")
        self.ctx.set_status(f"Received {payload.summary()}", "success")

    def _start_task(self):
        self._task_btn.configure(state="disabled")
        self._progress.set(0)
        self._append("[task]   started on a worker thread …")

        def work(steps=50):
            for i in range(steps):
                if self._stop.is_set():
                    return "cancelled"
                time.sleep(0.04)  # stand-in for ffmpeg / network / Whisper work
                self.ctx.call_in_ui(self._progress.set, (i + 1) / steps)
            return "done"

        self.ctx.run_in_thread(work, on_done=self._task_done, on_error=self._task_failed)

    def _task_done(self, result):
        self._task_btn.configure(state="normal")
        self._append(f"[task]   {result}")
        self.ctx.set_status("Background task finished", "success")

    def _task_failed(self, exc):
        self._task_btn.configure(state="normal")
        self._append(f"[task]   failed: {exc}")

    def _copy_last(self):
        if self._last_item:
            self.ctx.copy_to_clipboard(self._last_item)
            self.ctx.set_status("Copied to clipboard", "success")

    def _clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _append(self, line):
        self._log.configure(state="normal")
        self._log.insert("end", line + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    # ── lifecycle hooks ───────────────────────────────────────────────────
    def on_show(self):
        self.ctx.log.debug("shown")

    def on_hide(self):
        self.ctx.log.debug("hidden")

    def on_theme_changed(self, palette):
        if hasattr(self, "_log"):
            self._append(f"[theme]  switched to {self.ctx.theme.name} (accent {palette['accent']})")

    def shutdown(self):
        self._stop.set()  # worker threads must watch an Event; Python can't kill threads
