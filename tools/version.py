"""Prints the app version (used by the release workflow)."""

import os
import re

path = os.path.join(os.path.dirname(__file__), "..", "multimic", "__init__.py")
with open(path, encoding="utf-8") as f:
    print(re.search(r'__version__ = "([^"]+)"', f.read()).group(1))
