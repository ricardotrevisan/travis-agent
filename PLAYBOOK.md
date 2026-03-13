# PLAYBOOK

## Objetivo
Guia operacional do projeto para executar, validar, debugar e fazer rollback com segurança.

## Pré-requisitos
- Docker + Docker Compose instalados.
- Chaves/configurações preenchidas em `.env`.
- Nunca commitar `.env` com segredos.

## Setup rápido
1. Criar ambiente local:
   - `cp .env.sample .env`
2. Ajustar no `.env`:
   - `OPENAI_API_KEY`
   - `EVOLUTION_APIKEY`
   - `AUTHENTICATION_API_KEY`
   - `VOICE_API_URL` (ex.: `http://host.docker.internal:8000` quando o backend de voz roda fora do container `travis`)
   - `VOICE_API_TIMEOUT`
   - `VOICE_LANGUAGE_DEFAULT`
   - `N8N_SCHEDULE_WEBHOOK_URL`
   - `N8N_SCHEDULE_TIMEOUT`
   - `TASK_CALLBACK_SECRET` (se usar callback autenticado)
   - `SCHEDULE_TIMEZONE` (default: `America/Sao_Paulo`)
   - credenciais de banco/redis se necessário
3. Subir stack:
   - `docker compose up -d`
4. Conferir status:
   - `docker compose ps`

## Modo de execução do webhook
- Fluxo único (V2): `planner -> executor -> skills`.

## Build / Recreate (rotina padrão)
- Build da aplicação:
  - `docker compose build travis`
- Recreate com imagem nova:
  - `docker compose up -d --force-recreate travis`
- Ver logs:
  - `docker compose logs -f travis`

## Validação funcional
### 1) Smoke test sintático
- `python3 -m py_compile app.py runtime/*.py skills/*.py`

### 2) Confirmar roteamento
- Em logs do `travis`:
  - `mode=v2`

### 3) Confirmar planejamento/executor (V2)
- Logs esperados:
  - `[v2.planner] source=llm ...`
  - `[v2.orchestrator] plan_skills=[...]`
  - `[v2.executor] step_start skill=...`
  - `[v2.executor] step_end skill=... ok=True ...`

### 4) Confirmar `summarize_url` completo
- Logs esperados:
  - `[skill=summarize_url] phase=tool_call ok=true`
  - `[skill=summarize_url] phase=llm_synthesis ok=true`
- Em falha de síntese:
  - `fallback=raw_summary reason=...`

### 5) Confirmar fluxo de voz
- Pré-condição: backend de voz acessível em `VOICE_API_URL`.
- Ao enviar áudio (`audioMessage`):
  - o fluxo de planner/executor pode não aparecer (rota direta da skill `voice_note_reply`)
  - resposta esperada via Evolution `sendMedia` com `mediatype=audio` e `fileName=reply.mp3`
- Em falha de STT/TTS:
  - fallback para mensagem de texto padrão de erro/retorno.

### 6) Confirmar skill de agendamento n8n
- Enviar mensagem tipo: `agende no n8n para 2026-03-10T16:30:00-03:00 lembrete de reunião`.
- Logs esperados:
  - `[v2.orchestrator] plan_skills=['n8n_schedule_alert']`
  - `[v2.executor] step_end skill=n8n_schedule_alert ok=True ...`
- Observação de roteamento:
  - a seleção da skill é feita pelo planner/LLM (sem fallback lexical por lista de palavras no planner).
- Se faltar data/hora válida:
  - skill deve responder com uma pergunta curta de clarificação (sem criar tarefa).
- Para listar:
  - enviar `liste meus agendamentos no n8n`
  - contrato: `action=list` com `data.payload.target.sender`.
- Para excluir:
  - enviar `excluir idTask <id>`
  - contrato: `action=delete` com `data.idTask`.

## Garmin skill notes
- Skill name: `garmin_tracking` (V2).
- Auth mode: token-only via `GARMINTOKENS`.
- One-time token bootstrap (inside this repo, local `.venv`):
  - `python3 -m venv .venv`
  - `./.venv/bin/pip install -r requirements.txt`
  - `./.venv/bin/python3 scripts/bootstrap_garmin_tokens.py --token-dir ./.garminconnect`
- If MFA is required:
  - rerun with `--mfa <code>` or provide code interactively.
- Configure container mount in `.env`:
  - `GARMINTOKENS=/garmin_tokens`
  - `GARMINTOKENS_HOST_PATH=/home/<user>/repositories/travis-agent/.garminconnect`
- First successful sync (no state): backfill from `2026-01-01`.
- Next syncs: incremental from `last_success_end_date - 1 day`.
- Redis keys used:
  - `agent:v2:garmin:sync_state:{sender}`
  - `agent:v2:garmin:last_payload:{sender}`

## Troubleshooting
### Resposta muito longa
- Verificar se planner escolheu `direct_answer`.
- Ajustar prompt/limites da skill usada.

### Não houve `web_search`
- Confirmar skill escolhida no log `selected_skills=[...]`.
- Se foi `summarize_url` ou `direct_answer`, comportamento está coerente com plano atual.

### Áudio cai em fallback de texto
- Verificar conectividade do container `travis` com `VOICE_API_URL`:
  - `docker compose exec -T travis sh -lc "python - <<'PY'\nimport requests, os\nu=os.environ.get('VOICE_API_URL','').rstrip('/')+'/health'\nprint('URL',u)\nprint(requests.get(u, timeout=5).status_code)\nPY"`
- Se `Connection refused`:
  - confirmar porta/host do backend de voz;
  - se usar host gateway, prefira `host.docker.internal` e exponha a porta no host.

### Agendamento n8n falha
- Verificar `N8N_SCHEDULE_WEBHOOK_URL` e `N8N_SCHEDULE_TIMEOUT`.
- Se callback estiver em uso, validar `X-Task-Secret` no n8n igual a `TASK_CALLBACK_SECRET`.
- Para entrega automática por callback, usar endpoint:
  - `POST /webhook/task-callback`

### Testes de callback falhando com 401
- Se `TASK_CALLBACK_SECRET` estiver definido no ambiente, os testes de callback precisam enviar header `X-Task-Secret`.
- Para rodar a suíte local sem secret: `TASK_CALLBACK_SECRET= ./.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'`

### Falha de dependência Python no host
- Rodar comandos de teste dentro do container `travis`:
  - `docker compose exec -T travis python -V`
  - `docker compose exec -T travis python -m py_compile app.py`

### Serviço sobe com imagem antiga
- Executar sempre em sequência:
  - `docker compose build travis`
  - `docker compose up -d --force-recreate travis`

## Segurança mínima para repositório público
- Manter `.env` no `.gitignore`.
- Usar `.env.sample` sem segredos.
- Rotacionar chaves se houver suspeita de vazamento.
- Evitar logs com payload sensível em produção.
