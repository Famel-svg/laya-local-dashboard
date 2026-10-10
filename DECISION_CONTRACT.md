# Contrato de decisão de capacidade

Versão 1.1: seletor MCP Laya em modo sombra e telemetria local do dispatcher observado.

## Entrada permitida

```json
{
  "state": {"task_class": "structured_choice", "task_summary": "review repository code changes"},
  "candidates": [
    {"id": "skill:code-review", "capability": "review code changes", "cost_class": "low", "effects": ["read_repository"]},
    {"id": "mcp:github", "capability": "inspect pull request metadata", "cost_class": "medium", "effects": ["network_read"]}
  ]
}
```

`laya_select_dependency` aceita 2–5 opções, IDs únicos, `capability` obrigatório, `effects` como lista e `cost_class` curto. Host envia somente resumo sanitizado e metadados mínimos; nunca envia secrets, tokens, corpos completos de logs, histórico ou payload de ferramenta. Limite de estado no servidor: 5.000 bytes.

## Saída observada

```json
{
  "mode": "shadow",
  "recommendation": "skill:code-review",
  "candidate_ids": ["skill:code-review", "mcp:github"],
  "probabilities": {"skill:code-review": 0.8, "mcp:github": 0.2},
  "confidence": 0.8,
  "dispatch": "not_performed",
  "guardrail": "Host validates availability, policy, permissions, user authorization, and side effects."
}
```

Servidor faz uma predição Laya por chamada e rejeita recomendação fora do catálogo. Confidence não é calibrada; não usar como autorização ou threshold de enforcement.

## Telemetria Codex

Hook `PostToolUse` no seletor registra sessão/turno, recomendação, IDs dos candidatos e confidence em `%LOCALAPPDATA%\\Codex\\Laya\\dispatcher_shadow.sqlite3`. Hook `PreToolUse` observa próxima chamada local `mcp__` no mesmo turno e marca correspondência. Sem prompt, argumentos, resultado bruto, bloqueio ou rewrite. Hook não descobre nem despacha skills/plugins e não observa tools hospedadas fora do processo local.

Configuração está em `~/.codex/config.toml`. O processo MCP persistente e catálogo Codex exigem recarga antes da ferramenta nova ficar disponível na sessão nativa. Patches do runtime: `runtime/laya_mcp_dashboard.py.patch` e `runtime/laya_mcp_client.py.patch`; cópias originais guardadas no archive do Brain datado 06/10/2026.

## Política e saída de sombra

- Sombra registra recomendação sem alterar seleção atual.
- Resposta ausente, malformada, timeout ou opção indisponível não produz execução.
- Não ampliar escopo, permissões, efeitos ou acesso por sugestão Laya.
- Ação explícita do usuário e políticas do host têm precedência.
- Nunca delegar autorização, envio/publicação, compra, exclusão ou mudança de conta.
- Sair de sombra exige amostra rotulada, resultado melhor que baseline, erro/fallback aceitável, calibração e rollback demonstrado.

## Medição de tokens reais

`tools/codex_usage.py` lê `token_usage_record.payload.usage` do rollout Codex e deduplica por `(thread_id,response_id)`. Mede input, cached input, cache-write input, output, reasoning output e total; cached input é subconjunto de input. Ignora snapshots cumulativos `token_count`. Agrega subagentes pelos próprios registros por resposta.

Para economia observada, alimentar CSV `pair_id,group,thread_id` (`group=control|treatment`) em `tools/token_experiment.example.csv` e executar `python tools/codex_usage.py --manifest caminho.csv`. Parear tarefa, modelo, contexto inicial e critério de conclusão. Relatar tokens poupados como control menos treatment e percentual sobre control. Diferença real observada; causalidade depende de pares equivalentes e amostra suficiente. Medição isolada de uma sessão não é economia.
