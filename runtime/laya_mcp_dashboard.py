from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from laya.mcp import server as laya_server
from laya.mcp.server import _dump
from laya_dispatch import build_question, count_laya_input_tokens, select_dependency

from laya_dashboard import ROOT, CALL_AUDIT_DB, serve, init_db, estimate_tokens, LLM_TOKEN_NOTE, TOKEN_ESTIMATOR, dashboard_is_healthy
import sqlite3

init_db()

def init_call_audit():
    ROOT.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CALL_AUDIT_DB, timeout=10) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, caller TEXT NOT NULL, tool TEXT NOT NULL, question TEXT, routed TEXT, passed TEXT, error TEXT)")
        db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('started_at',?)", (datetime.now().astimezone().isoformat(),))
        present = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
        for column, kind in (("latency", "REAL"), ("confidence", "REAL"), ("reason", "TEXT"), ("error_code", "TEXT")):
            if column not in present:
                db.execute(f"ALTER TABLE calls ADD COLUMN {column} {kind}")

QUESTION_SHAPE_HINT = {
    "choice": {"type": "choice", "instructions": "Pick the best option.",
               "criteria": {"option_a": "when A applies", "option_b": "when B applies"}},
    "score": {"type": "score", "instructions": "Rate severity.", "criteria": ["low", "medium", "high"]},
    "noul": {"type": "noul", "instructions": "Is the condition true?"},
}

def normalize_questions(questions):
    """Repair the payload shapes agents most often get wrong, before validation.

    choice.criteria sent as a list becomes {label: description}; score.criteria sent
    as an object becomes a list of its descriptions. Anything else is left for the
    validator to reject. Returns (questions, notes).
    """
    if not isinstance(questions, dict):
        return questions, []
    fixed, notes = {}, []
    for name, spec in questions.items():
        if isinstance(spec, dict):
            criteria, qtype = spec.get("criteria"), spec.get("type")
            if qtype == "choice" and isinstance(criteria, list) and criteria:
                mapping = {}
                for item in criteria:
                    if isinstance(item, dict):
                        label = item.get("label") or item.get("id") or item.get("name") or item.get("value")
                        description = item.get("description") or item.get("desc") or label
                    else:
                        label = description = item
                    if isinstance(label, (str, int, float)) and str(label).strip():
                        mapping[str(label)] = str(description)
                if len(mapping) == len(criteria):
                    spec = {**spec, "criteria": mapping}
                    notes.append(f"{name}: choice.criteria list->object")
            elif qtype == "score" and isinstance(criteria, dict) and criteria:
                spec = {**spec, "criteria": [str(v) if str(v).strip() else str(k) for k, v in criteria.items()]}
                notes.append(f"{name}: score.criteria object->list")
        fixed[name] = spec
    return fixed, notes

def error_details(exc):
    """(code, message) from the JSON carried by McpToolError; falls back to the type name."""
    try:
        payload = json.loads(str(exc))
        return str(payload.get("error") or type(exc).__name__), str(payload.get("message") or "")
    except (ValueError, AttributeError):
        return type(exc).__name__, str(exc)

def audit_question(state, questions=None):
    """Keep only structured decision input, then apply existing redaction/cap."""
    payload = {}
    if state is not None:
        payload["context"] = state
    if questions:
        payload["questions"] = questions
    if not payload:
        return "Consulta sem pergunta textual"
    return safe_request(payload)[:2400]

def audit_caller():
    return os.environ.get("LAYA_AUDIT_CALLER", "Cliente MCP local (identidade não exposta)")[:120]

init_call_audit()
port = int(os.environ.get("LAYA_DASHBOARD_PORT", "8766"))
with socket.socket() as probe:
    port_open = probe.connect_ex(("127.0.0.1", port)) == 0
dashboard_running = dashboard_is_healthy(port)
if port_open and not dashboard_running:
    raise RuntimeError(f"Port {port} is occupied by a service that is not the Laya dashboard")
if not dashboard_running:
    threading.Thread(target=serve, name="laya-dashboard", daemon=True).start()
    for _ in range(80):
        if dashboard_is_healthy(port):
            dashboard_running = True
            break
        threading.Event().wait(0.25)
    if not dashboard_running:
        with socket.socket() as probe:
            port_open = probe.connect_ex(("127.0.0.1", port)) == 0
        if port_open:
            raise RuntimeError(f"Port {port} became occupied by a service that is not the Laya dashboard")
        raise RuntimeError(f"Laya dashboard did not become healthy on 127.0.0.1:{port}")

