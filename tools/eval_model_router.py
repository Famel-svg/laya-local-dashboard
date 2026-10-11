"""Would Laya pick the right model tier for a delegated task, before any large model runs?

Labels follow a fixed delegation table (cheap / mid / strong tier by task kind). Two approaches
are scored on the same tasks, in Portuguese and in English:

  preset  - the packaged `router` preset; its `difficulty` score is mapped to a tier
  choice  - one `choice` question whose options are the three tiers, described by the table

    python tools/eval_model_router.py            # needs the `laya` package and its checkpoints
"""
from __future__ import annotations

import json
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

TIERS = ("haiku", "sonnet", "opus")
TASKS = [  # (expected tier, Portuguese, English)
    ("haiku", "Escreva uma legenda curta para um post sobre o lançamento do painel.", "Write a short caption for a post about the dashboard launch."),
    ("haiku", "Resuma este log de erro em três linhas.", "Summarize this error log in three lines."),
    ("haiku", "Traduza o README do português para o inglês.", "Translate the README from Portuguese to English."),
    ("haiku", "Encontre em quais arquivos a função ensure_services é chamada.", "Find which files call the function ensure_services."),
    ("haiku", "Renomeie a variável tmp para resultado em todo o arquivo.", "Rename the variable tmp to result across the file."),
    ("haiku", "Extraia as datas e os valores desta lista de recibos.", "Extract the dates and amounts from this list of receipts."),
    ("haiku", "Escreva a mensagem de commit para esta correção de digitação.", "Write the commit message for this typo fix."),
    ("haiku", "Formate esta tabela em Markdown.", "Format this table as Markdown."),
    ("haiku", "Classifique estes dez e-mails como urgentes ou não urgentes.", "Classify these ten emails as urgent or not urgent."),
    ("haiku", "Reescreva este parágrafo em tom mais formal.", "Rewrite this paragraph in a more formal tone."),
    ("sonnet", "Implemente um filtro por status na tabela de chamadas do painel.", "Implement a status filter on the dashboard calls table."),
    ("sonnet", "Escreva testes unitários para a função normalize_questions.", "Write unit tests for the normalize_questions function."),
    ("sonnet", "Revise este diff de 80 linhas e aponte bugs.", "Review this 80-line diff and point out bugs."),
    ("sonnet", "Refatore o módulo de coleta para separar leitura e agregação, sem mudar o comportamento.", "Refactor the collector module to separate reading from aggregation without changing behavior."),
    ("sonnet", "Pesquise três bibliotecas de gráficos para Qt e sintetize prós e contras.", "Research three Qt charting libraries and synthesize pros and cons."),
    ("sonnet", "Analise este CSV de uso de tokens e descreva as tendências por dia.", "Analyze this token usage CSV and describe the daily trends."),
    ("sonnet", "Corrija o bug em que o botão Copiar não inclui a mensagem de erro.", "Fix the bug where the Copy button omits the error message."),
    ("sonnet", "Construa um protótipo do painel em PySide6 com os dados da API local.", "Build a dashboard prototype in PySide6 using the local API data."),
    ("sonnet", "Escreva meu currículo para uma vaga de engenheiro de dados.", "Write my resume for a data engineer position."),
    ("sonnet", "Adicione um endpoint POST para aquecer o modelo, com validação de cabeçalho.", "Add a POST endpoint to warm the model, with header validation."),
    ("opus", "Projete a arquitetura para rotear decisões de agentes antes do modelo grande, com fallback e medição.", "Design the architecture to route agent decisions before the large model, with fallback and measurement."),
    ("opus", "O serviço cai sem deixar log após o login; descubra a causa raiz.", "The service dies without a log after login; find the root cause."),
    ("opus", "Faça uma revisão de segurança do servidor MCP local e do endpoint de controle.", "Do a security review of the local MCP server and the control endpoint."),
    ("opus", "Planeje a migração do painel de Tkinter para outro toolkit, cobrindo bandeja, empacotamento e riscos.", "Plan the dashboard migration from Tkinter to another toolkit, covering tray, packaging and risks."),
    ("opus", "Dois testes falham de forma intermitente só na CI; investigue a causa.", "Two tests fail intermittently only in CI; investigate the cause."),
    ("opus", "Escolha entre três estratégias de carregamento do modelo, pesando VRAM, latência e risco.", "Choose between three model loading strategies, weighing VRAM, latency and risk."),
    ("opus", "Defina o modelo de dados e o contrato de API para auditoria de decisões entre vários agentes.", "Define the data model and API contract for auditing decisions across several agents."),
    ("opus", "Há uma condição de corrida entre a thread de coleta e a interface; encontre e proponha a correção.", "There is a race condition between the collector thread and the UI; find it and propose the fix."),
]
TIER_QUESTION = {"tier": {
    "type": "choice",
    "instructions": "Choose the cheapest model tier able to do this task well.",
    "criteria": {
        "haiku": "text generation, summary, translation, rewriting, formatting, extraction, classification, "
                 "lookup, locating files, reading logs, boilerplate, mechanical rename",
        "sonnet": "implement a scoped feature or fix, write tests, review a diff, bounded refactor, "
                  "research synthesis, data analysis, high-stakes text such as a resume",
        "opus": "architecture and design, root cause of an ambiguous bug, security review, "
                "plan for a cross-cutting change, judging between competing solutions",
    }}}
