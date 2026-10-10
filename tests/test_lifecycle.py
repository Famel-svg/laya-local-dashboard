"""Isolated lifecycle test: real GPU load, temp LOCALAPPDATA, live service untouched."""
import os, sys, tempfile, time, json, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="laya_life_")
os.environ["LAYA_PRELOAD"] = "0"; os.environ["LAYA_DEVICE"] = "cuda"; os.environ["HF_HUB_OFFLINE"] = "1"
src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [src + r"\runtime", src + r"\tools"]
import threading, asyncio
import laya_mcp_dashboard as m

def wait(pred, timeout, label):
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        if pred(): 
            print(f"  {label}: {time.perf_counter()-t:.1f} s"); return
        time.sleep(0.2)
    raise SystemExit(f"TIMEOUT {label}: {m.model_state()}")

free0 = m.vram_free_mb(max_age=0)
print("estado inicial:", m.model_state()["status"], "| VRAM livre", free0, "MB | torch importado:", "torch" in sys.modules)
assert m.model_state()["status"] == "cold" and "torch" not in sys.modules

# 1) tools/list (client connect) triggers background warm
tools = asyncio.run(m.laya_server.server.list_tools())
assert len(tools) >= 5
print("apos tools/list:", m.model_state()["status"])
wait(lambda: m.model_state()["status"] == "warm", 90, "aquecido apos conexao")
free1 = m.vram_free_mb(max_age=0); print("  VRAM usada pelo modelo:", free0 - free1, "MB | load_s:", m.model_state()["last_load_s"])
assert m.warm_model("x") == "já quente"

# 2) idle unload via the lifecycle loop
m.IDLE_UNLOAD_S = 3
threading.Thread(target=m.lifecycle_loop, daemon=True).start()
wait(lambda: m.model_state()["status"] == "cold", 60, "descarregado por ociosidade")
free2 = m.vram_free_mb(max_age=0); print("  VRAM residual:", free0 - free2, "MB | nota:", m.model_state()["note"])
m.IDLE_UNLOAD_S = 600

# 3) on-demand call while cold: loads and answers (re-warm cost)
def predict(state, questions, router=None, **_):
    from laya.mcp.tools import laya_predict
    return laya_predict(state, questions, "auto", router=m.laya_server._ensure_router())
predict.__name__ = "laya_predict"
t = time.perf_counter()
raw = m.audited_wrap(predict, state={"task": "Pick the next step for a small refactor."}, questions={"n": {"type": "choice", "instructions": "Choose.", "criteria": ["inspect", "edit"]}})
print(f"chamada com modelo frio: {(time.perf_counter()-t)*1000:.0f} ms ->", json.loads(raw)["answers"]["n"]["choice"], "| estado:", m.model_state()["status"], "| descarrega em", m.model_state()["unload_in_s"], "s")

# 4) VRAM guard: pretend a game left little free memory
m.unload_models("teste"); real = m.vram_free_mb
m.vram_free_mb = lambda max_age=15.0: 2500
print("trava de VRAM:", m.warm_model("teste")); assert m.model_state()["status"] == "cold"
# 5) pressure unload
m.vram_free_mb = real; m.warm_model("teste", force=True); wait(lambda: m.model_state()["status"] == "warm", 60, "reaquecido")
m.vram_free_mb = lambda max_age=15.0: 900; m._LIFE["last_use"] = time.time() - 120
wait(lambda: m.model_state()["status"] == "cold", 60, "descarregado por pressao de VRAM")
print("  nota:", m.model_state()["note"])
# 6) in-use guard
m.vram_free_mb = real; m._LIFE["active"] = 1; m.warm_model("t", force=True); wait(lambda: m.model_state()["status"] == "warm", 60, "reaquecido")
assert m.unload_models("x") == "em uso; não descarregado"; m._LIFE["active"] = 0
print("ALL OK")