def safe_request(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?i)(api[_ -]?key|token|password|secret)(\s*[=:]\s*)[^,}\s\"]+", r"\1\2[REDACTED]", text)
    return text[:2400]

def persist(event):
    ts = datetime.now().astimezone().isoformat()
    with sqlite3.connect(CALL_AUDIT_DB, timeout=10) as db:
        db.execute(
            "INSERT INTO calls(ts,caller,tool,question,routed,passed,error,latency,confidence,reason,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, event.get("caller") or audit_caller(), event.get("tool", "laya_tool"),
             event.get("question"), event.get("routed"), event.get("passed"), event.get("error"),
             event.get("latency"), event.get("confidence"),
             str(event["reason"])[:500] if event.get("reason") else None, event.get("error_code")),
        )
    init_db()
    with sqlite3.connect(ROOT / "telemetry.sqlite3", timeout=10) as db:
        db.execute(
            "INSERT INTO events(ts,tool,request,routed,passed,calls,latency,confidence,confidence_note,laya_tokens,llm_in,llm_out,reason,flow,error,llm_token_note,payload_in,payload_out) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).astimezone().isoformat(),
                event.get("tool"), event.get("request"), event.get("routed"), event.get("passed"),
                event.get("calls", 0), event.get("latency"), event.get("confidence"),
                event.get("confidence_note"), event.get("laya_tokens"), event.get("llm_in"),
                event.get("llm_out"), event.get("reason"), event.get("flow"), event.get("error"),
                event.get("llm_token_note"), event.get("payload_in"), event.get("payload_out")
            )
        )

# ---------------------------------------------------------------- model lifecycle
# Keep the GPU free unless an agent is around: warm the default checkpoint when a client
# connects (tools/list or laya_status), unload after an idle period or under VRAM pressure.
# Measured on this machine: resident english = 2.7 GB VRAM, reload after unload = 0.7 s.
WARM_MODEL = os.environ.get("LAYA_WARM_MODEL", "english").strip() or "english"
WARM_ON_CONNECT = os.environ.get("LAYA_WARM_ON_CONNECT", "1") != "0"
IDLE_UNLOAD_S = max(60, int(os.environ.get("LAYA_IDLE_UNLOAD_S", "600")))
WARM_MIN_FREE_MB = int(os.environ.get("LAYA_WARM_MIN_FREE_MB", "4000"))
PRESSURE_FREE_MB = int(os.environ.get("LAYA_PRESSURE_FREE_MB", "1500"))
_LIFE = {"warming": False, "note": "", "last_use": None, "last_load_s": None, "prewarm": "pendente", "active": 0}
_LIFE_LOCK = threading.Lock()
_VRAM_CACHE = {"at": 0.0, "free": None}

def uses_gpu():
    return (os.environ.get("LAYA_DEVICE") or "cuda").lower().startswith("cuda")

def vram_free_mb(max_age=15.0):
    """Free VRAM via nvidia-smi, so the check never imports torch or creates a CUDA context."""
    if not uses_gpu():
        return None
    now = time.monotonic()
    if now - _VRAM_CACHE["at"] < max_age:
        return _VRAM_CACHE["free"]
    free = None
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
        total, used = (int(x) for x in out.strip().splitlines()[0].split(","))
        free = total - used
    except Exception:
        free = None
    _VRAM_CACHE.update(at=now, free=free)
    return free

def loaded_models():
    router = laya_server._ROUTER
    try:
        return list(router.loaded) if router is not None else []
    except Exception:
        return []

def touch():
    _LIFE["last_use"] = time.time()

