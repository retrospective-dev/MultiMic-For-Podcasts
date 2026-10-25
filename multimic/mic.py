"""One microphone: its audio stream, meters and recording pipeline."""

import collections
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from . import config
from .clock import ClockLock, RateEstimator
from .devices import wasapi_resample


class Mic:
    """One input device: a running stream for metering and monitoring, plus
    a background writer while recording.

    Audio is cut into recording windows [start, end) on the shared stream
    clock, so start, pause, resume and stop land on the same instant in
    every file. The writer thread keeps the output locked to that clock
    (see clock.py), which also covers dropouts and unplugged mics."""

    def __init__(self, index, info, hostapi_name):
        self.index = index
        self.name = info["name"]
        self.label = self.name
        self.info = info
        self.hostapi_name = hostapi_name
        self.stream = None
        self.gain = 1.0
        self.muted = False
        self.pan = 0.0
        self.monitor = None
        self.peak = 0.0
        self.overflows = 0
        self.clips = 0
        self.last_audio = 0.0
        self.frames_written = 0
        self.error = None
        self.reconnects = 0
        self.estimator = None
        self._reset_timeline()
        self._clock_offset = 0.0
        self._queue = None
        self._queued = 0  # frames waiting for the writer thread
        self._writer = None
        self._file = None
        self._clock = None
        self._mix = None
        self._slot = 0
        # Recording windows [start, end) on the stream clock; end is None
        # while recording. Every mic gets the same windows.
        self._windows = []
        self._pruned = 0.0  # recorded seconds in windows already dropped
        self._last_block_end = 0.0
        # Recent audio kept for the pre-record buffer.
        self.ring_seconds = 0.0
        self._ring = collections.deque()
        self._ring_frames = 0
        self._lock = threading.Lock()

    # -- stream ---------------------------------------------------------------

    def _reset_timeline(self):
        self._frames_in = 0
        self._frame_offset = 0  # samples known to be lost (see _callback)
        self._held = []  # blocks waiting to see whether there was a gap
        self._held_frames = 0
        self._watch = None  # residuals measured after an overrun

    @property
    def is_open(self):
        return self.stream is not None

    @property
    def is_recording(self):
        return self._queue is not None

    @property
    def samplerate(self):
        return self.stream.samplerate

    def set_gain_db(self, db):
        self.gain = 10 ** (db / 20)

    def _new_stream(self, samplerate, channels):
        return sd.InputStream(
            device=self.index, channels=channels, samplerate=samplerate,
            dtype="float32", latency="high", callback=self._callback,
            extra_settings=wasapi_resample(self.hostapi_name, samplerate,
                                           self.info["default_samplerate"]))

    def open(self, samplerate=None, channels=None):
        samplerate = int(samplerate or self.info["default_samplerate"])
        channels = min(channels or 2, self.info["max_input_channels"])
        self.stream = self._new_stream(samplerate, channels)
        self.estimator = RateEstimator(samplerate)
        self._reset_timeline()
        self.last_audio = time.monotonic()
        self.stream.start()

    def restart_stream(self, index=None, info=None):
        """Replace the audio stream (after an unplug) without disturbing a
        recording in progress. Raises if the device can't be opened."""
        old = self.stream
        try:
            old.abort()
            old.close()
        except Exception:
            pass
        if index is not None:
            self.index, self.info = index, info
        new = self._new_stream(int(old.samplerate), old.channels)
        with self._lock:
            self.stream = new
            self.estimator = RateEstimator(int(old.samplerate))
            self._reset_timeline()
        self.last_audio = time.monotonic()
        new.start()
        self.reconnects += 1

    def close(self):
        self.stop()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            finally:
                self.stream = None

    def now(self):
        """Current time on the shared stream clock, even if the device has
        gone away."""
        stream = self.stream
        if stream is not None:
            try:
                t = stream.time
                self._clock_offset = t - time.perf_counter()
                return t
            except Exception:
                pass
        return time.perf_counter() + self._clock_offset

    @property
    def no_signal(self):
        """True when the device has stopped delivering audio, which is
        what an unplugged USB mic looks like."""
        if self.stream is None:
            return False
        try:
            if not self.stream.active:
                return True
        except Exception:
            return True
        return time.monotonic() - self.last_audio > config.NO_SIGNAL_AFTER

    @property
    def drift_ppm(self):
        return self.estimator.ppm if self.estimator else 0.0

    @property
    def backlog(self):
        """Seconds of audio waiting to be written to disk."""
        if self.stream is None:
            return 0.0
        return self._queued / self.stream.samplerate

    @property
    def gap_seconds(self):
        """Silence put into the file because the mic went missing."""
        if self._clock is None or self.stream is None:
            return 0.0
        return self._clock.filled / self.stream.samplerate

    # -- capture --------------------------------------------------------------

    @property
    def recording_now(self):
        return bool(self._windows) and self._windows[-1][1] is None

    def _block_time(self, frames, time_info):
        """Stream time of the block's first sample. Some drivers don't
        report it, so estimate it from the callback time and latency."""
        now = getattr(time_info, "currentTime", 0) or self.now()
        adc = time_info.inputBufferAdcTime
        if adc > 0 and 0 <= now - adc < 1.0:
            return adc
        latency = getattr(self.stream, "latency", 0) or 0
        return now - frames / self.stream.samplerate - latency

    def _clip(self, adc, data, rate):
        """The parts of a block inside the recording windows, each with the
        stream time of its end."""
        n = len(data)
        parts = []
        for lo, hi in self._windows:
            a = min(n, max(0, int(round((lo - adc) * rate))))
            b = n if hi is None else min(n, max(0, int(round((hi - adc)
                                                              * rate))))
            if b > a:
                parts.append((data[a:b], adc + b / rate))
        return parts

    def _recorded_until(self, t):
        """Seconds of recording covered up to stream time t."""
        total = self._pruned
        for lo, hi in self._windows:
            end = t if hi is None else min(t, hi)
            if end > lo:
                total += end - lo
        return total

    def _enqueue(self, data, t_end):
        self._queue.put((data, self._recorded_until(t_end),
                         self.estimator.rate if self.estimator
                         else self.stream.samplerate))
        self._queued += len(data)

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
            if peak >= config.CLIP_LEVEL and self.recording_now:
                self.clips += 1
        monitor = self.monitor
        if monitor is not None:
            monitor.feed(data)

        raw = self._block_time(frames, time_info)
        with self._lock:
            if self.estimator is None:
                self.estimator = RateEstimator(self.stream.samplerate)
                self._reset_timeline()
            self._timeline(raw, data, frames, status.input_overflow)

    def _timeline(self, raw, data, frames, overflow):
        """Places a block on the device's fitted timeline. Raw timestamps
        jitter with how promptly the computer runs the callback, so the
        fit is used instead, and only a lasting jump counts as lost
        samples."""
        est = self.estimator
        sr = self.stream.samplerate
        resid = 0.0
        if est.n:
            resid = raw - est.time_at(self._frames_in + self._frame_offset)
        if est.n and resid > config.JUMP:
            self._held.append((raw, data, self._frames_in, resid))
            self._held_frames += frames
            self._frames_in += frames
            if self._held_frames >= config.JUMP_CONFIRM * sr:
                # samples really went missing: shift the timeline by the
                # gap so the audio after it lands at the right time
                gap = sorted(h[3] for h in self._held)[len(self._held) // 2]
                self._frame_offset += int(round(gap * est.rate))
                self._release_held()
            return
        self._release_held()
        if overflow:
            self._watch = [0.0, 0, 0]
        elif self._watch is not None:
            # after an overrun, measure how far the audio now sits from
            # the line for a second and shift by that
            w = self._watch
            w[0] += resid
            w[1] += 1
            w[2] += frames
            if w[2] >= sr:
                mean = w[0] / w[1]
                if abs(mean) > config.OVERFLOW_SHIFT:
                    self._frame_offset += int(round(mean * est.rate))
                self._watch = None
        self._place(raw, data, frames)

    def _release_held(self):
        held, self._held, self._held_frames = self._held, [], 0
        frames_in = self._frames_in
        for raw, data, f, _ in held:
            self._frames_in = f
            self._place(raw, data, len(data))
        self._frames_in = frames_in

    def _place(self, raw, data, frames):
        est = self.estimator
        f = self._frames_in + self._frame_offset
        est.add(raw, f)
        adc = est.time_at(f)
        self._frames_in += frames
        sr = self.stream.samplerate
        if self.ring_seconds > 0:
            self._ring.append((adc, data))
            self._ring_frames += frames
            keep = self.ring_seconds * sr + frames
            while self._ring_frames - len(self._ring[0][1]) >= keep:
                self._ring_frames -= len(self._ring.popleft()[1])
        elif self._ring:
            self._ring.clear()
            self._ring_frames = 0
        if self._queue is not None and self._windows:
            for chunk, t_end in self._clip(adc, data, est.rate):
                self._enqueue(chunk, t_end)
        self._last_block_end = max(self._last_block_end, adc + frames / sr)

    # -- recording ------------------------------------------------------------

    def begin(self, writer, mix=None, slot=0, lock_clock=True):
        self.frames_written = 0
        self.overflows = 0
        self.clips = 0
        self.error = None
        self._windows = []
        self._pruned = 0.0
        self._file = writer
        self._clock = ClockLock(self.stream.samplerate, self.stream.channels,
                                lock_clock)
        self._mix, self._slot = mix, slot
        self._queued = 0
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def arm(self, preroll=0.0):
        """Start recording now, or preroll seconds ago from the
        pre-record buffer (padded with silence if it holds less)."""
        with self._lock:
            t = self.now()
            lo = t - preroll
            self._windows = [[lo, None]]
            if preroll > 0:
                sr = self.stream.samplerate
                first = self._ring[0][0] if self._ring else t
                if first > lo:
                    pad = int(round((first - lo) * sr))
                    self._queue.put((np.zeros((pad, self.stream.channels),
                                              np.float32), None, None))
                rate = self.estimator.rate if self.estimator else sr
                for adc, data in self._ring:
                    for chunk, t_end in self._clip(adc, data, rate):
                        self._enqueue(chunk, t_end)
        return t

    def pause(self):
        with self._lock:
            if self.recording_now:
                self._windows[-1][1] = self.now()

    def resume(self):
        with self._lock:
            if self._windows and not self.recording_now:
                now = self.now()
                # blocks older than a couple of seconds never arrive late
                keep = []
                for w in self._windows:
                    if w[1] < now - 2:
                        self._pruned += w[1] - w[0]
                    else:
                        keep.append(w)
                self._windows = keep + [[now, None]]

    @property
    def caught_up(self):
        """True once every block up to the last pause has arrived."""
        if not self._windows or self.recording_now:
            return True
        half_sample = 0.5 / self.stream.samplerate
        return (self._last_block_end >= self._windows[-1][1] - half_sample
                or self.no_signal)

    @property
    def recorded_seconds(self):
        with self._lock:
            return self._recorded_until(self.now())

    def _emit(self, block):
        self._file.write(block)
        self.frames_written += len(block)
        if self._mix is not None:
            self._mix.push(self._slot, block.mean(axis=1))

    def _write_loop(self):
        q, clock = self._queue, self._clock
        try:
            while True:
                item = q.get()
                if item[0] is None:  # stop: (None, total seconds)
                    blocks = clock.finish(item[1])
                else:
                    data, seconds, rate = item
                    if seconds is None:  # silence padding
                        blocks = clock.raw(data)
                    else:
                        self._queued -= len(data)
                        blocks = clock.process(data, seconds, rate)
                if not self.error:
                    try:
                        for block in blocks:
                            self._emit(block)
                    except Exception as e:  # disk full, etc.
                        self.error = str(e)
                if item[0] is None:
                    break
        finally:
            self._file.close()

    def stop(self):
        with self._lock:
            total = self._recorded_until(self.now())
            self._windows = []
            q, self._queue = self._queue, None
        if q is not None:
            q.put((None, total))
            self._writer.join()
            self._writer = None
            self._file = None
            self._mix = None
            self._queued = 0

    @property
    def seconds(self):
        if self.stream is None:
            return 0.0
        return self.frames_written / self.stream.samplerate
