"""Small formatting and file helpers."""

import os
import re
import shutil
import sys

import numpy as np


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "mic"


def fmt_time(seconds, millis=False):
    s = int(seconds)
    text = f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    if millis:
        text += f".{int(round((seconds - s) * 1000)) % 1000:03d}"
    return text


def fmt_duration(seconds):
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{max(0, seconds) / 60:.0f} min"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def to_db(peak):
    return 20 * np.log10(peak) if peak > 1e-6 else -120.0


def disk_free(path):
    """Free bytes on the drive holding path (or its nearest existing
    parent), or None if it can't be found."""
    path = os.path.abspath(path)
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def resource_path(name):
    """A file shipped with the app: next to the package, or inside the
    PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
