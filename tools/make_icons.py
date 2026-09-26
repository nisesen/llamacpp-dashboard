#!/usr/bin/env python3
"""Render the dashboard's app icons: stdlib only, no image library needed.

The mark is the top bar's own: a chart line over a baseline, white on the
blue-to-violet gradient of --series-1 -> --series-7. Icons are full-bleed
squares with the mark inside the central 60%, so one image serves as both
an ordinary and a maskable icon (Android crops to a circle, iOS rounds the
corners). Run from the repo root:  python3 tools/make_icons.py
"""

import itertools
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
C0, C1 = (0x39, 0x87, 0xE5), (0x90, 0x85, 0xE9)  # --series-1, --series-7
# The mark, in its 16-unit viewBox (index.html's .brand .mark svg).
STROKES = [[(2, 11.5), (5.6, 7), (8.4, 9.6), (13.8, 4)], [(2, 14), (14, 14)]]
WIDTH = 1.8
BBOX = (2 - WIDTH / 2, 4 - WIDTH / 2, 14 + WIDTH / 2, 14 + WIDTH / 2)


def seg_dist(px, py, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def render(size, fill=0.60):
    x0, y0, x1, y1 = BBOX
    scale = size * fill / max(x1 - x0, y1 - y0)
    ox = size / 2 - (x0 + x1) / 2 * scale
    oy = size / 2 - (y0 + y1) / 2 * scale
    segs = [
        ((ax * scale + ox, ay * scale + oy), (bx * scale + ox, by * scale + oy))
        for line in STROKES
        for (ax, ay), (bx, by) in itertools.pairwise(line)
    ]
    half = WIDTH * scale / 2
    # CSS linear-gradient(145deg): 0deg points up, angles run clockwise.
    ang = math.radians(145)
    ux, uy = math.sin(ang), -math.cos(ang)
    ext = (abs(ux) + abs(uy)) * size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter: none
        cy = y + 0.5
        for x in range(size):
            cx = x + 0.5
            t = ((cx - size / 2) * ux + (cy - size / 2) * uy) / (2 * ext) + 0.5
            bg = [round(a + (b - a) * t) for a, b in zip(C0, C1)]
            d = min(seg_dist(cx, cy, a, b) for a, b in segs)
            cover = max(0.0, min(1.0, half + 0.5 - d))  # 1-px anti-aliased edge
            row += bytes(round(c + (255 - c) * cover) for c in bg) + b"\xff"
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size, raw):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <defs><linearGradient id="g" x1="0.21" y1="0.09" x2="0.79" y2="0.91">
    <stop offset="0" stop-color="#3987e5"/><stop offset="1" stop-color="#9085e9"/>
  </linearGradient></defs>
  <rect width="16" height="16" rx="3.6" fill="url(#g)"/>
  <g transform="translate(2.24 2.4) scale(0.72)" fill="none" stroke="#fff" stroke-width="1.8"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M2 11.5 5.6 7l2.8 2.6L13.8 4"/><path d="M2 14h12"/>
  </g>
</svg>
"""

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
        (OUT / name).write_bytes(render(size))
        print("wrote", OUT / name)
    (OUT / "icon.svg").write_text(SVG)
    print("wrote", OUT / "icon.svg")
