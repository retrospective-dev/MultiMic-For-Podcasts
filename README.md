# MultiMic

Records several USB microphones at the same time on Windows. Each mic gets its
own file, and all files start at the same moment.

## Run

1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
2. Double-click `run.bat`. The first launch installs the libraries.

To build a standalone `MultiMic.exe`, double-click `build_exe.bat`.

## Use

- Tick each mic you want. Its level meter starts moving right away, so you
  can check gain before recording.
- Pick the sample rate (48000 recommended, all mics will match), format and
  output folder.
- Press **Record** (or Space). Press again to stop.
- Files go to `<output>\session_YYYY-MM-DD_HH-MM-SS\01_<mic name>.wav`, ...

Command line also works:

    python multimic.py --list
    python multimic.py -d 3 7 9 -t 60      # record devices 3, 7, 9 for 60 s

## Tips

- Keep the **Windows WASAPI** audio API (the default). MME shows names cut off
  at 31 characters, and its latency is higher.
- Identical mics show up as "Microphone (USB Audio)" and
  "Microphone (2- USB Audio)". Tap each one and watch which meter moves.
- "drops" means a buffer was lost. Spread mics across different USB ports or
  controllers, and avoid unpowered hubs.
- Each USB mic has its own clock, so over long takes (an hour or more) the
  files can drift apart by a few tens of milliseconds. For long sessions where
  sync has to be perfect, use one multi-input audio interface instead.
- A WAV file tops out at 4 GB (about 4 hours of 48 kHz/24-bit stereo per mic).
  Use FLAC for longer takes.
