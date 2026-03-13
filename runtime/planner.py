import json
import os
import re
from typing import Any

from openai import OpenAI

from runtime.models import ExecutionPlan, PlanStep, RequestContext

MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None


def _classify_needs_current_info_llm(ctx: RequestContext) -> bool | None:
    history_snippet = ""
    if ctx.history:
        lines = []
        for m in ctx.history[-4:]:  # last 2 turns for classifier context
            lines.append(f'{m["role"]}: {m["content"][:200]}')
        history_snippet = "\nContexto recente:\n" + "\n".join(lines) + "\n"
    classifier_prompt = (
        "Classifique se o pedido do usuário depende de informação externa atual.\n"
        "Responda APENAS JSON puro no formato:\n"
        '{"needs_current_info": true|false}\n'
        "Use true quando precisar de fatos de hoje/agora/recente, mercado, notícias, placar, tempo, cotações ou status atual.\n"
        "Use false para pedido conceitual, explicação atemporal, opinião, escrita ou reformulação.\n"
        f"{history_snippet}"
        f"Pedido do usuário: {ctx.user_text}\n"
    )
    try:
        response = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": classifier_prompt}],
        )
        parsed = _parse_json_object(response.output_text or "")
        if not parsed or "needs_current_info" not in parsed:
            print("[v2.planner] classifier source=llm result=invalid")
            return None
        value = parsed.get("needs_current_info")
        if isinstance(value, bool):
            print(f"[v2.planner] classifier source=llm needs_current_info={value}")
            return value
        print("[v2.planner] classifier source=llm result=non_boolean")
        return None
    except Exception as exc:
        print(f"[v2.planner] classifier fallback reason=llm_error err={exc}")
        return None


def _heuristic_plan(ctx: RequestContext, needs_current_info: bool = False) -> ExecutionPlan:
    if ctx.urls:
        plan = ExecutionPlan(steps=[PlanStep(skill="summarize_url", args={"url": ctx.urls[0]})])
        print(f"[v2.planner] source=heuristic selected_skill={plan.steps[0].skill} reason=explicit_url")
        return plan
    if needs_current_info:
        plan = ExecutionPlan(steps=[PlanStep(skill="web_search", args={"query": ctx.user_text, "max_results": 3})])
        print(f"[v2.planner] source=heuristic selected_skill={plan.steps[0].skill} reason=current_info")
        return plan
    plan = ExecutionPlan(steps=[PlanStep(skill="direct_answer", args={})])
    print(f"[v2.planner] source=heuristic selected_skill={plan.steps[0].skill} reason=default")
    return plan


def _build_prompt(
    ctx: RequestContext,
    available_skills: list[dict[str, str]],
    needs_current_info: bool,
    has_url: bool,
) -> str:
    payload = {
        "user_goal": ctx.user_text,
        "context": {
            "has_url": has_url,
            "needs_current_info": needs_current_info,
            "urls": ctx.urls[:3],
            "recent_history": ctx.history[-6:] if ctx.history else [],
        },
        "available_skills": available_skills,
        "rules": [
            "Use n8n_schedule_alert quando o usuário pedir para agendar, listar ou remover lembretes/alertas/tarefas no n8n.",
            "Quando usar n8n_schedule_alert, sempre inclua args.action com um de: create, list, delete.",
            "Para create em n8n_schedule_alert, inclua args.run_at com o datetime ISO 8601 extraído do pedido (ex: 2026-03-12T19:28:00).",
            "Para delete, inclua args.idTask quando já houver id explícito no pedido.",
            "Use garmin_tracking quando o usuário pedir por garmin ou update/atualização/dados de treino/atividade/histórico/agenda do Garmin. Os únicos args válidos são start_date e end_date (formato YYYY-MM-DD), ambos opcionais. Não invente outros args.",
            "Use summarize_url quando houver URL explícita.",
            "Use web_search quando depender de informação externa atual.",
            "Use direct_answer nos demais casos.",
            "Se needs_current_info=true e has_url=false, prefira web_search em 1 passo.",
            "Nunca invente skills.",
            "No máximo 2 passos.",
            "Prefira 1 passo.",
        ],
        "output_schema": {
            "steps": [{"skill": "string", "args": {}}],
            "final_response_mode": "skill_output",
        },
    }
    return (
        "Você é um planner. Retorne JSON puro com o menor plano possível.\n"
        "Sem markdown, sem texto extra.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_llm_plan(raw: str) -> ExecutionPlan | None:
    parsed = _parse_json_object(raw)
    if not parsed:
        return None

    steps_raw = parsed.get("steps")
    if not isinstance(steps_raw, list):
        return None
    steps: list[PlanStep] = []
    for item in steps_raw[:2]:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill") or "").strip()
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if skill:
            steps.append(PlanStep(skill=skill, args=args))
    if not steps:
        return None
    mode = str(parsed.get("final_response_mode") or "skill_output")
    return ExecutionPlan(steps=steps, final_response_mode=mode)


def plan(ctx: RequestContext, available_skills: list[dict[str, str]]) -> ExecutionPlan:
    has_url = bool(ctx.urls)
    needs_current_info = False

    if not os.getenv("OPENAI_API_KEY"):
        print(
            f"[v2.planner] signals has_url={has_url} needs_current_info={needs_current_info}"
        )
        print("[v2.planner] source=heuristic reason=missing_openai_api_key")
        return _heuristic_plan(ctx, needs_current_info=False)

    classified = _classify_needs_current_info_llm(ctx)
    if isinstance(classified, bool):
        needs_current_info = classified
    print(
        f"[v2.planner] signals has_url={has_url} needs_current_info={needs_current_info}"
    )

    prompt = _build_prompt(
        ctx,
        available_skills,
        needs_current_info=needs_current_info,
        has_url=has_url,
    )
    try:
        print(f"[v2.planner] source=llm start available_skills={[s.get('name') for s in available_skills]}")
        response = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": prompt}],
        )
        parsed = _parse_llm_plan(response.output_text or "")
        if parsed:
            selected_skills = [step.skill for step in parsed.steps]
            print(f"[v2.planner] source=llm selected_skills={selected_skills}")
            if needs_current_info and not has_url and "web_search" not in selected_skills:
                print(
                    "[v2.planner] policy_miss rule=current_info_without_url "
                    f"selected_skills={selected_skills}"
                )
            return parsed
        print("[v2.planner] source=llm fallback=heuristic reason=invalid_or_empty_plan")
        return _heuristic_plan(ctx, needs_current_info=needs_current_info)
    except Exception as exc:
        print(f"[v2.planner] fallback reason=llm_error err={exc}")
        return _heuristic_plan(ctx, needs_current_info=needs_current_info)
