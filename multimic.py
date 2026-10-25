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
import shutil
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
CLIP_LEVEL = 0.999
NO_SIGNAL_AFTER = 1.5  # seconds without audio callbacks
DISK_STOP_BYTES = 200_000_000  # stop recording before the disk is full
FLAC_RATIO = 0.6  # rough FLAC size vs. PCM, for time-left estimates


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


def data_rate(mics, format_name, mix_mode=None):
    """Bytes per second a recording of these (open) mics will write."""
    fmt, subtype, _ = FORMATS[format_name]
    width = BYTES_PER_SAMPLE[subtype]
    rate = sum(m.stream.samplerate * m.stream.channels * width
               for m in mics if m.stream is not None)
    if mix_mode and mics and mics[0].stream is not None:
        ch = 2 if mix_mode == "stereo" else len(mics)
        rate += mics[0].stream.samplerate * ch * width
    return rate * (FLAC_RATIO if fmt == "FLAC" else 1)


def disk_free(path):
    """Free bytes on the drive holding path (or its nearest existing
    parent), or None if it can't be found."""
    path = os.path.abspath(path)
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


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
        self.clips = 0
        self.last_audio = 0.0
        self.frames_written = 0
        self.error = None
        self._queue = None
        self._writer = None
        self._file = None
        self._mix = None
        self._slot = 0
        # Recording windows [start, end) on the stream clock; end is None
        # while recording. Every mic gets the same windows, so start, pause,
        # resume and stop land on the same instant in every file.
        self._windows = []
        self._last_block_end = 0.0
        # Recent audio kept for the pre-record buffer.
        self.ring_seconds = 0.0
        self._ring = collections.deque()
        self._ring_frames = 0
        self._lock = threading.Lock()

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
        self.last_audio = time.monotonic()
        self.stream.start()

    @property
    def no_signal(self):
        """True when the device has stopped delivering audio, which is
        what an unplugged USB mic looks like."""
        if self.stream is None:
            return False
        if not self.stream.active:
            return True
        return time.monotonic() - self.last_audio > NO_SIGNAL_AFTER

    def close(self):
        self.stop()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    @property
    def recording_now(self):
        return bool(self._windows) and self._windows[-1][1] is None

    def _block_time(self, frames, time_info):
        """Stream time of the block's first sample. Some drivers don't
        report it, so estimate it from the callback time and latency."""
        now = getattr(time_info, "currentTime", 0) or self.stream.time
        adc = time_info.inputBufferAdcTime
        if adc > 0 and 0 <= now - adc < 1.0:
            return adc
        latency = getattr(self.stream, "latency", 0) or 0
        return now - frames / self.stream.samplerate - latency

    def _clip(self, adc, data):
        """The part of a block that falls inside the recording windows."""
        sr, n = self.stream.samplerate, len(data)
        parts = []
        for lo, hi in self._windows:
            a = min(n, max(0, int(round((lo - adc) * sr))))
            b = n if hi is None else min(n, max(0, int(round((hi - adc) * sr))))
            if b > a:
                parts.append(data[a:b])
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _callback(self, indata, frames, time_info, status):
        self.last_audio = time.monotonic()
        if status.input_overflow:
            self.overflows += 1
        gain = 0.0 if self.muted else self.gain
        data = indata.copy() if gain == 1.0 else indata * np.float32(gain)
        if frames:
            peak = float(np.abs(data).max())
            if peak > self.peak:
                self.peak = peak
            if peak >= CLIP_LEVEL and self.recording_now:
                self.clips += 1
        monitor = self.monitor
        if monitor is not None:
            monitor.feed(data)

        adc = self._block_time(frames, time_info)
        with self._lock:
            if self.ring_seconds > 0:
                self._ring.append((adc, data))
                self._ring_frames += frames
                keep = self.ring_seconds * self.stream.samplerate + frames
                while self._ring_frames - len(self._ring[0][1]) >= keep:
                    self._ring_frames -= len(self._ring.popleft()[1])
            elif self._ring:
                self._ring.clear()
                self._ring_frames = 0
            q = self._queue
            if q is not None and self._windows:
                chunk = self._clip(adc, data)
                if chunk is not None:
                    q.put(chunk)
            self._last_block_end = adc + frames / self.stream.samplerate

    def begin(self, writer, mix=None, slot=0):
        self.frames_written = 0
        self.overflows = 0
        self.clips = 0
        self.error = None
        self._windows = []
        self._file = writer
        self._mix, self._slot = mix, slot
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def arm(self, preroll=0.0):
        """Start recording now, or preroll seconds ago from the
        pre-record buffer (padded with silence if it holds less)."""
        with self._lock:
            t = self.stream.time
            lo = t - preroll
            self._windows = [[lo, None]]
            if preroll > 0:
                sr = self.stream.samplerate
                first = self._ring[0][0] if self._ring else t
                if first > lo:
                    pad = int(round((first - lo) * sr))
                    self._queue.put(np.zeros((pad, self.stream.channels),
                                             np.float32))
                for adc, data in self._ring:
                    chunk = self._clip(adc, data)
                    if chunk is not None:
                        self._queue.put(chunk)
        return t

    def pause(self):
        with self._lock:
            if self.recording_now:
                self._windows[-1][1] = self.stream.time

    def resume(self):
        with self._lock:
            if self._windows and not self.recording_now:
                now = self.stream.time
                # blocks older than a couple of seconds never arrive late
                self._windows = [w for w in self._windows if w[1] > now - 2]
                self._windows.append([now, None])

    @property
    def caught_up(self):
        """True once every block up to the last pause has arrived."""
        if not self._windows or self.recording_now:
            return True
        return self._last_block_end >= self._windows[-1][1] or self.no_signal

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
        with self._lock:
            self._windows = []
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
                 split_minutes=0, name=""):
        self.mics = mics
        self.format_name = format_name
        self.fmt, self.subtype, self.ext = FORMATS[format_name]
        self.mix_mode = mix_mode
        self.split_minutes = split_minutes
        self.mix = None
        self.markers = []
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder = f"session_{stamp}"
        if name.strip():
            folder += "_" + safe_name(name.strip())
        self.folder = os.path.join(out_root, folder)
        self.started = None
        self.preroll = 0.0
        self._final_position = None

    def _split_frames(self, samplerate):
        if not self.split_minutes:
            return None
        return int(self.split_minutes * 60 * samplerate)

    def start(self, preroll=0.0):
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
        self.preroll = preroll
        for mic in self.mics:
            mic.arm(preroll)
        self.started = time.monotonic()

    @property
    def paused(self):
        return not self.mics[0].recording_now

    def pause(self):
        for mic in self.mics:
            mic.pause()

    def resume(self):
        for mic in self.mics:
            mic.resume()

    @property
    def position(self):
        """Seconds of audio recorded so far (pauses don't count, the
        pre-record buffer does), on the first mic's clock."""
        if self._final_position is not None:
            return self._final_position
        mic = self.mics[0]
        if not mic._windows or mic.stream is None:
            return 0.0
        now = mic.stream.time
        return max(0.0, sum((now if hi is None else hi) - lo
                            for lo, hi in mic._windows))

    def add_marker(self, label=None):
        t = self.position
        label = label or f"Marker {len(self.markers) + 1}"
        self.markers.append((t, label))
        self._write_markers()
        return t, label

    def _write_markers(self):
        # Audacity label track (also imports into Reaper and others) plus a
        # spreadsheet-friendly CSV that knows about split files.
        split = self.split_minutes * 60
        try:
            with open(os.path.join(self.folder, "markers.txt"), "w",
                      encoding="utf-8") as f:
                for t, label in self.markers:
                    f.write(f"{t:.6f}\t{t:.6f}\t{label}\n")
            with open(os.path.join(self.folder, "markers.csv"), "w",
                      encoding="utf-8") as f:
                f.write("number,time,seconds,part,time_in_part,label\n")
                for n, (t, label) in enumerate(self.markers, 1):
                    part = int(t // split) + 1 if split else 1
                    in_part = t - (part - 1) * split if split else t
                    label = '"' + label.replace('"', '""') + '"'
                    f.write(f"{n},{fmt_time(t, True)},{t:.3f},{part},"
                            f"{fmt_time(in_part, True)},{label}\n")
        except OSError:
            pass

    @property
    def data_rate(self):
        return data_rate(self.mics, self.format_name, self.mix_mode)

    def stop(self):
        # End every file at the same instant, then give blocks captured
        # before it (still in the drivers' buffers) a moment to arrive.
        self.pause()
        deadline = time.monotonic() + 1.0
        while not all(m.caught_up for m in self.mics) \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        self._final_position = self.position
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
        return self.position


def fmt_time(seconds, millis=False):
    s = int(seconds)
    text = f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    if millis:
        text += f".{int(round((seconds - s) * 1000)) % 1000:03d}"
    return text


def fmt_duration(seconds):
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{max(0, seconds) / 60:.0f} min"


def to_db(peak):
    return 20 * np.log10(peak) if peak > 1e-6 else -120.0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def resource_path(name):
    """A file shipped next to the script, or inside the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont

    BG = "#1c1c1c"
    FG = "#fafafa"
    DIM = "#9a9a9a"
    FAINT = "#5a5a5a"
    RED = "#e5484d"
    RED_HOT = "#ff6369"
    AMBER = "#ffb224"
    GREEN = "#3ddc84"
    YELLOW = "#ffd23f"
    WARN = "#ff6b6b"

    def blend(color, into, amount):
        a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(into[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x + (y - x) * amount):02x}"
                             for x, y in zip(a, b))

    class Meter(tk.Canvas):
        """Segmented LED level meter with a peak-hold marker."""

        SEG, GAP, FLOOR = 5, 2, -60.0

        def __init__(self, parent, height=16):
            super().__init__(parent, height=height, bg=BG,
                             highlightthickness=0, width=200)
            self.lit_ids, self.colors = [], []
            self.lit = -1
            self.peak_db = self.FLOOR
            self.peak_time = 0.0
            self.bind("<Configure>", lambda e: self._draw())

        def _color(self, i, n):
            db = self.FLOOR * (1 - (i + 0.5) / n)
            return RED if db > -3 else YELLOW if db > -12 else GREEN

        def _draw(self):
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            n = max(10, (w + self.GAP) // (self.SEG + self.GAP))
            self.lit_ids, self.colors = [], []
            for i in range(n):
                x = i * (self.SEG + self.GAP)
                c = self._color(i, n)
                self.create_rectangle(x, 0, x + self.SEG, h, width=0,
                                      fill=blend(c, BG, 0.82))
                self.lit_ids.append(self.create_rectangle(
                    x, 0, x + self.SEG, h, width=0, fill=c, state="hidden"))
                self.colors.append(c)
            self.peak_id = self.create_rectangle(0, 0, 0, h, width=0,
                                                 fill=FG, state="hidden")
            self.lit = 0

        def set(self, db, active=True):
            n = len(self.lit_ids)
            if not n:
                return
            frac = min(1.0, max(0.0, (db - self.FLOOR) / -self.FLOOR))
            lit = int(round(frac * n)) if active else 0
            if lit != self.lit:
                lo, hi = sorted((lit, self.lit))
                for i in range(lo, hi):
                    self.itemconfig(self.lit_ids[i],
                                    state="normal" if i < lit else "hidden")
                self.lit = lit
            now = time.monotonic()
            if db >= self.peak_db or now - self.peak_time > 1.5:
                self.peak_db = db
                self.peak_time = now
            pfrac = (self.peak_db - self.FLOOR) / -self.FLOOR
            i = min(n - 1, int(pfrac * n))
            if active and pfrac > 0.02:
                x = i * (self.SEG + self.GAP)
                self.coords(self.peak_id, x, 0, x + self.SEG,
                            self.winfo_height())
                self.itemconfig(self.peak_id, state="normal",
                                fill=blend(self.colors[i], "#ffffff", 0.5))
            else:
                self.itemconfig(self.peak_id, state="hidden")

    class RecordButton(tk.Canvas):
        """Big round record / stop button."""

        def __init__(self, parent, command, size=64):
            super().__init__(parent, width=size, height=size, bg=BG,
                             highlightthickness=0, cursor="hand2")
            self.size, self.command = size, command
            self.recording = self.hover = False
            self.pulse = 0
            self.bind("<Button-1>", lambda e: self.command())
            self.bind("<Enter>", lambda e: self._set_hover(True))
            self.bind("<Leave>", lambda e: self._set_hover(False))
            self._draw()

        def _set_hover(self, on):
            self.hover = on
            self._draw()

        def set_recording(self, on):
            self.recording = on
            self._draw()

        def tick(self, paused):
            if self.recording:
                self.pulse = (self.pulse + 1) % 40
                self._draw(paused)

        def _draw(self, paused=False):
            self.delete("all")
            s, pad = self.size, 3
            ring = "#3a3a3a"
            if self.recording:
                glow = abs(20 - self.pulse) / 20
                ring = AMBER if paused else blend(RED, "#3a3a3a", glow * 0.7)
            self.create_oval(pad, pad, s - pad, s - pad, outline=ring,
                             width=3)
            fill = RED_HOT if self.hover else RED
            if self.recording:
                q = s * 0.33
                self._round_rect(q, q, s - q, s - q, 5, fill)
            else:
                q = s * 0.24
                self.create_oval(q, q, s - q, s - q, fill=fill, width=0)

        def _round_rect(self, x1, y1, x2, y2, r, fill):
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
                   x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
                   x1, y1 + r, x1, y1]
            self.create_polygon(pts, smooth=True, fill=fill, width=0)

    class ScrollFrame(ttk.Frame):
        """A vertically scrolling container for the mic cards."""

        def __init__(self, parent):
            super().__init__(parent)
            self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
            self.bar = ttk.Scrollbar(self, orient="vertical",
                                     command=self.canvas.yview)
            self.inner = ttk.Frame(self.canvas)
            self.win = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
            self.canvas.configure(yscrollcommand=self.bar.set)
            self.canvas.pack(side="left", fill="both", expand=True)
            self.inner.bind("<Configure>", lambda e: self._refresh())
            self.canvas.bind("<Configure>", lambda e: (
                self.canvas.itemconfig(self.win, width=e.width),
                self._refresh()))
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", self._wheel)
            self.canvas.bind_all("<Button-5>", self._wheel)

        def _refresh(self):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            needed = self.inner.winfo_reqheight() > self.canvas.winfo_height()
            if needed and not self.bar.winfo_ismapped():
                self.bar.pack(side="right", fill="y", before=self.canvas)
            elif not needed and self.bar.winfo_ismapped():
                self.bar.pack_forget()
                self.canvas.yview_moveto(0)

        def _wheel(self, event):
            w = event.widget
            while w is not None and w is not self:
                w = getattr(w, "master", None)
            if w is None or self.inner.winfo_reqheight() <= \
                    self.canvas.winfo_height():
                return
            if getattr(event, "num", 0) == 4:
                step = -1
            elif getattr(event, "num", 0) == 5:
                step = 1
            else:
                step = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(step * 2, "units")

    class MicRow:
        """One microphone card."""

        def __init__(self, app, parent, row, mic, saved):
            self.app = app
            self.mic = mic
            self.held = 0.0
            card = self.card = ttk.Frame(parent, style="Card.TFrame",
                                         padding=(14, 10, 14, 12))
            card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
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
                return
            self.db.config(text="muted" if mic.muted else
                           ("-inf dB" if db <= -120 else f"{db:.1f} dB"))
            parts = [f"{int(mic.stream.samplerate) // 1000} kHz",
                     f"{mic.stream.channels} ch"]
            warn = False
            if mic.is_recording:
                parts.append(fmt_time(mic.seconds))
            if mic.clips:
                parts.append(f"CLIP ×{mic.clips}")
                warn = True
            if mic.overflows:
                parts.append(f"{mic.overflows} drops")
                warn = True
            text = "  ·  ".join(parts)
            if mic.no_signal:
                text, warn = "No signal - unplugged?", True
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
            s = self.settings
            root.title("MultiMic")
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
            self.marker_btn.pack(side="left", padx=(0, 16))
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
                              self.split_minutes(), self.name_var.get())
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
                      self.preroll_box, self.refresh_btn):
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
                      None if args.mix == "off" else args.mix, args.split,
                      args.name or "")
    session.start()
    try:
        import msvcrt  # Windows: read single key presses for markers
    except ImportError:
        msvcrt = None
    print(f"Recording {len(mics)} mic(s) to {session.folder}. "
          + ("Press M for a marker, " if msvcrt else "")
          + "Ctrl+C to stop.")
    try:
        while args.seconds is None or session.elapsed < args.seconds:
            while msvcrt and msvcrt.kbhit():
                if msvcrt.getwch().lower() == "m":
                    t, label = session.add_marker()
                    print(f"\n{label} at {fmt_time(t, True)}")
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
    p.add_argument("--name", help="session name, added to the folder name")
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
