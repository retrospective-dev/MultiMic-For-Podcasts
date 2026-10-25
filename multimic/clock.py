"""Keeping every mic on the same clock.

Each USB mic has its own crystal, so a mic that says 48000 Hz might really
deliver 48003 samples a second. Over an hour that is several tenths of a
second, enough to hear as an echo when files are mixed. Here every mic is
measured against the computer's clock (which all streams share) and resampled
by the tiny difference, so a second of real time is exactly one second of
samples in every file.

Bigger timing errors, from dropouts or an unplugged mic, are filled with
silence (or trimmed) straight away so the files stay in sync.
"""

import numpy as np

from . import config


class RateEstimator:
    """Measures a device's real sample rate from (time, frames) pairs with a
    running least-squares fit, which averages out timestamp jitter.

    Timestamps taken when a callback runs can only be late, never early, so
    the timeline is placed along the low edge of recent timestamps rather
    than through their middle."""

    RECENT = 500  # timestamps kept for the low edge (5 s of 10 ms blocks)
    EDGE = 5  # percentile

    def __init__(self, nominal):
        self.nominal = nominal
        self.n = 0
        self.mean_x = self.mean_y = 0.0
        self.cxx = self.cxy = 0.0
        self.t0 = self.f0 = None
        self.span = 0.0
        self._rt = np.zeros(self.RECENT)  # recent times (minus t0)...
        self._rf = np.zeros(self.RECENT)  # ...and frame counts (minus f0)
        self._m = 0
        self.edge = 0.0

    def add(self, t, frames_before):
        if self.t0 is None:
            self.t0, self.f0 = t, frames_before
        x, y = t - self.t0, float(frames_before - self.f0)
        self.n += 1
        dx = x - self.mean_x
        self.mean_x += dx / self.n
        self.mean_y += (y - self.mean_y) / self.n
        self.cxx += dx * (x - self.mean_x)
        self.cxy += dx * (y - self.mean_y)
        self.span = max(self.span, x)
        i = (self.n - 1) % self.RECENT
        self._rt[i], self._rf[i] = x, y
        self._m = min(self._m + 1, self.RECENT)
        # residuals against the current line, so the edge always matches it
        line = self.mean_x + (self._rf[:self._m] - self.mean_y) / self.rate
        self.edge = float(np.percentile(self._rt[:self._m] - line,
                                        self.EDGE))

    @property
    def rate(self):
        """Samples per second of real time (the nominal rate until there
        is enough data, or if the fit looks wrong)."""
        if self.span < config.RATE_MIN_SPAN or self.cxx <= 0:
            return self.nominal
        rate = self.cxy / self.cxx
        if abs(rate / self.nominal - 1) > 0.01:
            return self.nominal
        return rate

    @property
    def ppm(self):
        return (self.rate / self.nominal - 1) * 1e6

    def time_at(self, frames):
        """When sample number `frames` was captured, read off the fitted
        line rather than any one jittery timestamp."""
        if self.n == 0:
            return None
        return self._line(frames) + self.edge

    def _line(self, frames):
        return (self.t0 + self.mean_x
                + (frames - self.f0 - self.mean_y) / self.rate)


