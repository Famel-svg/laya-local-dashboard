"""Isolated test of the patched runtime: temp LOCALAPPDATA, no model load, live service untouched."""
import json, os, sys, tempfile, sqlite3

sys.stdout.reconfigure(encoding="utf-8")
tmp = tempfile.mkdtemp(prefix="laya_rt_")
os.environ["LOCALAPPDATA"] = tmp
src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [src + r"\runtime", src + r"\tools"]

import laya_mcp_dashboard as m
from laya.mcp.tools import validate_questions

# 1) normalization turns the historical failure shapes into valid payloads
q = {"a": {"type": "choice", "instructions": "pick", "criteria": ["x", "y"]},
     "b": {"type": "choice", "instructions": "pick", "criteria": [{"label": "p", "description": "when p"}, {"id": "q"}]},
     "c": {"type": "score", "instructions": "rate", "criteria": {"1": "low", "2": "high"}},
     "d": {"type": "noul", "instructions": "is it?"}}
fixed, notes = m.normalize_questions(q)
clean = validate_questions(fixed)
assert clean["a"]["criteria"] == {"x": "x", "y": "y"}, clean["a"]
assert clean["b"]["criteria"] == {"p": "when p", "q": "q"}, clean["b"]
assert clean["c"]["criteria"] == ["low", "high"], clean["c"]
assert len(notes) == 3, notes
print("normalize ok:", notes)

# 2) untouched when already valid / unrepairable
same, n2 = m.normalize_questions({"a": {"type": "choice", "instructions": "p", "criteria": {"x": "d"}}, "e": {"type": "choice", "instructions": "p", "criteria": []}})
assert n2 == [] and same["e"]["criteria"] == []

# 3) wrapped call: list criteria now succeeds and is flagged; bad payload gets hint + examples
def fake_predict(state, questions, **_):
    validate_questions(questions)
    return {"answers": {"a": {"answer": "x", "answer_confidence": 0.8}}, "routing": {"model": "english", "reason": "test"}, "latency_ms": 12.5}
fake_predict.__name__ = "laya_predict"

raw = m.audited_wrap(fake_predict, state={"s": 1}, questions={"a": q["a"]})
assert json.loads(raw)["answers"]["a"]["answer"] == "x"
try:
    m.audited_wrap(fake_predict, state={"s": 1}, questions={"a": {"type": "choice", "instructions": "", "criteria": {"x": "d"}}})
    raise SystemExit("expected rejection")
except m.laya_server.McpToolError as exc:
    body = json.loads(str(exc))
    assert body["error"] == "invalid_questions" and "valid_examples" in body and "hint" in body, body
    print("rejeicao:", body["message"], "| hint ok")

# 4) audit rows carry the new columns
db = sqlite3.connect(os.path.join(tmp, "Codex", "Laya", "call_audit.sqlite3"))
rows = db.execute("select passed, latency, confidence, error_code, reason from calls order by id").fetchall()
print("audit:", [(r[0], round(r[1], 1) if r[1] is not None else None, r[2], r[3]) for r in rows])
assert rows[0][0] == "yes" and rows[0][1] == 12.5 and rows[0][2] == 0.8
assert rows[1][0] == "error" and rows[1][3] == "invalid_questions" and rows[1][1] is not None

# 5) API payload separates rejected payloads from real failures
import laya_dashboard as d
data = d.data()
mt = data["metrics"]
print("metrics:", {k: mt[k] for k in ("requests", "errors", "rejected", "failures", "normalized")})
assert mt["errors"] == 1 and mt["rejected"] == 1 and mt["failures"] == 0 and mt["normalized"] == 1
log = data["call_logs"]
assert log[0]["error_code"] == "invalid_questions" and log[1]["latency"] == 12.5 and log[1]["confidence"] == 0.8, log[:2]

# 6) published tool schema
tool = m.laya_server.server._tool_manager.get_tool("laya_predict")
assert "OBJECT {label: description}" in tool.description and "object|array" not in tool.description
assert tool.parameters["properties"]["questions"]["additionalProperties"]["required"] == ["type", "instructions"]
print("schema ok")
print("ALL OK")
