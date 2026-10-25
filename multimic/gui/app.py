"""The main window."""

import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import sounddevice as sd

from .. import __version__
from ..config import (BACKLOG_STOP, BACKLOG_WARN, DISK_STOP_BYTES, FORMATS,
                      GAIN_RANGE, MIX_MODES, RECONNECT_EVERY, SAMPLE_RATES,
                      load_settings, save_settings)
from ..devices import (hostapis, input_devices, output_devices,
                       preferred_hostapi, rescan_devices)
from ..mic import Mic
from ..monitor import Monitor
from ..session import Session, data_rate
from ..util import (disk_free, fmt_bytes, fmt_duration, fmt_time,
                    resource_path, to_db)
from .widgets import (ACCENT, AMBER, BG, DIM, FAINT, FG, GREEN, RED, WARN,
                      Meter, RecordButton, ScrollFrame)


class MicRow:
    """One microphone card."""

    def __init__(self, app, parent, row, mic, saved):
        self.app = app
        self.mic = mic
        self.held = 0.0
        # the outer frame's border lights up for "Find mic"
        self.outer = tk.Frame(parent, bg=BG, highlightthickness=2,
                              highlightbackground=BG, highlightcolor=BG)
        self.outer.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        self.outer.columnconfigure(0, weight=1)
        card = self.card = ttk.Frame(self.outer, style="Card.TFrame",
                                     padding=(14, 10, 14, 12))
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(2, weight=1)

        self.var = tk.BooleanVar(value=False)
        self.cb = ttk.Checkbutton(card, style="Switch.TCheckbutton",
                                  variable=self.var,
                                  command=self.toggled)
        self.cb.grid(row=0, column=0, sticky="w")
        self.label_var = tk.StringVar(value=saved.get("label", mic.name))
        self.label_entry = ttk.Entry(card, width=22,
                                     textvariable=self.label_var,
                                     font=app.f_bold)
        self.label_entry.grid(row=0, column=1, sticky="w", padx=(4, 12))
        dev = mic.name if len(mic.name) < 48 else mic.name[:46] + "…"
        ttk.Label(card, text=dev, foreground=DIM,
                  font=app.f_small).grid(row=0, column=2, sticky="w")
        self.info = ttk.Label(card, font=app.f_small, anchor="e")
        self.info.grid(row=0, column=3, sticky="e")
        self.reconnect_btn = ttk.Button(card, text="Reconnect",
                                        command=app.reconnect_all)
        self.reconnect_shown = False

        meter_row = ttk.Frame(card)
        meter_row.grid(row=1, column=0, columnspan=4, sticky="ew",
                       pady=(10, 8))
        meter_row.columnconfigure(0, weight=1)
        self.meter = Meter(meter_row)
        self.meter.grid(row=0, column=0, sticky="ew")
        self.db = ttk.Label(meter_row, width=9, anchor="e",
                            font=app.f_mono)
        self.db.grid(row=0, column=1, padx=(10, 0))

        ctl = ttk.Frame(card)
        ctl.grid(row=2, column=0, columnspan=4, sticky="ew")
        ttk.Label(ctl, text="Gain", foreground=DIM).pack(side="left")
        gain_db = float(saved.get("gain_db", 0))
        gain_db = max(-GAIN_RANGE, min(GAIN_RANGE, gain_db))
        self.gain_var = tk.DoubleVar(value=gain_db)
        self.gain = ttk.Scale(ctl, from_=-GAIN_RANGE, to=GAIN_RANGE,
                              length=180, variable=self.gain_var,
                              command=lambda v: self.gain_changed())
        self.gain.pack(side="left", padx=(8, 6))
        self.gain.bind("<Double-Button-1>", lambda e: self.reset_gain())
        self.gain_lbl = ttk.Label(ctl, width=7, font=app.f_mono)
        self.gain_lbl.pack(side="left")
        self.gain_changed()

        self.mute_var = tk.BooleanVar(value=bool(saved.get("muted")))
        ttk.Checkbutton(ctl, text="Mute", style="Toggle.TButton",
                        variable=self.mute_var, width=6,
                        command=self.mute_changed).pack(side="left",
                                                        padx=(12, 0))
        self.mute_changed()

        ttk.Label(ctl, text="Pan", foreground=DIM).pack(side="left",
                                                       padx=(18, 0))
        pan = max(-100, min(100, float(saved.get("pan", 0))))
        self.pan_var = tk.DoubleVar(value=pan)
        self.pan = ttk.Scale(ctl, from_=-100, to=100, length=90,
                             variable=self.pan_var,
                             command=lambda v: self.pan_changed())
        self.pan.pack(side="left", padx=(8, 6))
        self.pan.bind("<Double-Button-1>", lambda e: self.reset_pan())
        self.pan_lbl = ttk.Label(ctl, width=4, font=app.f_mono)
        self.pan_lbl.pack(side="left")
        self.pan_changed()

        chans = mic.info["max_input_channels"]
        want = int(saved.get("channels", 2))
        self.ch_var = tk.StringVar(value=str(max(1, min(want, chans))))
        self.ch_box = ttk.Combobox(
            ctl, width=3, state="readonly", textvariable=self.ch_var,
            values=[str(c) for c in range(1, chans + 1)])
        self.ch_box.pack(side="right")
        self.ch_box.bind("<<ComboboxSelected>>", lambda e: self.reopen())
        ttk.Label(ctl, text="Channels", foreground=DIM).pack(
            side="right", padx=(0, 8))

        if saved.get("enabled"):
            self.var.set(True)
            self.reopen(quiet=True)

    @property
    def label(self):
        return self.label_var.get().strip() or self.mic.name

    def settings(self):
        return {"enabled": self.var.get(), "label": self.label,
                "channels": int(self.ch_var.get()),
                "gain_db": round(self.gain_var.get()),
                "muted": self.mute_var.get(),
                "pan": round(self.pan_var.get())}

    def gain_changed(self):
        db = round(self.gain_var.get())
        self.mic.set_gain_db(db)
        self.gain_lbl.config(text=f"{db:+d} dB")

    def reset_gain(self):
        self.gain_var.set(0)
        self.gain_changed()

    def mute_changed(self):
        self.mic.muted = self.mute_var.get()

    def pan_changed(self):
        p = round(self.pan_var.get())
        self.mic.pan = p / 100
        self.pan_lbl.config(text="C" if p == 0 else
                            f"{'L' if p < 0 else 'R'}{abs(p)}")

    def reset_pan(self):
        self.pan_var.set(0)
        self.pan_changed()

    def highlight(self, on):
        color = ACCENT if on else BG
        self.outer.config(highlightbackground=color, highlightcolor=color)

    def show_reconnect(self, on):
        if on != self.reconnect_shown:
            self.reconnect_shown = on
            if on:
                self.reconnect_btn.grid(row=0, column=4, padx=(10, 0))
            else:
                self.reconnect_btn.grid_remove()

    def toggled(self):
        if self.var.get():
            self.reopen()
        else:
            self.mic.close()
            self.app.update_monitor()
        self.app.update_summary()

    def reopen(self, quiet=False):
        if not self.var.get() or self.app.session:
            return
        self.mic.close()
        try:
            self.mic.open(self.app.samplerate(), int(self.ch_var.get()))
        except Exception as e:
            self.var.set(False)
            if quiet:
                self.app.set_status(f"Could not open {self.mic.name}: {e}",
                                    warn=True)
            else:
                messagebox.showerror(
                    "MultiMic", f"Could not open {self.mic.name}:\n{e}")
        self.app.update_monitor()

    def set_locked(self, locked):
        self.cb.config(state="disabled" if locked else "normal")
        self.ch_box.config(state="disabled" if locked else "readonly")
        self.label_entry.config(state="disabled" if locked else "normal")

    def update(self):
        mic = self.mic
        peak, mic.peak = mic.peak, 0.0
        self.held = max(peak, self.held * 0.85)
        db = to_db(self.held)
        self.meter.set(db, mic.is_open and not mic.muted)
        if not mic.is_open:
            self.db.config(text="")
            self.info.config(text="Off", foreground=FAINT)
            self.show_reconnect(False)
            return
        self.db.config(text="muted" if mic.muted else
                       ("-inf dB" if db <= -120 else f"{db:.1f} dB"))
        warn = False
        parts = [f"{int(mic.stream.samplerate) // 1000} kHz",
                 f"{mic.stream.channels} ch"]
        if mic.is_recording:
            parts.append(fmt_time(mic.seconds))
        if self.app.lock_var.get() and mic.estimator and \
                mic.estimator.span >= 5:
            parts.append(f"clock {mic.drift_ppm:+.0f} ppm")
        if mic.gap_seconds >= 0.05:
            parts.append(f"{mic.gap_seconds:.1f} s gap filled")
            warn = True
        if mic.backlog >= BACKLOG_WARN:
            parts.append(f"disk {mic.backlog:.0f} s behind")
            warn = True
        if mic.clips:
            parts.append(f"CLIP ×{mic.clips}")
            warn = True
        if mic.overflows:
            parts.append(f"{mic.overflows} drops")
            warn = True
        text = "  ·  ".join(parts)
        lost = mic.no_signal
        if lost:
            text = ("Disconnected - recording silence, retrying"
                    if mic.is_recording else "No signal - unplugged?")
            warn = True
        self.show_reconnect(lost)
        if mic.error:
            text, warn = f"Error: {mic.error}", True
        self.info.config(text=text, foreground=WARN if warn else DIM)

