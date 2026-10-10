"""Laya Local - dashboard nativo em PySide6 (Qt 6 Widgets).

Somente widgets nativos e gráficos desenhados com QPainter: sem QtWebEngine, sem HTML,
sem QtCharts (basta o pacote PySide6-Essentials).

Uso:
    LayaLocal.exe                  # abre a janela (ou traz a instância existente para a frente)
    LayaLocal.exe --background     # inicia oculto na bandeja (login do Windows)
    python laya_app.py --screenshot out.png [--sample] [--size 1000x720]
"""
from __future__ import annotations

import time

_T0 = time.perf_counter()

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

API_URL = "http://127.0.0.1:8766/api"
API_PORT, MCP_PORT = 8766, 8767
POLL_MS = 5_000
HIDDEN_POLL_S = 60  # oculto na bandeja: só confere os serviços de vez em quando
USD_BRL = 4.9801
SAMPLE = Path(__file__).resolve().parent.parent / "docs" / "demo_api.json"
INSTANCE_KEY = "LayaLocal.dashboard"
SERVICE_RETRY_S = 60  # intervalo mínimo entre tentativas automáticas de subir os serviços

# ---------------------------------------------------------------- paleta
BG, CARD, CARD2, LINE = "#0b1018", "#121a26", "#18222f", "#223044"
TEXT, MUTED, FAINT = "#e8eef8", "#93a3ba", "#5d6d85"
BLUE, GREEN, AMBER, RED, TEAL, PURPLE = "#72a7ff", "#4ad295", "#f2bd70", "#ff6b78", "#4dd4c0", "#b49aff"
ORANGE = "#ff9a5c"
C_CACHE, C_INPUT, C_OUTPUT = "#3f64a8", TEAL, AMBER
GRID = "#1c2838"

STATUS = {  # passed -> (rótulo, cor)
    "yes": ("Sucesso", GREEN),
    "error": ("Falha", RED),
    "rejected": ("Rejeitada", ORANGE),  # payload recusado na validação; não chegou ao modelo
    "no": ("Não roteada", AMBER),
    "route only": ("Só rota", BLUE),
    "status": ("Status", MUTED),
}
ROUTES = {
    "english": ("Inglês", BLUE),
    "multilingual": ("Multilíngue", PURPLE),
    "typed-decisions": ("Decisões tipadas", TEAL),
    "not routed": ("Não roteada", FAINT),
}


# ---------------------------------------------------------------- formatação pt-BR
def fmt_int(v) -> str:
    return f"{int(round(v or 0)):,}".replace(",", ".")


def fmt_dec(v: float, nd: int = 1) -> str:
    return f"{v:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_compact(v, nd: int = 1) -> str:
    v = float(v or 0)
    a = abs(v)
    if a >= 1e9:
        return f"{fmt_dec(v / 1e9, max(nd, 1))} bi"
    if a >= 1e6:
        return f"{fmt_dec(v / 1e6, nd)} mi"
    if a >= 1e4:
        return f"{fmt_dec(v / 1e3, 0)} mil"
    if a >= 1e3:
        return f"{fmt_dec(v / 1e3, 1)} mil"
    return fmt_int(v)


def fmt_brl(v: float, nd: int = 2) -> str:
    return f"R$ {fmt_dec(v, nd)}"


def fmt_pct(v: float, nd: int = 1) -> str:
    return f"{fmt_dec(v, nd)} %"


