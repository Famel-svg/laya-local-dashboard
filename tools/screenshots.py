"""Regenerate the README screenshots from docs/demo_api.json (no private data on screen).

    python tools/screenshots.py

Each shot runs the real app in its off-screen --screenshot mode against a throwaway
local server that serves the demo payload, so the pictures always match the code.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "docs" / "demo_api.json"
PORT = 8799
SHOTS = [
    ("dashboard.png", []),
    ("dashboard-custo.png", ["--chart", "cost", "--period", "all", "--hover", "31"]),
    ("dashboard-7dias.png", ["--period", "7", "--hover", "3"]),
    ("dashboard-compacto.png", ["--size", "1000x720"]),
]


def child(name: str, args: list[str]) -> int:
    body = DEMO.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.path.insert(0, str(ROOT / "prototype"))
    import laya_app

    laya_app.API_URL, laya_app.API_PORT = f"http://127.0.0.1:{PORT}/api", PORT
    laya_app.port_open = lambda _port: True          # picture the healthy state
    laya_app.Dashboard._auto_start_services = lambda self: None
    sys.argv = ["laya_app", "--screenshot", str(ROOT / "docs" / name), *args]
    return laya_app.main()


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        return child(sys.argv[2], sys.argv[3:])
    for name, args in SHOTS:
        code = subprocess.run([sys.executable, __file__, "--child", name, *args]).returncode
        print(f"{name}: {'ok' if code == 0 else 'falhou'}")
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
