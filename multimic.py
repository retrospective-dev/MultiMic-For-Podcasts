#!/usr/bin/env python3
"""MultiMic: record several USB microphones at the same time on Windows.

Every selected microphone is recorded to its own file in a timestamped session
folder. All recordings start at the same instant, so the files line up when
dropped into a DAW side by side. Optionally also writes a mixdown, splits long
takes into parts, and lets you listen to any mic through headphones.

    python multimic.py                 # GUI
    python multimic.py --list          # list input devices
    python multimic.py -d 3 7 9        # record devices 3, 7 and 9 from the
                                       # command line until Ctrl+C
"""

import argparse
import collections
import datetime
import json
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

FORMATS = {
    "WAV 24-bit": ("WAV", "PCM_24", ".wav"),
    "WAV 16-bit": ("WAV", "PCM_16", ".wav"),
    "WAV 32-bit float": ("WAV", "FLOAT", ".wav"),
    "FLAC 24-bit": ("FLAC", "PCM_24", ".flac"),
}
BYTES_PER_SAMPLE = {"PCM_16": 2, "PCM_24": 3, "FLOAT": 4}
WAV_MAX_BYTES = 3_900_000_000  # stay clear of the 4 GB RIFF limit
SAMPLE_RATES = ["Device default", "44100", "48000", "96000"]
MIX_MODES = {
    "Off": None,
    "Stereo mix": "stereo",
    "Multitrack (1 ch per mic)": "multitrack",
}
HOSTAPI_PREFERENCE = ["Windows WASAPI", "MME", "Windows DirectSound"]
GAIN_RANGE = 24  # dB, either way


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "MultiMic", "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def hostapis():
    return [h["name"] for h in sd.query_hostapis()]


def preferred_hostapi():
    names = hostapis()
    for want in HOSTAPI_PREFERENCE:
        if want in names:
            return names.index(want)
    return sd.default.hostapi if sd.default.hostapi >= 0 else 0


def input_devices(hostapi):
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == hostapi and d["max_input_channels"] > 0]


def output_devices(hostapi):
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == hostapi and d["max_output_channels"] > 0]


def rescan_devices():
    # PortAudio only enumerates devices at init, so restart it to pick up
    # microphones plugged in after launch.
    sd._terminate()
    sd._initialize()


def wasapi_resample(hostapi_name, samplerate, device_rate):
    # Shared-mode WASAPI only accepts the device's mix rate unless PortAudio
    # is allowed to resample.
    if "WASAPI" in hostapi_name and int(samplerate) != int(device_rate):
        try:
            return sd.WasapiSettings(auto_convert=True)
        except TypeError:
            pass
    return None


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "mic"


