import json
import os
import time
from typing import Any

from openai import OpenAI

from runtime.models import ExecutionPlan, RequestContext, SkillResult
from skills.registry import SkillRegistry


def validate_plan(plan: ExecutionPlan, registry: SkillRegistry) -> tuple[bool, str]:
    if not plan.steps:
        return False, "empty plan"
    if len(plan.steps) > 3:
        return False, "too many steps"

    seen: set[tuple[str, str]] = set()
    for step in plan.steps:
        if not registry.get(step.skill):
            print(f"[v2.executor] validate status=invalid reason=unknown_skill skill={step.skill}")
            return False, f"unknown skill: {step.skill}"
        key = (step.skill, str(sorted(step.args.items())))
        if key in seen:
            print(f"[v2.executor] validate status=invalid reason=duplicate_step skill={step.skill}")
            return False, f"duplicate step: {step.skill}"
        seen.add(key)
    print(f"[v2.executor] validate status=ok steps={[step.skill for step in plan.steps]}")
    return True, ""


def _resolve_step_args(step_args: dict[str, Any], last_result: SkillResult | None) -> dict[str, Any]:
    if not step_args.get("urls_from_previous_step") or not last_result:
        return step_args

    output = last_result.output if isinstance(last_result.output, dict) else {}
    results = output.get("results") if isinstance(output.get("results"), list) else []
    first_url = ""
    if results and isinstance(results[0], dict):
        first_url = str(results[0].get("url") or "")

    resolved = dict(step_args)
    resolved.pop("urls_from_previous_step", None)
    if first_url and "url" not in resolved:
        resolved["url"] = first_url
    return resolved


def execute_plan(ctx: RequestContext, plan: ExecutionPlan, registry: SkillRegistry) -> dict[str, Any]:
    print(f"[v2.executor] start sender={ctx.sender} steps={[step.skill for step in plan.steps]}")
    results: list[dict[str, Any]] = []
    last_result: SkillResult | None = None

    for step in plan.steps:
        skill = registry.get(step.skill)
        if not skill:
            print(f"[v2.executor] stop reason=skill_unavailable skill={step.skill}")
            return {"ok": False, "error": f"skill unavailable: {step.skill}", "results": results}

        args = _resolve_step_args(step.args or {}, last_result)
        print(f"[v2.executor] step_start skill={step.skill} args={args}")
        start = time.time()
        current = skill.run(ctx, args)
        latency_ms = int((time.time() - start) * 1000)
        results.append(
            {
                "skill": step.skill,
                "args": args,
                "ok": current.ok,
                "latency_ms": latency_ms,
                "error": current.error,
            }
        )
        print(
            f"[v2.executor] step_end skill={step.skill} ok={current.ok} "
            f"latency_ms={latency_ms} error={current.error}"
        )
        if not current.ok:
            print(f"[v2.executor] stop reason=step_failed skill={step.skill}")
            return {"ok": False, "error": current.error or "step failed", "results": results}
        last_result = current

    final_text = (last_result.user_visible_text if last_result else "").strip()
    final_text = _maybe_interpret_with_llm(ctx, plan, results, last_result, final_text)
    if not final_text:
        final_text = "Nao consegui responder agora."
    print(f"[v2.executor] done ok=true response_len={len(final_text)}")
    return {"ok": True, "response_text": final_text, "results": results}


def _maybe_interpret_with_llm(
    ctx: RequestContext,
    plan: ExecutionPlan,
    results: list[dict[str, Any]],
    last_result: SkillResult | None,
    fallback_text: str,
) -> str:
    _SKILLS_WITH_OWN_OUTPUT = {"n8n_schedule_alert", "garmin_tracking", "gmail_list"}
    if any(step.skill in _SKILLS_WITH_OWN_OUTPUT for step in plan.steps):
        return fallback_text
    enabled = (os.getenv("FINAL_INTERPRETER_ENABLED") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return fallback_text
    api_key = os.getenv("OPENAI_API_KEY") or ""
    if not api_key:
        return fallback_text

    model = os.getenv("FINAL_INTERPRETER_MODEL") or os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
    max_chars = int(os.getenv("FINAL_INTERPRETER_MAX_CHARS", "6000"))

    tool_names = [step.skill for step in plan.steps]
    raw_output = last_result.output if last_result and isinstance(last_result.output, dict) else {}
    raw_payload = {
        "tool_names": tool_names,
        "results": results,
        "tool_output": raw_output,
        "tool_text": fallback_text,
    }
    raw_json = _safe_json(raw_payload)
    if len(raw_json) > max_chars:
        raw_json = raw_json[:max_chars] + "…"

    prompt = (
        "Você é um pós-processador. Interprete o resultado de ferramentas e responda ao usuário "
        "de forma clara, concisa e útil. Se houver erro ou ausência de dados necessários, "
        "faça uma pergunta objetiva para continuar.\n"
        f"Pedido do usuário: {ctx.user_text}\n"
        f"Resultados das ferramentas (JSON): {raw_json}\n"
    )
    try:
        client = OpenAI(api_key=api_key)
        messages: list[dict] = []
        messages.extend(ctx.history)
        messages.append({"role": "user", "content": prompt})
        response = client.responses.create(
            model=model,
            input=messages,
        )
        text = (response.output_text or "").strip()
        return text or fallback_text
    except Exception:
        return fallback_text


def _safe_json(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        try:
            return str(payload)
        except Exception:
            return ""