class Resampler:
    """Streaming resampler for ratios very close to 1, using 4-point cubic
    (Catmull-Rom) interpolation. Keeps three samples of history between
    blocks, so it runs two samples behind its input."""

    HIST = 3

    def __init__(self, channels):
        self.hist = np.zeros((self.HIST, channels), np.float32)
        self.pos = float(self.HIST)  # where the next output sample is read

    def pending(self, ratio):
        """Output samples still held back from input already given."""
        return max(0.0, self.HIST - self.pos) * ratio

    def process(self, x, ratio):
        buf = np.concatenate([self.hist, x]) if len(x) else self.hist
        step = 1.0 / ratio
        last = len(buf) - 2  # need buf[i + 2]
        if self.pos < last:
            count = int(np.ceil((last - self.pos) / step - 1e-9))
        else:
            count = 0
        if count:
            p = self.pos + step * np.arange(count)
            i = np.floor(p).astype(np.int64)
            f = (p - i)[:, None].astype(np.float32)
            xm1, x0, x1, x2 = buf[i - 1], buf[i], buf[i + 1], buf[i + 2]
            c1 = 0.5 * (x1 - xm1)
            c2 = xm1 - 2.5 * x0 + 2.0 * x1 - 0.5 * x2
            c3 = 0.5 * (x2 - xm1) + 1.5 * (x0 - x1)
            out = ((c3 * f + c2) * f + c1) * f + x0
        else:
            out = np.zeros((0, buf.shape[1]), np.float32)
        self.pos += step * count - (len(buf) - self.HIST)
        self.hist = buf[-self.HIST:]
        return out.astype(np.float32, copy=False)

    def flush(self, ratio):
        return self.process(np.zeros((self.HIST - 1, self.hist.shape[1]),
                                     np.float32), ratio)


class ClockLock:
    """Turns a mic's blocks into output that tracks real time: resampled by
    the measured rate (when locking is on), steered back if it wanders, and
    with silence filled in for any gap."""

    def __init__(self, nominal, channels, lock=True):
        self.nominal = float(nominal)
        self.channels = channels
        self.resampler = Resampler(channels) if lock else None
        self.out = 0  # frames produced so far
        self.filled = 0  # frames of silence inserted for gaps
        self.trimmed = 0  # input frames dropped because they came too early

    def _zeros(self, n):
        return np.zeros((n, self.channels), np.float32)

    def raw(self, data):
        """Output-domain audio (e.g. silence padding) passed straight on."""
        self.out += len(data)
        return [data]

    def process(self, data, real_seconds, rate):
        """data: device samples ending at real_seconds of recorded time.
        rate: the device's measured sample rate."""
        blocks = []
        base = 1.0
        if self.resampler is not None:
            base = self.nominal / rate
            if abs(base - 1.0) < 1e-6:
                base = 1.0
        pending = self.resampler.pending(base) if self.resampler else 0.0
        error = real_seconds * self.nominal - (self.out + pending
                                               + len(data) * base)
        gap = config.GAP_FILL * self.nominal
        if error > gap:
            # a gap: let out what the resampler holds, fill the silence and
            # start afresh on the far side
            if self.resampler is not None:
                tail = self.resampler.flush(1.0)
                blocks.append(tail)
                self.out += len(tail)
                self.resampler = Resampler(self.channels)
                error = real_seconds * self.nominal - (self.out
                                                       + len(data) * base)
            n = int(round(error))
            blocks.append(self._zeros(n))
            self.out += n
            self.filled += n
            error -= n
        elif error < -gap:
            drop = min(len(data), int(round(-error / base)))
            data = data[drop:]
            self.trimmed += drop
            error += drop * base
        if self.resampler is None:
            out = data
        else:
            ratio = base
            if abs(error) >= 0.5:
                corr = error / (self.nominal * config.LOCK_TAU)
                lim = config.LOCK_MAX_CORRECTION
                ratio = base * (1.0 + max(-lim, min(lim, corr)))
            out = self.resampler.process(data, ratio)
        if len(out):
            blocks.append(out)
            self.out += len(out)
        return blocks

    def finish(self, real_seconds):
        """Flush what the resampler holds and pad the end with silence so
        the output covers exactly real_seconds."""
        blocks = []
        if self.resampler is not None:
            tail = self.resampler.flush(1.0)
            if len(tail):
                blocks.append(tail)
                self.out += len(tail)
        missing = int(round(real_seconds * self.nominal)) - self.out
        if missing > 0:
            blocks.append(self._zeros(missing))
            self.out += missing
            if missing > config.GAP_FILL * self.nominal:
                self.filled += missing
        return blocks
