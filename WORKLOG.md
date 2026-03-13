# WORKLOG

## Estado Atual
- Webhook usa somente V2 (`runtime.orchestrator` com planner + executor + registry de skills).
- Skills centrais ativas:
  - `direct_answer`
  - `web_search`
  - `summarize_url`
  - `n8n_schedule_alert`
  - `garmin_tracking`

## Mudanças 2026-03-12

### app.py — Redis dedupe movido para app layer
- `_mark_message_processed()` e `_redis` agora vivem em `app.py`, removendo dependência de `engine`.
- Conexão Redis inicializada no startup com fallback gracioso (`None` se indisponível).
- Chave de dedupe: `idempotency_key` ou `task:{task_id}`, TTL via `WEBHOOK_DEDUPE_TTL_SECONDS` (padrão 300s).

### runtime/orchestrator.py — SkillRegistry lazy singleton
- `SkillRegistry` não é mais instanciado a cada webhook.
- Singleton via `_get_registry()` / `_reset_registry()` a nível de módulo.
- `/new` agora chama `_reset_registry()` + `invalidate_mcp_tools_cache()`.

### runtime/executor.py — n8n bypass do LLM post-processor
- Se qualquer step do plano for `n8n_schedule_alert`, o interpretador LLM final é ignorado.
- Evita reescrita das mensagens de confirmação estruturadas do n8n.

### runtime/planner.py — regra explícita para args.run_at
- Prompt do planner agora instrui o LLM a usar `args.run_at` com datetime ISO 8601 extraído do pedido.

### skills/n8n_schedule_alert.py — múltiplos fixes
- `_ISO_8601_NO_TZ_RE` adicionado: parseia datetimes sem TZ (ex: `2026-03-12 19:28`), localiza via `SCHEDULE_TIMEZONE`.
- `_DMY_TIME_RE` agora exige componente de hora (não casa mais datas sem horário).
- `_format_success_text`: quando n8n retorna sem `idTask`, emite aviso explícito em vez de falso sucesso.
- `_clarification_text`: exemplo atualizado para formato mais natural (`2026-03-10 16:30`).
- Path `create` agora lê datetime dos args do planner: `run_at` → `datetime` → `time` → scan genérico de valores ISO → fallback user_text.

### tests/test_n8n_schedule_alert.py
- Novo test: `test_create_without_idtask_reports_unconfirmed`.

## Últimas Mudanças Relevantes
- Fluxo V2 consolidado como único caminho.
- Logs de execução adicionados nos pontos principais:
  - planner (origem e skills escolhidas)
  - executor (validação, start/end por step, latência)
  - orchestrator (fallbacks e envio final)
- `summarize_url` evoluída para fluxo interno em 2 etapas:
  1. `SummarizeURLTool`
  2. síntese final por LLM com prompt dirigido
- Fallback da `summarize_url`:
  - se síntese falhar/vier vazia -> usa texto bruto (`title + summary/local_summary`)
- Resumo da síntese ajustado para ficar um pouco mais detalhado (6-8 linhas).

## Evidências de Funcionamento (Logs)
- `mode=v2` com URL explícita:
  - `selected_skills=['summarize_url']`
  - `[skill=summarize_url] phase=tool_call ok=true`
  - `[skill=summarize_url] phase=llm_synthesis ok=true`
- Em teste sem URL explícita:
  - `selected_skills=['direct_answer']`
- Quando não há `web_search`, não aparece:
  - `step_start skill=web_search`

## Decisões Arquiteturais Ativas
- V2 sem YAML/dynamic skill loader nesta fase (registry estático).
- Fallback duplo no V2:
  - planner/execution inválido -> `direct_answer`
  - falha do `direct_answer` -> resposta mínima controlada.

## Comandos Operacionais Usados
- Build/recreate:
  - `docker compose build travis`
  - `docker compose up -d --force-recreate travis`
- Status:
  - `docker compose ps`
- Validação sintática:
  - `python3 -m py_compile app.py runtime/*.py skills/*.py`

## Próximos Passos 
- Estruturar persistência de longo prazo (episódica/semântica/vetorial).
