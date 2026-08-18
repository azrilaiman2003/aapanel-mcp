#!/usr/bin/env python3
# coding: utf-8
"""Draw plugin/aapanel_mcp/icon.png.

Written by hand with zlib and struct rather than Pillow: the repo has no third-party
dependencies and this is the only image it needs. Rendered at 4x and downsampled, which
is where the smooth edges come from.

The mark is a small graph — one lit node connected to two quiet ones — for an agent
reaching into a server's parts. Green is aaPanel's own accent, so the icon sits in the
app store grid without shouting.
"""

import os
import struct
import zlib

SIZE = 128
SCALE = 4
BG = (0x20, 0x24, 0x2a)
ACCENT = (0x20, 0xa5, 0x3a)
LIGHT = (0xf2, 0xf4, 0xf6)
EDGE = (0x8b, 0x95, 0xa1)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'plugin', 'aapanel_mcp', 'icon.png')


def rounded_square(x, y, size, radius):
    """Signed test: is (x, y) inside a rounded square of `size` starting at 0?"""
    left, top, right, bottom = 0.0, 0.0, float(size), float(size)
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    if left <= x <= right and top <= y <= bottom:
        if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
            return True
        return (radius <= x <= size - radius) or (radius <= y <= size - radius)
    return False


def in_disc(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_segment(x, y, ax, ay, bx, by, width):
    """Distance from the point to the line segment ab, within `width`/2."""
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return in_disc(x, y, ax, ay, width / 2.0)
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length))
    px, py = ax + t * dx, ay + t * dy
    return (x - px) ** 2 + (y - py) ** 2 <= (width / 2.0) ** 2


def render():
    big = SIZE * SCALE
    # Node positions, in 128-space, scaled up.
    nodes = [(64, 34, 11, ACCENT),    # the lit one, top centre
             (36, 92, 9, LIGHT),
             (92, 92, 9, LIGHT)]
    edges = [((64, 34), (36, 92)), ((64, 34), (92, 92)), ((36, 92), (92, 92))]

    rows = []
    for py in range(big):
        row = bytearray()
        y = (py + 0.5) / SCALE
        for px in range(big):
            x = (px + 0.5) / SCALE
            if not rounded_square(x, y, SIZE, 26):
                row += bytes((0, 0, 0, 0))
                continue
            colour = BG
            for (ax, ay), (bx, by) in edges:
                if in_segment(x, y, ax, ay, bx, by, 4.5):
                    colour = EDGE
                    break
            for cx, cy, r, node_colour in nodes:
                if in_disc(x, y, cx, cy, r):
                    colour = node_colour
                    break
                if in_disc(x, y, cx, cy, r + 3.5) and colour == EDGE:
                    colour = BG
            row += bytes(colour + (255,))
        rows.append(bytes(row))

    return downsample(rows, big)


def downsample(rows, big):
    """Box filter from `big` down to SIZE, which is what smooths the curves."""
    out = []
    for y in range(SIZE):
        line = bytearray()
        for x in range(SIZE):
            r = g = b = a = 0
            for sy in range(SCALE):
                row = rows[y * SCALE + sy]
                for sx in range(SCALE):
                    index = ((x * SCALE) + sx) * 4
                    r += row[index]
                    g += row[index + 1]
                    b += row[index + 2]
                    a += row[index + 3]
            count = SCALE * SCALE
            line += bytes((r // count, g // count, b // count, a // count))
        out.append(bytes(line))
    return out


def write_png(path, rows):
    raw = b''.join(b'\x00' + row for row in rows)

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body))

    header = struct.pack('>IIBBBBB', SIZE, SIZE, 8, 6, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', header)
           + chunk(b'IDAT', zlib.compress(raw, 9))
           + chunk(b'IEND', b''))
    with open(path, 'wb') as fp:
        fp.write(png)
    return len(png)


if __name__ == '__main__':
    size = write_png(OUT, render())
    print('wrote %s (%d bytes, %dx%d)' % (OUT, size, SIZE, SIZE))
