"""Finding audio devices."""

import sounddevice as sd

from .config import HOSTAPI_PREFERENCE


def hostapis():
    return [h["name"] for h in sd.query_hostapis()]


def preferred_hostapi():
    names = hostapis()
    for want in HOSTAPI_PREFERENCE:
        if want in names:
            return names.index(want)
    return sd.default.hostapi if sd.default.hostapi >= 0 else 0


def input_devices(hostapi):
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == hostapi and d["max_input_channels"] > 0]


def output_devices(hostapi):
    return [(i, d) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == hostapi and d["max_output_channels"] > 0]


def rescan_devices():
    # PortAudio only enumerates devices at init, so restart it to pick up
    # microphones plugged in after launch. This stops every open stream.
    sd._terminate()
    sd._initialize()


def wasapi_resample(hostapi_name, samplerate, device_rate):
    # Shared-mode WASAPI only accepts the device's mix rate unless PortAudio
    # is allowed to resample.
    if "WASAPI" in hostapi_name and int(samplerate) != int(device_rate):
        try:
            return sd.WasapiSettings(auto_convert=True)
        except TypeError:
            pass
    return None
