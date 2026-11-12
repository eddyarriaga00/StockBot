"""Tiny line-chart PNG renderer in pure Python (no Pillow/matplotlib), used for Discord messages."""
import struct
import zlib

BG, GRID, TEXT = (30, 31, 34), (58, 60, 66), (181, 186, 193)
GREEN, RED = (34, 195, 142), (255, 92, 92)

# 5x7 bitmap font: just what price labels need.
FONT = {
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "$": "00100 01111 10100 01110 00101 11110 00100", ",": "00000 00000 00000 00000 00110 00100 01000",
    ".": "00000 00000 00000 00000 00000 00110 00110", "%": "11001 11010 00010 00100 01000 01011 10011",
    "-": "00000 00000 00000 11111 00000 00000 00000", "+": "00000 00100 00100 11111 00100 00100 00000",
    "k": "10000 10000 10010 10100 11000 10100 10010", " ": "00000 00000 00000 00000 00000 00000 00000",
}


class Canvas:
    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def set(self, x, y, c, alpha=1.0):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            if alpha >= 1:
                self.px[i:i + 3] = bytes(c)
            else:
                for k in range(3):
                    self.px[i + k] = int(self.px[i + k] * (1 - alpha) + c[k] * alpha)

    def vline(self, x, y0, y1, c, alpha=1.0):
        for y in range(max(0, min(y0, y1)), min(self.h, max(y0, y1) + 1)):
            self.set(x, y, c, alpha)

    def text(self, x, y, s, c=TEXT, scale=2):
        for ch in s:
            rows = FONT.get(ch, FONT[" "]).split()
            for ry, row in enumerate(rows):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                self.set(x + rx * scale + dx, y + ry * scale + dy, c)
            x += 6 * scale

    def png(self):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def money(v):
    return f"${v / 1000:,.1f}k" if abs(v) >= 10000 else f"${v:,.0f}"


def line_chart(values, baseline=None, width=900, height=340, fmt=money):
    """PNG bytes for a line chart of `values` (oldest first). Green if it ends above `baseline`, else red.
    Returns None when there aren't at least two points."""
    values = [float(v) for v in values]
    if len(values) < 2:
        return None
    base = values[0] if baseline is None else float(baseline)
    c = Canvas(width, height)
    left, right, top, bottom = 14, width - 104, 18, height - 18
    lo, hi = min(min(values), base), max(max(values), base)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    ypx = lambda v: int(round(bottom - (v - lo) / (hi - lo) * (bottom - top)))
    for k in range(5):  # grid
        gy = top + (bottom - top) * k // 4
        for x in range(left, right, 2):
            c.set(x, gy, GRID)
    by = ypx(base)
    for x in range(left, right):  # dashed baseline (starting value)
        if (x // 6) % 2 == 0:
            c.set(x, by, TEXT, 0.55)
    color = GREEN if values[-1] >= base else RED
    n = right - left
    ys = []
    for x in range(n):  # resample to one value per pixel column
        pos = x / (n - 1) * (len(values) - 1)
        i = int(pos)
        f = pos - i
        ys.append(ypx(values[i] * (1 - f) + values[min(i + 1, len(values) - 1)] * f))
    for x in range(n):
        y, y2 = ys[x], ys[min(x + 1, n - 1)]
        c.vline(left + x, min(y, y2), bottom, color, 0.13)        # soft fill under the line
        c.vline(left + x, min(y, y2) - 1, max(y, y2) + 1, color)  # the line, 3px thick
    lx = right + 10
    labels = [(ypx(max(values)), fmt(max(values)), TEXT), (ypx(min(values)), fmt(min(values)), TEXT),
              (ys[-1], fmt(values[-1]), color)]
    used = []
    for y, s, col in reversed(labels):  # last value wins a spot first
        y = max(top, min(bottom - 14, y - 7))
        if all(abs(y - u) >= 18 for u in used):
            used.append(y)
            c.text(lx, y, s, col)
    return c.png()
