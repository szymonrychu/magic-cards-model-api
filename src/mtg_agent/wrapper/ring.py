"""The last 64 KiB of PTY output.

This is the only window into the interactive TUI, and it is deliberately a
narrow one: it detects boot dialogs and provides a tail for debug logging.
Turn results NEVER come from here - they come from the Stop hook and the
transcript. Parsing a terminal for results is how you build something that
breaks on the next release.
"""

from __future__ import annotations

import re
import threading

RING_CAP = 64 * 1024

_CSI = re.compile(r"\x1b\[[0-9;?:]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_OTHER = re.compile(r"\x1b[@-Z\\-_=>]")


def strip_ansi(text: str) -> str:
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    text = _OTHER.sub("", text)
    return "".join(c for c in text if not (c == "\x1b" or (c < "\x20" and c not in "\n\t")))


def strip_ws(text: str) -> str:
    return "".join(c for c in text if c not in " \t\n\r")


class Ring:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._total = 0

    def write(self, data: bytes) -> int:
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > RING_CAP:
                del self._buf[: len(self._buf) - RING_CAP]
            self._total += len(data)
        return len(data)

    def reset(self) -> None:
        """Clear the buffered output so dialog detection starts fresh, while
        leaving `written` monotonic. Called once per relaunch: a freshly
        booted claude must navigate against its own output, not the dead
        process's stale dialog text."""
        with self._lock:
            self._buf.clear()

    def written(self) -> int:
        with self._lock:
            return self._total

    def _text(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", "replace")

    def contains(self, needle: str) -> bool:
        return strip_ws(needle) in strip_ws(strip_ansi(self._text()))

    def tail(self, n: int) -> str:
        return strip_ansi(self._text())[-n:]
