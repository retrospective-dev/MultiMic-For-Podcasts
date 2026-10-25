"""A recording: a group of mics started, paused and stopped together."""

import datetime
import os
import time

from .config import BYTES_PER_SAMPLE, FLAC_RATIO, FORMATS
from .mixdown import Mixdown
from .util import fmt_time, safe_name
from .writer import SplitWriter


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


class Session:
    """Starts and stops a group of mics together."""

    def __init__(self, mics, out_root, format_name, mix_mode=None,
                 split_minutes=0, name="", lock_clocks=True):
        self.mics = mics
        self.format_name = format_name
        self.fmt, self.subtype, self.ext = FORMATS[format_name]
        self.mix_mode = mix_mode
        self.split_minutes = split_minutes
        self.lock_clocks = lock_clocks
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
            self.mix = Mixdown(self.mics, self.mix_mode, writer, sr)
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
            mic.begin(writer, self.mix, n - 1, self.lock_clocks)
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
        pre-record buffer does)."""
        if self._final_position is not None:
            return self._final_position
        mic = self.mics[0]
        if not mic._windows:
            return 0.0
        return max(0.0, mic.recorded_seconds)

    @property
    def elapsed(self):
        return self.position

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

    @property
    def backlog(self):
        """Longest disk backlog of any mic, in seconds."""
        return max((m.backlog for m in self.mics), default=0.0)

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
