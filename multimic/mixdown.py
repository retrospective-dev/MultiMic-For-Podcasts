"""The optional combined file."""

import collections
import threading

import numpy as np


def soft_clip(x, knee=0.8):
    """Leaves quiet audio alone and rounds peaks off below full scale
    instead of clipping them hard."""
    over = np.abs(x) > knee
    if over.any():
        x = x.copy()
        a = np.abs(x[over])
        x[over] = np.sign(x[over]) * (
            knee + (1 - knee) * np.tanh((a - knee) / (1 - knee)))
    return x


def pan_gains(pan):
    """Constant-power pan: -1 is hard left, 0 centre, 1 hard right."""
    theta = (max(-1.0, min(1.0, pan)) + 1) * np.pi / 4
    return np.cos(theta), np.sin(theta)


class Mixdown:
    """Combines the mics into one extra file: either mixed to stereo (with
    each mic's pan, headroom for several mics and a soft limiter) or one
    channel per mic."""

    def __init__(self, mics, mode, writer, samplerate):
        self.mics = mics
        self.mode = mode
        self.writer = writer
        self.slack = int(samplerate * 2)
        n = len(mics)
        self.bufs = [collections.deque() for _ in range(n)]
        self.counts = [0] * n
        # Silence this mixer already put in for a mic that fell behind; the
        # mic's own gap filling sends the same stretch later, which is then
        # skipped so the mix stays aligned.
        self.debt = [0] * n
        self.lock = threading.Lock()
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def push(self, slot, mono):
        with self.lock:
            skip = min(self.debt[slot], len(mono))
            if skip:
                self.debt[slot] -= skip
                mono = mono[skip:]
            if len(mono):
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
                self.debt[i] += need
            self.counts[i] = max(0, self.counts[i] - n)
            cols.append(np.concatenate(parts))
        return np.stack(cols, axis=1)

    def _render(self, frames):
        if self.mode != "stereo":
            return frames
        n = frames.shape[1]
        gains = np.array([pan_gains(m.pan) for m in self.mics],
                         np.float32)  # (n, 2)
        # sqrt(2) makes one centred mic unity gain; 1/sqrt(n) leaves room
        # for several people talking at once
        mix = frames @ gains * np.float32(np.sqrt(2) / np.sqrt(n))
        return soft_clip(mix)

    def _flush(self, final):
        with self.lock:
            hi = max(self.counts)
            n = hi if final else max(min(self.counts), hi - self.slack)
            if n <= 0:
                return
            frames = self._take(n)
        try:
            if not self.error:
                self.writer.write(self._render(frames))
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
