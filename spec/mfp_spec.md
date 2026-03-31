# MyFitnessPal Tracking Skill — Implementation Spec

## Overview

Single-file skill (`skills/mfp_tracking.py`) following the `garmin_tracking` pattern:
Playwright-based scraping, Redis persistence per sender, incremental sync, own output (bypasses LLM post-processor).

---

## Files to create / edit

| File | Change |
|---|---|
| `skills/mfp_tracking.py` | **New** — full skill implementation |
| `skills/registry.py` | Add import + `"mfp_tracking": MFPTrackingSkill()` |
| `runtime/executor.py` | Add `"mfp_tracking"` to `_SKILLS_WITH_OWN_OUTPUT` |
| `AGENT.md` | Add planner args contract |

---

## Module layout (`mfp_tracking.py`)

```
# env/config
# dataclasses: FoodEntry, MealSection, DayDiary
# --- Playwright layer ---
_load_session()        # load cookies from Redis
_save_session()        # persist cookies to Redis
_login()               # fill credentials, submit, detect session validity
_fetch_diary_page()    # navigate to /food/diary?date=YYYY-MM-DD, return HTML
# --- Parser layer ---
_parse_diary_html()    # parse HTML → DayDiary
_parse_meal_section()  # parse one meal block
_parse_nutrients()     # parse nutrient columns dynamically
# --- Merge/persistence layer ---
_merge_days()          # merge new days into stored payload (dedupe by date)
_safe_json_load()      # same helper pattern as garmin_tracking
# --- Skill class ---
class MFPTrackingSkill(BaseSkill)
  __init__()
  _init_redis()
  _key_payload()       # agent:v2:mfp:payload:{sender}
  _key_sync_state()    # agent:v2:mfp:sync_state:{sender}
  _key_session()       # agent:v2:mfp:session:{sender}  ← cookies
  _resolve_range()     # manual_range → incremental (last_end-1) → backfill(today-7)
  _ensure_session()    # load or re-create Playwright session
  _fetch_days()        # loop dates, fetch + parse each day
  run()
```

---

## Authentication

