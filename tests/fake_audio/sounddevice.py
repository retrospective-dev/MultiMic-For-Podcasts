"""A stand-in for the sounddevice module with three USB mics and a pair of
headphones on "Windows WASAPI". Mic streams deliver a sine wave in real
time from a thread, like PortAudio's callback thread."""

import threading
import time

import numpy as np

DEVICES = [
    dict(name="Microphone (USB Audio Device)", hostapi=0,
         max_input_channels=1, max_output_channels=0,
         default_samplerate=48000.0),
    dict(name="Microphone (2- USB Audio Device)", hostapi=0,
         max_input_channels=1, max_output_channels=0,
         default_samplerate=48000.0),
    dict(name="Microphone (Blue Yeti Stereo Microphone)", hostapi=0,
         max_input_channels=2, max_output_channels=0,
         default_samplerate=48000.0),
    dict(name="Headphones (Realtek Audio)", hostapi=0,
         max_input_channels=0, max_output_channels=2,
         default_samplerate=48000.0),
]
played = [0]  # non-silent frames sent to the headphones


class _Default:
    hostapi = 0


default = _Default()


def query_hostapis(index=None):
    apis = [dict(name="Windows WASAPI", default_output_device=3)]
    return apis if index is None else apis[index]


def query_devices(index=None):
    return DEVICES if index is None else DEVICES[index]


def _terminate():
    pass


def _initialize():
    pass


class WasapiSettings:
    def __init__(self, auto_convert=False):
        pass


class _Status:
    input_overflow = False


class _TimeInfo:
    inputBufferAdcTime = 0  # like drivers that don't report it


class InputStream:
    def __init__(self, device, channels, samplerate, callback, **kwargs):
        self.device, self.channels = device, channels
        self.samplerate, self.callback = samplerate, callback
        self.running = False
        self.active = True
        self.thread = None

    @property
    def time(self):
        return time.monotonic()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        n, done, t0 = 480, 0, time.monotonic()
        level = 0.3 * (self.device + 1) / 3
        while self.running:
            wait = t0 + (done + n) / self.samplerate - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            t = np.arange(done, done + n) / self.samplerate
            x = (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
            self.callback(np.repeat(x[:, None], self.channels, 1), n,
                          _TimeInfo(), _Status())
            done += n

    def stop(self):
        # like Pa_StopStream: no callbacks after it returns
        self.running = False
        if self.thread:
            self.thread.join()

    def abort(self):
        self.stop()
        self.active = False

    def close(self):
        pass

    def unplug(self):
        """Test hook: the device vanishes but the stream object stays."""
        self.running = False


class OutputStream:
    def __init__(self, device, samplerate, channels, callback, **kwargs):
        self.channels, self.callback = channels, callback
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            out = np.empty((480, self.channels), np.float32)
            self.callback(out, 480, None, None)
            played[0] += int((out != 0).any(axis=1).sum())
            time.sleep(0.01)

    def stop(self):
        self.running = False

    def close(self):
        pass
