"""Native Windows dashboard for local Laya usage and service status."""
from __future__ import annotations

import argparse
import sys
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from datetime import date, datetime, timedelta
from tkinter import messagebox, ttk

import startup
from windows_tray import WindowsTray

DASHBOARD_API = "http://127.0.0.1:8766/api"
MCP_HOST, MCP_PORT, DASHBOARD_PORT = "127.0.0.1", 8767, 8766
POLL_MS = 5_000
BG, PANEL, PANEL_ALT, LINE = "#0a0f18", "#111a27", "#182437", "#263449"
TEXT, MUTED, BLUE, GREEN, AMBER = "#edf3fc", "#a6b4c9", "#72a7ff", "#52d6b0", "#f2bd70"
CYAN, GRID, PURPLE = "#55d5e6", "#223045", "#b49aff"


def format_number(value: int | float | None) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def estimate_brl(usage: dict) -> float | None:
    rates = usage.get("rates_usd_per_million", {}).get("GPT-6 Luna")
    if not isinstance(rates, dict):
        return None
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = min(input_tokens, int(usage.get("cached_input_tokens") or 0))
    output_tokens = int(usage.get("output_tokens") or 0)
    usd = ((input_tokens-cached_tokens)*float(rates.get("input", 0)) + cached_tokens*float(rates.get("cached", 0)) + output_tokens*float(rates.get("output", 0))) / 1_000_000
    return usd * float(usage.get("usd_brl") or 4.9801)


def compact_number(value: int | float) -> str:
    value = int(value or 0)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} bi"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} mi"
    if value >= 10_000:
        return f"{value / 1_000:.0f} mil"
    return format_number(value)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.4):
            return True
    except OSError:
        return False


def _runtime_dir() -> Path:
    return Path.home() / ".codex" / "laya-runtime"


def _tray_icon_path() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "laya_tray.ico"


