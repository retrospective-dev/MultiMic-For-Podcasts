"""Fake streams and helpers shared by the tests."""

import os

import numpy as np
import soundfile as sf

from multimic.mic import Mic

SR = 1000


class FakeStream:
    def __init__(self, channels, samplerate=SR):
        self.samplerate = samplerate
        self.channels = channels
        self.time = 10.0
        self.latency = 0.0
        self.active = True

    def abort(self):
        self.active = False

    def close(self):
        pass

    def start(self):
        self.active = True


class Status:
    def __init__(self, overflow=False):
        self.input_overflow = overflow


class TimeInfo:
    def __init__(self, adc=0.0):
        self.inputBufferAdcTime = adc


def make_mic(index, name, channels):
    info = {"name": name, "max_input_channels": channels,
            "default_samplerate": SR}
    mic = Mic(index, info, "Windows WASAPI")
    mic.stream = FakeStream(channels)
    mic.label = name
    return mic


def feed(mic, value, frames=100, adc=None, overflow=False):
    """Deliver one block. By default it was captured starting at the
    stream's current time, and the clock moves on by its length."""
    if adc is None:
        adc = mic.stream.time
        mic.stream.time += frames / SR
    if np.isscalar(value):
        block = np.full((frames, mic.stream.channels), value, np.float32)
    else:
        block = np.asarray(value, np.float32).reshape(frames, -1)
    mic._callback(block, frames, TimeInfo(adc), Status(overflow))


def read_folder(folder):
    out = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith((".wav", ".flac")):
            data, _ = sf.read(os.path.join(folder, name), always_2d=True)
            out[name] = data
    return out