def warm_model(reason, force=False):
    """Load WARM_MODEL in the background. Returns a short status for the caller."""
    with _LIFE_LOCK:
        if _LIFE["warming"]:
            return "já aquecendo"
        if WARM_MODEL in loaded_models():
            touch()
            return "já quente"
        free = vram_free_mb(max_age=0)
        if not force and free is not None and free < WARM_MIN_FREE_MB:
            _LIFE["note"] = f"VRAM livre baixa ({free} MB): modelo carrega só quando houver chamada"
            return _LIFE["note"]
        _LIFE["warming"] = True
        _LIFE["note"] = f"aquecendo ({reason})"

    def work():
        # Importing torch holds the GIL in long stretches; let the handshake reply go out first.
        time.sleep(0 if force else 1.5)
        started = time.perf_counter()
        try:
            laya_server._ensure_router().load(WARM_MODEL)
            _LIFE["last_load_s"] = round(time.perf_counter() - started, 2)
            _LIFE["note"] = f"aquecido por {reason}"
        except Exception as exc:
            _LIFE["note"] = f"falha ao aquecer: {type(exc).__name__}"
            print(f"[laya-mcp] warm failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            touch()
            _LIFE["warming"] = False

    threading.Thread(target=work, name="laya-warm", daemon=True).start()
    return "aquecendo"

def unload_models(reason):
    with _LIFE_LOCK:
        if _LIFE["warming"] or _LIFE["active"]:
            return "em uso; não descarregado"
        router = laya_server._ROUTER
        names = loaded_models()
        if router is None or not names:
            return "já frio"
        router.unload()
        _VRAM_CACHE["at"] = 0.0
        _LIFE["note"] = f"descarregado: {reason}"
        return f"descarregado ({', '.join(names)})"

def lifecycle_loop():
    while True:
        time.sleep(20)
        try:
            if not loaded_models() or _LIFE["warming"] or _LIFE["active"]:
                continue
            if _LIFE["last_use"] is None:
                touch()
            idle = time.time() - _LIFE["last_use"]
            if idle >= IDLE_UNLOAD_S:
                unload_models(f"ocioso há {int(idle // 60)} min")
            elif idle >= 60:
                free = vram_free_mb()
                if free is not None and free < PRESSURE_FREE_MB:
                    unload_models(f"VRAM sob pressão ({free} MB livres)")
        except Exception as exc:
            print(f"[laya-mcp] lifecycle check failed: {type(exc).__name__}", file=sys.stderr)

def model_state():
    names = loaded_models()
    status = "warming" if _LIFE["warming"] else "warm" if names else "cold"
    last = _LIFE["last_use"]
    remaining = max(0, int(IDLE_UNLOAD_S - (time.time() - last))) if names and last else None
    return {"status": status, "loaded": names, "warm_model": WARM_MODEL, "gpu": uses_gpu(),
            "idle_unload_s": IDLE_UNLOAD_S, "unload_in_s": remaining,
            "last_use": datetime.fromtimestamp(last).astimezone().isoformat() if last else None,
            "last_load_s": _LIFE["last_load_s"], "note": _LIFE["note"], "prewarm": _LIFE["prewarm"],
            "vram_free_mb": vram_free_mb(max_age=60), "warm_min_free_mb": WARM_MIN_FREE_MB}

def disk_prewarm():
    """Read weights and torch DLLs once into the OS file cache at background I/O priority.

    Costs no VRAM and no committed RAM (standby cache is reclaimable); it only removes the
    cold-disk part of the first load after boot.
    """
    time.sleep(max(0, int(os.environ.get("LAYA_DISK_PREWARM_DELAY_S", "45"))))
    started, total = time.perf_counter(), 0
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.GetCurrentThread.restype = ctypes.c_void_p
            ctypes.windll.kernel32.SetThreadPriority(ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentThread()), 0x00010000)
        from pathlib import Path
        import importlib.util
        files = []
        repo = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub" / "models--convaiinnovations--laya"
        ref = repo / "refs" / "main"
        if ref.is_file():
            snapshot = repo / "snapshots" / ref.read_text(encoding="utf-8").strip()
            base = snapshot if WARM_MODEL == "english" else snapshot / WARM_MODEL
            if base.is_dir():
                files += [p for p in base.iterdir() if p.is_file()]
                for sub in ("tokenizer", "encoder"):
                    if (base / sub).is_dir():
                        files += [p for p in (base / sub).rglob("*") if p.is_file()]
        spec = importlib.util.find_spec("torch")  # locates the package without importing it
        if spec and spec.submodule_search_locations:
            files += sorted((Path(list(spec.submodule_search_locations)[0]) / "lib").glob("*.dll"))
        if loaded_models():  # a real call already paid the cold read; nothing left to gain
            _LIFE["prewarm"] = "dispensado: modelo já carregado"
            return
        _LIFE["prewarm"] = "em andamento"
        for path in files:
            with open(path, "rb", buffering=0) as stream:
                while chunk := stream.read(8 << 20):
                    total += len(chunk)
        _LIFE["prewarm"] = f"concluído: {total >> 20} MB em {time.perf_counter() - started:.0f} s"
    except Exception as exc:
        _LIFE["prewarm"] = f"falhou: {type(exc).__name__}"