def fmt_ms(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{fmt_dec(v / 1000, 1)} s" if v >= 1000 else f"{fmt_int(v)} ms"


def parse_ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return None


def ago(ts: datetime | None) -> str:
    if ts is None:
        return "sem registro"
    sec = max(0, (datetime.now().astimezone() - ts).total_seconds())
    if sec < 90:
        return "há instantes"
    if sec < 3600:
        return f"há {int(sec // 60)} min"
    if sec < 86400:
        return f"há {int(sec // 3600)} h"
    d = int(sec // 86400)
    return f"há {d} dia{'s' if d != 1 else ''}"


def nice_ceil(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 1.5, 2, 3, 4, 5, 6, 8, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def question_preview(q) -> str:
    """Extrai um trecho legível de uma pergunta que normalmente é JSON."""
    text = str(q or "").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        return " ".join(text.split())

    def first_str(o):
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            for key in ("task", "task_summary", "goal", "request", "instructions", "context"):
                if key in o:
                    found = first_str(o[key])
                    if found:
                        return found
            for value in o.values():
                found = first_str(value)
                if found:
                    return found
        if isinstance(o, list):
            for value in o:
                found = first_str(value)
                if found:
                    return found
        return ""

    return " ".join((first_str(obj) or text).split())


def pretty_question(q) -> str:
    text = str(q or "")
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        return text


# ---------------------------------------------------------------- coleta (thread de trabalho)
def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def collect(force_sample: bool = False) -> dict:
    out = {"data": None, "source": "none", "api": False, "mcp": False, "error": None}
    if not force_sample:
        try:
            req = urllib.request.Request(API_URL, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                out["data"] = json.loads(resp.read().decode("utf-8"))
                out["source"] = "api"
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira estado offline
            out["error"] = type(exc).__name__
        out["api"] = port_open(API_PORT)
        out["mcp"] = port_open(MCP_PORT)
    if out["data"] is None and force_sample:
        try:
            out["data"] = json.loads(SAMPLE.read_text(encoding="utf-8"))
            out["source"] = "sample"
        except Exception as exc:  # noqa: BLE001
            out["error"] = out["error"] or type(exc).__name__
    return out


# ---------------------------------------------------------------- serviços locais
def _runtime_dir() -> Path:
    return Path.home() / ".codex" / "laya-runtime"


def _log_dir() -> Path:
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Codex" / "Laya"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def _service_env() -> dict:
    env = os.environ.copy()
    env.update({"LAYA_TRANSPORT": "streamable-http", "LAYA_MCP_HOST": "127.0.0.1", "LAYA_MCP_PORT": str(MCP_PORT),
                "LAYA_HOST": "127.0.0.1", "LAYA_PORT": str(MCP_PORT), "LAYA_DASHBOARD_PORT": str(API_PORT),
                "LAYA_OPEN_DASHBOARD": "0", "LAYA_PRELOAD": "0", "PYTHONUTF8": "1",
                "LAYA_AUDIT_CALLER": "Codex local / agente não identificado"})
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # EXE one-file: o Python do runtime não pode herdar as DLLs/variáveis do pacote
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(meipass))
        for key in [k for k in env if k.startswith(("_MEI", "_PYI")) or k in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH")]:
            env.pop(key, None)
    return env


def ensure_services() -> str:
    """Sobe MCP (8767) e API do dashboard (8766) quando ausentes. Retorna o que foi feito."""
    runtime = _runtime_dir()
    python = runtime / ".venv" / "Scripts" / "pythonw.exe"
    mcp_script, api_script = runtime / "laya_mcp_dashboard.py", runtime / "laya_dashboard.py"
    if not (python.is_file() and mcp_script.is_file() and api_script.is_file()):
        return "runtime ausente"
    if not port_open(MCP_PORT):
        script, what = mcp_script, "MCP + API"  # o wrapper MCP também sobe a API
    elif not port_open(API_PORT):
        script, what = api_script, "API"
    else:
        return "já ativos"
    if getattr(sys, "frozen", False) and os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with (_log_dir() / "local-services.log").open("a", encoding="utf-8") as log:
        log.write(f"{datetime.now().isoformat(timespec='seconds')} iniciando {what}: {script.name}\n")
        log.flush()
        subprocess.Popen([str(python), str(script)], cwd=runtime, env=_service_env(), stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    return f"iniciando {what}"


def model_action(name: str) -> str:
    """POST /model/warm | /model/unload na API local."""
    req = urllib.request.Request(f"http://127.0.0.1:{API_PORT}/model/{name}", data=b"", method="POST",
                                 headers={"X-Laya-Local": "1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return str(json.loads(resp.read().decode("utf-8")).get("result"))


def _listener_pids() -> set[int]:
    """PIDs que escutam em 127.0.0.1:8766/8767 (via netstat; sem dependências)."""
    try:
        text = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=8,
                              creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4].isdigit() \
                and parts[1] in (f"127.0.0.1:{API_PORT}", f"127.0.0.1:{MCP_PORT}"):
            pids.add(int(parts[4]))
    return pids


def restart_services() -> str:
    """Encerra só processos python que escutam nas portas da Laya e sobe o runtime de novo."""
    killed = 0
    for pid in _listener_pids():
        try:
            image = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                   text=True, timeout=8, creationflags=0x08000000).stdout.lower()
            if not image.startswith(('"python.exe"', '"pythonw.exe"')):
                return f"porta ocupada por outro programa (PID {pid}); nada foi encerrado"
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=8,
                           creationflags=0x08000000)
            killed += 1
        except (OSError, subprocess.SubprocessError):
            return "falha ao encerrar serviço"
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline and (port_open(API_PORT) or port_open(MCP_PORT)):
        time.sleep(0.25)
    return f"{killed} encerrado(s) · {ensure_services()}"


# ---------------------------------------------------------------- Qt
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QFontMetrics, QLinearGradient, QPainter, QPainterPath,  # noqa: E402
                           QPalette, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,  # noqa: E402
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStyledItemDelegate, QSystemTrayIcon, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

QSS = f"""
* {{ font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 12px; color: {TEXT}; }}
QWidget#root {{ background: {BG}; }}
QFrame[card="true"] {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame[tile="true"] {{ background: {CARD2}; border: none; border-radius: 7px; }}
QLabel {{ background: transparent; border: none; }}
QLabel[role="brand"] {{ font-size: 16px; font-weight: 700; }}
QLabel[role="logo"] {{ background: {BLUE}; color: #0d1524; font-size: 15px; font-weight: 800; border-radius: 7px; }}
QLabel[role="chip"] {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 11px; padding: 2px 10px;
                       font-size: 11px; font-weight: 600; }}
QLabel[role="cap"] {{ color: {MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 0.6px; }}
QLabel[role="title"] {{ font-size: 13px; font-weight: 700; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 11px; }}
QLabel[role="kpi"] {{ font-size: 25px; font-weight: 700; }}
QLabel[role="tile"] {{ font-size: 16px; font-weight: 700; }}
QPushButton {{ background: {CARD2}; border: 1px solid {LINE}; border-radius: 6px; padding: 5px 12px; font-weight: 600; }}
QPushButton:hover {{ background: #1f2c3d; border-color: #33465f; }}
QPushButton:pressed {{ background: #16202d; }}
QPushButton:disabled {{ color: {FAINT}; }}
QPushButton[primary="true"] {{ background: #2f62b8; border-color: #3d73cf; }}
QPushButton[primary="true"]:hover {{ background: #3870cf; }}
QPushButton[seg="true"] {{ border-radius: 0; border-left-width: 0; padding: 4px 11px; color: {MUTED}; font-size: 11px; }}
QPushButton[seg="true"][pos="first"] {{ border-left-width: 1px; border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QPushButton[seg="true"][pos="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QPushButton[seg="true"]:checked {{ background: #26497f; color: {TEXT}; border-color: #3d73cf; }}
QComboBox {{ background: {CARD2}; border: 1px solid {LINE}; border-radius: 5px; padding: 1px 6px; font-size: 11px; min-height: 18px; }}
QComboBox:hover {{ border-color: #33465f; }}
QComboBox QAbstractItemView {{ background: {CARD2}; border: 1px solid {LINE}; selection-background-color: #26497f; outline: 0; }}
QCheckBox {{ color: {MUTED}; font-size: 11px; spacing: 5px; background: transparent; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid #3a4d68; border-radius: 3px; background: {CARD2}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; border-color: {BLUE}; }}
QTableWidget {{ background: transparent; border: none; gridline-color: transparent; outline: 0;
                selection-background-color: #1c3252; selection-color: {TEXT}; }}
QTableWidget::item {{ padding: 0 6px; border-bottom: 1px solid {GRID}; }}
QHeaderView::section {{ background: {CARD}; color: {MUTED}; border: none; border-bottom: 1px solid {LINE};
                        padding: 5px 6px; font-size: 10px; font-weight: 700; }}
QTableCornerButton::section {{ background: {CARD}; border: none; }}
QPlainTextEdit {{ background: {CARD2}; border: none; border-radius: 7px; padding: 6px;
                  font-family: 'Cascadia Mono', 'Consolas'; font-size: 11px; color: #cfd9e8; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2b3b52; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4f6d; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; height: 0; width: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QToolTip {{ background: #1f2c3d; color: {TEXT}; border: 1px solid #33465f; padding: 5px; font-size: 11px; }}
"""


def font(px: int, bold: bool = False) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def label(text: str = "", role: str = "", color: str | None = None) -> QLabel:
    w = QLabel(text)
    if role:
        w.setProperty("role", role)
    if color:
        w.setStyleSheet(f"color: {color};")
    return w


def card() -> QFrame:
    f = QFrame()
    f.setProperty("card", True)
    return f


def dot(color: str, text: str) -> str:
    return f"<span style='color:{color}'>●</span>&nbsp;{text}"


class Elided(QLabel):
    """QLabel de uma linha que corta com reticências e mostra o texto completo no tooltip."""

    def __init__(self, text: str = "", role: str = "muted"):
        super().__init__()
        self._full = ""
        self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setFull(text)

    def setFull(self, text: str) -> None:
        self._full = text
        self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        shown = fm.elidedText(self._full, Qt.ElideRight, max(10, self.width() - 2))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(20, super().minimumSizeHint().height())


class Segmented(QWidget):
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, (text, value) in enumerate(options):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("seg", True)
            b.setProperty("pos", "first" if i == 0 else "last" if i == len(options) - 1 else "mid")
            b.setChecked(value == current)
            b.clicked.connect(lambda _=False, v=value: self.changed.emit(v))
            self.group.addButton(b)
            lay.addWidget(b)


class Sparkline(QWidget):
    def __init__(self, color: str):
        super().__init__()
        self.color = QColor(color)
        self.values: list[float] = []
        self.setMinimumSize(60, 30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_values(self, values: list[float]) -> None:
        self.values = list(values)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        vals = self.values
        base = h - 3
        if len(vals) < 2 or max(vals) <= 0:
            p.setPen(QPen(QColor(LINE), 1, Qt.DashLine))
            p.drawLine(0, base, w, base)
            return
        lo, hi = min(0.0, min(vals)), max(vals)
        if min(vals) > hi * 0.5:  # séries tipo percentual: amplia a variação
            lo = min(vals) - (hi - min(vals)) * 0.4
        span = (hi - lo) or 1
        pts = [QPointF(3 + (w - 8) * i / (len(vals) - 1), base - (base - 5) * (v - lo) / span)
               for i, v in enumerate(vals)]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        area = QPainterPath(path)
        area.lineTo(pts[-1].x(), base)
        area.lineTo(pts[0].x(), base)
        area.closeSubpath()
        grad = QLinearGradient(0, 0, 0, h)
        c = QColor(self.color)
        c.setAlpha(90)
        grad.setColorAt(0, c)
        c.setAlpha(0)
        grad.setColorAt(1, c)
        p.fillPath(area, grad)
        p.setPen(QPen(self.color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(pts[-1], 2.6, 2.6)


class KpiCard(QFrame):
    def __init__(self, caption: str, color: str, extra: QWidget | None = None):
        super().__init__()
        self.setProperty("card", True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(label(caption, "cap"))
        top.addStretch(1)
        if extra:
            top.addWidget(extra)
        lay.addLayout(top)
        mid = QHBoxLayout()
        mid.setSpacing(12)
        self.value = label("—", "kpi", color)
        self.value.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        mid.addWidget(self.value)
        self.spark = Sparkline(color)
        mid.addWidget(self.spark, 1)
        lay.addLayout(mid, 1)
        self.sub = Elided("")
        self.sub.setStyleSheet(f"color: {TEXT}; font-size: 11px;")
        lay.addWidget(self.sub)
        self.sub2 = Elided("")
        lay.addWidget(self.sub2)
        self.setMinimumHeight(108)
        self.setMaximumHeight(124)

    def set_sub(self, line1: str, line2: str) -> None:
        self.sub.setFull(line1)
        self.sub2.setFull(line2)


class TokenChart(QWidget):
    """Barras empilhadas por dia desenhadas com QPainter (tokens ou custo), com tooltip no hover."""

    def __init__(self):
        super().__init__()
        self.days: list[dict] = []
        self.mode = "tokens"
        self.hide_cache = False
        self.hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def configure(self, days=None, mode=None, hide_cache=None) -> None:
        if days is not None:
            self.days = days
            if self.hover >= len(days):
                self.hover = -1
        if mode is not None:
            self.mode = mode
        if hide_cache is not None:
            self.hide_cache = hide_cache
        self.update()

    # ---- geometria
    def _plot_x(self) -> tuple[float, float]:
        return 54.0, self.width() - 6.0

    def _panels(self) -> list[dict]:
        if self.mode == "cost":
            return [{"title": "", "series": [("c_cache", C_CACHE), ("c_inp", C_INPUT), ("c_out", C_OUTPUT)], "share": 1}]
        small = {"title": "Sem cache · input novo + output (escala própria)",
                 "series": [("inp", C_INPUT), ("out", C_OUTPUT)], "share": 0.4}
        if self.hide_cache:
            return [dict(small, title="", share=1)]
        full = {"title": "", "series": [("cache", C_CACHE), ("inp", C_INPUT), ("out", C_OUTPUT)], "share": 0.6}
        return [full, small]

    def _axis_fmt(self, v: float, top: float) -> str:
        if self.mode == "cost":
            return fmt_brl(v, 0 if top >= 20 else 2)
        return fmt_compact(v, 0 if top >= 1e7 else 1) if v else "0"

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        left, right = self._plot_x()
        n = len(self.days)
        idx = -1
        x = event.position().x()
        if n and left <= x <= right:
            idx = min(n - 1, int((x - left) / ((right - left) / n)))
        if idx != self.hover:
            self.hover = idx
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self.hover != -1:
            self.hover = -1
            self.update()

    # ---- pintura
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self.days)
        if not n or not any(d["total"] for d in self.days):
            p.setPen(QColor(MUTED))
            p.setFont(font(12))
            p.drawText(self.rect(), Qt.AlignCenter, "Sem uso de tokens neste período")
            return
        left, right = self._plot_x()
        top, bottom = 8.0, h - 20.0
        slot = (right - left) / n
        bar_w = max(2.0, min(slot * 0.72, 40.0))
        panels = self._panels()
        gap = 26.0 if len(panels) > 1 else 0.0
        avail = bottom - top - gap
        y = top
        if 0 <= self.hover < n:
            p.fillRect(QRectF(left + slot * self.hover, top, slot, bottom - top), QColor(255, 255, 255, 14))
        for i, panel in enumerate(panels):
            ph = avail * panel["share"]
            if i > 0:
                y += gap
                p.setFont(font(10, True))
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(left, y - 19, right - left, 14), Qt.AlignLeft | Qt.AlignVCenter, panel["title"])
            self._draw_panel(p, panel, QRectF(left, y, right - left, ph), slot, bar_w)
            y += ph
        # eixo X
        p.setFont(font(10))
        step = max(1, math.ceil(n / max(1, int((right - left) / 46))))
        for i, d in enumerate(self.days):
            if (n - 1 - i) % step == 0:
                cx = left + slot * (i + 0.5)
                p.setPen(QColor(TEXT if i == self.hover else MUTED))
                p.drawText(QRectF(cx - 24, bottom + 3, 48, 15), Qt.AlignCenter, d["date"].strftime("%d/%m"))
        if 0 <= self.hover < n:
            self._draw_tooltip(p, left + slot * (self.hover + 0.5), top, w)

    def _draw_panel(self, p: QPainter, panel: dict, r: QRectF, slot: float, bar_w: float) -> None:
        keys = panel["series"]
        peak = max(sum(d[k] for k, _ in keys) for d in self.days)
        ceil_v = nice_ceil(peak)
        divisions = 4 if r.height() >= 110 else 2
        p.setFont(font(10))
        for t in range(divisions + 1):
            yy = r.bottom() - r.height() * t / divisions
            p.setPen(QPen(QColor(LINE if t == 0 else GRID), 1))
            p.drawLine(QPointF(r.left(), yy), QPointF(r.right(), yy))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, yy - 8, r.left() - 7, 16), Qt.AlignRight | Qt.AlignVCenter,
                       self._axis_fmt(ceil_v * t / divisions, ceil_v))
        p.setPen(Qt.NoPen)
        for i, d in enumerate(self.days):
            total = sum(d[k] for k, _ in keys)
            cx = r.left() + slot * (i + 0.5)
            if total <= 0:
                p.fillRect(QRectF(cx - bar_w / 2, r.bottom() - 1.5, bar_w, 1.5), QColor("#2a3a50"))
                continue
            bh = max(2.0, r.height() * total / ceil_v)
            bar = QRectF(cx - bar_w / 2, r.bottom() - bh, bar_w, bh)
            clip = QPainterPath()
            rad = min(3.0, bar_w / 2)
            clip.addRoundedRect(bar.adjusted(0, 0, 0, rad), rad, rad)
            p.save()
            p.setClipPath(clip)
            p.setClipRect(bar, Qt.IntersectClip)
            yb = r.bottom()
            for k, color in keys:
                sh = bh * d[k] / total
                c = QColor(color)
                if self.hover not in (-1, i):
                    c.setAlpha(150)
                p.fillRect(QRectF(bar.left(), yb - sh, bar_w, sh + 0.5), c)
                yb -= sh
            p.restore()

    def _draw_tooltip(self, p: QPainter, cx: float, top: float, w: int) -> None:
        d = self.days[self.hover]
        week = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][d["date"].weekday()]
        rows = [(C_CACHE, "Cache", fmt_int(d["cache"]), fmt_brl(d["c_cache"])),
                (C_INPUT, "Input novo", fmt_int(d["inp"]), fmt_brl(d["c_inp"])),
                (C_OUTPUT, "Output", fmt_int(d["out"]), fmt_brl(d["c_out"])),
                (None, "Total", fmt_int(d["total"]), fmt_brl(d["cost"]))]
        f_b, f_n = font(11, True), font(11)
        fm = QFontMetrics(f_b)
        c1 = max(fm.horizontalAdvance(r[1]) for r in rows) + 18
        c2 = max(fm.horizontalAdvance(r[2]) for r in rows) + 14
        c3 = max(fm.horizontalAdvance(r[3]) for r in rows)
        bw, bh = c1 + c2 + c3 + 20, 30 + 17 * len(rows) + 20
        bx = cx + 14 if cx + 14 + bw < w - 4 else cx - 14 - bw
        box = QRectF(max(2, bx), top + 4, bw, bh)
        p.setPen(QPen(QColor("#3a4f6d"), 1))
        p.setBrush(QColor(24, 34, 48, 245))
        p.drawRoundedRect(box, 7, 7)
        p.setFont(f_b)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(box.left() + 10, box.top() + 7, bw - 20, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{week}, {d['date'].strftime('%d/%m/%Y')}")
        yy = box.top() + 28
        for color, name, tokens, cost in rows:
            if color:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(box.left() + 10, yy + 4, 8, 8), 2, 2)
            p.setFont(f_n if color else f_b)
            p.setPen(QColor(MUTED if color else TEXT))
            p.drawText(QRectF(box.left() + 24, yy, c1, 16), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(box.left() + 10 + c1, yy, c2 - 4, 16), Qt.AlignRight | Qt.AlignVCenter, tokens)
            p.setPen(QColor(MUTED if color else AMBER))
            p.drawText(QRectF(box.right() - 10 - c3, yy, c3, 16), Qt.AlignRight | Qt.AlignVCenter, cost)
            yy += 17
        p.setFont(font(10))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(box.left() + 10, yy + 3, bw - 20, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{fmt_int(d['resp'])} respostas · cache {fmt_pct(d['rate'])}")


class StackBar(QWidget):
    def __init__(self):
        super().__init__()
        self.parts: list[tuple[float, str]] = []
        self.setFixedHeight(10)

    def set_parts(self, parts: list[tuple[float, str]]) -> None:
        self.parts = parts
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        total = sum(v for v, _ in self.parts)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 5, 5)
        p.setClipPath(clip)
        p.fillRect(self.rect(), QColor(CARD2))
        if total <= 0:
            return
        x = 0.0
        for v, color in self.parts:
            seg = self.width() * v / total
            p.fillRect(QRectF(x, 0, max(0.0, seg - 1.5), self.height()), QColor(color))
            x += seg


class PillDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        from PySide6.QtWidgets import QStyle
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor("#1c3252"))
        painter.setPen(QPen(QColor(GRID), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        text = index.data(Qt.DisplayRole) or ""
        color = QColor(index.data(Qt.UserRole) or MUTED)
        f = font(10, True)
        fm = QFontMetrics(f)
        pw = min(option.rect.width() - 8, fm.horizontalAdvance(text) + 18)
        pill = QRectF(option.rect.left() + 5, option.rect.center().y() - 8.5, pw, 18)
        bg = QColor(color)
        bg.setAlpha(38)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(pill, 9, 9)
        painter.setPen(color)
        painter.setFont(f)
        painter.drawText(pill, Qt.AlignCenter, fm.elidedText(text, Qt.ElideRight, int(pw - 10)))
        painter.restore()


class Bridge(QObject):
    fetched = Signal(object)  # object: Signal(dict) vira QVariantMap e reordena as chaves
    service_done = Signal(str)


# ---------------------------------------------------------------- janela principal
class Dashboard(QWidget):
    COLS = ["Quando", "Origem", "Ferramenta", "Status", "Rota", "Latência", "Conf.", "Pergunta"]

    def __init__(self, force_sample: bool = False, period: str = "30", chart_mode: str = "tokens"):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Laya Local")
        self.setMinimumSize(960, 680)
        self.force_sample = force_sample
        self.payload: dict = {}
        self.result: dict = {}
        self.period = period
        self.chart_mode = chart_mode
        self.loading = False
        self.first_data = False
        self.table_sig = None
        self.calls: list[dict] = []
        self.call_filter = "all"
        self.quitting = False
        self.tray: QSystemTrayIcon | None = None
        self.service_busy = False
        self.last_service_try = 0.0
        self.bridge = Bridge()
        self.bridge.fetched.connect(self._on_fetched)
        self.bridge.service_done.connect(self._on_service_done)
        self._build()
        self._sync_autostart()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.last_fetch = 0.0
        self.timer.start(POLL_MS)
        self.refresh()

    # ---- construção
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(10)
        root.addLayout(self._build_header())
        root.addLayout(self._build_kpis())
        mid = QHBoxLayout()
        mid.setSpacing(10)
        chart_card, quality_card, calls_card = self._build_chart_card(), self._build_quality_card(), self._build_calls_card()
        # Altura "Ignored" + mínimo explícito: o espaço vertical segue a proporção 13:8 em qualquer tamanho.
        for w, min_h in ((chart_card, 220), (quality_card, 286), (calls_card, 160)):
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
            w.setMinimumHeight(min_h)
        mid.addWidget(chart_card, 13)
        mid.addWidget(quality_card, 7)
        root.addLayout(mid, 13)
        root.addWidget(calls_card, 8)
        root.addLayout(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(8)
        logo = label("L", "logo")
        logo.setFixedSize(28, 28)
        logo.setAlignment(Qt.AlignCenter)
        h.addWidget(logo)
        h.addWidget(label("Laya Local", "brand"))
        h.addSpacing(6)
        self.chip_api = label(dot(FAINT, "API 8766"), "chip")
        self.chip_mcp = label(dot(FAINT, "MCP 8767"), "chip")
        self.chip_src = label("", "chip")
        self.chip_src.hide()
        self.chip_model = label(dot(FAINT, "Modelo —"), "chip")
        for chip in (self.chip_api, self.chip_mcp, self.chip_model, self.chip_src):
            chip.setTextFormat(Qt.RichText)
            h.addWidget(chip)
        h.addStretch(1)
        self.updated = label("Carregando…", "muted")
        h.addWidget(self.updated)
        seg = Segmented([("7 dias", "7"), ("30 dias", "30"), ("Tudo", "all")], self.period)
        seg.changed.connect(self._set_period)
        h.addWidget(seg)
        self.btn_refresh = QPushButton("↻  Atualizar")
        self.btn_refresh.setProperty("primary", True)
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh)
        h.addWidget(self.btn_refresh)
        return h

    def _build_kpis(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(10)
        self.model = QComboBox()
        self.model.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.model.setMinimumWidth(112)
        self.model.setToolTip("Modelo usado na tabela de tarifas (USD por milhão de tokens)")
        self.model.currentTextChanged.connect(lambda _t: self._render())
        self.k_tokens = KpiCard("TOKENS NO PERÍODO", BLUE)
        self.k_cost = KpiCard("CUSTO HIPOTÉTICO", AMBER, self.model)
        self.k_cache = KpiCard("TAXA DE CACHE", TEAL)
        self.k_calls = KpiCard("CHAMADAS LAYA", GREEN)
        for k in (self.k_tokens, self.k_cost, self.k_cache, self.k_calls):
            h.addWidget(k, 1)
        return h

    def _build_chart_card(self) -> QFrame:
        c = card()
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 10, 12, 8)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.chart_title = label("Tokens por dia", "title")
        top.addWidget(self.chart_title)
        self.legend = label("", "muted")
        self.legend.setTextFormat(Qt.RichText)
        top.addWidget(self.legend)
        top.addStretch(1)
        self.chk_cache = QCheckBox("Ocultar cache")
        self.chk_cache.setToolTip("O cache é ~96 % do volume; oculte para ampliar input novo e output")
        self.chk_cache.toggled.connect(lambda on: self.chart.configure(hide_cache=on))
        top.addWidget(self.chk_cache)
        seg = Segmented([("Tokens", "tokens"), ("Custo R$", "cost")], self.chart_mode)
        seg.changed.connect(self._set_chart_mode)
        top.addWidget(seg)
        v.addLayout(top)
        self.chart_sub = Elided("")
        v.addWidget(self.chart_sub)
        self.chart = TokenChart()
        v.addWidget(self.chart, 1)
        return c

    def _build_quality_card(self) -> QFrame:
        c = card()
        c.setMinimumWidth(300)
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("Qualidade da Laya", "title"))
        self.q_sub = Elided("")
        self.q_sub.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.q_sub, 1)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles: dict[str, QLabel] = {}
        defs = [("p50", "Latência p50"), ("p95", "Latência p95"), ("conf", "Confiança"),
                ("ok", "Falhas da Laya"), ("err", "Payload rejeitado"), ("skip", "Não roteadas")]
        for i, (key, cap) in enumerate(defs):
            t = QFrame()
            t.setProperty("tile", True)
            t.setMinimumHeight(44)
            tl = QVBoxLayout(t)
            tl.setContentsMargins(9, 5, 6, 6)
            tl.setSpacing(0)
            tl.addWidget(Elided(cap))
            val = label("—", "tile")
            tl.addWidget(val)
            self.tiles[key] = val
            grid.addWidget(t, i // 3, i % 3)
        v.addLayout(grid)
        v.addSpacing(2)
        v.addWidget(label("ROTAS", "cap"))
        self.routes_bar = StackBar()
        v.addWidget(self.routes_bar)
        self.routes_legend = QGridLayout()
        self.routes_legend.setHorizontalSpacing(10)
        self.routes_legend.setVerticalSpacing(1)
        v.addLayout(self.routes_legend)
        v.addSpacing(2)
        self.skip_cap = label("MOTIVOS DE NÃO ROTEAMENTO", "cap")
        v.addWidget(self.skip_cap)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(38)
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        self.skip_box = QVBoxLayout(holder)
        self.skip_box.setContentsMargins(0, 0, 4, 0)
        self.skip_box.setSpacing(3)
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)
        return c

    def _build_calls_card(self) -> QFrame:
        c = card()
        h = QHBoxLayout(c)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(label("Chamadas", "title"))
        self.calls_sub = Elided("")
        top.addWidget(self.calls_sub, 1)
        flt = Segmented([("Todas", "all"), ("Rejeitadas", "rejected"), ("Falhas", "error"), ("Não roteadas", "no")],
                        self.call_filter)
        flt.changed.connect(self._set_call_filter)
        top.addWidget(flt)
        left.addLayout(top)
        t = QTableWidget(0, len(self.COLS))
        t.setHorizontalHeaderLabels(self.COLS)
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(27)
        t.setShowGrid(False)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setFocusPolicy(Qt.NoFocus)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        t.setWordWrap(False)
        t.setTextElideMode(Qt.ElideRight)
        hh = t.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setHighlightSections(False)
        for col, width in enumerate((104, 86, 106, 102, 92, 70, 52)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            t.setColumnWidth(col, width)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        t.setItemDelegateForColumn(3, PillDelegate(t))
        t.itemSelectionChanged.connect(self._show_detail)
        self.table = t
        left.addWidget(t, 1)
        h.addLayout(left, 5)

        right = QVBoxLayout()
        right.setSpacing(5)
        self.d_title = label("Detalhe da chamada", "title")
        self.d_title.setTextFormat(Qt.RichText)
        d_top = QHBoxLayout()
        d_top.addWidget(self.d_title, 1)
        btn_copy = QPushButton("Copiar")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.setToolTip("Copia a pergunta completa e o erro")
        btn_copy.clicked.connect(self._copy_detail)
        d_top.addWidget(btn_copy)
        right.addLayout(d_top)
        self.d_meta = Elided("Selecione uma linha para ver a pergunta completa.")
        right.addWidget(self.d_meta)
        self.d_text = QPlainTextEdit()
        self.d_text.setReadOnly(True)
        self.d_text.setMinimumHeight(30)
        right.addWidget(self.d_text, 1)
        self.d_error = Elided("")
        self.d_error.hide()
        right.addWidget(self.d_error)
        holder = QWidget()
        holder.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        holder.setMinimumWidth(250)
        self.detail_holder = holder
        h.addWidget(holder, 2)
        return c

    def _build_footer(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setSpacing(10)
        self.f_services = label("", "muted")
        self.f_services.setTextFormat(Qt.RichText)
        h.addWidget(self.f_services)
        self.f_note = Elided("")
        h.addWidget(self.f_note, 1)
        self.chk_auto = QCheckBox("Iniciar com o Windows")
        self.chk_auto.setToolTip("Abre oculto na bandeja após entrar no Windows")
        self.chk_auto.clicked.connect(self._toggle_autostart)
        h.addWidget(self.chk_auto)
        self.btn_model = QPushButton("Aquecer modelo")
        self.btn_model.setCursor(Qt.PointingHandCursor)
        self.btn_model.clicked.connect(self._toggle_model)
        h.addWidget(self.btn_model)
        b = QPushButton("Reiniciar serviços")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip("Encerra e sobe de novo o MCP (8767) e a API (8766)")
        b.clicked.connect(self.ask_restart_services)
        self.btn_services = b
        h.addWidget(b)
        return h

    # ---- autostart, serviços, bandeja
    def _sync_autostart(self) -> None:
        try:
            import startup
            self.chk_auto.setChecked(startup.is_enabled())
        except Exception as exc:  # noqa: BLE001
            self.chk_auto.setEnabled(False)
            self.chk_auto.setToolTip(f"Autostart indisponível: {type(exc).__name__}")

    def _toggle_autostart(self, on: bool) -> None:
        try:
            import startup
            startup.set_enabled(bool(on))
            self._toast("Laya inicia com o Windows, oculta na bandeja." if on else "Início automático desligado.")
        except Exception as exc:  # noqa: BLE001
            self.chk_auto.setChecked(not on)
            self._toast(f"Não foi possível alterar o início automático: {type(exc).__name__}")

    def _run_service_job(self, job, message: str) -> None:
        if self.service_busy:
            return
        self.service_busy = True
        self.last_service_try = time.perf_counter()
        self.btn_services.setEnabled(False)
        self._toast(message)

        def work() -> None:
            try:
                outcome = job()
            except Exception as exc:  # noqa: BLE001
                outcome = f"falha: {type(exc).__name__}"
            self.bridge.service_done.emit(outcome)

        threading.Thread(target=work, daemon=True, name="laya-services").start()

    def _on_service_done(self, outcome: str) -> None:
        self.service_busy = False
        self.btn_services.setEnabled(True)
        self._toast(f"Serviços: {outcome}")
        QTimer.singleShot(2500, self.refresh)

    def ask_restart_services(self) -> None:
        if not self.isVisible():
            self.show_window()
        answer = QMessageBox.question(
            self, "Reiniciar serviços",
            "Encerrar e iniciar de novo o MCP Laya (8767) e a API do dashboard (8766)?\n\n"
            "Chamadas em andamento de agentes serão interrompidas.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._run_service_job(restart_services, "Reiniciando serviços…")

    def _auto_start_services(self) -> None:
        """Serviço caído sem ação do usuário: tenta subir, no máximo uma vez por minuto."""
        if self.force_sample or self.service_busy:
            return
        if self.last_service_try and time.perf_counter() - self.last_service_try < SERVICE_RETRY_S:
            return
        self._run_service_job(ensure_services, "Serviço fora do ar; iniciando…")

    def setup_tray(self, icon: QIcon) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("Laya Local")
        menu = QMenu()
        for text, slot in (("Abrir Laya Local", self.show_window), ("Atualizar", self.refresh),
                           ("Reiniciar serviços…", self.ask_restart_services), (None, None), ("Sair", self.quit_app)):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, menu)
            action.triggered.connect(slot)
            menu.addAction(action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self.show_window() if reason in (
            QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        tray.show()
        self.tray, self._tray_menu = tray, menu

    def show_window(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def quit_app(self) -> None:
        self.quitting = True
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.quitting or self.tray is None:
            event.accept()
            return
        event.ignore()
        self.hide()  # fechar oculta na bandeja; "Sair" fica no menu do ícone

    def _toggle_model(self) -> None:
        status = (self.payload.get("model") or {}).get("status")
        action = "unload" if status == "warm" else "warm"
        self._run_service_job(lambda: model_action(action),
                              "Descarregando modelo…" if action == "unload" else "Aquecendo modelo…")

    def _render_model(self, model) -> None:
        if not isinstance(model, dict):
            self.chip_model.setText(dot(FAINT, "Modelo —"))
            self.chip_model.setToolTip("Estado do modelo indisponível nesta versão do serviço")
            self.btn_model.setEnabled(False)
            return
        status, names = model.get("status"), ", ".join(model.get("loaded") or [])
        left = model.get("unload_in_s")
        if status == "warm":
            when = f" · descarrega em {max(1, round(left / 60))} min" if left is not None else ""
            text, color = f"Modelo quente{when}", GREEN
        elif status == "warming":
            text, color = "Modelo aquecendo…", AMBER
        else:
            text, color = "Modelo frio", FAINT
        self.chip_model.setText(dot(color, text))
        free = model.get("vram_free_mb")
        tip = [f"Carregado: {names or 'nenhum'}",
               f"Aquece {model.get('warm_model')} quando um agente conecta; descarrega após "
               f"{round((model.get('idle_unload_s') or 0) / 60)} min sem chamadas",
               f"VRAM livre: {fmt_int(free)} MB (não aquece abaixo de {fmt_int(model.get('warm_min_free_mb'))} MB)"
               if free is not None else "VRAM livre: indisponível",
               f"Última carga: {fmt_dec(model['last_load_s'])} s" if model.get("last_load_s") else "",
               f"Cache de disco: {model.get('prewarm')}", str(model.get("note") or "")]
        self.chip_model.setToolTip("\n".join(t for t in tip if t))
        self.btn_model.setEnabled(status != "warming" and not self.service_busy)
        self.btn_model.setText("Descarregar modelo" if status == "warm" else "Aquecer modelo")
        self.btn_model.setToolTip("Libera a VRAM agora" if status == "warm" else "Carrega o modelo na GPU agora")

    def _set_call_filter(self, value: str) -> None:
        self.call_filter = value
        self._render()

    def _copy_detail(self) -> None:
        text = self.d_text.toPlainText()
        if self.d_error.isVisible():
            text += "\n\n" + self.d_error._full
        QGuiApplication.clipboard().setText(text)
        self._toast("Detalhe copiado.")

    NOTE = ("Dados só neste computador (127.0.0.1) · custo hipotético, não é cobrança · economia medida e limite "
            "da conta: indisponíveis nos dados locais")

    def _toast(self, text: str) -> None:
        self.f_note.setFull(text)
        self.f_note.setStyleSheet(f"color: {AMBER};")
        QTimer.singleShot(4500, self._untoast)

    def _untoast(self) -> None:
        self.f_note.setStyleSheet("")
        self.f_note.setFull(self.NOTE)

    # ---- coleta
    def _tick(self) -> None:
        if self.isVisible() or time.perf_counter() - self.last_fetch >= HIDDEN_POLL_S:
            self.refresh()

    def refresh(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.last_fetch = time.perf_counter()
        self.btn_refresh.setEnabled(False)
        threading.Thread(target=self._worker, daemon=True, name="laya-refresh").start()

    def _worker(self) -> None:
        try:
            result = collect(self.force_sample)
        except Exception as exc:  # noqa: BLE001
            result = {"data": None, "source": "none", "api": False, "mcp": False, "error": type(exc).__name__}
        self.bridge.fetched.emit(result)  # sinal enfileirado para a thread da UI

    def _on_fetched(self, result: dict) -> None:
        self.loading = False
        self.btn_refresh.setEnabled(True)
        self.result = result
        if isinstance(result.get("data"), dict):
            self.payload = result["data"]
        self._render()
        self.first_data = True
        if not (result.get("api") and result.get("mcp")):
            self._auto_start_services()

    def _set_period(self, value: str) -> None:
        self.period = value
        self._render()

    def _set_chart_mode(self, value: str) -> None:
        self.chart_mode = value
        self._render()

    # ---- derivação dos dados
    def _rates(self) -> dict:
        usage = self.payload.get("codex_usage") or {}
        return (usage.get("rates_usd_per_million") or {}).get(self.model.currentText()) or {}

    def _fx(self) -> float:
        return float((self.payload.get("codex_usage") or {}).get("usd_brl") or USD_BRL)

    def _days(self) -> tuple[list[dict], list[dict]]:
        """Dias contínuos do período (com zeros nos dias sem uso) e a janela anterior equivalente."""
        usage = self.payload.get("codex_usage") or {}
        daily = {str(d.get("date")): d for d in usage.get("daily") or []}
        rates = self._rates()
        fx = self._fx()
        today = date.today()
        if self.period == "all":
            firsts = [date.fromisoformat(k) for k in daily] or [today]
            start = min(firsts)
        else:
            start = today - timedelta(days=int(self.period) - 1)

        def build(a: date, b: date) -> list[dict]:
            out = []
            cur = a
            while cur <= b:
                raw = daily.get(cur.isoformat()) or {}
                inp_all = int(raw.get("input_tokens") or 0)
                cache = min(inp_all, int(raw.get("cached_input_tokens") or 0))
                inp, outp = inp_all - cache, int(raw.get("output_tokens") or 0)
                k = fx / 1e6
                c_cache = cache * float(rates.get("cached", 0)) * k
                c_inp = inp * float(rates.get("input", 0)) * k
                c_out = outp * float(rates.get("output", 0)) * k
                out.append({"date": cur, "cache": cache, "inp": inp, "out": outp, "total": inp_all + outp,
                            "resp": int(raw.get("responses") or 0), "c_cache": c_cache, "c_inp": c_inp,
                            "c_out": c_out, "cost": c_cache + c_inp + c_out,
                            "rate": cache * 100 / inp_all if inp_all else 0.0})
                cur += timedelta(days=1)
            return out

        days = build(start, today)
        prev: list[dict] = []
        first = parse_ts(usage.get("first_usage"))
        if self.period != "all" and first and first.date() <= start - timedelta(days=len(days)):
            prev = build(start - timedelta(days=len(days)), start - timedelta(days=1))
        return days, prev

    def _calls_in_period(self) -> list[dict]:
        rows = {r.get("id"): r for r in self.payload.get("rows") or []}
        cutoff = None if self.period == "all" else date.today() - timedelta(days=int(self.period) - 1)
        out = []
        for c in self.payload.get("call_logs") or []:
            ts = parse_ts(c.get("ts"))
            if cutoff and (ts is None or ts.date() < cutoff):
                continue
            extra = {}
            cid = str(c.get("id"))
            if cid.startswith("legacy-") and cid[7:].isdigit():
                extra = rows.get(int(cid[7:])) or {}
            reason = c.get("reason") or extra.get("reason")
            code = c.get("error_code")
            if code is None and c.get("passed") == "error":  # API antiga: o código só existe no JSON do motivo
                try:
                    code = json.loads(reason).get("error")
                except (ValueError, TypeError, AttributeError):
                    code = None
            kind = "rejected" if c.get("passed") == "error" and str(code or "").startswith("invalid_") else c.get("passed")
            out.append({**c, "_ts": ts, "_kind": kind, "_code": code, "_reason": reason, "_flow": extra.get("flow"),
                        "_lat": c["latency"] if c.get("latency") is not None else extra.get("latency"),
                        "_conf": c["confidence"] if c.get("confidence") is not None else extra.get("confidence")})
        return out

    # ---- renderização
    def _render(self) -> None:
        res, data = self.result, self.payload
        api_ok, mcp_ok = bool(res.get("api")), bool(res.get("mcp"))
        self.chip_api.setText(dot(GREEN if api_ok else RED, "API 8766"))
        self.chip_mcp.setText(dot(GREEN if mcp_ok else RED, "MCP 8767"))
        self.chip_api.setToolTip("Dashboard de uso · API local" + ("" if api_ok else " · sem resposta"))
        self.chip_mcp.setToolTip("MCP Laya · Streamable HTTP" + ("" if mcp_ok else " · sem resposta"))
        offline = res.get("source") != "api"
        self.chip_src.setVisible(offline)
        if offline:
            self.chip_src.setText(f"<span style='color:{AMBER}'>offline · dados de amostra</span>"
                                  if res.get("source") == "sample" else f"<span style='color:{RED}'>"
                                  f"{'API sem resposta · últimos dados' if data else 'API sem resposta'}</span>")
        self.updated.setText(f"Atualizado {datetime.now().strftime('%H:%M:%S')}")
        usage = data.get("codex_usage") or {}
        scan = usage.get("scan") or {}
        self.f_services.setText(
            dot(GREEN if api_ok else RED, f"API {'online' if api_ok else 'offline'}") + "&nbsp;&nbsp;&nbsp;"
            + dot(GREEN if mcp_ok else RED, f"MCP {'online' if mcp_ok else 'offline'}") + "&nbsp;&nbsp;&nbsp;"
            + dot(GREEN if scan.get("status") == "ready" else AMBER,
                  f"coleta {scan.get('status', '—')} · {fmt_int(scan.get('files_indexed'))} arquivos"))
        if not self.f_note.styleSheet():
            self.f_note.setFull(self.NOTE)
        self._render_model(data.get("model") if api_ok else None)
        if not data:  # sem resposta e sem dados anteriores: não exibir zeros como se fossem medição
            return

        names = list((usage.get("rates_usd_per_million") or {}).keys())
        if names and [self.model.itemText(i) for i in range(self.model.count())] != names:
            current = self.model.currentText()
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItems(names)
            self.model.setCurrentText(current if current in names else names[0])
            self.model.blockSignals(False)

        days, prev = self._days()
        calls = self._calls_in_period()
        self._render_kpis(days, prev, calls, usage)
        self._render_chart(days)
        self._render_quality(data)
        self._render_table([c for c in calls if self.call_filter == "all" or c["_kind"] == self.call_filter])

    def _period_label(self) -> str:
        return {"7": "últimos 7 dias", "30": "últimos 30 dias", "all": "todo o histórico"}[self.period]

    def _render_kpis(self, days, prev, calls, usage) -> None:
        total = sum(d["total"] for d in days)
        resp = sum(d["resp"] for d in days)
        active = sum(1 for d in days if d["total"])
        self.k_tokens.value.setText(fmt_compact(total, 2 if total >= 1e9 else 1))
        self.k_tokens.value.setToolTip(f"{fmt_int(total)} tokens")
        last_use = ago(parse_ts(usage.get("last_usage")))
        line2 = f"{active} dias ativos · último uso {last_use}"
        prev_total = sum(d["total"] for d in prev)
        if prev_total:
            delta = (total - prev_total) * 100 / prev_total
            line2 = f"{'▲' if delta >= 0 else '▼'} {fmt_pct(abs(delta), 0)} vs. anterior · último uso {last_use}"
        self.k_tokens.set_sub(f"{fmt_int(total)} tokens · {fmt_int(resp)} respostas", line2)
        self.k_tokens.spark.set_values([d["total"] for d in days])

        cost = sum(d["cost"] for d in days)
        r = self._rates()
        self.k_cost.value.setText(fmt_brl(cost) if r else "—")
        usd = " / ".join(f"{float(r.get(k, 0)):g}".replace(".", ",") for k in ("input", "cached", "output"))
        self.k_cost.set_sub("custo hipotético · não é cobrança",
                            f"US$ {usd} por mi (input/cache/output) · câmbio {fmt_dec(self._fx(), 4)}")
        self.k_cost.sub.setStyleSheet(f"color: {AMBER}; font-size: 11px;")
        self.k_cost.spark.set_values([d["cost"] for d in days])

        cache = sum(d["cache"] for d in days)
        inp_all = cache + sum(d["inp"] for d in days)
        self.k_cache.value.setText(fmt_pct(cache * 100 / inp_all) if inp_all else "—")
        self.k_cache.set_sub(f"{fmt_compact(cache, 2)} de {fmt_compact(inp_all, 2)} de input em cache",
                             f"input novo (tarifa cheia): {fmt_compact(inp_all - cache)}")
        self.k_cache.spark.set_values([d["rate"] for d in days if d["total"]])

        n = len(calls)
        rejected = sum(1 for c in calls if c["_kind"] == "rejected")
        failed = sum(1 for c in calls if c["_kind"] == "error")
        answered = sum(1 for c in calls if c["_kind"] == "yes")
        self.k_calls.value.setText(fmt_int(n))
        all_ts = [t for t in (parse_ts(c.get("ts")) for c in self.payload.get("call_logs") or []) if t]
        last_call = ago(max(all_ts)) if all_ts else "sem registro"
        if n:
            self.k_calls.set_sub(f"{answered} respondidas · {rejected} rejeitadas · {failed} falhas",
                                 f"última chamada {last_call}")
        else:
            self.k_calls.set_sub("nenhuma chamada no período", f"última chamada {last_call}")
        per_day: dict[date, int] = {d["date"]: 0 for d in days}
        for c in calls:
            if c["_ts"] and c["_ts"].date() in per_day:
                per_day[c["_ts"].date()] += 1
        self.k_calls.spark.set_values(list(per_day.values()))

    def _render_chart(self, days) -> None:
        cost_mode = self.chart_mode == "cost"
        self.chk_cache.setVisible(not cost_mode)
        self.chart_title.setText("Custo hipotético por dia" if cost_mode else "Tokens por dia")
        self.legend.setText("&nbsp;&nbsp;".join(
            f"<span style='color:{c}'>■</span> {n}" for c, n in
            ((C_CACHE, "Cache"), (C_INPUT, "Input novo"), (C_OUTPUT, "Output"))))
        cache, inp, out = (sum(d[k] for d in days) for k in ("cache", "inp", "out"))
        total = cache + inp + out
        if cost_mode:
            parts = [sum(d[k] for d in days) for k in ("c_cache", "c_inp", "c_out")]
            self.chart_sub.setFull(f"{self.model.currentText()} · cache {fmt_brl(parts[0])} · input novo {fmt_brl(parts[1])} · "
                                   f"output {fmt_brl(parts[2])} · {self._period_label()}")
        elif total:
            self.chart_sub.setFull(f"Cache {fmt_compact(cache, 2)} ({fmt_pct(cache * 100 / total)}) · input novo "
                                   f"{fmt_compact(inp)} ({fmt_pct(inp * 100 / total)}) · output {fmt_compact(out)} "
                                   f"({fmt_pct(out * 100 / total, 2)}) · {self._period_label()}")
        else:
            self.chart_sub.setFull(self._period_label())
        self.chart.configure(days=days, mode=self.chart_mode)

    def _render_quality(self, data) -> None:
        m = data.get("metrics") or {}
        req = int(m.get("requests") or 0)
        self.q_sub.setFull(f"histórico completo · {fmt_int(req)} requisições")

        def tile(key, text, color=TEXT):
            self.tiles[key].setText(text)
            self.tiles[key].setStyleSheet(f"color: {color};")

        p95 = m.get("latency_p95")
        tile("p50", fmt_ms(m.get("latency_p50")))
        tile("p95", fmt_ms(p95), AMBER if (p95 or 0) >= 5000 else TEXT)
        conf = m.get("confidence")
        tile("conf", fmt_pct(conf * 100, 0) if conf is not None else "—")
        failures, rejected = m.get("failures"), m.get("rejected")
        if failures is None:  # API antiga não separa: mostra o total de erros como estava
            failures, rejected = int(m.get("errors") or 0), None
        tile("ok", fmt_int(failures), RED if failures else GREEN)
        tile("err", fmt_int(rejected) if rejected is not None else "—", ORANGE if rejected else TEXT)
        self.tiles["err"].setToolTip("Chamadas recusadas na validação do payload (formato de questions); não chegaram "
                                     f"ao modelo. Corrigidas automaticamente desde a normalização: {fmt_int(m.get('normalized'))}")
        tile("skip", fmt_int(m.get("skipped")))
        self.tiles["p95"].setToolTip(f"Média: {fmt_ms(m.get('latency'))} · {fmt_int(m.get('samples'))} amostras de latência")
        self.tiles["ok"].setToolTip(f"Erros de execução do modelo/runtime. Respondidas: {fmt_int(m.get('passed'))} "
                                    f"de {fmt_int(req)} ({fmt_pct(float(m.get('pass_rate') or 0))})")

        routes = data.get("routes") or {}
        order = sorted(routes.items(), key=lambda kv: (kv[0] == "not routed", -kv[1]))
        total = sum(routes.values()) or 1
        self.routes_bar.set_parts([(v, ROUTES.get(k, (k, MUTED))[1]) for k, v in order])
        while self.routes_legend.count():
            item = self.routes_legend.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, (k, v) in enumerate(order):
            name, color = ROUTES.get(k, (k, MUTED))
            lab = label(f"<span style='color:{color}'>■</span> {name} <b>{fmt_int(v)}</b> "
                        f"<span style='color:{MUTED}'>{fmt_pct(v * 100 / total, 0)}</span>")
            lab.setTextFormat(Qt.RichText)
            lab.setStyleSheet("font-size: 11px;")
            self.routes_legend.addWidget(lab, i // 2, i % 2)

        reasons = sorted(data.get("skip_reasons") or [], key=lambda r: -int(r.get("count") or 0))
        sig = json.dumps(reasons, ensure_ascii=False)
        if sig != getattr(self, "_skip_sig", None):
            self._skip_sig = sig
            while self.skip_box.count():
                item = self.skip_box.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.skip_cap.setText(f"MOTIVOS DE NÃO ROTEAMENTO · {len(reasons)}")
            for r in reasons:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(6)
                badge = label(f"{int(r.get('count') or 0)}×", "muted", AMBER)
                badge.setFixedWidth(20)
                rl.addWidget(badge)
                text = Elided(" ".join(str(r.get("reason") or "").split()))
                text.setStyleSheet(f"color: {TEXT}; font-size: 11px;")
                rl.addWidget(text, 1)
                self.skip_box.addWidget(row)
            self.skip_box.addStretch(1)

    def _render_table(self, calls) -> None:
        self.calls = calls
        counts: dict[str, int] = {}
        for c in calls:
            counts[c["_kind"] or "?"] = counts.get(c["_kind"] or "?", 0) + 1
        parts = [f"{STATUS.get(k, (k, MUTED))[0].lower()} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        self.calls_sub.setFull(f"{fmt_int(len(calls))} em {self._period_label()}" + (" · " + " · ".join(parts) if parts else ""))
        sig = (self.period, self.call_filter, len(calls), calls[0].get("id") if calls else None)
        if sig == self.table_sig:
            return
        self.table_sig = sig
        t = self.table
        selected = t.currentRow()
        keep = self._call_id(selected)
        t.blockSignals(True)
        t.setRowCount(len(calls))
        target = 0
        for i, c in enumerate(calls):
            ts = c["_ts"]
            caller = str(c.get("caller") or "Não identificado")
            short = "Histórico" if "histórico" in caller.lower() else caller.split(" (")[0].replace("Cliente MCP local", "MCP local")
            tool = str(c.get("tool") or "—")
            status, color = STATUS.get(c["_kind"], (str(c.get("passed") or "—"), MUTED))
            route = ROUTES.get(c.get("routed"), (c.get("routed") or "—", MUTED))[0] if c.get("routed") else "—"
            conf = c["_conf"]
            cells = [ts.strftime("%d/%m %H:%M:%S") if ts else "—", short, tool.removeprefix("laya_"), status, route,
                     fmt_ms(c["_lat"]), fmt_pct(conf * 100, 0) if conf is not None else "—",
                     question_preview(c.get("question"))[:240]]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setToolTip(caller)
                if col == 2:
                    item.setToolTip(tool)
                if col == 7:
                    item.setToolTip(text)
                if col == 3:
                    item.setData(Qt.UserRole, color)
                if col in (5, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col in (0, 1, 4, 5, 6) or text == "—":
                    item.setForeground(QColor(MUTED))
                if col == 5 and (c["_lat"] or 0) >= 5000:
                    item.setForeground(QColor(AMBER))
                t.setItem(i, col, item)
            if keep is not None and c.get("id") == keep:
                target = i
        t.blockSignals(False)
        if calls:
            t.selectRow(target)
        self._show_detail()

    def _call_id(self, row: int):
        return self.calls[row].get("id") if 0 <= row < len(self.calls) else None

    def _show_detail(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self.calls)):
            self.d_title.setText("Detalhe da chamada")
            self.d_meta.setFull("Nenhuma chamada no período." if not self.calls else "Selecione uma linha.")
            self.d_text.setPlainText("")
            self.d_error.hide()
            return
        c = self.calls[row]
        status, color = STATUS.get(c["_kind"], (str(c.get("passed") or "—"), MUTED))
        self.d_title.setText(f"Detalhe da chamada &nbsp;<span style='color:{color}; font-size:11px'>● {status}</span>")
        meta = [str(c.get("tool") or "—"), f"id {c.get('id')}"]
        if c["_ts"]:
            meta.append(c["_ts"].strftime("%d/%m/%Y %H:%M:%S"))
        if c.get("routed"):
            meta.append(f"rota {c['routed']}")
        if c["_lat"] is not None:
            meta.append(fmt_ms(c["_lat"]))
        if c["_conf"] is not None:
            meta.append(f"confiança {fmt_pct(c['_conf'] * 100, 0)}")
        self.d_meta.setFull(" · ".join(meta))
        self.d_text.setPlainText(pretty_question(c.get("question")) or "Pergunta não preservada")
        err = c.get("error")
        if err:
            detail = ""
            try:
                detail = json.loads(c["_reason"]).get("message", "") if c["_reason"] else ""
            except (ValueError, AttributeError):
                detail = str(c["_reason"] or "")
            self.d_error.setFull(("Payload rejeitado" if c["_kind"] == "rejected" else "Falha")
                                 + f": {c['_code'] or err}" + (f" — {detail}" if detail else ""))
            self.d_error.show()
        elif c["_reason"]:
            self.d_error.setFull(f"Motivo da rota: {c['_reason']}")
            self.d_error.setStyleSheet(f"color: {MUTED};")
            self.d_error.show()
        else:
            self.d_error.hide()
        if err:
            self.d_error.setStyleSheet(f"color: {RED};")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        narrow = self.width() < 1150
        self.table.setColumnHidden(6, narrow)
        self.table.setColumnHidden(5, self.width() < 1040)


# ---------------------------------------------------------------- entrada
def main() -> int:
    ap = argparse.ArgumentParser(description="Laya Local")
    ap.add_argument("--background", action="store_true", help="inicia oculto na bandeja")
    ap.add_argument("--screenshot", metavar="PNG", help="renderiza, salva PNG e sai")
    ap.add_argument("--size", default="1320x900", help="tamanho da janela, ex.: 1000x720")
    ap.add_argument("--sample", action="store_true", help="usa a amostra local (estado offline)")
    ap.add_argument("--period", choices=("7", "30", "all"), default="30")
    ap.add_argument("--chart", choices=("tokens", "cost"), default="tokens")
    ap.add_argument("--hover", type=int, default=None, help="(screenshot) simula hover na barra N")
    args = ap.parse_args()
    try:
        width, height = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        ap.error("--size deve ser LxA, ex.: 1320x900")
    if args.screenshot:  # PNG com o tamanho lógico exato, independente da escala do monitor
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = "1"

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Laya Local")
    app.setStyle("Fusion")
    icon = QIcon(str(resource_path("laya_tray.ico")))
    app.setWindowIcon(icon)

    server = None
    if not args.screenshot:  # instância única: a segunda só pede para a primeira aparecer
        probe = QLocalSocket()
        probe.connectToServer(INSTANCE_KEY)
        if probe.waitForConnected(400):
            if not args.background:
                probe.write(b"show")
                probe.waitForBytesWritten(400)
            probe.disconnectFromServer()
            return 0
        QLocalServer.removeServer(INSTANCE_KEY)
        server = QLocalServer()
        server.listen(INSTANCE_KEY)
    pal = QPalette()
    for role, color in ((QPalette.Window, BG), (QPalette.Base, CARD), (QPalette.Text, TEXT), (QPalette.WindowText, TEXT),
                        (QPalette.Button, CARD2), (QPalette.ButtonText, TEXT), (QPalette.Highlight, "#26497f"),
                        (QPalette.HighlightedText, TEXT), (QPalette.ToolTipBase, CARD2), (QPalette.ToolTipText, TEXT)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(QSS)

    win = Dashboard(force_sample=args.sample, period=args.period, chart_mode=args.chart)
    win.resize(width, height)
    code = {"value": 0}

    if args.screenshot:
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        deadline = time.perf_counter() + 15

        def shoot() -> None:
            if not win.first_data and time.perf_counter() < deadline:
                QTimer.singleShot(100, shoot)
                return
            if args.hover is not None and win.chart.days:
                win.chart.hover = max(0, min(len(win.chart.days) - 1, args.hover))
            app.processEvents()
            ok = win.grab().save(args.screenshot, "PNG")
            print(f"screenshot={'ok' if ok else 'falhou'} fonte={win.result.get('source')} "
                  f"pronto_em_ms={int((time.perf_counter() - _T0) * 1000)}")
            code["value"] = 0 if ok and win.first_data else 1
            app.quit()

        QTimer.singleShot(350, shoot)
    else:
        app.setQuitOnLastWindowClosed(False)
        win.setup_tray(icon)

        def on_connection() -> None:
            conn = server.nextPendingConnection()
            conn.readyRead.connect(lambda: win.show_window() if bytes(conn.readAll()) == b"show" else None)
            conn.disconnected.connect(conn.deleteLater)

        server.newConnection.connect(on_connection)
        if not args.background or win.tray is None:
            win.show()
    app.exec()
    return code["value"]


if __name__ == "__main__":
    sys.exit(main())
