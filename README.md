# MultiMic

Records several USB microphones at the same time on Windows. Each mic gets its
own file, and all files start at the same moment.

## Run

1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
2. Double-click `run.bat`. The first launch installs the libraries.

To build a standalone `MultiMic.exe`, double-click `build_exe.bat`.

## Use

- **Pick mics:** tick each mic you want. Its level meter starts moving right
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
- Press **Record** (or Space) to start and again to stop.

Files go to `<output>\session_YYYY-MM-DD_HH-MM-SS\`:

    00_mixdown.wav       (if mixdown is on)
    01_Host.wav
    02_Guest.wav

Everything you set (mics ticked, names, gain, mute, folder, format and so on)
is remembered for next time in `%APPDATA%\MultiMic\settings.json`.

## Command line

    python multimic.py --list
    python multimic.py -d 3 7 -n Host Guest -m stereo -s 30 -t 3600

`-d` devices, `-n` names, `-m off|stereo|multitrack` mixdown,
`-s` minutes per file, `-t` stop after N seconds. See `--help`.

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
- Each USB mic has its own clock, so over long takes (an hour or more) the
  files can drift apart by a few tens of milliseconds. For long sessions where
  sync has to be perfect, use one multi-input audio interface instead.
