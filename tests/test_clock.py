"""Clock locking, dropouts, unplugged mics and the stereo mix."""

import collections
import threading

import numpy as np
import pytest

from helpers import SR, FakeStream, feed, make_mic, read_folder
from multimic import config
from multimic.clock import RateEstimator, Resampler
from multimic.mixdown import Mixdown, pan_gains, soft_clip
from multimic.session import Session


def drifting_feed(mic, true_rate, until, value=0.1, frames=100,
                  click_at=None):
    """Feed a mic whose crystal runs at true_rate while the stream clock
    (the computer's) runs true, up to stream time `until`. Optionally puts
    a single click at a given stream time so alignment can be checked."""
    while mic.stream.time + frames / true_rate <= until:
        t0 = mic.stream.time
        block = np.full((frames, mic.stream.channels), value, np.float32)
        if click_at is not None and t0 <= click_at < t0 + frames / true_rate:
            block[int((click_at - t0) * true_rate)] = 1.0
        mic.stream.time = t0 + frames / true_rate
        feed(mic, block, frames, adc=t0)
    mic.stream.time = until  # streams share one clock: same "now" for all


def click_index(data):
    return int(np.argmax(data[:, 0]))


# -- building blocks ---------------------------------------------------------

def test_resampler_is_exact_at_ratio_one():
    r = Resampler(1)
    x = np.random.default_rng(1).standard_normal((1000, 1)).astype(np.float32)
    out = np.concatenate([r.process(x[:300], 1.0), r.process(x[300:], 1.0),
                          r.flush(1.0)])
    assert np.array_equal(out, x)


def test_resampler_quality():
    ratio = 1.001
    t = np.arange(48000) / 48000
    x = np.sin(2 * np.pi * 1000 * t).astype(np.float32)[:, None]
    r = Resampler(1)
    out = np.concatenate([r.process(x[i:i + 480], ratio)
                          for i in range(0, len(x), 480)])
    assert len(out) == pytest.approx(len(x) * ratio, abs=3)
    # compare with the ideal resampled sine, away from the edges
    n = np.arange(len(out))
    ideal = np.sin(2 * np.pi * 1000 * (n / ratio) / 48000)
    err = out[100:-100, 0] - ideal[100:-100]
    assert 20 * np.log10(np.abs(err).max()) < -50


def test_rate_estimator_sees_through_jitter():
    true_rate = 48000 * (1 + 80e-6)
    est = RateEstimator(48000)
    rng = np.random.default_rng(2)
    frames = 0
    for k in range(30000):  # 300 s of 10 ms blocks
        t = frames / true_rate + rng.uniform(-0.002, 0.002)
        est.add(10.0 + t, frames)
        frames += 480
    assert est.ppm == pytest.approx(80, abs=2)


def test_pan_and_soft_clip():
    assert pan_gains(-1) == pytest.approx((1, 0), abs=1e-9)
    assert pan_gains(1) == pytest.approx((0, 1), abs=1e-9)
    assert pan_gains(0) == pytest.approx((np.sqrt(0.5), np.sqrt(0.5)))
    x = np.linspace(-3, 3, 601, dtype=np.float32)
    y = soft_clip(x)
    assert np.abs(y).max() <= 1.0
    quiet = np.abs(x) <= 0.8
    assert np.array_equal(y[quiet], x[quiet])


# -- whole recordings --------------------------------------------------------

def test_drifting_mics_end_up_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    fast, slow = make_mic(1, "Fast", 1), make_mic(2, "Slow", 1)
    s = Session([fast, slow], str(tmp_path), "WAV 32-bit float")
    s.start()  # at stream time 10
    # +1000 ppm and -1000 ppm: unlocked they'd be 0.24 s apart after 120 s
    drifting_feed(fast, SR * 1.001, 130.0, click_at=100.0)
    drifting_feed(slow, SR * 0.999, 130.0, click_at=100.0)
    s.stop()
    files = read_folder(s.folder)
    a, b = files["01_Fast.wav"], files["02_Slow.wav"]
    assert abs(len(a) - 120 * SR) <= 2 and abs(len(b) - 120 * SR) <= 2
    assert abs(click_index(a) - 90 * SR) <= 2  # click 90 s after start
    assert abs(click_index(a) - click_index(b)) <= 2
    assert fast.drift_ppm == pytest.approx(1000, abs=5)
    assert slow.drift_ppm == pytest.approx(-1000, abs=5)


