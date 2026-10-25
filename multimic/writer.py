"""Sound files that split into parts."""

import soundfile as sf

from . import config


class SplitWriter:
    """Sound file writer that rolls over to name_part02, name_part03, ...
    after a set number of frames, and before a WAV would pass 4 GB."""

    def __init__(self, base, ext, samplerate, channels, fmt, subtype,
                 split_frames=None):
        self.base, self.ext = base, ext
        self.args = dict(samplerate=samplerate, channels=channels,
                         format=fmt, subtype=subtype)
        limits = [split_frames] if split_frames else []
        if fmt == "WAV":
            limits.append(config.WAV_MAX_BYTES
                          // (channels * config.BYTES_PER_SAMPLE[subtype]))
        self.limit = min(limits) if limits else None
        self.numbered = bool(split_frames)
        self.part = 0
        self.file = None
        self.in_file = 0
        self.paths = []
        self._open()  # open the first file now so errors show at start

    def _open(self):
        self.part += 1
        if self.numbered or self.part > 1:
            path = f"{self.base}_part{self.part:02d}{self.ext}"
        else:
            path = self.base + self.ext
        self.file = sf.SoundFile(path, "w", **self.args)
        self.paths.append(path)
        self.in_file = 0

    def write(self, block):
        while len(block):
            if self.file is None:
                self._open()
            room = len(block)
            if self.limit:
                room = min(room, self.limit - self.in_file)
            self.file.write(block[:room])
            self.in_file += room
            block = block[room:]
            if self.limit and self.in_file >= self.limit:
                self.file.close()
                self.file = None

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
