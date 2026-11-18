"""ANSI colours, enabled only when writing to a terminal.

Override with ``MGIT_COLOR=always|never``; ``NO_COLOR`` (https://no-color.org) is honoured.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

CODES = {
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "bold": "1",
    "dim": "2",
}

_vt_enabled = False


def _enable_windows_vt() -> None:
    """Turn on ANSI escape processing in the Windows console (no-op elsewhere)."""
    global _vt_enabled
    if _vt_enabled or os.name != "nt":
        return
    _vt_enabled = True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # pragma: no cover - best effort only
        pass


def enabled(stream: TextIO | None = None) -> bool:
    setting = os.environ.get("MGIT_COLOR", "auto").lower()
    if setting == "always":
        _enable_windows_vt()
        return True
    if setting == "never" or "NO_COLOR" in os.environ:
        return False
    stream = stream or sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    if tty:
        _enable_windows_vt()
    return tty


def paint(text: str, *styles: str, on: bool = True) -> str:
    if not on or not styles:
        return text
    seq = ";".join(CODES[s] for s in styles)
    return f"\033[{seq}m{text}\033[0m"