class SplitWriter:
    """Sound file writer that rolls over to name_part02, name_part03, ...
    after a set number of frames, and before a WAV would pass 4 GB."""

    def __init__(self, base, ext, samplerate, channels, fmt, subtype,
                 split_frames=None):
        self.base, self.ext = base, ext
        self.args = dict(samplerate=samplerate, channels=channels,
                         format=fmt, subtype=subtype)
        limits = [split_frames] if split_frames else []
        if fmt == "WAV":
            limits.append(WAV_MAX_BYTES
                          // (channels * BYTES_PER_SAMPLE[subtype]))
        self.limit = min(limits) if limits else None
        self.numbered = bool(split_frames)
        self.part = 0
        self.file = None
        self.in_file = 0
        self.paths = []
        self._open()  # open the first file now so errors show at start

    def _open(self):
        self.part += 1
        if self.numbered or self.part > 1:
            path = f"{self.base}_part{self.part:02d}{self.ext}"
        else:
            path = self.base + self.ext
        self.file = sf.SoundFile(path, "w", **self.args)
        self.paths.append(path)
        self.in_file = 0

    def write(self, block):
        while len(block):
            if self.file is None:
                self._open()
            room = len(block)
            if self.limit:
                room = min(room, self.limit - self.in_file)
            self.file.write(block[:room])
            self.in_file += room
            block = block[room:]
            if self.limit and self.in_file >= self.limit:
                self.file.close()
                self.file = None

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


class Monitor:
    """Plays one mic through an output device. Mic and output run on
    different clocks, so the buffer is kept short and trimmed when it
    grows."""

    def __init__(self, device, samplerate, hostapi_name):
        self.lock = threading.Lock()
        self.blocks = collections.deque()
        self.count = 0
        self.max_frames = int(samplerate * 0.2)
        info = sd.query_devices(device)
        self.stream = sd.OutputStream(
            device=device, samplerate=samplerate,
            channels=min(2, info["max_output_channels"]), dtype="float32",
            latency="low", callback=self._callback,
            extra_settings=wasapi_resample(hostapi_name, samplerate,
                                           info["default_samplerate"]))
        self.stream.start()

    def feed(self, data):
        with self.lock:
            self.blocks.append(data)
            self.count += len(data)
            while self.count > self.max_frames and len(self.blocks) > 1:
                self.count -= len(self.blocks.popleft())

    def _callback(self, outdata, frames, time_info, status):
        out_ch = outdata.shape[1]
        pos = 0
        with self.lock:
            while pos < frames and self.blocks:
                block = self.blocks[0]
                n = min(frames - pos, len(block))
                chunk = block[:n]
                if chunk.shape[1] >= out_ch:
                    outdata[pos:pos + n] = chunk[:, :out_ch]
                else:
                    outdata[pos:pos + n] = chunk[:, :1]
                pos += n
                self.count -= n
                if n == len(block):
                    self.blocks.popleft()
                else:
                    self.blocks[0] = block[n:]
        outdata[pos:] = 0

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class Mixdown:
    """Combines the mics into one extra file: either summed to stereo or
    one channel per mic."""

    def __init__(self, n_mics, mode, writer, samplerate):
        self.mode = mode
        self.writer = writer
        self.slack = int(samplerate * 2)
        self.bufs = [collections.deque() for _ in range(n_mics)]
        self.counts = [0] * n_mics
        self.lock = threading.Lock()
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def push(self, slot, mono):
        with self.lock:
            self.bufs[slot].append(mono.astype(np.float32, copy=False))
            self.counts[slot] += len(mono)

    def _take(self, n):
        cols = []
        for i, buf in enumerate(self.bufs):
            parts, need = [], n
            while need and buf:
                a = buf[0]
                if len(a) <= need:
                    parts.append(buf.popleft())
                    need -= len(a)
                else:
                    parts.append(a[:need])
                    buf[0] = a[need:]
                    need = 0
            if need:  # this mic fell behind (unplugged?): fill with silence
                parts.append(np.zeros(need, np.float32))
            self.counts[i] = max(0, self.counts[i] - n)
            cols.append(np.concatenate(parts))
        return np.stack(cols, axis=1)

    def _flush(self, final):
        with self.lock:
            hi = max(self.counts)
            n = hi if final else max(min(self.counts), hi - self.slack)
            if n <= 0:
                return
            frames = self._take(n)
        if self.mode == "stereo":
            mono = np.clip(frames.sum(axis=1, keepdims=True), -1, 1)
            frames = np.repeat(mono, 2, axis=1)
        try:
            if not self.error:
                self.writer.write(frames)
        except Exception as e:
            self.error = str(e)

    def _loop(self):
        while not self._stop.wait(0.2):
            self._flush(final=False)
        self._flush(final=True)
        self.writer.close()

    def close(self):
        self._stop.set()
        self._thread.join()


class Mic:
    """One input device: a running stream for metering and monitoring, plus
    a background writer while recording."""

    def __init__(self, index, info, hostapi_name):
        self.index = index
        self.name = info["name"]
        self.label = self.name
        self.info = info
        self.hostapi_name = hostapi_name
        self.stream = None
        self.gain = 1.0
        self.muted = False
        self.monitor = None
        self.peak = 0.0
        self.overflows = 0
        self.frames_written = 0
        self.error = None
        self._queue = None
        self._writer = None
        self._file = None
        self._mix = None
        self._slot = 0
        self._start_at = None
        self._started = False

    @property
    def is_open(self):
        return self.stream is not None

    @property
    def is_recording(self):
        return self._queue is not None

    def set_gain_db(self, db):
        self.gain = 10 ** (db / 20)

    def open(self, samplerate=None, channels=None):
        default_sr = int(self.info["default_samplerate"])
        samplerate = int(samplerate or default_sr)
        channels = min(channels or 2, self.info["max_input_channels"])
        self.stream = sd.InputStream(
            device=self.index, channels=channels, samplerate=samplerate,
            dtype="float32", latency="high", callback=self._callback,
            extra_settings=wasapi_resample(self.hostapi_name, samplerate,
                                           default_sr))
        self.stream.start()

    def close(self):
        self.stop()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    def _callback(self, indata, frames, time_info, status):
        if status.input_overflow:
            self.overflows += 1
        gain = 0.0 if self.muted else self.gain
        data = indata.copy() if gain == 1.0 else indata * np.float32(gain)
        if frames:
            peak = float(np.abs(data).max())
            if peak > self.peak:
                self.peak = peak
        monitor = self.monitor
        if monitor is not None:
            monitor.feed(data)

        q, start = self._queue, self._start_at
        if q is None or start is None:
            return
        if not self._started:
            # Drop the samples captured before the shared start instant so
            # every mic begins at (nearly) the same sample.
            skip = 0
            adc = time_info.inputBufferAdcTime
            if adc > 0 and 0 < start - adc < 1.0:
                skip = int(round((start - adc) * self.stream.samplerate))
            if skip >= frames:
                return
            data = data[skip:]
            self._started = True
        q.put(data)

    def begin(self, writer, mix=None, slot=0):
        self.frames_written = 0
        self.overflows = 0
        self.error = None
        self._started = False
        self._start_at = None
        self._file = writer
        self._mix, self._slot = mix, slot
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def arm(self):
        self._start_at = self.stream.time

    def _write_loop(self):
        q, f, mix, slot = self._queue, self._file, self._mix, self._slot
        try:
            while True:
                block = q.get()
                if block is None:
                    break
                if self.error:
                    continue  # keep draining so memory doesn't grow
                try:
                    f.write(block)
                except Exception as e:  # disk full, etc.
                    self.error = str(e)
                    continue
                self.frames_written += len(block)
                if mix is not None:
                    mix.push(slot, block.mean(axis=1))
        finally:
            f.close()

    def stop(self):
        self._start_at = None
        q, self._queue = self._queue, None
        if q is not None:
            q.put(None)
            self._writer.join()
            self._writer = None
            self._file = None
            self._mix = None

    @property
    def seconds(self):
        if self.stream is None:
            return 0.0
        return self.frames_written / self.stream.samplerate


class Session:
    """Starts and stops a group of mics together."""

    def __init__(self, mics, out_root, format_name, mix_mode=None,
                 split_minutes=0):
        self.mics = mics
        self.fmt, self.subtype, self.ext = FORMATS[format_name]
        self.mix_mode = mix_mode
        self.split_minutes = split_minutes
        self.mix = None
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.folder = os.path.join(out_root, f"session_{stamp}")
        self.started = None

    def _split_frames(self, samplerate):
        if not self.split_minutes:
            return None
        return int(self.split_minutes * 60 * samplerate)

    def start(self):
        rates = {int(m.stream.samplerate) for m in self.mics}
        if self.mix_mode and len(rates) > 1:
            raise ValueError("Mixdown needs every mic at the same sample "
                             "rate. Pick a fixed sample rate.")
        os.makedirs(self.folder, exist_ok=True)
        if self.mix_mode:
            sr = rates.pop()
            channels = 2 if self.mix_mode == "stereo" else len(self.mics)
            name = "00_mixdown" if self.mix_mode == "stereo" \
                else "00_multitrack"
            writer = SplitWriter(os.path.join(self.folder, name), self.ext,
                                 sr, channels, self.fmt, self.subtype,
                                 self._split_frames(sr))
            self.mix = Mixdown(len(self.mics), self.mix_mode, writer, sr)
        used = set()
        for n, mic in enumerate(self.mics, 1):
            base = f"{n:02d}_{safe_name(mic.label)}"
            while base in used:
                base += "_"
            used.add(base)
            sr = int(mic.stream.samplerate)
            writer = SplitWriter(os.path.join(self.folder, base), self.ext,
                                 sr, mic.stream.channels, self.fmt,
                                 self.subtype, self._split_frames(sr))
            mic.begin(writer, self.mix, n - 1)
        # Streams are already running; arming them back to back makes them
        # all start writing from the same moment.
        for mic in self.mics:
            mic.arm()
        self.started = time.monotonic()

    def stop(self):
        for mic in self.mics:
            mic._start_at = None
        for mic in self.mics:
            mic.stop()
        if self.mix is not None:
            self.mix.close()

    @property
    def errors(self):
        errs = [f"{m.label}: {m.error}" for m in self.mics if m.error]
        if self.mix is not None and self.mix.error:
            errs.append(f"mixdown: {self.mix.error}")
        return errs

    @property
    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0.0


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def to_db(peak):
    return 20 * np.log10(peak) if peak > 1e-6 else -120.0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    METER_W, METER_H = 180, 14

    class MicRow:
        def __init__(self, app, parent, row, mic, saved):
            self.app = app
            self.mic = mic
            self.held = 0.0
            self.var = tk.BooleanVar(value=False)
            self.cb = ttk.Checkbutton(parent, variable=self.var,
                                      command=self.toggled)
            self.cb.grid(row=row, column=0, padx=4, pady=3)

            self.label_var = tk.StringVar(value=saved.get("label", mic.name))
            self.label_entry = ttk.Entry(parent, width=16,
                                         textvariable=self.label_var)
            self.label_entry.grid(row=row, column=1, padx=4, sticky="we")

            dev = f"[{mic.index}] {mic.name}"
            ttk.Label(parent, text=dev if len(dev) < 42 else dev[:40] + "…",
                      foreground="#666").grid(row=row, column=2, sticky="w",
                                              padx=4)

            chans = mic.info["max_input_channels"]
            want = int(saved.get("channels", 2))
            self.ch_var = tk.StringVar(value=str(max(1, min(want, chans))))
            self.ch_box = ttk.Combobox(
                parent, width=3, state="readonly", textvariable=self.ch_var,
                values=[str(c) for c in range(1, chans + 1)])
            self.ch_box.grid(row=row, column=3, padx=4)
            self.ch_box.bind("<<ComboboxSelected>>", lambda e: self.reopen())

            gain_db = float(saved.get("gain_db", 0))
            gain_db = max(-GAIN_RANGE, min(GAIN_RANGE, gain_db))
            self.gain_var = tk.DoubleVar(value=gain_db)
            self.gain = ttk.Scale(parent, from_=-GAIN_RANGE, to=GAIN_RANGE,
                                  length=110, variable=self.gain_var,
                                  command=lambda v: self.gain_changed())
            self.gain.grid(row=row, column=4, padx=(4, 0))
            self.gain.bind("<Double-Button-1>", lambda e: self.reset_gain())
            self.gain_lbl = ttk.Label(parent, width=7)
            self.gain_lbl.grid(row=row, column=5)
            self.gain_changed()

            self.mute_var = tk.BooleanVar(value=bool(saved.get("muted")))
            ttk.Checkbutton(parent, text="Mute", variable=self.mute_var,
                            command=self.mute_changed).grid(row=row,
                                                            column=6, padx=4)
            self.mute_changed()

            self.meter = tk.Canvas(parent, width=METER_W, height=METER_H,
                                   bg="#222", highlightthickness=0)
            self.meter.grid(row=row, column=7, padx=4)
            self.bar = self.meter.create_rectangle(0, 0, 0, METER_H,
                                                   fill="#3c3", width=0)
            self.db = ttk.Label(parent, width=8, anchor="e")
            self.db.grid(row=row, column=8, padx=4)
            self.info = ttk.Label(parent, width=24)
            self.info.grid(row=row, column=9, sticky="w", padx=4)

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
                    "muted": self.mute_var.get()}

        def gain_changed(self):
            db = round(self.gain_var.get())
            self.mic.set_gain_db(db)
            self.gain_lbl.config(text=f"{db:+d} dB")

        def reset_gain(self):
            self.gain_var.set(0)
            self.gain_changed()

        def mute_changed(self):
            self.mic.muted = self.mute_var.get()

        def toggled(self):
            if self.var.get():
                self.reopen()
            else:
                self.mic.close()
                self.info.config(text="")
                self.app.update_monitor()

        def reopen(self, quiet=False):
            if not self.var.get() or self.app.session:
                return
            self.mic.close()
            try:
                self.mic.open(self.app.samplerate(), int(self.ch_var.get()))
            except Exception as e:
                self.var.set(False)
                if quiet:
                    self.app.status.config(
                        text=f"Could not open {self.mic.name}: {e}")
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
            frac = min(1.0, max(0.0, (db + 60) / 60))
            color = "#d33" if db > -1 else "#dc3" if db > -12 else "#3c3"
            self.meter.coords(self.bar, 0, 0, frac * METER_W, METER_H)
            self.meter.itemconfig(self.bar, fill=color)
            self.db.config(text=f"{db:6.1f} dB" if mic.is_open else "")
            if not mic.is_open:
                return
            text = f"{int(mic.stream.samplerate)} Hz"
            if mic.is_recording:
                text += f"  {fmt_time(mic.seconds)}"
            if mic.overflows:
                text += f"  drops:{mic.overflows}"
            if mic.error:
                text = f"ERROR: {mic.error}"
            self.info.config(text=text)

    class App:
        def __init__(self, root):
            self.root = root
            self.session = None
            self.monitor = None
            self.rows = []
            self.outputs = []
            self.settings = load_settings()
            s = self.settings
            root.title("MultiMic")
            root.minsize(1100, 340)
            root.protocol("WM_DELETE_WINDOW", self.quit)

            top = ttk.Frame(root, padding=8)
            top.pack(fill="x")
            ttk.Label(top, text="Audio API:").pack(side="left")
            self.api_var = tk.StringVar()
            self.api_box = ttk.Combobox(top, state="readonly", width=22,
                                        textvariable=self.api_var,
                                        values=hostapis())
            apis = hostapis()
            self.api_box.current(apis.index(s["api"]) if s.get("api") in apis
                                 else preferred_hostapi())
            self.api_box.bind("<<ComboboxSelected>>",
                              lambda e: self.build_rows())
            self.api_box.pack(side="left", padx=4)
            ttk.Label(top, text="Sample rate:").pack(side="left", padx=(12, 0))
            sr = s.get("samplerate", "48000")
            self.sr_var = tk.StringVar(
                value=sr if sr in SAMPLE_RATES else "48000")
            self.sr_box = ttk.Combobox(top, state="readonly", width=14,
                                       textvariable=self.sr_var,
                                       values=SAMPLE_RATES)
            self.sr_box.bind("<<ComboboxSelected>>",
                             lambda e: self.reopen_all())
            self.sr_box.pack(side="left", padx=4)
            self.refresh_btn = ttk.Button(top, text="Rescan devices",
                                          command=self.rescan)
            self.refresh_btn.pack(side="right")

            self.list_frame = ttk.LabelFrame(root, text="Microphones",
                                             padding=6)
            self.list_frame.pack(fill="both", expand=True, padx=8)

            mon = ttk.Frame(root, padding=(8, 8, 8, 0))
            mon.pack(fill="x")
            ttk.Label(mon, text="Listen to:").pack(side="left")
            self.mon_var = tk.StringVar(value="Off")
            self.mon_box = ttk.Combobox(mon, state="readonly", width=24,
                                        textvariable=self.mon_var,
                                        postcommand=self.fill_monitor_list)
            self.mon_box.bind("<<ComboboxSelected>>",
                              lambda e: self.update_monitor())
            self.mon_box.pack(side="left", padx=4)
            ttk.Label(mon, text="through:").pack(side="left", padx=(8, 0))
            self.out_dev_var = tk.StringVar()
            self.out_dev_box = ttk.Combobox(mon, state="readonly", width=40,
                                            textvariable=self.out_dev_var)
            self.out_dev_box.bind("<<ComboboxSelected>>",
                                  lambda e: self.update_monitor())
            self.out_dev_box.pack(side="left", padx=4)

            out = ttk.Frame(root, padding=8)
            out.pack(fill="x")
            ttk.Label(out, text="Save to:").pack(side="left")
            default_out = os.path.join(os.path.expanduser("~"), "Music",
                                       "MultiMic")
            self.out_var = tk.StringVar(value=s.get("out", default_out))
            ttk.Entry(out, textvariable=self.out_var).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(out, text="Browse...", command=self.browse).pack(
                side="left")
            ttk.Button(out, text="Open folder", command=self.open_folder).pack(
                side="left", padx=(4, 0))

            bottom = ttk.Frame(root, padding=8)
            bottom.pack(fill="x")
            ttk.Label(bottom, text="Format:").pack(side="left")
            fmt = s.get("format", "WAV 24-bit")
            self.fmt_var = tk.StringVar(
                value=fmt if fmt in FORMATS else "WAV 24-bit")
            self.fmt_box = ttk.Combobox(bottom, state="readonly", width=16,
                                        textvariable=self.fmt_var,
                                        values=list(FORMATS))
            self.fmt_box.pack(side="left", padx=4)
            ttk.Label(bottom, text="Mixdown:").pack(side="left", padx=(12, 0))
            mix = s.get("mixdown", "Off")
            self.mix_var = tk.StringVar(value=mix if mix in MIX_MODES
                                        else "Off")
            self.mix_box = ttk.Combobox(bottom, state="readonly", width=24,
                                        textvariable=self.mix_var,
                                        values=list(MIX_MODES))
            self.mix_box.pack(side="left", padx=4)
            ttk.Label(bottom, text="New file every").pack(side="left",
                                                          padx=(12, 0))
            self.split_var = tk.StringVar(value=str(s.get("split_minutes", 0)))
            self.split_box = ttk.Spinbox(bottom, from_=0, to=600, increment=5,
                                         width=5, textvariable=self.split_var)
            self.split_box.pack(side="left", padx=4)
            ttk.Label(bottom, text="min (0 = off)").pack(side="left")

            self.rec_btn = tk.Button(bottom, text="●  Record", width=14,
                                     bg="#c22", fg="white",
                                     activebackground="#e33",
                                     font=("Segoe UI", 11, "bold"),
                                     command=self.toggle_record)
            self.rec_btn.pack(side="right")
            self.clock = ttk.Label(bottom, text="00:00:00",
                                   font=("Consolas", 16))
            self.clock.pack(side="right", padx=12)
            self.status = ttk.Label(root, text="Tick the microphones to use.",
                                    padding=(8, 0, 8, 8))
            self.status.pack(fill="x")

            root.bind("<space>", self.space)
            self.build_rows()
            self.tick()

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
            heads = ["Use", "Name", "Device", "Ch", "Gain", "", "", "Level",
                     "", "Status"]
            for col, head in enumerate(heads):
                ttk.Label(self.list_frame, text=head,
                          font=("Segoe UI", 9, "bold")).grid(
                    row=0, column=col, sticky="w", padx=4)
            devices = input_devices(api)
            if not devices:
                ttk.Label(self.list_frame,
                          text="No input devices for this audio API.").grid(
                    row=1, column=1, columnspan=3, sticky="w")
            saved = self.settings.get("mics", {})
            for n, (idx, info) in enumerate(devices, 1):
                self.rows.append(MicRow(self, self.list_frame, n,
                                        Mic(idx, info, api_name),
                                        saved.get(info["name"], {})))
            self.list_frame.columnconfigure(1, weight=1)

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
            self.status.config(text="Device list refreshed.")

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
                self.status.config(text=f"Could not start monitoring: {e}")
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

        def space(self, event):
            # let the space bar type in text fields
            if isinstance(event.widget, (tk.Entry, ttk.Entry)):
                return
            self.toggle_record()

        def toggle_record(self):
            if self.session:
                self.stop()
            else:
                self.start()

        def start(self):
            rows = [r for r in self.rows if r.mic.is_open]
            if not rows:
                messagebox.showinfo("MultiMic",
                                    "Tick at least one microphone first.")
                return
            for r in rows:
                r.mic.label = r.label
            session = Session([r.mic for r in rows], self.out_var.get(),
                              self.fmt_var.get(),
                              MIX_MODES[self.mix_var.get()],
                              self.split_minutes())
            try:
                session.start()
            except Exception as e:
                session.stop()
                messagebox.showerror("MultiMic", f"Could not start:\n{e}")
                return
            self.session = session
            self.save()
            self.set_locked(True)
            self.rec_btn.config(text="■  Stop", bg="#333",
                                activebackground="#555")
            self.status.config(text=f"Recording {len(rows)} mic(s) to "
                                    f"{session.folder}")

        def stop(self):
            session, self.session = self.session, None
            session.stop()
            self.set_locked(False)
            self.rec_btn.config(text="●  Record", bg="#c22",
                                activebackground="#e33")
            drops = sum(m.overflows for m in session.mics)
            msg = f"Saved {len(session.mics)} mic(s) to {session.folder}"
            if session.mix is not None:
                msg += " (+ mixdown)"
            if drops:
                msg += f"  ({drops} buffer drop(s) - try fewer mics per USB hub)"
            if session.errors:
                msg = "Errors: " + "; ".join(session.errors)
            self.status.config(text=msg)

        def set_locked(self, locked):
            state = "disabled" if locked else "readonly"
            for box in (self.api_box, self.sr_box, self.fmt_box,
                        self.mix_box):
                box.config(state=state)
            self.split_box.config(state="disabled" if locked else "normal")
            self.refresh_btn.config(state="disabled" if locked else "normal")
            for r in self.rows:
                r.set_locked(locked)

        def tick(self):
            for r in self.rows:
                r.update()
            if self.session:
                self.clock.config(text=fmt_time(self.session.elapsed))
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

    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def list_devices(api):
    print(f"Input devices ({hostapis()[api]}):")
    for idx, info in input_devices(api):
        print(f"  [{idx:3d}] {info['name']}  "
              f"({info['max_input_channels']} ch, "
              f"{int(info['default_samplerate'])} Hz)")
    print("\nOther audio APIs: " + ", ".join(
        f"{n}={i}" for i, n in enumerate(hostapis())))


