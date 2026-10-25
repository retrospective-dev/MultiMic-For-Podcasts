# MultiMic

Records several USB microphones at the same time on Windows. Each mic gets its
own file, and all files start at the same moment.

A dark, Windows 11-style window: one card per mic with a live LED level meter,
gain and mute, a big record button and clock up top, and every setting in the
side panel.

## Get it

**Ready-made exe:** open the repo's **Releases** page (right-hand side of
the main GitHub page) and download `MultiMic.exe` from the latest release.
Every push to `main` builds and publishes a new one. No install needed: just
run it. Windows may show a SmartScreen warning the first time because the exe
isn't signed; click *More info > Run anyway*.

**From source:**

1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
2. Double-click `run.bat`. The first launch installs the libraries.

To build the exe yourself, double-click `build_exe.bat`.

## Use

- **Pick mics:** switch on each mic you want. Its level meter starts moving right
  away, so you can check levels before recording.
- **Name them:** type a name ("Host", "Guest 1"). It's used for the file name.
- **Gain / Mute:** the slider boosts or cuts each mic by up to 24 dB
  (double-click to reset to 0). Muted mics record silence, so files stay in
  sync. Meters show the level after gain, which is what gets recorded.
- **Listen to:** choose a mic and an output (e.g. your headphones) to monitor
  it live.
- **Mixdown:** also save a combined file. *Stereo mix* sums every mic into
  one stereo file. *Multitrack* writes one file with one channel per mic.
- **New file every N min:** splits long recordings into `_part01`, `_part02`,
  and so on, with all mics split at the same point. WAV files also split
  automatically before reaching the 4 GB WAV limit.
- **Session name:** optional, added to the folder name
  (`session_2026-10-23_20-00-00_Episode 12`).
- **Stop automatically after N min:** handy for timed takes or overnight
  recordings.
- **Pre-record N s:** keeps the last few seconds (up to 30) in memory, so
  when you press Record the files also include what was said just before.
- Press the round **record button** (or Space) to start, and again to stop.
- **Pause (P):** pauses and resumes every mic at the same instant. The paused
  stretch is left out of all files, so they stay in sync. The clock blinks
  orange while paused.
- **Markers:** press **M** (or the Marker button) while recording to mark a
  moment. They're saved as `markers.txt` (import into Audacity with
  *File > Import > Labels*, or into Reaper) and `markers.csv`, which also
  says which split part each marker falls in.

Each mic's status shows warnings in red:

- **CLIP xN:** the mic hit full scale N times. Turn its gain down.
- **NO SIGNAL:** the mic stopped sending audio, usually because it was
  unplugged or the USB link dropped.
- **drops:** audio buffers were lost (see Tips).

The bottom right shows free disk space and roughly how much recording time
is left. If the drive gets nearly full (under 200 MB), recording stops on its
own so the files already written stay intact.

Files go to `<output>\session_YYYY-MM-DD_HH-MM-SS\`:

    00_mixdown.wav       (if mixdown is on)
    01_Host.wav
    02_Guest.wav
    markers.txt          (if you added markers)
    markers.csv

Everything you set (mics ticked, names, gain, mute, folder, format and so on)
is remembered for next time in `%APPDATA%\MultiMic\settings.json`.

## Command line

    python multimic.py --list
    python multimic.py -d 3 7 -n Host Guest --name "Ep 12" -m stereo -s 30 -t 3600

`-d` devices, `-n` mic names, `--name` session name,
`-m off|stereo|multitrack` mixdown, `-s` minutes per file,
`-t` stop after N seconds. Press **M** for a marker. See `--help`.

## Tests

    pip install numpy soundfile pytest
    pytest tests

The tests need no audio hardware.

## Tips

- Keep the **Windows WASAPI** audio API (the default). MME cuts names off
  at 31 characters, and its latency is higher.
- Identical mics show up as "Microphone (USB Audio)" and
  "Microphone (2- USB Audio)". Tap each one and watch which meter moves, then
  give them names. Settings are remembered by these Windows names.
- "drops" means a buffer was lost. Spread mics across different USB ports or
  controllers, and avoid unpowered hubs.
- Mixdown needs every mic at the same sample rate. Pick a fixed rate
  (48000 is the default), not "Device default".
- Start, pause, resume and stop happen at the same sample position on every
  mic, using the timestamps the audio driver reports (or an estimate from
  the latency when it doesn't).
- Each USB mic has its own clock, so over long takes (an hour or more) the
  files can drift apart by a few tens of milliseconds. For long sessions where
  sync has to be perfect, use one multi-input audio interface instead.
