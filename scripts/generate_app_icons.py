"""Write `ui/icons/icon-192.png` and `ui/icons/icon-512.png`, the home-screen
app icons `ui/manifest.webmanifest` and `index.html`'s apple-touch-icon name.

A dark rounded square in the board's own `--bg`, with a mana-symbol-like
glyph in `--accent`: a ring around a diamond. Drawn with signed distances
(one pixel of antialiasing) and encoded with the stdlib only, so it needs no
image library. Deterministic - run twice, git status stays clean.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICONS = Path(__file__).resolve().parents[1] / "ui" / "icons"
SIZES = (192, 512)
BG = (0x10, 0x13, 0x1A)
EDGE = (0x3C, 0x46, 0x5A)
ACCENT = (0x6B, 0xA4, 0xFF)


def _coverage(distance: float) -> float:
    """How much of a pixel a shape covers, from the signed distance (in
    pixels) of the pixel centre to its edge - negative inside."""
    return min(1.0, max(0.0, 0.5 - distance))


def _rounded_square(x: float, y: float, half: float, radius: float) -> float:
    qx, qy = abs(x) - half + radius, abs(y) - half + radius
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _over(
    below: tuple[float, ...], colour: tuple[int, int, int], alpha: float
) -> tuple[float, ...]:
    return tuple(b + (c - b) * alpha for b, c in zip(below, colour, strict=True))


def render(size: int) -> bytes:
    scale = size / 512
    half = size / 2
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter type: none
        for px in range(size):
            x, y = px + 0.5 - half, py + 0.5 - half
            square = _rounded_square(x, y, half - 0.5, 112 * scale)
            body = _coverage(square)
            if body <= 0.0:
                rows.extend((0, 0, 0, 0))
                continue
            colour: tuple[float, ...] = _over(BG, EDGE, 1.0 - _coverage(square + 10 * scale))
            radius = math.hypot(x, y)
            ring = max(radius - 168 * scale, 128 * scale - radius)
            colour = _over(colour, ACCENT, _coverage(ring))
            diamond = (abs(x) + abs(y) - 84 * scale) / math.sqrt(2)
            colour = _over(colour, ACCENT, _coverage(diamond))
            rows.extend(round(c) for c in colour)
            rows.append(round(255 * body))
    return _png(size, bytes(rows))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(size: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (ICONS / f"icon-{size}.png").write_bytes(render(size))


if __name__ == "__main__":
    main()
