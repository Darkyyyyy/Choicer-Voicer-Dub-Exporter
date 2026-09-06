#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI for exporting The Choicer Voicer dubs.

Run with:  pythonw export_dub_gui.py   (or double-click "Dub Exporter.pyw")
"""

import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import export_dub

APP_TITLE = "Choicer Voicer — Dub Exporter"
PROGRESS_SCALE = 1000  # bar resolution


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("880x640")
        self.minsize(720, 500)

        self.game_dir = export_dub.DEFAULT_GAME_DIR
        self.out_dir = tk.StringVar(
            value=str(Path(__file__).resolve().parent / "exports"))
        self.voice_gain = tk.DoubleVar(value=0.0)
        self.music_gain = tk.DoubleVar(value=0.0)
        self.keep_original = tk.BooleanVar(value=False)

        self.sessions = []
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.exporting = False

        self._build_ui()
        self.refresh_sessions()
        self.after(100, self._poll_queues)

    # ------------------------------------------------------------- UI
    def _build_ui(self):
        # --- top bar: game folder
        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Game folder:").pack(side="left")
        self.game_label = ttk.Label(top, text=str(self.game_dir),
                                    foreground="#555")
        self.game_label.pack(side="left", padx=(4, 8))
        ttk.Button(top, text="Change…", command=self.pick_game_dir).pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_sessions).pack(side="right")

        # --- session list
        mid = ttk.Frame(self, padding=10)
        mid.pack(fill="both", expand=True)

        cols = ("pack", "scene", "session", "count")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings",
                                 selectmode="extended")
        self.tree.heading("pack", text="Pack")
        self.tree.heading("scene", text="Scene")
        self.tree.heading("session", text="Session")
        self.tree.heading("count", text="Lines")
        self.tree.column("pack", width=110, stretch=False)
        self.tree.column("scene", width=340)
        self.tree.column("session", width=170, stretch=False)
        self.tree.column("count", width=60, stretch=False, anchor="e")
        self.tree.bind("<Double-1>", lambda e: self.export_selected())

        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # --- options
        opts = ttk.LabelFrame(self, text="Options", padding=8)
        opts.pack(fill="x", padx=10)

        ttk.Label(opts, text="Voice volume (dB)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=-24, to=24, increment=1,
                    textvariable=self.voice_gain, width=6).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(opts, text="Music volume (dB)").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, from_=-24, to=24, increment=1,
                    textvariable=self.music_gain, width=6).grid(row=0, column=3, padx=(4, 16))
        ttk.Checkbutton(opts, text="Also keep the video's original audio",
                        variable=self.keep_original).grid(row=0, column=4, sticky="w")

        ttk.Label(opts, text="Output folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(opts, textvariable=self.out_dir).grid(
            row=1, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0))
        ttk.Button(opts, text="Browse…", command=self.pick_out_dir).grid(
            row=1, column=4, sticky="w", pady=(6, 0))
        opts.columnconfigure(3, weight=1)

        # --- action buttons + progress
        actions = ttk.Frame(self, padding=(10, 8))
        actions.pack(fill="x")
        self.btn_export = ttk.Button(actions, text="Export selection",
                                     command=self.export_selected)
        self.btn_export.pack(side="left")
        self.btn_export_all = ttk.Button(actions, text="Export all",
                                         command=self.export_all)
        self.btn_export_all.pack(side="left", padx=6)
        ttk.Button(actions, text="Open output folder",
                   command=self.open_out_dir).pack(side="left", padx=6)

        prog = ttk.Frame(self, padding=(10, 0, 10, 4))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate",
                                        maximum=PROGRESS_SCALE)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_label = ttk.Label(prog, text="Ready", width=42, anchor="e")
        self.status_label.pack(side="right", padx=(8, 0))

        # --- log
        logf = ttk.LabelFrame(self, text="Log", padding=4)
        logf.pack(fill="both", padx=10, pady=(0, 10))
        self.log_text = tk.Text(logf, height=7, state="disabled", wrap="word",
                                font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------- helpers
    def log(self, msg):
        self.log_queue.put(msg)

    def set_progress(self, fraction, text):
        """Thread-safe: queued, applied by the Tk thread."""
        self.progress_queue.put((fraction, text))

    def _poll_queues(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            fraction, text = None, None
            while True:  # only keep the most recent update
                fraction, text = self.progress_queue.get_nowait()
        except queue.Empty:
            pass
        if fraction is not None:
            self.progress.configure(value=round(fraction * PROGRESS_SCALE))
            self.status_label.configure(text=text)
        self.after(100, self._poll_queues)

    # ------------------------------------------------------------- actions
    def pick_game_dir(self):
        chosen = filedialog.askdirectory(
            title="Select The Choicer Voicer 'game' folder",
            initialdir=str(self.game_dir))
        if chosen:
            self.game_dir = Path(chosen)
            self.game_label.configure(text=chosen)
            self.refresh_sessions()

    def pick_out_dir(self):
        chosen = filedialog.askdirectory(title="Output folder",
                                         initialdir=self.out_dir.get())
        if chosen:
            self.out_dir.set(chosen)

    def open_out_dir(self):
        out = Path(self.out_dir.get())
        out.mkdir(parents=True, exist_ok=True)
        os.startfile(out)  # noqa: S606 — opens Windows Explorer

    def refresh_sessions(self):
        self.tree.delete(*self.tree.get_children())
        self.sessions = export_dub.find_sessions(self.game_dir)
        for i, (pack, scene, sdir, count) in enumerate(self.sessions):
            self.tree.insert("", "end", iid=str(i),
                             values=(pack, scene, sdir.name, count))
        if not self.sessions:
            self.log("No sessions found. Check the game folder.")

    def export_selected(self):
        indices = [int(i) for i in self.tree.selection()]
        if not indices:
            messagebox.showinfo(APP_TITLE, "Select at least one session in the list.")
            return
        self._start_export(indices)

    def export_all(self):
        if self.sessions:
            self._start_export(list(range(len(self.sessions))))

    def _start_export(self, indices):
        if self.exporting:
            return
        self.exporting = True
        self.btn_export.configure(state="disabled")
        self.btn_export_all.configure(state="disabled")
        self.set_progress(0.0, "Starting…")
        threading.Thread(target=self._export_worker, args=(indices,),
                         daemon=True).start()

    def _export_worker(self, indices):
        out = Path(self.out_dir.get())
        total = len(indices)
        ok = 0
        for pos, idx in enumerate(indices):
            pack, scene, sdir, count = self.sessions[idx]
            self.log(f"=== [{pack}] {scene} ({sdir.name}) ===")

            def on_progress(frac, _pos=pos, _scene=scene):
                overall = (_pos + frac) / total
                self.set_progress(
                    overall,
                    f"{_pos + 1}/{total}  {_scene} — {frac * 100:.0f}%")

            try:
                success = export_dub.build_export(
                    self.game_dir, pack, scene, sdir, out,
                    self.voice_gain.get(), self.music_gain.get(),
                    self.keep_original.get(), log=self.log,
                    progress_cb=on_progress)
            except Exception as e:  # keep the GUI alive no matter what
                self.log(f"  [ERROR] {e}")
                success = False
            if success:
                ok += 1
        self.log(f"Done: {ok}/{total} export(s) succeeded -> {out}")
        self.set_progress(1.0, f"Done — {ok}/{total} succeeded")
        self.after(0, self._export_done)

    def _export_done(self):
        self.exporting = False
        self.btn_export.configure(state="normal")
        self.btn_export_all.configure(state="normal")


def main():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror(APP_TITLE, "ffmpeg was not found in PATH.")
        raise SystemExit(1)
    App().mainloop()


if __name__ == "__main__":
    main()
