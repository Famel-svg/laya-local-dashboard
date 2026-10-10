"""Benchmark one Laya loading strategy in a fresh process. Usage: laya_bench.py <cuda|cpu> [threads]

Runs beside the live MCP service without touching it. Prints one JSON line.
"""
import ctypes, json, os, statistics, subprocess, sys, time
from ctypes import wintypes

T0 = time.perf_counter()
device = sys.argv[1]
threads = int(sys.argv[2]) if len(sys.argv) > 2 else 0
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def ram_mb():
    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return round(pmc.WorkingSetSize / 2**20), round(pmc.PrivateUsage / 2**20)


def gpu_used_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


def torch_vram():
    import torch
    if device != "cuda":
        return None
    torch.cuda.synchronize()
    return round(torch.cuda.memory_allocated() / 2**20), round(torch.cuda.memory_reserved() / 2**20)


out = {"device": device, "threads": threads or "default"}
gpu0 = gpu_used_mb()
t = time.perf_counter()
import torch
from laya import Router
if threads:
    torch.set_num_threads(threads)
out["import_s"] = round(time.perf_counter() - t, 2)
out["ram_after_import_mb"] = ram_mb()

QUESTIONS = {"next": {"type": "choice", "instructions": "Choose the best next step for the task.",
                      "criteria": {"inspect": "read files and state first", "edit": "apply the change now",
                                   "ask": "ask the user for a missing decision", "test": "run the tests"}},
             "risk": {"type": "score", "instructions": "Rate the risk of the action.", "criteria": ["low", "medium", "high"]},
             "safe": {"type": "noul", "instructions": "Is the action reversible?"}}
STATES = {"english": {"task": "Refactor the dashboard polling loop to avoid blocking the UI thread; tests exist but are slow.",
                      "context": "Python desktop app, single maintainer, change touches two files."},
          "multilingual": {"tarefa": "Refatorar o laço de atualização do painel para não bloquear a interface; há testes, mas são lentos.",
                           "contexto": "Aplicativo desktop em Python, um mantenedor, a mudança toca dois arquivos."}}

router = Router(device=device, max_loaded=2)


def bench_model(name, n=25):
    r = {}
    cpu0 = time.process_time(); t = time.perf_counter()
    router.load(name)
    r["load_s"] = round(time.perf_counter() - t, 2)
    r["load_cpu_s"] = round(time.process_time() - cpu0, 2)
    t = time.perf_counter()
    res = router.predict(STATES[name], QUESTIONS, model=name)
    r["first_predict_ms"] = round((time.perf_counter() - t) * 1000)
    lat, cpu0 = [], time.process_time()
    for _ in range(n):
        t = time.perf_counter()
        router.predict(STATES[name], QUESTIONS, model=name)
        lat.append((time.perf_counter() - t) * 1000)
    r["cpu_ms_per_call"] = round((time.process_time() - cpu0) * 1000 / n)
    lat.sort()
    r["p50_ms"], r["p95_ms"], r["max_ms"] = round(statistics.median(lat)), round(lat[int(len(lat) * .95) - 1]), round(lat[-1])
    r["answer"] = {k: (v.get("answer") if isinstance(v, dict) else v) for k, v in (res.get("answers") or {}).items()}
    r["torch_vram_mb"] = torch_vram()
    r["ram_mb"] = ram_mb()
    r["gpu_total_delta_mb"] = (gpu_used_mb() - gpu0) if gpu0 is not None else None
    return r


out["english"] = bench_model("english")
out["ready_first_answer_s"] = round(time.perf_counter() - T0, 2)  # cold process -> first answer (approx.)
out["multilingual"] = bench_model("multilingual")

# idle unload, then re-warm in the same (already initialised) process
t = time.perf_counter(); router.unload(); out["unload_s"] = round(time.perf_counter() - t, 2)
time.sleep(1.0)
out["after_unload"] = {"torch_vram_mb": torch_vram(), "ram_mb": ram_mb(),
                       "gpu_total_delta_mb": (gpu_used_mb() - gpu0) if gpu0 is not None else None}
t = time.perf_counter(); router.load("english"); out["rewarm_load_s"] = round(time.perf_counter() - t, 2)
t = time.perf_counter(); router.predict(STATES["english"], QUESTIONS, model="english")
out["rewarm_first_predict_ms"] = round((time.perf_counter() - t) * 1000)
print("RESULT " + json.dumps(out, ensure_ascii=False))