DIFFICULTY_TO_TIER = {0: "haiku", 1: "haiku", 2: "sonnet", 3: "opus"}


def pick(answer: dict):
    """(label or level index, confidence) from a Laya answer, whatever the question type."""
    conf = answer.get("answer_confidence", answer.get("confidence"))
    for key in ("choice", "score", "level", "label"):
        if key in answer:
            return answer[key], conf
    return None, conf


def main() -> int:
    from laya import Router, router_questions

    router = Router(device="cuda")
    preset = router_questions()
    levels = preset["difficulty"]["criteria"]
    print("preset router:", {k: v["type"] for k, v in preset.items()}, "| níveis de dificuldade:", levels)
    results = []
    for lang_index, lang in ((1, "pt"), (2, "en")):
        for task in TASKS:
            expected, text = task[0], task[lang_index]
            row = {"lang": lang, "expected": expected, "text": text}
            for approach, questions, key in (("preset", preset, "difficulty"), ("choice", TIER_QUESTION, "tier")):
                started = time.perf_counter()
                out = router.predict({"request": text}, questions)
                elapsed = (time.perf_counter() - started) * 1000
                value, conf = pick(out["answers"][key])
                if approach == "preset":
                    # `score` answers come back as an expected level on a continuous 0..3 scale
                    index = levels.index(value) if value in levels else max(0, min(3, round(float(value))))
                    tier = DIFFICULTY_TO_TIER.get(index)
                else:
                    tier = value
                row[approach] = {"tier": tier, "raw": value, "conf": conf, "ms": elapsed,
                                 "model": out["routing"]["model"]}
            results.append(row)
    print("exemplo bruto:", json.dumps(results[0], ensure_ascii=False)[:420])

    def report(rows, approach, label):
        got = [r[approach] for r in rows]
        hits = [g["tier"] == r["expected"] for g, r in zip(got, rows)]
        under = sum(TIERS.index(g["tier"]) < TIERS.index(r["expected"]) for g, r in zip(got, rows) if g["tier"] in TIERS)
        over = sum(TIERS.index(g["tier"]) > TIERS.index(r["expected"]) for g, r in zip(got, rows) if g["tier"] in TIERS)
        per = {t: f"{sum(h for h, r in zip(hits, rows) if r['expected'] == t)}/{sum(r['expected'] == t for r in rows)}" for t in TIERS}
        picked = {t: sum(g["tier"] == t for g in got) for t in TIERS}
        confs = [g["conf"] for g in got if isinstance(g["conf"], (int, float))]
        sure = [(h, g) for h, g in zip(hits, got) if isinstance(g["conf"], (int, float)) and g["conf"] >= 0.6]
        print(f"{label:<22} acerto {sum(hits)}/{len(rows)} ({sum(hits) * 100 / len(rows):.0f}%) | por nível {per} | "
              f"escolhas {picked} | abaixo {under} acima {over} | conf média {statistics.mean(confs):.2f} | "
              f"conf>=0,6: {sum(h for h, _ in sure)}/{len(sure)} | {statistics.median(g['ms'] for g in got):.0f} ms")

    for approach in ("preset", "choice"):
        for lang in ("pt", "en"):
            report([r for r in results if r["lang"] == lang], approach, f"{approach} · {lang}")
        report(results, approach, f"{approach} · total")
    # Ceiling for the preset: best two cut points on the raw difficulty score, fitted on these same tasks.
    scored = [(float(r["preset"]["raw"]), r["expected"]) for r in results]
    cuts = sorted({v for v, _ in scored})
    best = max(((sum((TIERS[(v > lo) + (v > hi)] == e) for v, e in scored), lo, hi) for lo in cuts for hi in cuts if hi >= lo))
    print(f"preset · teto ajustado  acerto {best[0]}/{len(scored)} com cortes {best[1]:.2f} e {best[2]:.2f} (otimista: ajustado nos próprios dados)")
    for tier in TIERS:
        vals = [v for v, e in scored if e == tier]
        print(f"  dificuldade para tarefas {tier}: média {statistics.mean(vals):.2f}, min {min(vals):.2f}, max {max(vals):.2f}")
    print("resposta bruta do preset:", json.dumps(router.predict({"request": TASKS[20][2]}, preset)["answers"], ensure_ascii=False)[:700])
    print(f"referências: sempre 'sonnet' {sum(t[0] == 'sonnet' for t in TASKS)}/{len(TASKS)} | acaso ~{len(TASKS) / 3:.0f}/{len(TASKS)}")
    print("checkpoints usados:", sorted({r["choice"]["model"] for r in results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
