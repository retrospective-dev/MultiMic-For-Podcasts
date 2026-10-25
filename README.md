# MultiMic

Records several USB microphones at the same time on Windows. Each mic gets its
own file, every file starts at the same moment, and they stay locked together
however long you record.

A dark, Windows 11-style window: one card per mic with a live LED level meter,
gain, pan and mute, a big record button and clock up top, and every setting in
the side panel.

## Get it

Open the repo's **Releases** page (right-hand side of the main GitHub page)
and download from the latest release:

- **MultiMic-Setup.exe** installs MultiMic with a Start menu entry and an
  uninstaller. No admin rights needed.
- **MultiMic-portable.zip**: unzip anywhere and run `MultiMic.exe` inside
  the folder.

Windows may warn that the app is from an unknown publisher the first time,
because it isn't code-signed; click *More info > Run anyway*.

**From source:** install Python 3.9+ from python.org (tick "Add Python to
PATH"), then double-click `run.bat`. The first launch installs the libraries.
`build_exe.bat` builds the app folder yourself, plus the installer if
[Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed.

## Use

- **Pick mics:** switch on each mic you want. Its meter starts moving right
  away, so you can check levels before recording.
- **Find mic:** press it, then speak into or tap a mic: its card lights up.
  Handy when two identical mics show up as "USB Audio" and "2- USB Audio".
- **Name them:** type a name ("Host", "Guest 1"). It's used for the file name.
- **Gain / Mute:** the slider boosts or cuts each mic by up to 24 dB
  (double-click to reset). Muted mics record silence, so files stay in sync.
  Meters show the level after gain, which is what gets recorded.
- **Pan:** where the mic sits in the stereo mixdown (double-click to centre).
- **Listen to:** choose a mic and an output (e.g. your headphones) to monitor
  it live.
- **Mixdown:** also save a combined file. *Stereo mix* mixes every mic using
  its pan, leaves headroom for several people talking at once, and rounds
  off peaks instead of clipping. *Multitrack* writes one file with one
  channel per mic.
- **New file every N min:** splits long recordings into `_part01`, `_part02`,
  and so on, with all mics split at the same point. WAV files also split
  automatically before reaching the 4 GB WAV limit.
- **Pre-record N s:** keeps the last few seconds (up to 30) in memory, so
  pressing Record also captures what was said just before.
- **Auto-stop after N min:** for timed takes or overnight recordings.
- **Lock mic clocks** (on by default): see *Staying in sync* below.
- Press the round **record button** (or Space) to start, and again to stop.
- **Pause (P):** pauses and resumes every mic at the same instant. The paused
  stretch is left out of all files. The clock blinks orange while paused.
- **Markers (M):** mark a moment while recording. They're saved as
  `markers.txt` (import into Audacity with *File > Import > Labels*, or into
  Reaper) and `markers.csv`, which also says which split part each marker
  falls in.

Each mic's status line shows its clock offset (e.g. `clock +23 ppm`) and
warnings in red:

- **CLIP xN:** the mic hit full scale N times. Turn its gain down.
- **Disconnected - recording silence:** the mic stopped sending audio (usually
  unplugged). Its file keeps getting silence so it stays in sync, MultiMic
  keeps trying to reopen it, and a **Reconnect** button re-detects all
  devices. A mic that comes back carries on in the same file.
- **N s gap filled:** how much silence went in for a dropout.
- **disk N s behind:** the disk can't keep up and audio is piling up in
  memory. If it gets 2 minutes behind, recording stops safely.
- **drops:** the driver lost audio buffers (see Tips).

The bottom right shows free disk space and roughly how much recording time
is left. If the drive gets nearly full (under 200 MB), recording stops on its
own so the files already written stay intact.

Files go to `<output>\session_YYYY-MM-DD_HH-MM-SS_<session name>\`:

    00_mixdown.wav       (if mixdown is on)
    01_Host.wav
    02_Guest.wav
    markers.txt          (if you added markers)
    markers.csv

Everything you set is remembered for next time in
`%APPDATA%\MultiMic\settings.json`.

## Staying in sync

Every USB mic has its own clock crystal, so a mic that says 48000 Hz might
really deliver 48003 samples a second. Over an hour that adds up to several
tenths of a second between mics: an audible echo when you mix them.

With **Lock mic clocks** on, MultiMic measures each mic's real rate against
the computer's clock while it runs, and resamples by that tiny difference, so
one second of real time is one second in every file. A 1-hour recording
comes out the same length on every mic, within a sample or two.

It also copes with the messier things that happen on a real PC:

- A late audio callback (the computer was busy) isn't mistaken for lost
  audio.
- Audio the driver really did lose, or a mic that drops out or gets
  unplugged, is replaced by silence of the same length, so everything after
  it still lines up.
- Start, pause, resume and stop happen at the same instant on every mic.

## Command line

    python -m multimic --list
    python -m multimic -d 3 7 -n Host Guest --name "Ep 12" -m stereo -p -30 30 -s 30 -t 3600

`-d` devices, `-n` mic names, `--name` session name,
`-m off|stereo|multitrack` mixdown, `-p` pan per mic (-100 left to 100
right), `-s` minutes per file, `-t` stop after N seconds, `--no-lock` to turn
clock locking off. Press **M** for a marker. See `--help`.

## Tips

- Keep the **Windows WASAPI** audio driver (the default). MME cuts names off
  at 31 characters, and its latency is higher.
- Settings are matched to mics by their Windows name. Identical mics can swap
  names after a reboot or replug, so use **Find mic** to check which is which.
- "drops" means the driver lost audio. Spread mics across different USB ports
  or controllers, and avoid unpowered hubs.
- Mixdown needs every mic at the same sample rate. Pick a fixed rate
  (48000 is the default), not "Device default".

## Development

    multimic/
      config.py      constants and the settings file
      devices.py     finding audio devices
      mic.py         one mic: stream, meters, recording pipeline
      clock.py       rate measurement, resampling, gap filling
      session.py     a recording: start, pause, markers, stop
      mixdown.py     the combined file
      monitor.py     headphone monitoring
      writer.py      sound files that split into parts
      cli.py         command line
      gui/           the window (app.py) and its custom widgets
    launch.py        entry point for the exe
    packaging/       installer script (Inno Setup)
    tests/           unit tests and a GUI smoke test

Tests need no audio hardware:

    pip install numpy soundfile pytest
    pytest tests
    xvfb-run python tests/gui_smoke.py     # the real window, fake devices

GitHub builds the app on every push. To publish a release, bump
`__version__` in `multimic/__init__.py` (or run the *Build* workflow by hand
from the Actions tab): the installer and portable zip are attached to a new
release named after the version.