- Credentials: `MFP_USERNAME` / `MFP_PASSWORD` env vars (already set)
- Cookies stored in Redis key `agent:v2:mfp:session:{sender}` as JSON, TTL 7 days
- On each run: load cookies → verify session (check `/food/diary` doesn't redirect to login) → if expired, re-login with credentials
- Playwright runs headless
- No local file storage — all state in Redis

---

## Date range resolution (mirrors garmin pattern)

```python
def _resolve_range(sender, args):
    if args.get("start_date"):
        return "manual_range", start, end or today
    sync_state = redis.get(_key_sync_state(sender))
    if sync_state["last_success_end_date"]:
        return "incremental", last_end - 1day, today
    return "backfill", today - 7days, today
```

---

## Parsing strategy

- Playwright renders the page fully, then parse HTML with `BeautifulSoup`
- **Meal sections**: locate by `aria-label` or visible heading text (resilient to HTML refactors)
- **Nutrient columns**: read header row dynamically → build `col_index → nutrient_name` map; never assume fixed columns
- **Day totals / goals**: parse footer rows of the diary table
- **Water**: parse water tracker widget if present (optional field)

---

## Data model

```json
{
  "date": "2026-03-30",
  "meals": [
    {
      "name": "Café da manhã",
      "entries": [
        {
          "food_name": "Aveia em Flocos",
          "brand": "Quaker",
          "serving_size": "40g",
          "calories": 148,
          "macros": {
            "carbohydrates_g": 27.2,
            "protein_g": 5.2,
            "fat_g": 2.8,
            "fiber_g": 2.8,
            "sugar_g": 0.4,
            "sodium_mg": 4.0
          }
        }
      ],
      "meal_totals": {
        "calories": 148,
        "carbohydrates_g": 27.2,
        "protein_g": 5.2,
        "fat_g": 2.8
      }
    }
  ],
  "day_totals": {
    "calories": 1850,
    "carbohydrates_g": 210.0,
    "protein_g": 130.0,
    "fat_g": 55.0,
    "fiber_g": 28.0,
    "sugar_g": 45.0,
    "sodium_mg": 1800.0
  },
  "day_goals": {
    "calories": 2200,
    "carbohydrates_g": 250.0,
    "protein_g": 150.0,
    "fat_g": 70.0
  },
  "water_ml": 1500
}
```

Optional nutrient fields (captured if column is visible in user's diary settings):
`fiber_g`, `sugar_g`, `sodium_mg`, `potassium_mg`, `cholesterol_mg`, `vitamin_a_%`, `vitamin_c_%`, `calcium_%`, `iron_%`

---

## Redis persistence

| Key | Content | TTL |
|---|---|---|
| `agent:v2:mfp:session:{sender}` | Playwright cookies JSON | 7 days |
| `agent:v2:mfp:sync_state:{sender}` | `{last_success_end_date, last_sync_at, mode}` | none |
| `agent:v2:mfp:payload:{sender}` | `{days: {...}, lastUpdate, sourceRange}` | none |

`payload["days"]` is a dict keyed by ISO date string; new fetches overwrite existing entries for the same date.

---

## SkillResult.output schema

```json
{
  "raw": {
    "days": { "2026-03-30": { ... } },
    "lastUpdate": "2026-03-30T12:00:00Z",
    "sourceRange": { "start": "2026-03-24", "end": "2026-03-30" }
  },
  "metrics": {
    "daysCount": 7,
    "avgCalories": 1750,
    "avgProtein_g": 120
  },
  "sync": {
    "mode": "incremental",
    "effective_start": "2026-03-29",
    "effective_end": "2026-03-30"
  }
}
```

---

## user_visible_text format

Single day:
```
Diário MFP — 30 de março de 2026
──────────────────────────────────
Café da manhã    148 kcal
Almoço           620 kcal
Jantar           480 kcal
Lanches          210 kcal

Total: 1.458 / 2.200 kcal (66%)
Proteína:  98g / 150g  (65%)
Carbo:    180g / 250g  (72%)
Gordura:   42g / 70g   (60%)
```

Multi-day: one block per day separated by `---`.

---

## Planner args contract

```
mfp_tracking accepts:
  start_date: YYYY-MM-DD (optional)
  end_date:   YYYY-MM-DD (optional, defaults to today)
```

---

## Error handling

| Scenario | Behavior |
|---|---|
| Redis unavailable | `SkillResult(ok=False, error="mfp redis unavailable")` |
| Login failed / session invalid | `SkillResult(ok=False, error="mfp login failed — check MFP_USERNAME/MFP_PASSWORD")` |
| Day has no entries | Return `DayDiary` with empty meals (not an error) |
| Nutrient column absent | Skip silently (dynamic column map) |
| Rate limiting | 1.5s delay between page loads |

---

## Dependencies

All already in `requirements.txt`:
- `playwright` ✅
- `beautifulsoup4` ✅
- `redis` ✅
- `python-dotenv` ✅

No new dependencies needed.

---

## Análise da implementação atual (`skills/mfp_tracking.py`)


### Onde estão as instruções para o agente

As instruções estão no atributo `description` da classe (`MFPTrackingSkill`):

```python
description = (
    "Buscar o diário alimentar do MyFitnessPal: refeições, calorias, macros e metas do dia. "
    "Aceita start_date e end_date (YYYY-MM-DD). Sem datas: busca incremental desde a última sync."
)
```

Esse campo é registrado no `skills/registry.py` e exposto ao LLM como descrição da tool, então o agente sabe quando e como chamar a skill.

---

### Capacidades implementadas

| Capacidade | Detalhe |
|---|---|
| **Login headless** | Playwright (Chromium) faz login no MFP com usuário/senha via env vars, remove overlay de privacidade se presente |
| **Cache de sessão (Redis)** | Cookies são salvos no Redis com TTL de 7 dias (chave `agent:v2:mfp:session:<sender>`) |
| **Renovação automática de sessão** | Se a sessão expira durante o fetch, faz re-login e retenta a data falhada |
| **Sync incremental** | Sem parâmetros: busca desde o último `end_date` bem-sucedido menos 1 dia até hoje |
| **Backfill inicial** | Se nunca sincronizou, busca os últimos 7 dias (configurável via `MFP_BACKFILL_DAYS`) |
| **Range manual** | Aceita `start_date` e `end_date` como argumentos |
| **Parsing de HTML** | BeautifulSoup lê a tabela `#diary-table`: refeições, itens, totais por refeição, totais do dia e metas diárias |
| **Normalização de nutrientes** | Mapeamento PT/EN para calorias, carboidratos, proteína, gordura, fibra, sódio, potássio, etc. |
| **Persistência de payload** | Dados mergeados por data são salvos no Redis (chave `agent:v2:mfp:payload:<sender>`) |
| **Métricas resumidas** | Calcula média de calorias e proteína sobre os dias armazenados |
| **Texto legível para o usuário** | Formata diário em PT-BR com calorias por refeição, totais e % das metas |
| **Rate limiting** | `MFP_PAGE_DELAY` (default 1.5s) entre requisições quando busca múltiplos dias |
