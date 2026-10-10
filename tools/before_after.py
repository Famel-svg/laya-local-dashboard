"""Compose docs/antes-depois.png (4:5) from the old Tkinter and the new PySide6 screenshots.

    python tools/before_after.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen

DOCS = Path(__file__).resolve().parent.parent / "docs"
W, H, IMG_W = 1200, 1500, 1010
BG, TEXT, MUTED, LINE = "#0b1018", "#e8eef8", "#93a3ba", "#223044"


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841 - needed for font rendering
    canvas = QImage(W, H, QImage.Format_ARGB32)
    canvas.fill(QColor(BG))
    p = QPainter(canvas)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    y = 34
    for tag, color, title, name in (
            ("ANTES", "#ff6b78", "Tkinter · gráfico vazio, 2 de 5 cartões sem dado", "antes-tkinter.png"),
            ("DEPOIS", "#4ad295", "PySide6 · tokens por dia, qualidade, status por chamada", "dashboard.png")):
        image = QImage(str(DOCS / name)).scaledToWidth(IMG_W, Qt.SmoothTransformation)
        x = (W - IMG_W) // 2
        font = QFont("Segoe UI", 15, QFont.Bold)
        p.setFont(font)
        p.setPen(QColor(color))
        p.drawText(x, y + 18, tag)
        offset = p.fontMetrics().horizontalAdvance(tag) + 14
        p.setFont(QFont("Segoe UI", 13))
        p.setPen(QColor(MUTED))
        p.drawText(x + offset, y + 18, title)
        y += 34
        frame = QRectF(x, y, image.width(), image.height())
        path = QPainterPath()
        path.addRoundedRect(frame, 12, 12)
        p.save()
        p.setClipPath(path)
        p.drawImage(frame.topLeft(), image)
        p.restore()
        p.setPen(QPen(QColor(LINE), 1.5))
        p.drawPath(path)
        y += image.height() + 26
    p.end()
    out = DOCS / "antes-depois.png"
    print(out, canvas.save(str(out), "PNG"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
