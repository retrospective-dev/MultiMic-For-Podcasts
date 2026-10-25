import os
import sys
import types

# The tests never touch audio hardware: sounddevice is replaced by an empty
# module and audio blocks are fed straight into each mic's callback.
sys.modules.setdefault("sounddevice", types.ModuleType("sounddevice"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
