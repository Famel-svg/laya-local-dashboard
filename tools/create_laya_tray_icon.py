"""Create a small multi-resolution Windows icon for Laya Local (stdlib only)."""
from __future__ import annotations

import struct
from pathlib import Path


OUTPUT = Path(__file__).resolve().parents[1] / "prototype" / "laya_tray.ico"
SIZES = (16, 32, 48)


def pixel(size: int, x: float, y: float) -> tuple[int, int, int, int]:
    margin, radius = size * 0.06, size * 0.22
    left, top, right, bottom = margin, margin, size - margin, size - margin
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
        return (0, 0, 0, 0)

    scale = size / 48
    bg = (17, 26, 39, 255)
    mark = (237, 243, 252, 255)
    accent = (85, 213, 230, 255)
    vertical = 15 * scale <= x <= 21 * scale and 10 * scale <= y <= 37 * scale
    baseline = 15 * scale <= x <= 37 * scale and 30 * scale <= y <= 37 * scale
    dot = ((x - 35 * scale) ** 2 + (y - 14 * scale) ** 2) <= (3.4 * scale) ** 2
    if dot:
        return accent
    if vertical or baseline:
        return mark
    return bg


def dib(size: int) -> bytes:
    pixels = bytearray()
    for row in range(size - 1, -1, -1):
        for col in range(size):
            samples = [pixel(size, col + (sx + .5) / 4, row + (sy + .5) / 4)
                       for sy in range(4) for sx in range(4)]
            alpha = sum(p[3] for p in samples) // len(samples)
            if alpha:
                rgb = [sum(p[i] * p[3] for p in samples) // max(1, sum(p[3] for p in samples))
                       for i in range(3)]
            else:
                rgb = [0, 0, 0]
            pixels.extend((rgb[2], rgb[1], rgb[0], alpha))

    mask_row = ((size + 31) // 32) * 4
    mask = bytes(mask_row * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels), 0, 0, 0, 0)
    return header + pixels + mask


def main() -> None:
    frames = [dib(size) for size in SIZES]
    directory = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = []
    for size, frame in zip(SIZES, frames):
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(directory + b"".join(entries) + b"".join(frames))
    print(f"Created {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
