#!/usr/bin/env python3
"""MultiMic: record several USB microphones at the same time on Windows.

Every selected microphone is recorded to its own file in a timestamped session
folder. All recordings start at the same instant, so the files line up when
dropped into a DAW side by side.

    python multimic.py                 # GUI
    python multimic.py --list          # list input devices
    python multimic.py -d 3 7 9        # record devices 3, 7 and 9 from the
                                       # command line until Ctrl+C
"""

import argparse
import datetime
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
SAMPLE_RATES = ["Device default", "44100", "48000", "96000"]
HOSTAPI_PREFERENCE = ["Windows WASAPI", "MME", "Windows DirectSound"]


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


def rescan_devices():
    # PortAudio only enumerates devices at init, so restart it to pick up
    # microphones plugged in after launch.
    sd._terminate()
    sd._initialize()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "mic"


class Mic:
    """One input device: a running stream for metering plus an optional
    background writer while recording."""

    def __init__(self, index, info, hostapi_name):
        self.index = index
        self.name = info["name"]
        self.info = info
        self.hostapi_name = hostapi_name
        self.stream = None
        self.peak = 0.0
        self.overflows = 0
        self.frames_written = 0
        self.error = None
        self._queue = None
        self._writer = None
        self._file = None
        self._start_at = None
        self._started = False

    @property
    def is_open(self):
        return self.stream is not None

    @property
    def is_recording(self):
        return self._queue is not None

    def open(self, samplerate=None, channels=None):
        default_sr = int(self.info["default_samplerate"])
        samplerate = int(samplerate or default_sr)
        channels = min(channels or 2, self.info["max_input_channels"])
        extra = None
        if "WASAPI" in self.hostapi_name and samplerate != default_sr:
            # Shared-mode WASAPI only accepts the device's mix rate unless
            # PortAudio is allowed to resample.
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except TypeError:
                extra = None
        self.stream = sd.InputStream(
            device=self.index, channels=channels, samplerate=samplerate,
            dtype="float32", latency="high", extra_settings=extra,
            callback=self._callback)
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
        if frames:
            peak = float(np.abs(indata).max())
            if peak > self.peak:
                self.peak = peak

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
            indata = indata[skip:]
            self._started = True
        q.put(indata.copy())

    def begin(self, path, fmt, subtype):
        self.frames_written = 0
        self.overflows = 0
        self.error = None
        self._started = False
        self._start_at = None
        self._file = sf.SoundFile(
            path, "w", samplerate=int(self.stream.samplerate),
            channels=self.stream.channels, format=fmt, subtype=subtype)
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def arm(self):
        self._start_at = self.stream.time

    def _write_loop(self):
        q, f = self._queue, self._file
        try:
            while True:
                block = q.get()
                if block is None:
                    break
                f.write(block)
                self.frames_written += len(block)
        except Exception as e:  # disk full, etc.
            self.error = str(e)
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

    @property
    def seconds(self):
        if self.stream is None:
            return 0.0
        return self.frames_written / self.stream.samplerate


