"""Tests for the recording engine. They run without audio hardware: the
sounddevice module is replaced by a stub and audio blocks are fed straight
into each mic's callback."""

import collections
import csv
import os
import sys
import threading
import types

import numpy as np
import pytest
import soundfile as sf

sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import multimic as mm  # noqa: E402

SR = 1000


class FakeStream:
    def __init__(self, channels):
        self.samplerate = SR
        self.channels = channels
        self.time = 10.0
        self.active = True


class Status:
    input_overflow = False


class TimeInfo:
    def __init__(self, adc=0.0):
        self.inputBufferAdcTime = adc


def make_mic(index, name, channels):
    info = {"name": name, "max_input_channels": channels,
            "default_samplerate": SR}
    mic = mm.Mic(index, info, "Windows WASAPI")
    mic.stream = FakeStream(channels)
    mic.label = name
    return mic


def feed(mic, value, frames=100, adc=0.0):
    block = np.full((frames, mic.stream.channels), value, np.float32)
    mic._callback(block, frames, TimeInfo(adc), Status())


def read_folder(folder):
    out = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith((".wav", ".flac")):
            data, _ = sf.read(os.path.join(folder, name), always_2d=True)
            out[name] = data
    return out


def test_one_file_per_mic_with_names(tmp_path):
    a, b = make_mic(1, "Host", 2), make_mic(2, 'Guest "1"', 1)
    s = mm.Session([a, b], str(tmp_path), "WAV 24-bit", name="Ep 12")
    s.start()
    for _ in range(10):
        feed(a, 0.5)
        feed(b, 0.25)
    s.stop()
    assert s.folder.endswith("_Ep 12")
    files = read_folder(s.folder)
    assert set(files) == {"01_Host.wav", "02_Guest _1_.wav"}
    assert files["01_Host.wav"].shape == (1000, 2)
    assert files["02_Guest _1_.wav"].shape == (1000, 1)
    assert s.errors == []


def test_start_is_trimmed_to_shared_instant(tmp_path):
    mic = make_mic(1, "A", 1)
    s = mm.Session([mic], str(tmp_path), "FLAC 24-bit")
    s.start()
    # block captured 40 ms before the start instant: 40 samples dropped
    feed(mic, 0.5, frames=100, adc=10.0 - 0.040)
    s.stop()
    assert read_folder(s.folder)["01_A.flac"].shape == (60, 1)


def test_gain_and_mute(tmp_path):
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    a.set_gain_db(-6.0206)
    b.muted = True
    s = mm.Session([a, b], str(tmp_path), "WAV 32-bit float")
    s.start()
    feed(a, 0.5)
    feed(b, 0.5)
    s.stop()
    files = read_folder(s.folder)
    assert files["01_A.wav"].max() == pytest.approx(0.25, abs=1e-4)
    assert files["02_B.wav"].max() == 0
    assert len(files["02_B.wav"]) == 100  # muted mics keep their length


@pytest.mark.parametrize("mode,name,channels", [
    ("stereo", "00_mixdown.flac", 2),
    ("multitrack", "00_multitrack.flac", 2),
])
def test_mixdown(tmp_path, mode, name, channels):
    a, b = make_mic(1, "A", 2), make_mic(2, "B", 1)
    s = mm.Session([a, b], str(tmp_path), "FLAC 24-bit", mode)
    s.start()
    for _ in range(5):
        feed(a, 0.25)
        feed(b, 0.5)
    s.stop()
    mix = read_folder(s.folder)[name]
    assert mix.shape == (500, channels)
    if mode == "stereo":
        assert mix[:, 0] == pytest.approx(0.75, abs=1e-4)
    else:
        assert mix[:, 0] == pytest.approx(0.25, abs=1e-4)
        assert mix[:, 1] == pytest.approx(0.5, abs=1e-4)


def test_mixdown_needs_matching_rates(tmp_path):
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    b.stream.samplerate = 2 * SR
    s = mm.Session([a, b], str(tmp_path), "WAV 16-bit", "stereo")
    with pytest.raises(ValueError):
        s.start()


