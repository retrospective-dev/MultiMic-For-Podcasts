"""Command line: list devices, or record without the window.

    python -m multimic                 # open the window
    python -m multimic --list          # list input devices
    python -m multimic -d 3 7 9        # record devices 3, 7 and 9 until
                                       # Ctrl+C
"""

import argparse
import os
import sys
import time

from . import __version__
from .config import FORMATS, RECONNECT_EVERY
from .devices import hostapis, input_devices, preferred_hostapi
from .mic import Mic
from .session import Session
from .util import fmt_time, to_db


def list_devices(api):
    print(f"Input devices ({hostapis()[api]}):")
    for idx, info in input_devices(api):
        print(f"  [{idx:3d}] {info['name']}  "
              f"({info['max_input_channels']} ch, "
              f"{int(info['default_samplerate'])} Hz)")
    print("\nOther audio APIs: " + ", ".join(
        f"{n}={i}" for i, n in enumerate(hostapis())))


def run_cli(args, api):
    all_devs = dict(input_devices(api))
    for opt in ("names", "pans"):
        values = getattr(args, opt)
        if values and len(values) != len(args.devices):
            sys.exit(f"--{opt} needs one value per device.")
    mics = []
    for n, idx in enumerate(args.devices):
        if idx not in all_devs:
            sys.exit(f"Device {idx} is not an input on {hostapis()[api]}. "
                     "Use --list.")
        mic = Mic(idx, all_devs[idx], hostapis()[api])
        if args.names:
            mic.label = args.names[n]
        if args.pans:
            mic.pan = max(-100, min(100, args.pans[n])) / 100
        mic.open(args.samplerate, args.channels)
        mics.append(mic)
    session = Session(mics, args.out, args.format,
                      None if args.mix == "off" else args.mix, args.split,
                      args.name or "", lock_clocks=not args.no_lock)
    session.start()
    try:
        import msvcrt  # Windows: read single key presses for markers
    except ImportError:
        msvcrt = None
    print(f"Recording {len(mics)} mic(s) to {session.folder}. "
          + ("Press M for a marker, " if msvcrt else "")
          + "Ctrl+C to stop.")
    next_retry = 0.0
    try:
        while args.seconds is None or session.elapsed < args.seconds:
            while msvcrt and msvcrt.kbhit():
                if msvcrt.getwch().lower() == "m":
                    t, label = session.add_marker()
                    print(f"\n{label} at {fmt_time(t, True)}")
            if time.monotonic() > next_retry:
                next_retry = time.monotonic() + RECONNECT_EVERY
                for m in mics:
                    if m.no_signal:
                        try:
                            m.restart_stream()
                        except Exception:
                            pass
            levels = "  ".join(
                f"{m.label[:12]} "
                + ("--LOST--" if m.no_signal else f"{to_db(m.peak):6.1f}dB")
                for m in mics)
            for m in mics:
                m.peak = 0.0
            print(f"\r{fmt_time(session.elapsed)}  {levels}  ", end="",
                  flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    session.stop()
    for m in mics:
        m.close()
        notes = []
        if m.overflows:
            notes.append(f"{m.overflows} drop(s)")
        if m.gap_seconds >= 0.05:
            notes.append(f"{m.gap_seconds:.1f} s of silence filled in")
        if not args.no_lock:
            notes.append(f"clock {m.drift_ppm:+.0f} ppm")
        print(f"\n  {m.label}: {fmt_time(m.seconds)}"
              + (f" ({', '.join(notes)})" if notes else ""), end="")
    for err in session.errors:
        print(f"\n  ERROR {err}", end="")
    print(f"\nSaved to {session.folder}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="multimic",
        description="Record several USB microphones at once.")
    p.add_argument("--version", action="version",
                   version=f"MultiMic {__version__}")
    p.add_argument("--list", action="store_true", help="list input devices")
    p.add_argument("-d", "--devices", type=int, nargs="+",
                   help="device numbers to record (command-line mode)")
    p.add_argument("-n", "--names", nargs="+",
                   help="a name per device, used for the file names")
    p.add_argument("--name", help="session name, added to the folder name")
    p.add_argument("--api", type=int, help="audio API number "
                   "(default: WASAPI on Windows)")
    p.add_argument("-o", "--out", default=os.path.join(
        os.path.expanduser("~"), "Music", "MultiMic"), help="output folder")
    p.add_argument("-f", "--format", default="WAV 24-bit",
                   choices=list(FORMATS))
    p.add_argument("-r", "--samplerate", type=int, default=48000)
    p.add_argument("-c", "--channels", type=int, default=2,
                   help="channels per mic (capped at what the mic has)")
    p.add_argument("-m", "--mix", default="off",
                   choices=["off", "stereo", "multitrack"],
                   help="also write a mixdown file")
    p.add_argument("-p", "--pans", type=float, nargs="+",
                   help="stereo mix position per device, -100 (left) to "
                        "100 (right)")
    p.add_argument("-s", "--split", type=float, default=0,
                   help="start a new file every N minutes")
    p.add_argument("-t", "--seconds", type=float,
                   help="stop automatically after this many seconds")
    p.add_argument("--no-lock", action="store_true",
                   help="don't correct for clock drift between mics")
    args = p.parse_args(argv)
    api = preferred_hostapi() if args.api is None else args.api

    if args.list:
        list_devices(api)
    elif args.devices:
        run_cli(args, api)
    else:
        from .gui.app import run_gui
        run_gui()
