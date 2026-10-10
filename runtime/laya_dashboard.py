from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.request
import atexit
import os
import hashlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone

try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding("o200k_base")
    TOKEN_ESTIMATOR = "tiktoken o200k_base"
except Exception:
    _ENCODING = None
    TOKEN_ESTIMATOR = "UTF-8 bytes / 4 fallback"

ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Codex" / "Laya"
DB = ROOT / "telemetry.sqlite3"
CALL_AUDIT_DB = ROOT / "call_audit.sqlite3"
LLM_TOKEN_NOTE = (
    "Payload MCP é estimativa de tamanho, não tokens LLM. Tokens reais dos rollouts Codex são agregados no painel; "
    "não há correlação por chamada individual do Laya."
)
CODEX_SESSIONS = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
USAGE_SCAN_INTERVAL = max(30, int(os.environ.get("LAYA_USAGE_SCAN_INTERVAL", "120")))
USAGE_SCAN_STATE = {"status": "starting", "last_scan": None, "files": 0, "errors": 0}
USAGE_SCAN_LOCK = threading.Lock()
_USAGE_THREAD_STARTED = False
RATE_CARD = {
    "GPT-6 Luna": {"input": 0.10, "cached": 0.01, "output": 0.50},
    "GPT-6 Sol": {"input": 2.00, "cached": 0.20, "output": 10.00},
    "GPT-6.1 Sol": {"input": 2.00, "cached": 0.10, "output": 10.00},
    "GPT-6 Astra": {"input": 10.00, "cached": 1.00, "output": 50.00},
    "GPT-5.6 Luna": {"input": 0.20, "cached": 0.02, "output": 1.20},
    "GPT-5.6 Terra": {"input": 2.00, "cached": 0.20, "output": 12.00},
    "GPT-5.6 Sol": {"input": 4.00, "cached": 0.40, "output": 20.00},
    "GPT-5.3": {"input": 1.75, "cached": 0.175, "output": 14.00},
    "GPT-5.3-Codex": {"input": 1.75, "cached": 0.175, "output": 14.00},
    "GPT-5.2": {"input": 1.75, "cached": 0.175, "output": 14.00},
}

def _usage_key(thread_id, response_id):
    return hashlib.sha256((str(thread_id) + "\0" + str(response_id)).encode("utf-8")).hexdigest()