class Session:
    """Starts and stops a group of mics together."""

    def __init__(self, mics, out_root, format_name):
        self.mics = mics
        self.fmt, self.subtype, self.ext = FORMATS[format_name]
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.folder = os.path.join(out_root, f"session_{stamp}")
        self.started = None

    def start(self):
        os.makedirs(self.folder, exist_ok=True)
        used = set()
        for n, mic in enumerate(self.mics, 1):
            base = f"{n:02d}_{safe_name(mic.name)}"
            while base in used:
                base += "_"
            used.add(base)
            mic.begin(os.path.join(self.folder, base + self.ext),
                      self.fmt, self.subtype)
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

    METER_W, METER_H = 220, 14

    class MicRow:
        def __init__(self, app, parent, row, mic):
            self.app = app
            self.mic = mic
            self.var = tk.BooleanVar(value=False)
            self.held = 0.0
            self.cb = ttk.Checkbutton(parent, variable=self.var,
                                      command=self.toggled)
            self.cb.grid(row=row, column=0, padx=4, pady=3)
            ttk.Label(parent, text=f"[{mic.index}] {mic.name}").grid(
                row=row, column=1, sticky="w", padx=4)
            chans = mic.info["max_input_channels"]
            self.ch_var = tk.StringVar(value=str(min(2, chans)))
            self.ch_box = ttk.Combobox(
                parent, width=3, state="readonly", textvariable=self.ch_var,
                values=[str(c) for c in range(1, chans + 1)])
            self.ch_box.grid(row=row, column=2, padx=4)
            self.ch_box.bind("<<ComboboxSelected>>", lambda e: self.reopen())
            self.meter = tk.Canvas(parent, width=METER_W, height=METER_H,
                                   bg="#222", highlightthickness=0)
            self.meter.grid(row=row, column=3, padx=4)
            self.bar = self.meter.create_rectangle(0, 0, 0, METER_H,
                                                   fill="#3c3", width=0)
            self.db = ttk.Label(parent, width=8, anchor="e")
            self.db.grid(row=row, column=4, padx=4)
            self.info = ttk.Label(parent, width=22)
            self.info.grid(row=row, column=5, sticky="w", padx=4)

        def toggled(self):
            if self.var.get():
                self.reopen()
            else:
                self.mic.close()
                self.info.config(text="")

        def reopen(self):
            if not self.var.get() or self.app.session:
                return
            self.mic.close()
            try:
                self.mic.open(self.app.samplerate(), int(self.ch_var.get()))
            except Exception as e:
                self.var.set(False)
                messagebox.showerror("MultiMic",
                                     f"Could not open {self.mic.name}:\n{e}")

        def set_locked(self, locked):
            state = "disabled" if locked else "normal"
            self.cb.config(state=state)
            self.ch_box.config(state="disabled" if locked else "readonly")

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
            sr = int(mic.stream.samplerate)
            text = f"{sr} Hz"
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
            self.rows = []
            root.title("MultiMic")
            root.minsize(760, 300)
            root.protocol("WM_DELETE_WINDOW", self.quit)

            top = ttk.Frame(root, padding=8)
            top.pack(fill="x")
            ttk.Label(top, text="Audio API:").pack(side="left")
            self.api_var = tk.StringVar()
            self.api_box = ttk.Combobox(top, state="readonly", width=22,
                                        textvariable=self.api_var,
                                        values=hostapis())
            self.api_box.current(preferred_hostapi())
            self.api_box.bind("<<ComboboxSelected>>",
                              lambda e: self.build_rows())
            self.api_box.pack(side="left", padx=4)
            ttk.Label(top, text="Sample rate:").pack(side="left", padx=(12, 0))
            self.sr_var = tk.StringVar(value="48000")
            self.sr_box = ttk.Combobox(top, state="readonly", width=14,
                                       textvariable=self.sr_var,
                                       values=SAMPLE_RATES)
            self.sr_box.bind("<<ComboboxSelected>>", lambda e: self.reopen_all())
            self.sr_box.pack(side="left", padx=4)
            self.refresh_btn = ttk.Button(top, text="Rescan devices",
                                          command=self.rescan)
            self.refresh_btn.pack(side="right")

            self.list_frame = ttk.LabelFrame(root, text="Microphones",
                                             padding=6)
            self.list_frame.pack(fill="both", expand=True, padx=8)

            out = ttk.Frame(root, padding=8)
            out.pack(fill="x")
            ttk.Label(out, text="Save to:").pack(side="left")
            default_out = os.path.join(os.path.expanduser("~"), "Music",
                                       "MultiMic")
            self.out_var = tk.StringVar(value=default_out)
            ttk.Entry(out, textvariable=self.out_var).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(out, text="Browse...", command=self.browse).pack(
                side="left")
            ttk.Button(out, text="Open folder", command=self.open_folder).pack(
                side="left", padx=(4, 0))

            bottom = ttk.Frame(root, padding=8)
            bottom.pack(fill="x")
            ttk.Label(bottom, text="Format:").pack(side="left")
            self.fmt_var = tk.StringVar(value="WAV 24-bit")
            self.fmt_box = ttk.Combobox(bottom, state="readonly", width=16,
                                        textvariable=self.fmt_var,
                                        values=list(FORMATS))
            self.fmt_box.pack(side="left", padx=4)
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

            root.bind("<space>", lambda e: self.toggle_record())
            self.build_rows()
            self.tick()

        def samplerate(self):
            v = self.sr_var.get()
            return None if v == "Device default" else int(v)

        def build_rows(self):
            for r in self.rows:
                r.mic.close()
            for w in self.list_frame.winfo_children():
                w.destroy()
            self.rows = []
            api = self.api_box.current()
            api_name = hostapis()[api]
            for col, head in enumerate(["Use", "Device", "Ch", "Level", "",
                                        "Status"]):
                ttk.Label(self.list_frame, text=head,
                          font=("Segoe UI", 9, "bold")).grid(
                    row=0, column=col, sticky="w", padx=4)
            devices = input_devices(api)
            if not devices:
                ttk.Label(self.list_frame,
                          text="No input devices for this audio API.").grid(
                    row=1, column=1, sticky="w")
            for n, (idx, info) in enumerate(devices, 1):
                self.rows.append(MicRow(self, self.list_frame, n,
                                        Mic(idx, info, api_name)))
            self.list_frame.columnconfigure(1, weight=1)

        def reopen_all(self):
            for r in self.rows:
                r.reopen()

        def rescan(self):
            if self.session:
                return
            for r in self.rows:
                r.mic.close()
            rescan_devices()
            self.api_box.config(values=hostapis())
            if self.api_box.current() < 0:
                self.api_box.current(preferred_hostapi())
            self.build_rows()
            self.status.config(text="Device list refreshed.")

        def browse(self):
            d = filedialog.askdirectory(initialdir=self.out_var.get())
            if d:
                self.out_var.set(d)

        def open_folder(self):
            path = self.session.folder if self.session else self.out_var.get()
            os.makedirs(path, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(path)

        def toggle_record(self):
            if self.session:
                self.stop()
            else:
                self.start()

        def start(self):
            mics = [r.mic for r in self.rows if r.mic.is_open]
            if not mics:
                messagebox.showinfo("MultiMic",
                                    "Tick at least one microphone first.")
                return
            session = Session(mics, self.out_var.get(), self.fmt_var.get())
            try:
                session.start()
            except Exception as e:
                session.stop()
                messagebox.showerror("MultiMic", f"Could not start:\n{e}")
                return
            self.session = session
            self.set_locked(True)
            self.rec_btn.config(text="■  Stop", bg="#333",
                                activebackground="#555")
            self.status.config(text=f"Recording {len(mics)} mic(s) to "
                                    f"{session.folder}")

        def stop(self):
            session, self.session = self.session, None
            session.stop()
            self.set_locked(False)
            self.rec_btn.config(text="●  Record", bg="#c22",
                                activebackground="#e33")
            errors = [m for m in session.mics if m.error]
            drops = sum(m.overflows for m in session.mics)
            msg = f"Saved {len(session.mics)} file(s) to {session.folder}"
            if drops:
                msg += f"  ({drops} buffer drop(s) - try fewer mics per USB hub)"
            if errors:
                msg = "Errors: " + "; ".join(f"{m.name}: {m.error}"
                                             for m in errors)
            self.status.config(text=msg)

        def set_locked(self, locked):
            state = "disabled" if locked else "readonly"
            for box in (self.api_box, self.sr_box, self.fmt_box):
                box.config(state=state)
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
    mics = []
    for idx in args.devices:
        if idx not in all_devs:
            sys.exit(f"Device {idx} is not an input on {hostapis()[api]}. "
                     "Use --list.")
        mic = Mic(idx, all_devs[idx], hostapis()[api])
        mic.open(args.samplerate, args.channels)
        mics.append(mic)
    session = Session(mics, args.out, args.format)
    session.start()
    print(f"Recording {len(mics)} mic(s) to {session.folder}. "
          "Press Ctrl+C to stop.")
    try:
        while args.seconds is None or session.elapsed < args.seconds:
            levels = "  ".join(f"[{m.index}] {to_db(m.peak):6.1f}dB"
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
        print(f"\n  {m.name}: {fmt_time(m.seconds)}{note}"
              + (f"  ERROR: {m.error}" if m.error else ""), end="")
    print(f"\nSaved to {session.folder}")


def main():
    p = argparse.ArgumentParser(
        description="Record several USB microphones at once.")
    p.add_argument("--list", action="store_true", help="list input devices")
    p.add_argument("-d", "--devices", type=int, nargs="+",
                   help="device numbers to record (command-line mode)")
    p.add_argument("--api", type=int, help="audio API number "
                   "(default: WASAPI on Windows)")
    p.add_argument("-o", "--out", default=os.path.join(
        os.path.expanduser("~"), "Music", "MultiMic"), help="output folder")
    p.add_argument("-f", "--format", default="WAV 24-bit",
                   choices=list(FORMATS))
    p.add_argument("-r", "--samplerate", type=int, default=48000)
    p.add_argument("-c", "--channels", type=int, default=2,
                   help="channels per mic (capped at what the mic has)")
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