def test_split_every_n_minutes(tmp_path):
    mic = make_mic(1, "A", 1)
    s = mm.Session([mic], str(tmp_path), "WAV 16-bit", "multitrack",
                   split_minutes=1 / 60)  # 1 s = 1000 frames
    s.start()
    for _ in range(25):
        feed(mic, 0.1)
    s.stop()
    files = read_folder(s.folder)
    for base in ("00_multitrack", "01_A"):
        assert [len(files[f"{base}_part{p:02d}.wav"]) for p in (1, 2, 3)] \
            == [1000, 1000, 500]


def test_wav_rolls_over_before_4gb(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "WAV_MAX_BYTES", 4000)
    w = mm.SplitWriter(str(tmp_path / "x"), ".wav", SR, 2, "WAV", "PCM_16")
    w.write(np.zeros((2500, 2), np.float32))
    w.close()
    names = [os.path.basename(p) for p in w.paths]
    assert names == ["x.wav", "x_part02.wav", "x_part03.wav"]
    assert [sf.info(p).frames for p in w.paths] == [1000, 1000, 500]


def test_markers(tmp_path):
    mic = make_mic(1, "A", 1)
    s = mm.Session([mic], str(tmp_path), "WAV 16-bit", split_minutes=1)
    s.start()
    mic.stream.time = 10.0 + 12.5
    s.add_marker()
    mic.stream.time = 10.0 + 75.25
    s.add_marker('Big "laugh"')
    s.stop()
    with open(os.path.join(s.folder, "markers.txt")) as f:
        lines = f.read().splitlines()
    assert lines[0].split("\t") == ["12.500000", "12.500000", "Marker 1"]
    assert lines[1].endswith('Big "laugh"')
    with open(os.path.join(s.folder, "markers.csv")) as f:
        rows = list(csv.DictReader(f))
    assert rows[1]["time"] == "00:01:15.250"
    assert rows[1]["part"] == "2"
    assert rows[1]["time_in_part"] == "00:00:15.250"
    assert rows[1]["label"] == 'Big "laugh"'


def test_clips_and_no_signal(tmp_path, monkeypatch):
    mic = make_mic(1, "A", 1)
    s = mm.Session([mic], str(tmp_path), "WAV 16-bit")
    s.start()
    feed(mic, 1.0)
    feed(mic, 0.5)
    assert mic.clips == 1
    assert not mic.no_signal
    now = mm.time.monotonic()
    monkeypatch.setattr(mm.time, "monotonic",
                        lambda: now + mm.NO_SIGNAL_AFTER + 1)
    assert mic.no_signal
    monkeypatch.undo()
    s.stop()


def test_data_rate_and_disk_free(tmp_path):
    a, b = make_mic(1, "A", 2), make_mic(2, "B", 1)
    assert mm.data_rate([a, b], "WAV 24-bit") == SR * 3 * 3
    assert mm.data_rate([a, b], "WAV 16-bit", "stereo") == SR * 5 * 2
    assert mm.disk_free(str(tmp_path / "not" / "made" / "yet")) > 0


def test_monitor_buffer_stays_short():
    m = mm.Monitor.__new__(mm.Monitor)
    m.lock = threading.Lock()
    m.blocks = collections.deque()
    m.count = 0
    m.max_frames = 250
    for i in range(5):
        m.feed(np.full((100, 1), i, np.float32))
    out = np.empty((150, 2), np.float32)
    m._callback(out, 150, None, None)
    # oldest audio was dropped; mono is copied to both channels
    assert list(out[0]) == [3, 3] and list(out[149]) == [4, 4]
    assert m.count == 50


def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert mm.load_settings() == {}
    mm.save_settings({"format": "FLAC 24-bit", "mics": {"A": {"gain_db": 3}}})
    assert mm.load_settings()["mics"]["A"]["gain_db"] == 3