def ensure_services() -> None:
    """Start required Laya runtime services when API/MCP listeners are absent."""
    runtime = _runtime_dir()
    python = runtime / ".venv" / "Scripts" / "pythonw.exe"
    mcp_script = runtime / "laya_mcp_dashboard.py"
    dashboard_script = runtime / "laya_dashboard.py"
    if not python.is_file() or not mcp_script.is_file() or not dashboard_script.is_file():
        return
    env = os.environ.copy()
    env.update({"LAYA_TRANSPORT": "streamable-http", "LAYA_HOST": "127.0.0.1", "LAYA_PORT": str(MCP_PORT),
                "LAYA_DASHBOARD_PORT": str(DASHBOARD_PORT), "LAYA_OPEN_DASHBOARD": "0", "LAYA_PRELOAD": "0",
                "LAYA_AUDIT_CALLER": "Codex local / agente não identificado"})
    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Codex" / "Laya"
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 | 0x00000008
    with (log_dir / "local-services.log").open("a", encoding="utf-8") as log:
        if not port_open(MCP_PORT):
            # MCP wrapper starts the dashboard API itself when both are absent.
            subprocess.Popen([str(python), str(mcp_script)], cwd=runtime, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
        elif not port_open(DASHBOARD_PORT):
            subprocess.Popen([str(python), str(dashboard_script)], cwd=runtime, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)


def read_services() -> dict:
    result = {"dashboard": None, "mcp": False, "error": None}
    try:
        request = urllib.request.Request(DASHBOARD_API, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=2.5) as response:
            result["dashboard"] = json.load(response) if response.status == 200 else None
    except Exception as exc:
        result["error"] = type(exc).__name__
    result["mcp"] = port_open(MCP_PORT)
    return result


class LayaLocalApp:
    def __init__(self, root: tk.Tk, background: bool = False) -> None:
        self.root = root
        self.results: queue.Queue[dict] = queue.Queue()
        self.loading = False
        self.tray: WindowsTray | None = None
        self._configure_window()
        self._build_ui()
        self._set_startup_state()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.refresh()
        self.root.after(POLL_MS, self._poll)
        self.root.after(150, self._drain_results)
        # Keep the notification icon available while the dashboard is visible.
        self.root.after_idle(self._ensure_tray)
        if background:
            self.root.after_idle(self.hide_to_tray)

    def _configure_window(self) -> None:
        self.root.title("Laya Local")
        self.root.geometry("1320x940")
        self.root.minsize(980, 720)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(str(_tray_icon_path()))
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Laya.TCheckbutton", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Laya.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=34, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Laya.Treeview.Heading", background=PANEL_ALT, foreground=MUTED,
                        relief="flat", font=("Segoe UI", 9, "bold"), padding=(8, 8))
        style.map("Laya.Treeview", background=[("selected", "#25436c")], foreground=[("selected", TEXT)])
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)
        self.content_view = tk.Canvas(shell, bg=BG, highlightthickness=0, borderwidth=0)
        self.content_scroll = ttk.Scrollbar(shell, orient="vertical", command=self.content_view.yview)
        self.content_view.configure(yscrollcommand=self.content_scroll.set)
        self.content_view.pack(side="left", fill="both", expand=True)
        self.content_scroll.pack(side="right", fill="y")
        outer = tk.Frame(self.content_view, bg=BG, padx=30, pady=24)
        self.content_window = self.content_view.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _e: self.content_view.configure(scrollregion=self.content_view.bbox("all")))
        self.content_view.bind("<Configure>", lambda e: self.content_view.itemconfigure(self.content_window, width=e.width))
        self.content_view.bind_all("<MouseWheel>", self._scroll_content, add="+")
        self.period = tk.StringVar(value="30")
        self.activity_data: list[dict] = []
        self.call_summary: dict = {}
        self.chart_points: list[tuple[float, float, dict]] = []
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 15))
        tk.Label(header, text="L", bg="#769dff", fg="#101827", font=("Segoe UI", 18, "bold"), width=2, pady=3).pack(side="left", padx=(0, 12))
        brand = tk.Frame(header, bg=BG); brand.pack(side="left")
        tk.Label(brand, text="Laya Local", bg=BG, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(brand, text="APLICATIVO NATIVO · DADOS NESTE COMPUTADOR", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(3, 0))
        self.overall = tk.Label(header, text="Verificando serviços…", bg=PANEL, fg=MUTED, font=("Segoe UI", 10, "bold"), padx=13, pady=9)
        self.overall.pack(side="right")

        title = tk.Frame(outer, bg=BG); title.pack(fill="x", pady=(0, 17))
        copy = tk.Frame(title, bg=BG); copy.pack(side="left", fill="x", expand=True)
        tk.Label(copy, text="VISÃO GERAL  /  LOCAL", bg=BG, fg=BLUE, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(copy, text="Uso e estado da Laya", bg=BG, fg=TEXT, font=("Segoe UI", 25, "bold")).pack(anchor="w", pady=(2, 2))
        tk.Label(copy, text="Tokens observados, chamadas Laya e serviços locais.", bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w")
        self.refresh_button = self._button(title, "Atualizar", self.refresh, primary=True)
        self.refresh_button.pack(side="right", anchor="center")

        metrics = tk.Frame(outer, bg=BG); metrics.pack(fill="x", pady=(0, 14))
        self.metric_values: dict[str, tk.Label] = {}
        cards = [("tokens", "TOKENS OBSERVADOS", "—", "Somatório dos rollouts locais", BLUE),
                 ("cost", "CUSTO EQUIVALENTE", "—", "Tarifa hipotética; não é cobrança", AMBER),
                 ("calls", "CHAMADAS LAYA", "—", "Auditoria local", CYAN),
                 ("savings", "ECONOMIA MEDIDA", "Indisponível", "Exige tarefas comparáveis", TEXT),
                 ("limit", "LIMITE DA CONTA", "Indisponível", "Não consta nos dados locais", TEXT)]
        for col, (key, label, value, detail, color) in enumerate(cards):
            card = self._card(metrics, 15); card.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col < 3 else (6, 0))
            metrics.grid_columnconfigure(col, weight=1, uniform="metric")
            tk.Label(card, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            number = tk.Label(card, text=value, bg=PANEL, fg=color, font=("Segoe UI", 19, "bold"), anchor="w")
            number.pack(anchor="w", pady=(9, 3)); self.metric_values[key] = number
            tk.Label(card, text=detail, bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=360, justify="left").pack(anchor="w")

        charts = tk.Frame(outer, bg=BG); charts.pack(fill="x", pady=(0, 14))
        charts.grid_columnconfigure(0, weight=2, uniform="chart")
        charts.grid_columnconfigure(1, weight=1, uniform="chart")
        trend = self._card(charts, 18); trend.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        trend_head = tk.Frame(trend, bg=PANEL); trend_head.pack(fill="x", pady=(0, 7))
        trend_title = tk.Frame(trend_head, bg=PANEL); trend_title.pack(side="left", fill="x", expand=True)
        tk.Label(trend_title, text="Chamadas à Laya", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(trend_title, text="Volume diário registrado no log local", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))
        periods = tk.Frame(trend_head, bg=PANEL); periods.pack(side="right")
        for label, value in (("7 dias", "7"), ("30 dias", "30"), ("Tudo", "all")):
            btn = tk.Button(periods, text=label, command=lambda v=value: self._set_period(v),
                            relief="flat", bd=0, padx=11, pady=8, cursor="hand2",
                            font=("Segoe UI", 9, "bold"), highlightthickness=0)
            btn.pack(side="left", padx=(4, 0))
            setattr(self, f"period_button_{value}", btn)
        self._paint_period_buttons()
        self.usage_chart = tk.Canvas(trend, height=250, bg=PANEL, highlightthickness=0)
        self.usage_chart.pack(fill="x", expand=True, pady=(4, 0))
        self.usage_chart.bind("<Configure>", lambda _e: self._render_charts())
        self.usage_chart.bind("<Motion>", self._chart_hover)
        self.usage_chart.bind("<Leave>", lambda _e: self.usage_chart.delete("tooltip"))

        mix = self._card(charts, 18); mix.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(mix, text="Resultado das chamadas", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(mix, text="Sucesso, falha e sem resultado", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 5))
        self.mix_chart = tk.Canvas(mix, height=250, bg=PANEL, highlightthickness=0)
        self.mix_chart.pack(fill="x", expand=True)
        self.mix_chart.bind("<Configure>", lambda _e: self._render_charts())
        self.mix_chart.bind("<Motion>", self._mix_hover)
        self.mix_chart.bind("<Leave>", lambda _e: self.mix_chart.delete("tooltip"))

        middle = tk.Frame(outer, bg=BG); middle.pack(fill="x", pady=(0, 14))
        middle.grid_columnconfigure(0, weight=3, uniform="middle"); middle.grid_columnconfigure(1, weight=2, uniform="middle")
        services = self._card(middle, 17); services.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        tk.Label(services, text="Serviços locais", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        tk.Label(services, text="Acessíveis só pelo computador · 127.0.0.1", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 7))
        self.dashboard_status = self._service_row(services, "Dashboard de uso", "API local · porta 8766")
        self.mcp_status = self._service_row(services, "MCP Laya", "Streamable HTTP · porta 8767")
        tk.Label(services, text="Reinicie Codex para recarregar ferramentas MCP.", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=460, justify="left").pack(anchor="w", pady=(10, 0))

        actions = self._card(middle, 17); actions.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(actions, text="Execução", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 9))
        self.startup_var = tk.BooleanVar(value=False)
        self.startup_check = ttk.Checkbutton(actions, text="Iniciar com o Windows", variable=self.startup_var,
                                              command=self._toggle_startup, style="Laya.TCheckbutton")
        self.startup_check.pack(anchor="w", pady=(2, 0), ipady=5)
        tk.Label(actions, text="Abre oculto na bandeja após entrar no Windows.", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=250, justify="left").pack(anchor="w", pady=(3, 11))
        self._button(actions, "Ocultar na bandeja", self.hide_to_tray).pack(fill="x", pady=4)
        self._button(actions, "Encerrar Laya", self.quit).pack(fill="x", pady=4)

        logs = self._card(outer, 15); logs.pack(fill="both", expand=True, pady=(0, 13))
        logs_head = tk.Frame(logs, bg=PANEL); logs_head.pack(fill="x", pady=(0, 8))
        tk.Label(logs_head, text="Chamadas da Laya", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(logs_head, text="Autor e pergunta registrados localmente", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")
        table_frame = tk.Frame(logs, bg=PANEL); table_frame.pack(fill="both", expand=True)
        self.log_tree = ttk.Treeview(table_frame, columns=("when", "caller", "tool", "question"), show="headings", height=6, style="Laya.Treeview")
        self.log_tree.heading("when", text="Quando")
        self.log_tree.heading("caller", text="Quem / origem")
        self.log_tree.heading("tool", text="Chamada")
        self.log_tree.heading("question", text="Pergunta enviada à Laya")
        self.log_tree.column("when", width=150, minwidth=130, stretch=False)
        self.log_tree.column("caller", width=190, minwidth=150, stretch=False)
        self.log_tree.column("tool", width=170, minwidth=140, stretch=False)
        self.log_tree.column("question", width=430, minwidth=240, stretch=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scroll.set)
        self.log_tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.log_tree.bind("<<TreeviewSelect>>", self._show_log_detail)
        self.log_details: dict[str, str] = {}
        self.question_detail = tk.Label(logs, text="Selecione uma chamada para ver a pergunta completa.", bg=PANEL_ALT, fg=MUTED,
                                        font=("Segoe UI", 9), wraplength=910, justify="left", anchor="w", padx=10, pady=7)
        self.question_detail.pack(fill="x", pady=(8, 0))

        info = self._card(outer, 17); info.pack(fill="x")
        tk.Label(info, text="Medição e privacidade", bg=PANEL, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 7))
        tk.Label(info, text="Chamadas vêm da auditoria local Laya. Tokens dependem da leitura dos rollouts Codex; falha de coleta aparece como indisponível. Valor é equivalência por tarifa hipotética, não cobrança. Perguntas ficam em log local sanitizado.", bg=PANEL, fg=MUTED, font=("Segoe UI", 10), wraplength=1100, justify="left").pack(anchor="w")
        self.footer = tk.Label(outer, text="Aguardando coleta local…", bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.footer.pack(fill="x", pady=(12, 0))

    @staticmethod
    def _card(parent: tk.Misc, padding: int) -> tk.Frame:
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=padding, pady=padding)

    def _scroll_content(self, event) -> None:
        widget = str(event.widget)
        if hasattr(self, "log_tree") and widget.startswith(str(self.log_tree)):
            return
        if event.delta:
            self.content_view.yview_scroll(-1 * int(event.delta / 120), "units")

    def _set_period(self, value: str) -> None:
        self.period.set(value)
        self._paint_period_buttons()
        self._render_charts()

    def _paint_period_buttons(self) -> None:
        for value in ("7", "30", "all"):
            button = getattr(self, f"period_button_{value}", None)
            if button:
                selected = self.period.get() == value
                button.configure(bg="#315b93" if selected else PANEL_ALT,
                                 fg=TEXT if selected else MUTED,
                                 activebackground="#3b6eae", activeforeground=TEXT)

    def _filtered_activity(self) -> list[dict]:
        rows = list(self.activity_data)
        period = self.period.get()
        if period != "all":
            cutoff = (date.today() - timedelta(days=int(period) - 1)).isoformat()
            rows = [row for row in rows if str(row.get("date") or "") >= cutoff]
        if len(rows) > 45:
            weekly: dict[str, dict] = {}
            for row in rows:
                try:
                    day = date.fromisoformat(str(row["date"])[:10])
                    start = day - timedelta(days=day.weekday())
                    key = start.isoformat()
                except (KeyError, ValueError):
                    continue
                bucket = weekly.setdefault(key, {"date": key, "calls": 0, "passed": 0, "failed": 0, "other": 0})
                for field in ("calls", "passed", "failed", "other"):
                    bucket[field] += int(row.get(field) or 0)
            rows = list(weekly.values())
        return rows

    def _render_charts(self) -> None:
        if not hasattr(self, "usage_chart") or not self.usage_chart.winfo_exists():
            return
        self._draw_activity_chart(self._filtered_activity())
        self._draw_result_chart(self._filtered_activity())

    def _draw_activity_chart(self, rows: list[dict]) -> None:
        canvas = self.usage_chart
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right, top, bottom = 58, width - 18, 18, height - 34
        self.chart_points = []
        if not rows or right - left < 80 or bottom - top < 70:
            canvas.create_text(width / 2, height / 2, text="Sem chamadas neste período",
                               fill=MUTED, font=("Segoe UI", 10))
            return
        values = [int(row.get("calls") or 0) for row in rows]
        maximum = max(values) or 1
        ceiling = maximum if maximum <= 4 else ((maximum + 3) // 4) * 4
        for tick in range(5):
            value = ceiling * tick / 4
            y = bottom - (bottom - top) * tick / 4
            canvas.create_line(left, y, right, y, fill=GRID, width=1)
            canvas.create_text(left - 9, y, text=f"{value:g}", fill=MUTED,
                               font=("Segoe UI", 8), anchor="e")
        step = (right - left) / max(1, len(rows) - 1)
        coords = []
        for index, row in enumerate(rows):
            x = left + index * step if len(rows) > 1 else (left + right) / 2
            y = bottom - (bottom - top) * (int(row.get("calls") or 0) / ceiling)
            self.chart_points.append((x, y, row))
            coords.extend((x, y))
        if len(rows) == 1:
            x, y = coords
            canvas.create_rectangle(x - 14, y, x + 14, bottom, fill="#25466c", outline="")
        else:
            fill = [coords[0], bottom, *coords, coords[-2], bottom]
            canvas.create_polygon(*fill, fill="#172c43", outline="", smooth=True)
            canvas.create_line(*coords, fill=CYAN, width=3, smooth=True, splinesteps=16)
        for index, (x, y, row) in enumerate(self.chart_points):
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=CYAN,
                               outline=PANEL, width=2, tags=(f"point-{index}",))
        label_indexes = sorted(set((0, len(rows) // 2, len(rows) - 1)))
        for index in label_indexes:
            x, _, row = self.chart_points[index]
            label = str(row.get("date") or "")
            if len(label) >= 10:
                label = label[8:10] + "/" + label[5:7]
            canvas.create_text(x, bottom + 17, text=label, fill=MUTED,
                               font=("Segoe UI", 8), anchor="n")

    def _draw_result_chart(self, rows: list[dict]) -> None:
        canvas = self.mix_chart
        canvas.delete("all")
        self.result_segments = []
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        values = [sum(int(row.get(key) or 0) for row in rows)
                  for key in ("passed", "failed", "other")]
        labels = (("Sucesso", GREEN), ("Falha", "#ff7882"), ("Indefinido", AMBER))
        total = sum(values)
        if not rows or total == 0 or width < 100:
            canvas.create_text(width / 2, height / 2, text="Sem resultados para exibir",
                               fill=MUTED, font=("Segoe UI", 10))
            return
        x0, x1 = 18, width - 18
        y0, y1 = 22, 58
        cursor = x0
        for (label, color), value in zip(labels, values):
            if not value:
                continue
            segment_width = (x1 - x0) * value / total
            end = cursor + segment_width
            canvas.create_rectangle(cursor, y0, end, y1, fill=color, outline=PANEL,
                                    width=2, tags=(f"result-{label}",))
            self.result_segments.append((cursor, end, label, value, color))
            cursor = end
        y = 84
        for (label, color), value in zip(labels, values):
            pct = value * 100 / total if total else 0
            canvas.create_oval(20, y + 3, 29, y + 12, fill=color, outline="")
            canvas.create_text(39, y, text=label, fill=TEXT, font=("Segoe UI", 9), anchor="nw")
            canvas.create_text(width - 18, y, text=f"{value}  ·  {pct:.0f}%", fill=MUTED,
                               font=("Segoe UI", 9, "bold"), anchor="ne")
            y += 31
        canvas.create_text(20, height - 20, text=f"{total} chamadas no período",
                           fill=MUTED, font=("Segoe UI", 9), anchor="w")

    def _chart_hover(self, event) -> None:
        canvas = self.usage_chart
        canvas.delete("tooltip")
        if not self.chart_points:
            return
        x, y, row = min(self.chart_points, key=lambda point: (point[0] - event.x) ** 2 + (point[1] - event.y) ** 2)
        if (x - event.x) ** 2 + (y - event.y) ** 2 > 24 ** 2:
            return
        label = str(row.get("date") or "")
        tip = f"{label}  ·  {int(row.get('calls') or 0)} chamadas"
        canvas.create_rectangle(x + 12, max(8, y - 31), x + 168, max(35, y - 3),
                                fill="#23344a", outline=LINE, tags="tooltip")
        canvas.create_text(x + 20, max(11, y - 27), text=tip, fill=TEXT,
                           font=("Segoe UI", 8, "bold"), anchor="nw", tags="tooltip")

    def _mix_hover(self, event) -> None:
        canvas = self.mix_chart
        canvas.delete("tooltip")
        for left, right, label, value, _color in getattr(self, "result_segments", []):
            if left <= event.x <= right and 22 <= event.y <= 58:
                tip = f"{label}: {value} chamadas"
                x = min(max(12, event.x + 10), max(12, canvas.winfo_width() - 170))
                canvas.create_rectangle(x, 5, x + 158, 28, fill="#23344a", outline=LINE, tags="tooltip")
                canvas.create_text(x + 8, 9, text=tip, fill=TEXT, font=("Segoe UI", 8, "bold"), anchor="nw", tags="tooltip")
                break

    @staticmethod
    def _button(parent: tk.Misc, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg="#3e73c7" if primary else PANEL_ALT, fg=TEXT,
                         activebackground="#345f9f", activeforeground="white", relief="flat", bd=0, padx=15, pady=10,
                         cursor="hand2", font=("Segoe UI", 10, "bold"), highlightthickness=2,
                         highlightbackground=LINE, highlightcolor=BLUE)

    def _service_row(self, parent: tk.Misc, title: str, detail: str) -> tk.Label:
        row = tk.Frame(parent, bg=PANEL); row.pack(fill="x", pady=7)
        text = tk.Frame(row, bg=PANEL); text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=title, bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(text, text=detail, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        status = tk.Label(row, text="Verificando", bg=PANEL_ALT, fg=AMBER, padx=10, pady=6, font=("Segoe UI", 9, "bold")); status.pack(side="right")
        return status

    def _set_startup_state(self) -> None:
        try:
            self.startup_var.set(startup.is_enabled())
        except Exception as exc:
            self.footer.configure(text=f"Autostart indisponível: {type(exc).__name__}")

    def _toggle_startup(self) -> None:
        try:
            startup.set_enabled(self.startup_var.get())
        except Exception as exc:
            self.startup_var.set(not self.startup_var.get())
            messagebox.showerror("Autostart", f"Não foi possível atualizar: {type(exc).__name__}", parent=self.root)

    def refresh(self) -> None:
        if self.loading: return
        self.loading = True
        self.refresh_button.configure(state="disabled", text="Atualizando…")
        threading.Thread(target=self._fetch_worker, daemon=True, name="laya-dashboard-refresh").start()

    def _fetch_worker(self) -> None:
        try: self.results.put(read_services())
        except Exception as exc: self.results.put({"dashboard": None, "mcp": False, "error": type(exc).__name__})

    def _drain_results(self) -> None:
        if self.loading and not self.results.empty():
            result = self.results.get_nowait(); self.loading = False
            self.refresh_button.configure(state="normal", text="Atualizar")
            dashboard = result.get("dashboard")
            usage = dashboard.get("codex_usage") if isinstance(dashboard, dict) else None
            dashboard_ok = isinstance(dashboard, dict) and isinstance(dashboard.get("metrics"), dict)
            mcp_ok = bool(result.get("mcp"))
            if dashboard_ok:
                self.activity_data = dashboard.get("call_timeline") or []
                self.call_summary = dashboard.get("call_summary") or {}
                self._render_call_logs(dashboard.get("call_logs") or [])
                self._render_charts()
                self.metric_values["calls"].configure(text=format_number(self.call_summary.get("total")))
            self.dashboard_status.configure(text="Online" if dashboard_ok else "Sem resposta", fg=GREEN if dashboard_ok else AMBER)
            self.mcp_status.configure(text="Porta ativa" if mcp_ok else "Sem resposta", fg=GREEN if mcp_ok else AMBER)
            if dashboard_ok and usage:
                scan = usage.get("scan") or {}
                usage_unavailable = scan.get("status") == "error" or any("ilegível" in str(w).lower() for w in dashboard.get("warnings", []))
                self.metric_values["tokens"].configure(text="Indisponível" if usage_unavailable else format_number(usage.get("total_tokens")))
                cost = None if usage_unavailable else estimate_brl(usage)
                amount = f"R$ {cost:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if cost is not None else "Indisponível"
                self.metric_values["cost"].configure(text=amount)
                scan_state = "histórico ilegível" if usage_unavailable else scan.get("status", "indisponível")
                self.footer.configure(text=f"Atualizado {datetime.now().strftime('%H:%M:%S')} · {format_number(self.call_summary.get('total'))} chamadas Laya · coleta tokens: {scan_state}")
            else:
                self.footer.configure(text=f"Dashboard indisponível ({result.get('error') or 'sem resposta'}) · {datetime.now().strftime('%H:%M:%S')}")
            if dashboard_ok and mcp_ok: label, color = "Serviços locais ativos", GREEN
            elif dashboard_ok or mcp_ok: label, color = "Serviço parcial", AMBER
            else: label, color = "Serviços indisponíveis", "#ff8c8c"
            self.overall.configure(text=label, fg=color)
        self.root.after(150, self._drain_results)

    def _render_call_logs(self, rows: list[dict]) -> None:
        self.log_tree.delete(*self.log_tree.get_children())
        self.log_details.clear()
        for index, row in enumerate(rows[:200]):
            stamp = str(row.get("ts") or "")
            stamp = stamp.replace("T", " ")[:19]
            caller = str(row.get("caller") or "Não identificado")
            tool = str(row.get("tool") or "Chamada MCP")
            question = str(row.get("question") or "Pergunta não preservada")
            if len(question) > 180:
                preview = question[:177].rstrip() + "…"
            else:
                preview = question
            iid = f"call-{index}"
            self.log_tree.insert("", "end", iid=iid, values=(stamp, caller, tool, preview))
            details = f"{question}\n\nResultado: {row.get('passed') or 'sem resultado'}"
            if row.get("routed"):
                details += f" · modelo: {row['routed']}"
            if row.get("error"):
                details += f" · erro: {row['error']}"
            self.log_details[iid] = details
        if not rows:
            self.question_detail.configure(text="Nenhuma chamada registrada ainda. Próximas chamadas aparecerão aqui.")

    def _show_log_detail(self, _event=None) -> None:
        selected = self.log_tree.selection()
        if selected:
            self.question_detail.configure(text=self.log_details.get(selected[0], "Pergunta não preservada"))

    def _poll(self) -> None:
        self.refresh()
        self.root.after(POLL_MS, self._poll)

    def _ensure_tray(self) -> bool:
        if self.tray is None:
            try:
                self.tray = WindowsTray(self.root, refresh=self.refresh, on_exit=self.quit,
                                        on_restart=self.restart)
                self.tray.attach()
            except Exception as exc:
                messagebox.showerror("Bandeja do sistema", f"Não foi possível criar ícone: {type(exc).__name__}", parent=self.root)
                self.tray = None
                return False
        return True

    def hide_to_tray(self) -> None:
        if not self._ensure_tray():
            return
        self.tray.hide()

    def restart(self) -> None:
        """Replace this UI process; keep the independent MCP/API services alive."""
        if self.tray:
            self.tray.shutdown()
            self.tray = None
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, str(Path(__file__).resolve())]
        try:
            os.execv(sys.executable, command)
        except OSError as exc:
            self.root.deiconify()
            self.root.lift()
            messagebox.showerror("Reiniciar Laya", f"Não foi possível reiniciar: {type(exc).__name__}", parent=self.root)

    def quit(self) -> None:
        if self.tray: self.tray.shutdown()
        self.content_view.unbind_all("<MouseWheel>")
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true", help="start hidden in notification area")
    args = parser.parse_args()
    ensure_services()
    root = tk.Tk()
    app = LayaLocalApp(root, background=args.background)
    root.mainloop()


if __name__ == "__main__":
    main()
