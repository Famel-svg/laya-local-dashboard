"""Compose 4:5 (1200x1500) versions of the screenshots: full view on top, zoomed detail below.

    python tools/social_images.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen

DOCS = Path(__file__).resolve().parent.parent / "docs"
W, H, IMG_W = 1200, 1500, 1120
BG, TEXT, MUTED, LINE, ACCENT = "#0b1018", "#e8eef8", "#93a3ba", "#223044", "#72a7ff"
CHART = QRect(14, 171, 834, 416)  # chart card inside a 1320x900 screenshot
SHOTS = [
    ("dashboard.png", "social-visao-geral.png", "Laya Local", "tokens, qualidade das decisões e estado do modelo",
     "Tokens por dia, com o cache separado em escala própria"),
    ("dashboard-custo.png", "social-custo.png", "Custo hipotético por dia", "todo o histórico, por componente",
     "Detalhe do dia ao passar o mouse"),
]


def rounded(p: QPainter, image: QImage, x: float, y: float) -> None:
    frame = QRectF(x, y, image.width(), image.height())
    path = QPainterPath()
    path.addRoundedRect(frame, 12, 12)
    p.save()
    p.setClipPath(path)
    p.drawImage(frame.topLeft(), image)
    p.restore()
    p.setPen(QPen(QColor(LINE), 1.5))
    p.drawPath(path)


def compose(source: str, target: str, title: str, subtitle: str, detail: str) -> None:
    shot = QImage(str(DOCS / source))
    full = shot.scaledToWidth(IMG_W, Qt.SmoothTransformation)
    zoom = shot.copy(CHART).scaledToWidth(IMG_W, Qt.SmoothTransformation)
    canvas = QImage(W, H, QImage.Format_ARGB32)
    canvas.fill(QColor(BG))
    p = QPainter(canvas)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    x = (W - IMG_W) // 2
    used = 44 + full.height() + 52 + zoom.height()
    y = (H - used) // 2
    p.setFont(QFont("Segoe UI", 17, QFont.Bold))
    p.setPen(QColor(TEXT))
    p.drawText(x, y + 22, title)
    offset = p.fontMetrics().horizontalAdvance(title) + 14
    p.setFont(QFont("Segoe UI", 13))
    p.setPen(QColor(MUTED))
    p.drawText(x + offset, y + 22, subtitle)
    y += 44
    rounded(p, full, x, y)
    y += full.height() + 18
    p.setFont(QFont("Segoe UI", 12, QFont.Bold))
    p.setPen(QColor(ACCENT))
    p.drawText(x, y + 18, detail.upper())
    y += 34
    rounded(p, zoom, x, y)
    p.end()
    print(target, canvas.save(str(DOCS / target), "PNG"))


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841 - needed for font rendering
    for shot in SHOTS:
        compose(*shot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