import laya_dashboard as _dashboard_module
_dashboard_module.MODEL_STATE_PROVIDER = model_state
_dashboard_module.MODEL_ACTIONS.update({"warm": lambda: warm_model("pedido manual", force=True),
                                        "unload": lambda: unload_models("pedido manual")})

# A client listing tools has just connected: that is the cheapest "an agent is here" signal.
_original_list_tools = laya_server.server.list_tools
async def _list_tools_and_warm(*args, **kwargs):
    if WARM_ON_CONNECT:
        try:
            warm_model("conexão de cliente MCP")
        except Exception as exc:
            print(f"[laya-mcp] warm on connect failed: {type(exc).__name__}", file=sys.stderr)
    return await _original_list_tools(*args, **kwargs)
laya_server.server.list_tools = _list_tools_and_warm

original_wrap = laya_server._wrap
def audited_wrap(fn, **kwargs):
    """Track in-flight calls for the lifecycle manager, then run the audited call."""
    is_status = getattr(fn, "__name__", "") == "laya_status"
    if is_status:
        if WARM_ON_CONNECT:
            warm_model("laya_status")
        return _audited_call(fn, **kwargs)
    with _LIFE_LOCK:
        _LIFE["active"] += 1
    touch()
    try:
        return _audited_call(fn, **kwargs)
    finally:
        _LIFE["active"] -= 1
        touch()

def _audited_call(fn, **kwargs):
    tool = getattr(fn, "__name__", "laya_tool")
    state = kwargs.get("state")
    notes = []
    if "questions" in kwargs:
        kwargs["questions"], notes = normalize_questions(kwargs["questions"])
    public_args = {key: value for key, value in kwargs.items() if key not in ("router", "agent", "preset_builder")}
    started = time.perf_counter()
    try:
        raw = original_wrap(fn, **kwargs)
        result = json.loads(raw)
        routing = result.get("routing") or {}
        model = routing.get("model") or result.get("model")
        answers = result.get("answers") or {}
        confidence_values = [a.get("answer_confidence", a.get("confidence")) for a in answers.values() if isinstance(a, dict) and isinstance(a.get("answer_confidence", a.get("confidence")), (int, float))]
        latency = result.get("latency_ms")
        is_predict = tool in ("laya_predict", "laya_preset")
        tokens = None
        try:
            router = laya_server._ROUTER
            agent = router._agents.get(model) if router and model else None
            questions = kwargs.get("questions")
            if questions is None and tool == "laya_preset":
                from laya.mcp.tools import PRESETS
                questions = laya_server._preset_builder(PRESETS[kwargs["preset"]])
            if agent is not None and state is not None and isinstance(questions, dict):
                from laya.mcp.tools import validate_questions
                questions = validate_questions(questions)
                ids = list(questions)
                internal = {qid: agent._to_internal(questions[qid]) for qid in ids}
                encoded = agent._encode_state(state, ids, internal)
                tokens = sum(len(item["ids"]) for item in encoded)
        except Exception:
            pass
        try:
            questions = kwargs.get("questions")
            if questions is None and tool == "laya_preset":
                try:
                    from laya.mcp.tools import PRESETS
                    questions = laya_server._preset_builder(PRESETS[kwargs["preset"]])
                except Exception:
                    questions = None
            event = {"tool": tool, "request": safe_request(state) if state is not None else "status", "caller": audit_caller(), "question": audit_question(state, questions), "routed": model, "passed": "error" if "error" in result else "yes" if is_predict else "route only" if tool == "laya_route" else "status", "calls": 1 if is_predict else 0, "latency": latency, "confidence": (sum(confidence_values) / len(confidence_values)) if confidence_values else None, "confidence_note": "answer_confidence; calibration depends on checkpoint/option bucket" if confidence_values else None, "laya_tokens": tokens, "payload_in": estimate_tokens(public_args), "payload_out": estimate_tokens(raw), "llm_token_note": LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + ".", "reason": routing.get("reason") or result.get("reason") or result.get("message"), "flow": f"MCP → {tool} → {model or 'no model'} → {'forward pass' if is_predict else 'route check' if tool == 'laya_route' else 'status'}" + (" · criteria normalizado" if notes else "")}
            persist(event)
        except Exception as telemetry_exc:
            print(f"Laya telemetry persistence failed: {type(telemetry_exc).__name__}", file=sys.stderr)
        return raw
    except Exception as exc:
        is_predict = tool in ("laya_predict", "laya_preset")
        code, message = error_details(exc)
        try:
            persist({"tool": tool, "request": safe_request(state) if state is not None else "", "caller": audit_caller(), "question": audit_question(state, kwargs.get("questions")), "passed": "error", "calls": 1 if is_predict else 0, "latency": (time.perf_counter() - started) * 1000, "reason": str(exc)[:500], "flow": f"MCP → {tool} → {'payload rejeitado' if code.startswith('invalid_') else 'error'}", "error": type(exc).__name__, "error_code": code, "payload_in": estimate_tokens(public_args), "llm_token_note": LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + "."})
        except Exception as telemetry_exc:
            print(f"Laya error telemetry persistence failed: {type(telemetry_exc).__name__}", file=sys.stderr)
        if code == "invalid_questions":
            # Give the caller a valid shape so the retry succeeds without guessing.
            raise laya_server.McpToolError(_dump({
                "error": code, "message": message,
                "hint": "questions is {name: spec}. choice.criteria is an OBJECT {label: description}; "
                        "score.criteria is a LIST of rubric levels; noul takes no criteria. "
                        "type and non-empty instructions are required.",
                "valid_examples": QUESTION_SHAPE_HINT,
            })) from exc
        raise

