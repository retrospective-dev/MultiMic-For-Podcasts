"""Constants and the saved settings file."""

import json
import os

FORMATS = {
    "WAV 24-bit": ("WAV", "PCM_24", ".wav"),
    "WAV 16-bit": ("WAV", "PCM_16", ".wav"),
    "WAV 32-bit float": ("WAV", "FLOAT", ".wav"),
    "FLAC 24-bit": ("FLAC", "PCM_24", ".flac"),
}
BYTES_PER_SAMPLE = {"PCM_16": 2, "PCM_24": 3, "FLOAT": 4}
WAV_MAX_BYTES = 3_900_000_000  # stay clear of the 4 GB RIFF limit
SAMPLE_RATES = ["Device default", "44100", "48000", "96000"]
MIX_MODES = {
    "Off": None,
    "Stereo mix": "stereo",
    "Multitrack (1 ch per mic)": "multitrack",
}
HOSTAPI_PREFERENCE = ["Windows WASAPI", "MME", "Windows DirectSound"]
GAIN_RANGE = 24  # dB, either way
CLIP_LEVEL = 0.999
NO_SIGNAL_AFTER = 1.5  # seconds without audio callbacks
DISK_STOP_BYTES = 200_000_000  # stop recording before the disk is full
FLAC_RATIO = 0.6  # rough FLAC size vs. PCM, for time-left estimates

# Clock locking (see clock.py)
RATE_MIN_SPAN = 5.0  # seconds of timestamps before trusting a rate estimate
LOCK_TAU = 5.0  # seconds over which a timing error is steered away
LOCK_MAX_CORRECTION = 500e-6  # never bend the rate more than 0.05 %
GAP_FILL = 0.01  # bigger timing errors are filled with silence or trimmed
# Audio arriving this much later than the device's timeline predicts, for
# at least JUMP_CONFIRM seconds of audio, means samples were lost (a USB
# hiccup); shorter delays are just a busy computer calling back late.
JUMP = 0.25
JUMP_CONFIRM = 0.5
OVERFLOW_SHIFT = 0.005  # re-measure the offset after an overrun above this

# Disk backlog: audio waiting in memory because the disk is too slow
BACKLOG_WARN = 5.0  # seconds
BACKLOG_STOP = 120.0

RECONNECT_EVERY = 3.0  # seconds between attempts to reopen a lost mic


def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "MultiMic", "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