def scan_codex_usage_once():
    """Import per-response token metadata only; never persist conversation content."""
    if not USAGE_SCAN_LOCK.acquire(blocking=False):
        return
    scanned = errors = missing_ids = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        if not CODEX_SESSIONS.is_dir():
            USAGE_SCAN_STATE.update(status="unavailable", last_scan=now, files=0, errors=0)
            return
        with sqlite3.connect(DB, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS codex_usage (usage_key TEXT PRIMARY KEY, usage_at TEXT, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS codex_usage_files (file_key TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0)")
            if "offset" not in {r[1] for r in db.execute("PRAGMA table_info(codex_usage_files)")}:
                db.execute("ALTER TABLE codex_usage_files ADD COLUMN offset INTEGER NOT NULL DEFAULT 0")
            known = {r[0]: (r[1], r[2], r[3]) for r in db.execute("SELECT file_key,size,mtime_ns,offset FROM codex_usage_files")}
            batch = []
            for path in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
                try:
                    st = path.stat()
                    file_key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
                    identity = (file_key, st.st_size, st.st_mtime_ns)
                    previous = known.get(file_key)
                    if previous and previous[:2] == identity[1:] and previous[2] >= st.st_size:
                        continue
                    offset = previous[2] if previous and st.st_size >= previous[0] else 0
                    scanned += 1
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        while True:
                            line_start = stream.tell()
                            line = stream.readline(max(0, st.st_size - line_start))
                            if not line:
                                offset = line_start
                                break
                            complete = line.endswith(b"\n")
                            offset = stream.tell() if complete else line_start
                            if b'"type":"token_usage_record"' not in line and b'"type": "token_usage_record"' not in line:
                                if complete:
                                    continue
                                break
                            try:
                                row = json.loads(line.decode("utf-8"))
                                if not complete:
                                    offset = stream.tell()
                                if row.get("type") != "token_usage_record":
                                    continue
                                payload = row.get("payload") or {}
                                thread, response = payload.get("thread_id"), payload.get("response_id")
                                if not thread or not response:
                                    missing_ids += 1
                                    continue
                                usage = payload.get("usage") or {}
                                names = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
                                v = {k: max(0, int(usage.get(k) or 0)) for k in names}
                                if not v["total_tokens"]:
                                    v["total_tokens"] = v["input_tokens"] + v["output_tokens"]
                                batch.append((_usage_key(thread, response), row.get("timestamp"), v["input_tokens"], v["cached_input_tokens"], v["output_tokens"], v["reasoning_output_tokens"], v["total_tokens"]))
                            except json.JSONDecodeError:
                                if complete:
                                    errors += 1
                                else:
                                    offset = line_start
                                    break
                            except (ValueError, TypeError):
                                errors += 1
                    db.execute("INSERT INTO codex_usage_files(file_key,size,mtime_ns,offset) VALUES(?,?,?,?) ON CONFLICT(file_key) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,offset=excluded.offset", (*identity, offset))
                except (OSError, UnicodeError):
                    errors += 1
            db.executemany("INSERT INTO codex_usage VALUES(?,?,?,?,?,?,?) ON CONFLICT(usage_key) DO UPDATE SET usage_at=excluded.usage_at,input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,output_tokens=excluded.output_tokens,reasoning_output_tokens=excluded.reasoning_output_tokens,total_tokens=excluded.total_tokens WHERE excluded.total_tokens>codex_usage.total_tokens", batch)
            db.commit()
        USAGE_SCAN_STATE.update(status="ready", last_scan=now, files=scanned, errors=errors, missing_ids=missing_ids)
    except sqlite3.Error:
        USAGE_SCAN_STATE.update(status="error", last_scan=now, files=scanned, errors=errors + 1)
    finally:
        USAGE_SCAN_LOCK.release()

def usage_scan_loop():
    while True:
        scan_codex_usage_once()
        time.sleep(USAGE_SCAN_INTERVAL)

def codex_usage_summary():
    daily = []
    with sqlite3.connect(DB) as db:
        try:
            r = db.execute("SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(reasoning_output_tokens),0),COALESCE(SUM(total_tokens),0),MIN(usage_at),MAX(usage_at) FROM codex_usage").fetchone()
            files = db.execute("SELECT COUNT(*) FROM codex_usage_files").fetchone()[0]
            daily = [dict(zip(("date", "responses", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"), row))
                     for row in db.execute("SELECT substr(usage_at,1,10),COUNT(*),SUM(input_tokens),SUM(cached_input_tokens),SUM(output_tokens),SUM(total_tokens) FROM codex_usage WHERE usage_at IS NOT NULL GROUP BY substr(usage_at,1,10) ORDER BY substr(usage_at,1,10)")]
        except sqlite3.DatabaseError:
            r, files = (0,0,0,0,0,0,None,None), 0
    state = dict(USAGE_SCAN_STATE)
    state["files_indexed"] = files
    return {"responses": r[0], "input_tokens": r[1], "cached_input_tokens": r[2], "uncached_input_tokens": max(0,r[1]-r[2]), "output_tokens": r[3], "reasoning_output_tokens": r[4], "total_tokens": r[5], "first_usage": r[6], "last_usage": r[7], "daily": daily, "scan": state, "pricing_source": "OpenAI Enterprise token rate card; assumed model selected in dashboard; not account billing", "pricing_source_url": "https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing", "rates_usd_per_million": RATE_CARD, "measured_savings": None, "measured_savings_note": "Indisponível: não há pares control/treatment comparáveis registrados."}

def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if _ENCODING is not None:
        return len(_ENCODING.encode(text, disallowed_special=()))
    return max(1, round(len(text.encode("utf-8")) / 4))

# Filled in by the MCP wrapper when it hosts this API; empty when the API runs standalone.
MODEL_STATE_PROVIDER = None
MODEL_ACTIONS = {}

def error_code_of(reason, error=None, stored=None):
    """Error code for a failed call: stored value, else the JSON kept in ``reason``."""
    if stored:
        return stored
    try:
        return str(json.loads(reason).get("error") or error or "") or None
    except (ValueError, TypeError, AttributeError):
        return error

def dashboard_is_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=1) as response:
            payload = json.load(response)
        metrics = payload.get("metrics", {})
        return all(key in metrics for key in ("requests", "passed", "calls", "skipped")) and isinstance(payload.get("rows"), list)
    except Exception:
        return False

def init_db():
    global _USAGE_THREAD_STARTED
    ROOT.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tool TEXT NOT NULL, request TEXT, routed TEXT, passed TEXT, calls INTEGER DEFAULT 0, latency REAL, confidence REAL, confidence_note TEXT, laya_tokens INTEGER, llm_in INTEGER, llm_out INTEGER, reason TEXT, flow TEXT, error TEXT, llm_token_note TEXT, payload_in INTEGER, payload_out INTEGER)"
        )
        cols = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if "llm_token_note" not in cols:
            db.execute("ALTER TABLE events ADD COLUMN llm_token_note TEXT")
        if "payload_in" not in cols:
            db.execute("ALTER TABLE events ADD COLUMN payload_in INTEGER")
        if "payload_out" not in cols:
            db.execute("ALTER TABLE events ADD COLUMN payload_out INTEGER")
        try:
            db.execute("UPDATE events SET payload_in=llm_in WHERE payload_in IS NULL AND llm_in IS NOT NULL")
            db.execute("UPDATE events SET payload_out=llm_out WHERE payload_out IS NULL AND llm_out IS NOT NULL")
            db.execute("UPDATE events SET llm_in=NULL,llm_out=NULL WHERE payload_in IS NOT NULL OR payload_out IS NOT NULL")
            db.commit()
        except sqlite3.DatabaseError:
            # Keep serving from a damaged legacy DB; new audit records use their own file.
            db.rollback()
    if not _USAGE_THREAD_STARTED:
        _USAGE_THREAD_STARTED = True
        threading.Thread(target=usage_scan_loop, name="laya-codex-usage-scan", daemon=True).start()

def data():
    init_db()
    legacy_error = False
    rows, routes, skip_reasons = [], {}, []
    agg = (0, 0, 0, None, None, 0, 0)
    rejected = normalized = 0
    try:
        with sqlite3.connect(DB) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200")]
            agg = db.execute(
                "SELECT COUNT(*), SUM(CASE WHEN passed='yes' THEN 1 ELSE 0 END), SUM(calls), AVG(latency), AVG(confidence), SUM(CASE WHEN passed='no' THEN 1 ELSE 0 END), SUM(CASE WHEN passed='error' THEN 1 ELSE 0 END) FROM events WHERE tool!='laya_status'"
            ).fetchone()
            # Payloads the validator refused never reached a model; count them apart from real failures.
            rejected = db.execute("SELECT COUNT(*) FROM events WHERE passed='error' AND instr(replace(reason,' ',''), ?)>0", ('"error":"invalid_',)).fetchone()[0]
            normalized = db.execute("SELECT COUNT(*) FROM events WHERE instr(flow, 'criteria normalizado')>0").fetchone()[0]
            routes = {r[0] or "not routed": r[1] for r in db.execute("SELECT routed,COUNT(*) FROM events WHERE tool!='laya_status' GROUP BY routed")}
            skip_reasons = [{"reason": r[0], "count": r[1]} for r in db.execute("SELECT reason,COUNT(*) FROM events WHERE tool='laya_log_not_routed' AND reason IS NOT NULL GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 8")]
    except sqlite3.DatabaseError:
        legacy_error = True
    call_logs = []
    call_timeline = []
    call_summary = {"total": 0, "passed": 0, "failed": 0, "other": 0}
    cutoff = None
    try:
        with sqlite3.connect(f"file:{CALL_AUDIT_DB}?mode=ro", uri=True, timeout=2) as audit:
            audit.row_factory = sqlite3.Row
            meta = audit.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()
            cutoff = meta[0] if meta else None
            present = {r[1] for r in audit.execute("PRAGMA table_info(calls)")}
            columns = ["id", "ts", "caller", "tool", "question", "routed", "passed", "error",
                       *(c for c in ("latency", "confidence", "reason", "error_code") if c in present)]
            call_logs = [dict(row) for row in audit.execute("SELECT " + ",".join(columns) + " FROM calls ORDER BY id DESC LIMIT 200")]
            for call in call_logs:
                if call.get("passed") == "error":
                    call["error_code"] = error_code_of(call.get("reason"), call.get("error"), call.get("error_code"))
            call_timeline = [dict(zip(("date", "calls", "passed", "failed", "other"), row))
                             for row in audit.execute("SELECT substr(ts,1,10),COUNT(*),SUM(CASE WHEN passed='yes' THEN 1 ELSE 0 END),SUM(CASE WHEN passed IN ('no','error') THEN 1 ELSE 0 END),SUM(CASE WHEN passed IS NULL OR passed NOT IN ('yes','no','error') THEN 1 ELSE 0 END) FROM calls GROUP BY substr(ts,1,10) ORDER BY substr(ts,1,10)")]
            totals = audit.execute("SELECT COUNT(*),SUM(CASE WHEN passed='yes' THEN 1 ELSE 0 END),SUM(CASE WHEN passed IN ('no','error') THEN 1 ELSE 0 END),SUM(CASE WHEN passed IS NULL OR passed NOT IN ('yes','no','error') THEN 1 ELSE 0 END) FROM calls").fetchone()
            call_summary = dict(zip(("total", "passed", "failed", "other"), (int(v or 0) for v in totals)))
    except sqlite3.Error:
        pass
    if cutoff:
        for row in rows:
            if row.get("ts") and row["ts"] < cutoff and row.get("tool") != "laya_status":
                call_logs.append({"id": f"legacy-{row['id']}", "ts": row.get("ts"),
                                  "caller": "Não registrado (histórico)", "tool": row.get("tool"),
                                  "question": row.get("request") or "Pergunta não preservada",
                                  "routed": row.get("routed"), "passed": row.get("passed"),
                                  "error": row.get("error"), "latency": row.get("latency"),
                                  "confidence": row.get("confidence"), "reason": row.get("reason"),
                                  "error_code": error_code_of(row.get("reason"), row.get("error")) if row.get("passed") == "error" else None})
    call_logs.sort(key=lambda item: item.get("ts") or "", reverse=True)
    recent = list(reversed([r for r in rows if r["tool"] != "laya_status"][:20]))
    latencies = sorted(float(r["latency"]) for r in rows if r["tool"] != "laya_status" and r["latency"] is not None)
    def percentile(values, p):
        if not values: return None
        return values[max(0, min(len(values)-1, int((len(values)-1)*p + 0.5)))]
    tracked = agg[0] or 0
    errors = agg[6] or 0
    success_rate = ((tracked-errors)/tracked*100) if tracked else None
    pass_rate = ((agg[1] or 0)/tracked*100) if tracked else None
    warnings = [
        LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + ".",
        "Laya confidence usa answer_confidence; calibração varia por checkpoint e faixa de opções."
    ]
    if legacy_error:
        warnings.append("Histórico antigo de telemetria está ilegível; chamadas novas ficam em arquivo de auditoria separado.")
    return {
        "rows": [{**r, "time": r["ts"].replace("T", " ")[:19]} for r in rows],
        "call_logs": call_logs[:200],
        "call_timeline": call_timeline,
        "call_summary": call_summary,
        "metrics": {"requests": agg[0] or 0, "passed": agg[1] or 0, "calls": agg[2] or 0, "latency": agg[3], "confidence": agg[4], "skipped": agg[5] or 0, "errors": errors, "rejected": rejected, "failures": max(0, errors - rejected), "normalized": normalized, "success_rate": success_rate, "pass_rate": pass_rate, "latency_p50": percentile(latencies,.50), "latency_p95": percentile(latencies,.95), "samples": len(latencies)},
        "routes": routes,
        "skip_reasons": skip_reasons,
        "timeline": {"calls": [r["calls"] or 0 for r in recent], "skips": [1 if r["tool"] == "laya_log_not_routed" else 0 for r in recent]},
        "codex_usage": codex_usage_summary(),
        "model": MODEL_STATE_PROVIDER() if MODEL_STATE_PROVIDER else None,
        "warnings": warnings
    }

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api":
            body = json.dumps(data(), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            body = b"Not found"
            content_type = "text/plain; charset=utf-8"
            self.send_response(404)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Local model controls: POST /model/warm | /model/unload (loopback only, custom header required)."""
        action = MODEL_ACTIONS.get(self.path.removeprefix("/model/")) if self.path.startswith("/model/") else None
        # The header cannot be set by a plain cross-site form, so a web page cannot trigger these.
        if action is None or self.headers.get("X-Laya-Local") != "1":
            status, payload = 404, {"error": "not_found"}
        else:
            try:
                status, payload = 200, {"result": action()}
            except Exception as exc:
                status, payload = 500, {"error": type(exc).__name__}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

def serve():
    init_db()
    port = int(os.environ.get("LAYA_DASHBOARD_PORT", "8766"))
    lock_path = ROOT / "dashboard.lock"
    lock_file = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            atexit.register(lambda: (lock_file.seek(0), msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1), lock_file.close()))
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            atexit.register(lock_file.close)
    except OSError:
        lock_file.close()
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return
    server.serve_forever()

if __name__ == "__main__":
    serve()