laya_server._wrap = audited_wrap

# The packaged tool text says "criteria?: object|array", which is where most rejected
# payloads come from. Publish the exact shape per question type instead.
_QUESTIONS_DOC = (
    " questions is an object {name: spec}; every spec needs type and non-empty instructions. "
    "choice: criteria is an OBJECT {label: description}, e.g. {'approve': 'safe to run', 'reject': 'needs review'}. "
    "score: criteria is a LIST of rubric levels from lowest to highest, e.g. ['low', 'medium', 'high']. "
    "noul: omit criteria."
)
_QUESTIONS_SCHEMA = {
    "type": "object", "title": "Questions", "minProperties": 1,
    "additionalProperties": {
        "type": "object", "required": ["type", "instructions"],
        "properties": {
            "type": {"type": "string", "enum": ["choice", "score", "noul"]},
            "instructions": {"type": "string", "minLength": 1},
            "criteria": {"description": "choice: object {label: description}. score: array of rubric level strings. noul: omit."},
        },
    },
}
for _name in ("laya_predict", "laya_route"):
    try:
        _tool = laya_server.server._tool_manager.get_tool(_name)
        _tool.description = (_tool.description or "").replace(", criteria?: object|array", "") + _QUESTIONS_DOC
        _tool.parameters = {**_tool.parameters, "properties": {**_tool.parameters["properties"], "questions": _QUESTIONS_SCHEMA}}
    except Exception as schema_exc:  # packaged server changed shape; keep serving with the stock schema
        print(f"[laya-mcp] tool schema hint not applied to {_name}: {type(schema_exc).__name__}", file=sys.stderr)

@laya_server.server.tool(name="laya_log_not_routed", description="Record a considered structured-decision request that was intentionally not sent to a Laya prediction, with reason. Use only when Laya was a plausible candidate but was skipped; do not log ordinary unrelated requests.")
def laya_log_not_routed(request_summary: str, reason: str) -> str:
    if not request_summary.strip() or not reason.strip():
        raise ValueError("request_summary and reason must be non-empty")
    args = {"request_summary": request_summary, "reason": reason}
    response = _dump({"recorded": True, "reason": reason})
    persist({
        "tool": "laya_log_not_routed",
        "request": safe_request(request_summary),
        "caller": audit_caller(),
        "question": safe_request(request_summary),
        "routed": None,
        "passed": "no",
        "calls": 0,
        "reason": reason[:500],
        "flow": "request considered -> not passed to Laya -> reason recorded",
        "payload_in": estimate_tokens(args),
        "payload_out": estimate_tokens(response),
        "llm_token_note": LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + "."
    })
    return response


