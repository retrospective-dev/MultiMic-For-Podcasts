"""Drives the real window against fake audio devices: switch mics on,
record with pre-record, pause, add a marker, unplug a mic mid-recording and
check it reconnects, then check the files. Needs a display (xvfb-run on
Linux).

    python tests/gui_smoke.py
"""

import glob
import os
import sys
import tempfile
import tkinter as tk

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "fake_audio"))
sys.path.insert(0, os.path.join(here, ".."))
appdata = tempfile.mkdtemp()
os.environ["APPDATA"] = appdata

import soundfile as sf  # noqa: E402
import sounddevice  # noqa: E402  (the fake one)
from tkinter import messagebox  # noqa: E402

from multimic.gui.app import run_gui  # noqa: E402

errors = []
messagebox.showerror = lambda *a: errors.append(a)
out = os.path.join(appdata, "rec")
seen = {}

# find the App instance: it schedules its own tick() with after()
_after = tk.Misc.after


def after(self, ms, func=None, *args):
    if getattr(func, "__name__", "") == "tick" and "app" not in seen:
        seen["app"] = func.__self__
    return _after(self, ms, func, *args)


tk.Misc.after = after
_mainloop = tk.Tk.mainloop


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        errors.append(what)


def script(root):
    a = seen["app"]

    def setup():
        a.out_var.set(out)
        for i in (0, 2):
            a.rows[i].var.set(True)
            a.rows[i].toggled()
        a.rows[0].label_var.set("Host")
        a.rows[2].label_var.set("Guest")
        a.rows[0].pan_var.set(-40)
        a.rows[0].pan_changed()
        a.mix_var.set("Stereo mix")
        a.preroll_var.set("1")
        a.mon_box.current(3)
        a.update_monitor()
        a.toggle_find()

    def start():
        a.stop_find()
        a.name_var.set("Smoke")
        a.start()
        check(a.session is not None, "recording started")

    def pause_and_mark():
        a.add_marker()
        a.toggle_pause()
        check(a.session.paused, "paused")

    def resume_and_unplug():
        a.toggle_pause()
        a.rows[2].mic.stream.unplug()

    def lost():
        check(a.rows[2].mic.no_signal or a.rows[2].mic.reconnects,
              "unplugged mic noticed")

    def finish():
        check(a.rows[2].mic.reconnects >= 1, "mic reconnected by itself")
        a.stop()
        a.quit()

    for ms, fn in ((300, setup), (2000, start), (2600, pause_and_mark),
                   (3400, resume_and_unplug), (5000, lost), (9000, finish)):
        root.after(ms, fn)
    root.after(20000, root.destroy)  # never hang CI


def mainloop(self):
    script(self)
    _mainloop(self)


tk.Tk.mainloop = mainloop
run_gui()

sessions = glob.glob(os.path.join(out, "session_*_Smoke"))
check(len(sessions) == 1, "session folder named after the session")
if sessions:
    folder = sessions[0]
    files = {f: sf.info(os.path.join(folder, f))
             for f in os.listdir(folder) if f.endswith(".wav")}
    check(set(files) == {"00_mixdown.wav", "01_Host.wav", "02_Guest.wav"},
          "one file per mic plus the mixdown")
    lengths = [i.frames for i in files.values()]
    check(lengths and max(lengths) - min(lengths) < 48 * 5,
          f"files end together (lengths {lengths})")
    check(lengths and min(lengths) > 48000 * 5, "about 6 s recorded")
    check(os.path.exists(os.path.join(folder, "markers.csv")),
          "marker saved")
check(sounddevice.played[0] > 0, "monitoring played audio")
check(os.path.exists(os.path.join(appdata, "MultiMic", "settings.json")),
      "settings saved")
check(not [e for e in errors if isinstance(e, tuple)], "no error boxes")
sys.exit(1 if errors else 0)
