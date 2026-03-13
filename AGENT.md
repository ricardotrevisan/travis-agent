# AGENT

## Purpose
Canonical context and definitions for `travis-agent`.
Use this file to quickly reload operational context before edits.

## Current Runtime Modes
- Webhook uses `runtime/orchestrator.py` (`planner -> executor -> skills`).

## Non-Negotiable Constraints
- No word-list intent routing in planner heuristics.
- n8n routing must be planner/LLM-driven.
- Keep `voice_note_reply` planner-hidden and direct-routed for inbound audio events.

## Collaboration Directive
- Before any architectural/policy behavior change (planner strategy, heuristics, fallback policy, routing rules, contract semantics), stop and present:
  - intended change,
  - reason/tradeoff,
  - expected impact.
- Execute those changes only after explicit user approval.
- If request scope is implementation-only, do not expand scope with proactive policy changes.

## Core V2 Flow
1. `app.py` receives webhook.
2. `runtime/orchestrator.py` builds context and runs planner/executor.
3. `runtime/planner.py` produces plan from LLM output.
4. `runtime/executor.py` validates and executes skill steps.
5. Final response is sent through Evolution API.

## Voice Capability (Atomized Skill)
- Skill: `skills/voice_note_reply.py`
- Trigger: inbound WhatsApp `audioMessage` in V2.
- Path: direct skill call (planner bypassed).
- Contracts:
  - STT: `POST {VOICE_API_URL}/transcribe` (multipart file).
  - TTS: `POST {VOICE_API_URL}/tts` (`format=mp3`).
- Output behavior:
  - success: send `audio/mpeg` via Evolution `sendMedia` (`reply.mp3`).
  - failure: text fallback.

## n8n Schedule Capability
- Skill: `skills/n8n_schedule_alert.py`
- Planner visibility: enabled.
- Action selection is LLM-defined via `args.action` only (`create|list|delete`).
- The skill does not infer action from lexical/keyword lists.
- Supported actions:
  - `create`
  - `list`
  - `delete`
- Planner arg contract for `create`: planner must include `args.run_at` with ISO 8601 datetime extracted from the user request (e.g. `2026-03-12T19:28:00`).
- Skill datetime resolution order for `create`:
  1. `args.run_at`
  2. `args.datetime`
  3. `args.time`
  4. generic scan of all string arg values for ISO 8601 pattern
  5. fallback: regex extraction from `ctx.user_text`
- Datetime formats recognized by skill (all localized to `SCHEDULE_TIMEZONE` if no TZ):
  - ISO 8601 with TZ: `2026-03-12T19:28:00-03:00` / `...Z`
  - ISO 8601 no TZ: `2026-03-12T19:28:00` / `2026-03-12 19:28`
  - Day/month + time: `12/03 19:28`, `12-03 às 19:28`, `12/03/2026 15:00`
  - Relative + time: `hoje às 15:30`, `amanhã 10:00`
  - No date/time-only expressions (e.g. `às 19h` alone) → clarification requested
- Create payload contract:
  - `{"action":"create","data":{"title":"...","run_at":"ISO8601Z","payload":{"message":"...","target":{"sender":"<jid>","instance":"<instance>"}}}}`
- List payload contract:
  - `{"action":"list","data":{"payload":{"target":{"sender":"<jid>","instance":"<instance>"}}}}`
- Delete payload contract:
  - `{"action":"delete","data":{"idTask":"...","task_id":"...","payload":{"target":{"sender":"<jid>","instance":"<instance>"}}}}`
- Guardrails:
  - sender must be WhatsApp JID (`...@s.whatsapp.net`).
  - ambiguous/missing datetime in `create`: return short clarification, do not call n8n.
  - missing `idTask` in `delete`: return short clarification, do not call n8n.
  - n8n response without `idTask`: warn user explicitly instead of false success message.
  - `n8n_schedule_alert` steps bypass the final LLM interpreter in executor.

## Callback Delivery Endpoint
- Endpoint: `POST /webhook/task-callback` in `app.py`.
- Optional auth header: `X-Task-Secret` (checked against `TASK_CALLBACK_SECRET`).
- Required fields for `register_type=message`:
  - `task_id|idTask`
  - sender (`target.sender` or equivalent)
  - `message`
- Behavior:
  - dedupe via Redis in `app.py` (`_mark_message_processed`), key = `idempotency_key` or `task:{task_id}`, TTL = `WEBHOOK_DEDUPE_TTL_SECONDS` (default 300s)
  - without Redis: delivers without dedupe
  - allowlist enforced when configured
  - sends WhatsApp text through current send function

## Env Compatibility Layer
- OpenAI:
  - `OPENAI_API_KEY <- WA_AGENT_OPENAI_API_KEY`
- Evolution:
  - `EVOLUTION_APIKEY <- WA_AGENT_EVOLUTION_APIKEY`
  - `INSTANCE <- WA_AGENT_INSTANCE`
- Allowlist:
  - `ALLOWED_WHATSAPP_NUMBERS <- WA_AGENT_ALLOWED_SENDERS`
- Schedule timezone:
  - `SCHEDULE_TIMEZONE <- TZ <- America/Sao_Paulo`

## Operational Notes
- If `OPENAI_API_KEY` is missing, planner falls back to heuristic default (`direct_answer`).
- Callback tests can fail with `401` when `TASK_CALLBACK_SECRET` is set in environment but header is not provided in tests.
- Container logs are checked via:
  - `docker logs travis --tail=120`
- SkillRegistry is a lazy process-level singleton in `orchestrator.py` (`_get_registry`/`_reset_registry`).
- `/new` command resets both the registry singleton and the MCP tools cache.

## Validation Commands
- Syntax smoke:
  - `python3 -m py_compile app.py runtime/*.py skills/*.py`
- Unit tests:
  - `OPENAI_API_KEY=dummy ./.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'`
- If callback secret is set and tests need neutral mode:
  - `TASK_CALLBACK_SECRET= OPENAI_API_KEY=dummy ./.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'`

## Source of Truth Files
- App entrypoint: `app.py`
- Orchestrator: `runtime/orchestrator.py`
- Planner: `runtime/planner.py`
- Executor: `runtime/executor.py`
- Skill registry: `skills/registry.py`
- Voice skill: `skills/voice_note_reply.py`
- n8n skill: `skills/n8n_schedule_alert.py`
- Ops guide: `PLAYBOOK.md`
- Public overview: `README.md`
