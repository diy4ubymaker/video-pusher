import os
import shutil
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The tests exercise the logic, not the window. If Tkinter isn't installed (e.g. a minimal
# CI image), stub it so main.py can still be imported.
try:
    import tkinter  # noqa: F401
except ImportError:
    tk = types.ModuleType("tkinter")
    tk.Tk = object
    for name in ("ttk", "filedialog", "scrolledtext", "colorchooser"):
        sub = types.ModuleType("tkinter." + name)
        setattr(tk, name, sub)
        sys.modules["tkinter." + name] = sub
    sys.modules["tkinter"] = tk


def _tool(name):
    exe = name + ".exe" if os.name == "nt" else name
    local = os.path.join(ROOT, "bin", exe)
    return local if os.path.isfile(local) else shutil.which(name)


@pytest.fixture(scope="session")
def ffmpeg_tools():
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    if not (ffmpeg and ffprobe):
        pytest.skip("ffmpeg/ffprobe not found in bin/ or on PATH")
    return ffmpeg, ffprobe