def test_without_lock_drift_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    mic = make_mic(1, "A", 1)
    s = Session([mic], str(tmp_path), "WAV 16-bit", lock_clocks=False)
    s.start()
    drifting_feed(mic, SR * 1.002, 70.0)
    s.stop()
    data = read_folder(s.folder)["01_A.wav"]
    # no resampling, but anything beyond the gap limit is trimmed
    assert abs(len(data) - 60 * SR) <= config.GAP_FILL * SR + 1


def test_unplugged_mic_gets_silence_and_stays_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    s = Session([a, b], str(tmp_path), "WAV 32-bit float", "multitrack")
    s.start()
    for _ in range(20):           # 2 s together
        feed(a, 0.2)
        feed(b, 0.2)
    for _ in range(30):           # B unplugged for 3 s
        feed(a, 0.2)
        b.stream.time += 0.1
    for _ in range(20):           # B back
        feed(a, 0.2)
        feed(b, 0.2)
    click = np.full((100, 1), 0.2, np.float32)
    click[50] = 1.0
    feed(a, click)
    feed(b, click)
    s.stop()
    files = read_folder(s.folder)
    fa, fb, mix = files["01_A.wav"], files["02_B.wav"], \
        files["00_multitrack.wav"]
    assert len(fa) == len(fb) == len(mix) == 7100
    assert click_index(fa) == click_index(fb) == 7050
    assert np.abs(fb[2000:5000]).max() == 0  # the missing stretch is silent
    assert b.gap_seconds == pytest.approx(3.0, abs=0.01)
    assert np.allclose(mix[:, 1], fb[:, 0])


def test_mic_lost_at_the_end_is_padded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    s = Session([a, b], str(tmp_path), "WAV 16-bit")
    s.start()
    for i in range(50):
        feed(a, 0.2)
        if i < 30:
            feed(b, 0.2)
        else:
            b.stream.time += 0.1
    s.stop()
    files = read_folder(s.folder)
    assert len(files["01_A.wav"]) == len(files["02_B.wav"]) == 5000


def test_restart_stream_keeps_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    mic = make_mic(1, "A", 1)
    s = Session([mic], str(tmp_path), "WAV 16-bit")
    s.start()
    for _ in range(10):
        feed(mic, 0.3)
    old = mic.stream
    new = FakeStream(1)
    new.time = old.time + 1.5     # it took 1.5 s to get the mic back
    monkeypatch.setattr(mic, "_new_stream", lambda sr, ch: new)
    mic.restart_stream()
    assert mic.stream is new and not old.active and mic.reconnects == 1
    for _ in range(10):
        feed(mic, 0.3)
    s.stop()
    data = read_folder(s.folder)["01_A.wav"]
    assert len(data) == 3500
    assert np.abs(data[1000:2500]).max() == 0


def test_mixdown_debt_keeps_a_late_mic_aligned():
    class Collect:
        def __init__(self):
            self.blocks = []

        def write(self, b):
            self.blocks.append(b)

    mics = [make_mic(1, "A", 1), make_mic(2, "B", 1)]
    m = Mixdown.__new__(Mixdown)
    m.mics, m.mode, m.writer, m.slack = mics, "multitrack", Collect(), 2000
    m.bufs = [collections.deque(), collections.deque()]
    m.counts, m.debt = [0, 0], [0, 0]
    m.lock, m.error = threading.Lock(), None
    m.push(0, np.ones(3000, np.float32))
    m._flush(final=False)         # B is 3 s behind: 1 s padded for it
    assert m.debt == [0, 1000]
    # B's own gap filling then sends silence for its missing 3 s, then audio
    m.push(1, np.zeros(3000, np.float32))
    m.push(1, np.full(500, 0.5, np.float32))
    m.push(0, np.ones(500, np.float32))
    m._flush(final=True)
    out = np.concatenate(m.writer.blocks)
    assert out.shape == (3500, 2)
    assert out[:3000, 1].max() == 0 and np.all(out[3000:, 1] == 0.5)