@laya_server.server.tool(
    name="laya_select_dependency",
    description=(
        "Recommend one capability from a closed catalog of 2-5 available skills, plugins, or MCP tools. "
        "Input: state={task_summary, task_class?}; candidates=[{id, capability, effects?, cost_class?}]. "
        "Returns a typed recommendation in shadow mode; does not execute or authorize the option. "
        "Send concise sanitized task summary and trusted catalog metadata only; never send secrets or raw logs. "
        "Host must validate candidate availability, policy, permissions, arguments, user authorization, and effects."
    ),
)
def laya_select_dependency_tool(state: dict, candidates: list[dict]) -> str:
    started = time.perf_counter()
    ids: list[str] = []
    touch()
    try:
        ids, questions = build_question(candidates)
        if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > 5000:
            raise ValueError("state exceeds 5000-byte limit; send a shorter sanitized summary")
        router = laya_server._router_or_error()
        raw_result = laya_server.laya_predict(state=state, questions=questions, model="auto", router=router)
        result = select_dependency(state, candidates, laya_server.laya_predict, router, prediction_result=raw_result)
        model = (raw_result.get("routing") or {}).get("model")
        request_meta = json.dumps({"candidate_ids": ids}, ensure_ascii=False, separators=(",", ":"))
        response = _dump(result)
        persist({
            "tool": "laya_select_dependency",
            "request": request_meta,
            "caller": audit_caller(),
            "question": audit_question(state, questions),
            "routed": model,
            "passed": "yes" if result.get("recommendation") in ids else "error",
            "calls": 1,
            "latency": raw_result.get("latency_ms"),
            "confidence": result.get("confidence"),
            "confidence_note": "answer_confidence; checkpoint calibration not established",
            "laya_tokens": count_laya_input_tokens(state, questions, raw_result, router),
            "reason": (raw_result.get("routing") or {}).get("reason"),
            "flow": "Codex host → Laya dependency shortlist → shadow recommendation; no dispatch",
            "payload_in": estimate_tokens({"state": state, "candidates": candidates}),
            "payload_out": estimate_tokens(response),
            "llm_token_note": LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + ".",
        })
        return response
    except Exception as exc:
        error_code = getattr(exc, "code", "invalid_request" if isinstance(exc, ValueError) else "internal_error")
        error_message = getattr(exc, "message", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        try:
            persist({
                "tool": "laya_select_dependency",
                "request": json.dumps({"candidate_ids": ids}, ensure_ascii=False),
                "caller": audit_caller(),
                "question": audit_question(state, questions if "questions" in locals() else None),
                "passed": "error",
                "calls": 1,
                "latency": (time.perf_counter() - started) * 1000,
                "reason": str(error_message)[:300],
                "flow": "Codex host → Laya dependency shortlist → error",
                "error": str(error_code),
                "payload_in": estimate_tokens({"candidate_ids": ids}),
                "llm_token_note": LLM_TOKEN_NOTE + " Tokenizador: " + TOKEN_ESTIMATOR + ".",
            })
        except Exception as telemetry_exc:
            print(f"Laya selector telemetry persistence failed: {type(telemetry_exc).__name__}", file=sys.stderr)
        raise laya_server.McpToolError(_dump({"error": error_code, "message": error_message})) from exc

def main():
    transport = os.environ.get("LAYA_TRANSPORT", "stdio")
    if transport == "streamable-http":
        if os.name == "nt":
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        if laya_server._env_bool("LAYA_PRELOAD", True):
            try:
                laya_server._ensure_router()
            except Exception as exc:
                print(f"[laya-mcp] preload failed (will retry on demand): {exc}", file=sys.stderr)
        threading.Thread(target=lifecycle_loop, name="laya-lifecycle", daemon=True).start()
        if os.environ.get("LAYA_DISK_PREWARM", "1") != "0":
            threading.Thread(target=disk_prewarm, name="laya-disk-prewarm", daemon=True).start()
        host = os.environ.get("LAYA_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("LAYA_MCP_PORT", "8767"))
        laya_server.server.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path="/mcp",
            json_response=True,
        )
    else:
        laya_server.main()

if __name__ == "__main__":
    main()
