"""Build a shareable demo payload from the live dashboard API.

Aggregated numbers (tokens, latency, routes) are kept; every free-text field that
could carry private context (questions, reasons, callers) is replaced by generic
samples. Used for the screenshots in docs/ and for trying the UI without a runtime.

    python tools/make_demo_data.py [saida.json]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

API_URL = "http://127.0.0.1:8766/api"

QUESTIONS = [
    ("Escolher o formato de saída do relatório semanal", ["tabela", "texto corrido", "gráfico"]),
    ("Choose the next step for a small refactor", ["inspect", "edit", "test"]),
    ("Classificar a prioridade do chamado de suporte", ["baixa", "média", "alta"]),
    ("Pick the storage backend for the cache layer", ["sqlite", "arquivo", "memória"]),
    ("Decidir se a tarefa precisa de revisão humana", ["sim", "não"]),
    ("Select the test scope for this change", ["unit", "integration", "full"]),
    ("Escolher a pasta de destino da nota", ["projetos", "referência", "arquivo"]),
    ("Triage the incoming bug report", ["bug", "feature", "question"]),
    ("Definir a estratégia de retry da integração", ["imediato", "backoff", "desistir"]),
    ("Choose a chart type for a skewed series", ["stacked", "small multiples", "log"]),
    ("Avaliar o risco da migração de banco", ["baixo", "médio", "alto"]),
    ("Pick the reviewer for this pull request", ["backend", "frontend", "infra"]),
]
SKIP_REASONS = [
    "Análise aberta de código: Laya pontua escolhas estruturadas, não investiga repositório.",
    "Pedido de geração de texto livre; fora do escopo de decisão tipada.",
    "Usuário já definiu a prioridade explicitamente; não há escolha a fazer.",
    "Tarefa depende de autenticação humana, não de classificação.",
    "Diagnóstico de várias etapas com evidências; exige raciocínio encadeado.",
    "Decisão criativa de redação, sem rubrica finita.",
]


def sample_question(index: int, as_list: bool = False) -> str:
    text, options = QUESTIONS[index % len(QUESTIONS)]
    criteria = options if as_list else {o: f"quando {o} se aplica" for o in options}
    return json.dumps({"context": {"task": text},
                       "questions": {"decisao": {"type": "choice", "instructions": text, "criteria": criteria}}},
                      ensure_ascii=False)


def sanitize(data: dict) -> dict:
    for i, call in enumerate(data.get("call_logs") or []):
        rejected = call.get("passed") == "error"
        call["question"] = sample_question(i, as_list=rejected)
        call["caller"] = "Agente local" if "hist" not in str(call.get("caller", "")).lower() else "Não registrado (histórico)"
        if rejected:
            call["error_code"] = "invalid_questions"
            call["reason"] = json.dumps({"error": "invalid_questions",
                                         "message": "questions[decisao].criteria must be a non-empty object of label -> description"})
        elif call.get("passed") == "no":
            call["reason"] = SKIP_REASONS[i % len(SKIP_REASONS)]
        elif call.get("reason"):
            call["reason"] = "Latin script; language detected as English" if call.get("routed") == "english" \
                else "Latin script but language looks like 'pt', not English"
    for i, row in enumerate(data.get("rows") or []):
        row["request"] = sample_question(i)
        if row.get("passed") == "error":
            row["reason"] = json.dumps({"error": "invalid_questions",
                                        "message": "questions[decisao].criteria must be a non-empty object of label -> description"})
        elif row.get("tool") == "laya_log_not_routed":
            row["reason"] = SKIP_REASONS[i % len(SKIP_REASONS)]
        elif row.get("reason"):
            row["reason"] = "language routing"
    data["skip_reasons"] = [{"reason": reason, "count": max(1, 9 - 2 * i)} for i, reason in enumerate(SKIP_REASONS)]
    return data


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/demo_api.json")
    with urllib.request.urlopen(API_URL, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sanitize(data), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
