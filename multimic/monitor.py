"""Listening to a mic through headphones."""

import collections
import threading

import sounddevice as sd

from .devices import wasapi_resample


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