def test_stereo_mix_pans(tmp_path):
    a, b = make_mic(1, "Left", 1), make_mic(2, "Right", 1)
    a.pan, b.pan = -1.0, 1.0
    s = Session([a, b], str(tmp_path), "WAV 32-bit float", "stereo")
    s.start()
    feed(a, 0.3)
    feed(b, 0.6)
    s.stop()
    mix = read_folder(s.folder)["00_mixdown.wav"]
    assert mix[:, 0] == pytest.approx(0.3, abs=1e-4)
    assert mix[:, 1] == pytest.approx(0.6, abs=1e-4)


def test_backlog_is_reported_in_seconds():
    mic = make_mic(1, "A", 1)
    mic._queued = 6 * SR
    assert mic.backlog == pytest.approx(6.0)


def test_late_callbacks_are_not_gaps(tmp_path, monkeypatch):
    """A busy computer runs callbacks late (and then several at once);
    no audio is lost, so nothing may be filled in or trimmed."""
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    rng = np.random.default_rng(3)
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    s = Session([a, b], str(tmp_path), "WAV 32-bit float")
    s.start()
    stall_until = 0.0
    for k in range(600):          # 60 s
        t = 10.0 + k * 0.1
        if k % 50 == 25:          # every 5 s, B's callbacks stall 200 ms
            stall_until = t + 0.3
        late = max(t + 0.1, stall_until) + rng.uniform(0, 0.003)
        block = np.full((100, 1), 0.1, np.float32)
        if k == 450:
            block[30] = 1.0
        a.stream.time = t + 0.1
        feed(a, block, adc=t)
        b.stream.time = late
        feed(b, block, adc=late - 0.1)   # timestamp from the late callback
    a.stream.time = b.stream.time = 70.0
    s.stop()
    files = read_folder(s.folder)
    fa, fb = files["01_A.wav"], files["02_B.wav"]
    assert len(fa) == 60 * SR and abs(len(fb) - len(fa)) <= 1
    assert b.gap_seconds == 0 and b._clock.trimmed == 0
    assert abs(click_index(fa) - click_index(fb)) <= 2


def test_overrun_is_realigned(tmp_path, monkeypatch):
    """When a driver overruns, samples are lost. The mic is shifted back
    into line instead of running early for the rest of the recording."""
    monkeypatch.setattr(config, "NO_SIGNAL_AFTER", 0.0)
    a, b = make_mic(1, "A", 1), make_mic(2, "B", 1)
    s = Session([a, b], str(tmp_path), "WAV 32-bit float")
    s.start()
    for k in range(600):          # 60 s
        t = 10.0 + k * 0.1
        block = np.full((100, 1), 0.1, np.float32)
        if k == 450:
            block[30] = 1.0
        for mic in (a, b):
            mic.stream.time = t + 0.1
        feed(a, block, adc=t)
        if k == 300:              # B's driver drops the first 20 samples
            feed(b, block[20:], 80, adc=t + 0.02, overflow=True)
        else:
            feed(b, block, adc=t)
    a.stream.time = b.stream.time = 70.0
    s.stop()
    files = read_folder(s.folder)
    fa, fb = files["01_A.wav"], files["02_B.wav"]
    assert len(fa) == 60 * SR and abs(len(fb) - len(fa)) <= 1
    assert click_index(fa) == 45030
    assert abs(click_index(fb) - click_index(fa)) <= 1