def run_cli(args, api):
    all_devs = dict(input_devices(api))
    if args.names and len(args.names) != len(args.devices):
        sys.exit("--names needs one name per device.")
    mics = []
    for n, idx in enumerate(args.devices):
        if idx not in all_devs:
            sys.exit(f"Device {idx} is not an input on {hostapis()[api]}. "
                     "Use --list.")
        mic = Mic(idx, all_devs[idx], hostapis()[api])
        if args.names:
            mic.label = args.names[n]
        mic.open(args.samplerate, args.channels)
        mics.append(mic)
    session = Session(mics, args.out, args.format,
                      None if args.mix == "off" else args.mix, args.split)
    session.start()
    print(f"Recording {len(mics)} mic(s) to {session.folder}. "
          "Press Ctrl+C to stop.")
    try:
        while args.seconds is None or session.elapsed < args.seconds:
            levels = "  ".join(f"{m.label[:12]} {to_db(m.peak):6.1f}dB"
                               for m in mics)
            for m in mics:
                m.peak = 0.0
            print(f"\r{fmt_time(session.elapsed)}  {levels}  ", end="",
                  flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    session.stop()
    for m in mics:
        m.close()
        note = f", {m.overflows} drop(s)" if m.overflows else ""
        print(f"\n  {m.label}: {fmt_time(m.seconds)}{note}", end="")
    for err in session.errors:
        print(f"\n  ERROR {err}", end="")
    print(f"\nSaved to {session.folder}")


def main():
    p = argparse.ArgumentParser(
        description="Record several USB microphones at once.")
    p.add_argument("--list", action="store_true", help="list input devices")
    p.add_argument("-d", "--devices", type=int, nargs="+",
                   help="device numbers to record (command-line mode)")
    p.add_argument("-n", "--names", nargs="+",
                   help="a name per device, used for the file names")
    p.add_argument("--api", type=int, help="audio API number "
                   "(default: WASAPI on Windows)")
    p.add_argument("-o", "--out", default=os.path.join(
        os.path.expanduser("~"), "Music", "MultiMic"), help="output folder")
    p.add_argument("-f", "--format", default="WAV 24-bit",
                   choices=list(FORMATS))
    p.add_argument("-r", "--samplerate", type=int, default=48000)
    p.add_argument("-c", "--channels", type=int, default=2,
                   help="channels per mic (capped at what the mic has)")
    p.add_argument("-m", "--mix", default="off",
                   choices=["off", "stereo", "multitrack"],
                   help="also write a mixdown file")
    p.add_argument("-s", "--split", type=float, default=0,
                   help="start a new file every N minutes")
    p.add_argument("-t", "--seconds", type=float,
                   help="stop automatically after this many seconds")
    args = p.parse_args()
    api = preferred_hostapi() if args.api is None else args.api

    if args.list:
        list_devices(api)
    elif args.devices:
        run_cli(args, api)
    else:
        run_gui()


if __name__ == "__main__":
    main()