class App:
    def __init__(self, root):
        self.root = root
        self.session = None
        self.monitor = None
        self.rows = []
        self.outputs = []
        self.settings = load_settings()
        self._ticks = 0
        self.find_until = 0.0
        self.next_retry = 0.0
        s = self.settings
        root.title(f"MultiMic {__version__}")
        root.geometry("1220x800")
        root.minsize(1000, 560)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        fams = set(tkfont.families())
        ui = next((f for f in ("Segoe UI Variable Text", "Segoe UI",
                               "Inter", "DejaVu Sans") if f in fams),
                  "TkDefaultFont")
        mono = next((f for f in ("Cascadia Mono", "Consolas",
                                 "DejaVu Sans Mono") if f in fams),
                    "TkFixedFont")
        self.f_title = (ui, 20, "bold")
        self.f_bold = (ui, 10, "bold")
        self.f_small = (ui, 9)
        self.f_head = (ui, 11, "bold")
        self.f_mono = (mono, 10)
        self.f_clock = (mono, 34)

        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        self._build_header()
        self._build_body(s)
        self._build_status()

        root.bind("<space>", self.space)
        for key, fn in (("m", self.marker_key), ("p", self.pause_key)):
            root.bind(f"<Key-{key}>", fn)
            root.bind(f"<Key-{key.upper()}>", fn)
        self.build_rows()
        self.update_summary()
        self.tick()

    # -- layout ---------------------------------------------------------

    def _build_header(self):
        head = ttk.Frame(self.root, padding=(24, 18, 24, 14))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)

        left = ttk.Frame(head)
        left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="MultiMic", font=self.f_title).pack(
            anchor="w")
        self.summary = ttk.Label(left, foreground=DIM)
        self.summary.pack(anchor="w")

        mid = ttk.Frame(head)
        mid.grid(row=0, column=1)
        self.clock = ttk.Label(mid, text="00:00:00", font=self.f_clock)
        self.clock.pack()
        self.state = ttk.Label(mid, text="READY", foreground=DIM,
                               font=self.f_bold)
        self.state.pack()

        right = ttk.Frame(head)
        right.grid(row=0, column=2, sticky="e")
        self.pause_btn = ttk.Button(right, text="Pause  (P)", width=12,
                                    state="disabled",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=(0, 8))
        self.marker_btn = ttk.Button(right, text="Marker  (M)", width=12,
                                     state="disabled",
                                     command=self.add_marker)
        self.marker_btn.pack(side="left", padx=(0, 8))
        self.find_btn = ttk.Button(right, text="Find mic", width=10,
                                   command=self.toggle_find)
        self.find_btn.pack(side="left", padx=(0, 16))
        self.rec_btn = RecordButton(right, self.toggle_record)
        self.rec_btn.pack(side="left")

        ttk.Separator(self.root).grid(row=1, column=0, sticky="ew")

    def _build_body(self, s):
        body = ttk.Frame(self.root, padding=(24, 16, 24, 8))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        bar = ttk.Frame(body)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(bar, text="Microphones", font=self.f_head).pack(
            side="left")
        ttk.Label(bar, text="Switch on the mics you want to record",
                  foreground=DIM).pack(side="left", padx=12)
        self.refresh_btn = ttk.Button(bar, text="⟳  Rescan",
                                      command=self.rescan)
        self.refresh_btn.pack(side="right")

        self.scroll = ScrollFrame(body)
        self.scroll.grid(row=1, column=0, sticky="nsew")
        self.list_frame = self.scroll.inner
        self.list_frame.columnconfigure(0, weight=1)

        side = ttk.Frame(body, width=340)
        side.grid(row=0, column=1, rowspan=2, sticky="nsew",
                  padx=(20, 0))
        self._build_sidebar(side, s)

    def _section(self, parent, title, note=None):
        card = ttk.Frame(parent, style="Card.TFrame",
                         padding=(14, 8, 14, 10))
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text=title, font=self.f_bold).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        if note:
            ttk.Label(card, text=note, foreground=FAINT,
                      font=self.f_small).grid(row=0, column=1,
                                              columnspan=2, sticky="e",
                                              pady=(0, 6))
        return card

    def _field(self, card, row, label, widget, hint=None):
        ttk.Label(card, text=label, foreground=DIM).grid(
            row=row, column=0, sticky="w", pady=3, padx=(0, 12))
        widget.grid(row=row, column=1, sticky="ew", pady=2)
        if hint:
            ttk.Label(card, text=hint, foreground=FAINT,
                      font=self.f_small).grid(row=row, column=2,
                                              sticky="w", padx=(6, 0))

    def _build_sidebar(self, side, s):
        rec = self._section(side, "Recording")
        default_out = os.path.join(os.path.expanduser("~"), "Music",
                                   "MultiMic")
        self.out_var = tk.StringVar(value=s.get("out", default_out))
        folder = ttk.Frame(rec)
        folder.grid(row=1, column=0, columnspan=3, sticky="ew",
                    pady=(0, 6))
        ttk.Entry(folder, textvariable=self.out_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(folder, text="…", width=3,
                   command=self.browse).pack(side="left", padx=(6, 0))
        ttk.Button(folder, text="Open", width=6,
                   command=self.open_folder).pack(side="left",
                                                  padx=(6, 0))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(rec, textvariable=self.name_var)
        self._field(rec, 3, "Session name", self.name_entry)
        fmt = s.get("format", "WAV 24-bit")
        self.fmt_var = tk.StringVar(
            value=fmt if fmt in FORMATS else "WAV 24-bit")
        self.fmt_box = ttk.Combobox(rec, state="readonly",
                                    textvariable=self.fmt_var,
                                    values=list(FORMATS))
        self.fmt_box.bind("<<ComboboxSelected>>",
                          lambda e: self.update_summary())
        self._field(rec, 4, "Format", self.fmt_box)
        mix = s.get("mixdown", "Off")
        self.mix_var = tk.StringVar(value=mix if mix in MIX_MODES
                                    else "Off")
        self.mix_box = ttk.Combobox(rec, state="readonly",
                                    textvariable=self.mix_var,
                                    values=list(MIX_MODES))
        self._field(rec, 5, "Mixdown", self.mix_box)

        opt = self._section(side, "Options", "0 = off")
        self.api_var = tk.StringVar()
        apis = hostapis()
        self.api_box = ttk.Combobox(opt, state="readonly", width=14,
                                    textvariable=self.api_var,
                                    values=apis)
        self.api_box.current(apis.index(s["api"]) if s.get("api") in apis
                             else preferred_hostapi())
        self.api_box.bind("<<ComboboxSelected>>",
                          lambda e: self.build_rows())
        self._field(opt, 1, "Audio driver", self.api_box)
        sr = s.get("samplerate", "48000")
        self.sr_var = tk.StringVar(
            value=sr if sr in SAMPLE_RATES else "48000")
        self.sr_box = ttk.Combobox(opt, state="readonly", width=6,
                                   textvariable=self.sr_var,
                                   values=SAMPLE_RATES)
        self.sr_box.bind("<<ComboboxSelected>>",
                         lambda e: (self.reopen_all(),
                                    self.update_summary()))
        self._field(opt, 2, "Sample rate", self.sr_box)
        self.preroll_var = tk.StringVar(
            value=str(s.get("preroll_seconds", 0)))
        self.preroll_box = ttk.Spinbox(opt, from_=0, to=30, increment=1,
                                       width=6,
                                       textvariable=self.preroll_var)
        self._field(opt, 3, "Pre-record", self.preroll_box, "sec")
        self.split_var = tk.StringVar(value=str(s.get("split_minutes", 0)))
        self.split_box = ttk.Spinbox(opt, from_=0, to=600, increment=5,
                                     width=6, textvariable=self.split_var)
        self._field(opt, 4, "New file every", self.split_box, "min")
        self.stop_after_var = tk.StringVar(
            value=str(s.get("stop_after_minutes", 0)))
        self.stop_after_box = ttk.Spinbox(
            opt, from_=0, to=1440, increment=5, width=6,
            textvariable=self.stop_after_var)
        self._field(opt, 5, "Auto-stop after", self.stop_after_box, "min")
        self.lock_var = tk.BooleanVar(value=s.get("lock_clocks", True))
        self.lock_cb = ttk.Checkbutton(opt, text="Lock mic clocks",
                                       style="Switch.TCheckbutton",
                                       variable=self.lock_var)
        self.lock_cb.grid(row=6, column=0, columnspan=3, sticky="w",
                          pady=(6, 0))

        mon = self._section(side, "Monitor")
        self.mon_var = tk.StringVar(value="Off")
        self.mon_box = ttk.Combobox(mon, state="readonly", width=22,
                                    textvariable=self.mon_var,
                                    postcommand=self.fill_monitor_list)
        self.mon_box.bind("<<ComboboxSelected>>",
                          lambda e: self.update_monitor())
        self._field(mon, 1, "Listen to", self.mon_box)
        self.out_dev_var = tk.StringVar()
        self.out_dev_box = ttk.Combobox(mon, state="readonly", width=22,
                                        textvariable=self.out_dev_var)
        self.out_dev_box.bind("<<ComboboxSelected>>",
                              lambda e: self.update_monitor())
        self._field(mon, 2, "Through", self.out_dev_box)

        keys = ttk.Label(side, foreground=FAINT, font=self.f_small,
                         text="Space  record / stop    P  pause    "
                              "M  marker")
        keys.pack(anchor="w", pady=(4, 0))

    def _build_status(self):
        ttk.Separator(self.root).grid(row=3, column=0, sticky="ew")
        bar = ttk.Frame(self.root, padding=(24, 8, 24, 10))
        bar.grid(row=4, column=0, sticky="ew")
        self.status = ttk.Label(bar, text="Switch on the microphones "
                                          "you want to use.",
                                foreground=DIM)
        self.status.pack(side="left", fill="x", expand=True)
        self.disk = ttk.Label(bar, foreground=DIM)
        self.disk.pack(side="right")

    def set_status(self, text, warn=False):
        self.status.config(text=text, foreground=WARN if warn else DIM)

    def update_summary(self):
        n = sum(1 for r in self.rows if r.mic.is_open)
        sr = self.sr_var.get()
        rate = "device rate" if sr == "Device default" \
            else f"{int(sr) / 1000:g} kHz"
        self.summary.config(
            text=f"{n} of {len(self.rows)} mics on  ·  {rate}  ·  "
                 f"{self.fmt_var.get()}")

    # -- settings -------------------------------------------------------

    def save(self):
        s = self.settings
        mics = s.setdefault("mics", {})
        for r in self.rows:
            mics[r.mic.name] = r.settings()
        mon = self.monitored_row()
        out = self.output_device()
        s.update({
            "api": self.api_var.get(),
            "samplerate": self.sr_var.get(),
            "format": self.fmt_var.get(),
            "mixdown": self.mix_var.get(),
            "split_minutes": self.split_minutes(),
            "stop_after_minutes": self.stop_after_minutes(),
            "preroll_seconds": self.preroll_seconds(),
            "lock_clocks": self.lock_var.get(),
            "out": self.out_var.get(),
            "monitor_mic": mon.mic.name if mon else None,
            "monitor_out": sd.query_devices(out)["name"]
            if out is not None else None,
        })
        save_settings(s)

    # -- devices --------------------------------------------------------

    def samplerate(self):
        v = self.sr_var.get()
        return None if v == "Device default" else int(v)

    def split_minutes(self):
        try:
            return max(0, float(self.split_var.get()))
        except ValueError:
            return 0

    def preroll_seconds(self):
        try:
            return min(30.0, max(0.0, float(self.preroll_var.get())))
        except ValueError:
            return 0.0

    def stop_after_minutes(self):
        try:
            return max(0, float(self.stop_after_var.get()))
        except ValueError:
            return 0

    def build_rows(self):
        self.close_monitor()
        if self.rows:
            self.save()
        for r in self.rows:
            r.mic.close()
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows = []
        api = self.api_box.current()
        api_name = hostapis()[api]
        devices = input_devices(api)
        if not devices:
            ttk.Label(self.list_frame, foreground=DIM, padding=20,
                      text="No microphones found for this audio API.\n"
                           "Plug one in and press Rescan.").grid(
                row=0, column=0, sticky="w")
        saved = self.settings.get("mics", {})
        for n, (idx, info) in enumerate(devices):
            self.rows.append(MicRow(self, self.list_frame, n,
                                    Mic(idx, info, api_name),
                                    saved.get(info["name"], {})))

        # monitor choices
        self.outputs = output_devices(api)
        self.out_dev_box.config(values=[d["name"]
                                        for _, d in self.outputs])
        want = self.settings.get("monitor_out")
        default = sd.query_hostapis(api)["default_output_device"]
        pick = 0
        for i, (idx, d) in enumerate(self.outputs):
            if d["name"] == want:
                pick = i
                break
            if idx == default:
                pick = i
        if self.outputs:
            self.out_dev_box.current(pick)
        else:
            self.out_dev_var.set("")
        self.fill_monitor_list()
        want = self.settings.get("monitor_mic")
        self.mon_var.set(next((r.label for r in self.rows
                               if r.mic.name == want), "Off"))
        self.update_monitor()
        self.update_summary()

    def reopen_all(self):
        for r in self.rows:
            r.reopen()

    def rescan(self):
        if self.session:
            return
        self.save()
        self.close_monitor()
        for r in self.rows:
            r.mic.close()
        self.rows = []
        rescan_devices()
        apis = hostapis()
        self.api_box.config(values=apis)
        want = self.settings.get("api")
        self.api_box.current(apis.index(want) if want in apis
                             else preferred_hostapi())
        self.build_rows()
        self.set_status("Device list refreshed.")

    def reconnect_all(self):
        """Re-detect devices and reopen every mic. During a recording
        all mics get a short gap, filled with silence so files stay in
        sync."""
        if not self.session:
            self.rescan()
            return
        self.close_monitor()
        for r in self.rows:
            if r.mic.is_open:
                try:
                    r.mic.stream.abort()
                    r.mic.stream.close()
                except Exception:
                    pass
        rescan_devices()
        apis = hostapis()
        api = apis.index(self.api_var.get()) \
            if self.api_var.get() in apis else preferred_hostapi()
        found = {d["name"]: (i, d) for i, d in input_devices(api)}
        missing = []
        for r in self.rows:
            if r.mic.name not in found:
                if r.mic.is_open:
                    missing.append(r.label)
                continue
            idx, info = found[r.mic.name]
            r.mic.index, r.mic.info = idx, info
            if r.mic.is_open:
                try:
                    r.mic.restart_stream(idx, info)
                except Exception:
                    missing.append(r.label)
        self.outputs = output_devices(api)
        self.update_monitor()
        if missing:
            self.set_status("Still missing: " + ", ".join(missing)
                            + ". Their files get silence until they "
                              "come back.", warn=True)
        else:
            self.set_status("All mics reconnected.")

    def retry_lost(self):
        """Quietly try to reopen mics that dropped out mid-recording."""
        for r in self.rows:
            if r.mic.is_recording and r.mic.no_signal:
                try:
                    r.mic.restart_stream()
                except Exception:
                    pass

    # -- find mic -------------------------------------------------------

    def toggle_find(self):
        if self.find_until:
            self.stop_find()
        else:
            self.find_until = time.monotonic() + 20
            self.find_btn.config(text="Done")
            self.set_status("Speak into a mic or tap it: its card lights "
                            "up. Rename it if needed.")

    def stop_find(self):
        self.find_until = 0.0
        self.find_btn.config(text="Find mic")
        for r in self.rows:
            r.highlight(False)

    def update_find(self):
        if not self.find_until:
            return
        if time.monotonic() > self.find_until:
            self.stop_find()
            return
        live = [r for r in self.rows if r.mic.is_open]
        best = max(live, key=lambda r: r.held, default=None)
        for r in self.rows:
            r.highlight(r is best and to_db(r.held) > -40)

    # -- monitoring -----------------------------------------------------

    def fill_monitor_list(self):
        self.mon_box.config(values=["Off"] + [r.label for r in self.rows])

    def monitored_row(self):
        k = self.mon_box.current()
        if k <= 0:
            return None
        labels = [r.label for r in self.rows]
        name = self.mon_var.get()
        # labels may have been edited since the list was filled
        if name in labels:
            return self.rows[labels.index(name)]
        return self.rows[k - 1] if k - 1 < len(self.rows) else None

    def output_device(self):
        k = self.out_dev_box.current()
        return self.outputs[k][0] if 0 <= k < len(self.outputs) else None

    def close_monitor(self):
        for r in self.rows:
            r.mic.monitor = None
        if self.monitor is not None:
            self.monitor.close()
            self.monitor = None

    def update_monitor(self):
        self.close_monitor()
        row = self.monitored_row()
        out = self.output_device()
        if row is None or out is None or not row.mic.is_open:
            return
        api_name = hostapis()[self.api_box.current()]
        try:
            self.monitor = Monitor(out, int(row.mic.stream.samplerate),
                                   api_name)
        except Exception as e:
            self.set_status(f"Could not start monitoring: {e}", warn=True)
            return
        row.mic.monitor = self.monitor

    # -- recording ------------------------------------------------------

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    def open_folder(self):
        path = self.session.folder if self.session else self.out_var.get()
        os.makedirs(path, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)

    def _typing(self, event):
        return isinstance(event.widget, (tk.Entry, ttk.Entry,
                                         ttk.Spinbox))

    def space(self, event):
        if not self._typing(event):
            self.toggle_record()

    def marker_key(self, event):
        if not self._typing(event):
            self.add_marker()

    def pause_key(self, event):
        if not self._typing(event):
            self.toggle_pause()

    def toggle_pause(self):
        if not self.session:
            return
        if self.session.paused:
            self.session.resume()
            self.pause_btn.config(text="Pause  (P)")
            self.set_status("Recording resumed.")
        else:
            self.session.pause()
            self.pause_btn.config(text="Resume  (P)")
            self.set_status("Paused. Nothing is being recorded until you "
                            "resume.")

    def add_marker(self):
        if not self.session:
            return
        t, label = self.session.add_marker()
        self.set_status(f"{label} at {fmt_time(t, True)}")

    def toggle_record(self):
        if self.session:
            self.stop()
        else:
            self.start()

    def start(self):
        rows = [r for r in self.rows if r.mic.is_open]
        if not rows:
            messagebox.showinfo("MultiMic",
                                "Switch on at least one microphone first.")
            return
        for r in rows:
            r.mic.label = r.label
        session = Session([r.mic for r in rows], self.out_var.get(),
                          self.fmt_var.get(),
                          MIX_MODES[self.mix_var.get()],
                          self.split_minutes(), self.name_var.get(),
                          lock_clocks=self.lock_var.get())
        free = disk_free(self.out_var.get())
        if free is not None and free < DISK_STOP_BYTES:
            messagebox.showerror(
                "MultiMic", f"Only {fmt_bytes(free)} free on the output "
                            "drive. Free up space or pick another folder.")
            return
        try:
            session.start(self.preroll_seconds())
        except Exception as e:
            session.stop()
            messagebox.showerror("MultiMic", f"Could not start:\n{e}")
            return
        self.session = session
        self.save()
        self.set_locked(True)
        self.rec_btn.set_recording(True)
        self.set_status(f"Recording {len(rows)} mic(s) to "
                        f"{session.folder}")
        self.update_disk()

    def stop(self, reason=""):
        session, self.session = self.session, None
        session.stop()
        self.set_locked(False)
        self.rec_btn.set_recording(False)
        self.clock.config(text=fmt_time(session.position), foreground=FG)
        self.state.config(text="SAVED", foreground=GREEN)
        drops = sum(m.overflows for m in session.mics)
        clips = sum(m.clips for m in session.mics)
        msg = f"Saved {len(session.mics)} mic(s) to {session.folder}"
        if session.mix is not None:
            msg += " (+ mixdown)"
        if session.markers:
            msg += f", {len(session.markers)} marker(s)"
        warn = False
        if clips:
            msg += "  -  some mics clipped, turn their gain down"
            warn = True
        if drops:
            msg += f"  -  {drops} buffer drop(s), try fewer mics per hub"
            warn = True
        if session.errors:
            msg, warn = "Errors: " + "; ".join(session.errors), True
        if reason:
            msg = f"{reason}. {msg}"
        self.set_status(msg, warn)
        self.update_disk()

    def set_locked(self, locked):
        state = "disabled" if locked else "readonly"
        for box in (self.api_box, self.sr_box, self.fmt_box,
                    self.mix_box):
            box.config(state=state)
        for w in (self.split_box, self.stop_after_box, self.name_entry,
                  self.preroll_box, self.refresh_btn, self.lock_cb):
            w.config(state="disabled" if locked else "normal")
        for w in (self.marker_btn, self.pause_btn):
            w.config(state="normal" if locked else "disabled")
        self.pause_btn.config(text="Pause  (P)")
        for r in self.rows:
            r.set_locked(locked)

    def update_disk(self):
        free = disk_free(self.out_var.get())
        if free is None:
            self.disk.config(text="")
            return
        if self.session:
            rate = self.session.data_rate
        else:
            rate = data_rate([r.mic for r in self.rows if r.mic.is_open],
                             self.fmt_var.get(),
                             MIX_MODES[self.mix_var.get()])
        text = f"{fmt_bytes(free)} free"
        low = False
        if rate:
            left = (free - DISK_STOP_BYTES) / rate
            text += f"  ·  about {fmt_duration(left)} of recording left"
            low = left < 600
        self.disk.config(text=text, foreground=WARN if low else DIM)
        if self.session and free < DISK_STOP_BYTES:
            self.stop("Stopped: the disk is nearly full")

    def tick(self):
        preroll = self.preroll_seconds()
        for r in self.rows:
            r.mic.ring_seconds = preroll
            r.update()
        if self.session:
            elapsed = self.session.elapsed
            paused = self.session.paused
            blink = paused and (self._ticks // 10) % 2
            self.clock.config(text=fmt_time(elapsed),
                              foreground=AMBER if paused else FG)
            self.state.config(
                text="PAUSED" if paused else "●  RECORDING",
                foreground=(BG if blink else AMBER) if paused else RED)
            self.rec_btn.tick(paused)
            limit = self.stop_after_minutes()
            if limit and elapsed >= limit * 60:
                self.stop(f"Stopped automatically after "
                          f"{fmt_time(limit * 60)}")
            if self.session and self.session.backlog > BACKLOG_STOP:
                self.stop("Stopped: the disk can't keep up with this "
                          "many mics")
            if self.session and time.monotonic() > self.next_retry:
                self.next_retry = time.monotonic() + RECONNECT_EVERY
                self.retry_lost()
        self.update_find()
        self._ticks += 1
        if self._ticks % 40 == 0:  # every 2 s
            self.update_disk()
            self.update_summary()
        self.root.after(50, self.tick)

    def quit(self):
        if self.session:
            if not messagebox.askyesno("MultiMic",
                                       "Stop recording and quit?"):
                return
            self.stop()
        self.save()
        self.close_monitor()
        for r in self.rows:
            r.mic.close()
        self.root.destroy()


def run_gui():
    if sys.platform == "win32":
        try:
            import ctypes
            # crisp text on high-DPI screens, and our own taskbar icon
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MultiMic")
        except Exception:
            pass
    root = tk.Tk()
    try:
        import sv_ttk
        sv_ttk.set_theme("dark")
    except Exception:  # theme missing: fall back to a plain dark look
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG,
                        fieldbackground="#2b2b2b")
        style.configure("Card.TFrame", background=BG, relief="solid",
                        borderwidth=1)
    icon = resource_path("multimic.ico")
    if sys.platform == "win32" and os.path.exists(icon):
        root.iconbitmap(default=icon)
    App(root)
    if sys.platform == "win32":
        try:  # dark title bar on Windows 10/11
            import ctypes
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            pass
    root.mainloop()
