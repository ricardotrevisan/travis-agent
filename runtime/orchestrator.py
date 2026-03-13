import json
from typing import Any, Callable, Dict

from runtime.context_builder import build_request_context
from runtime.models import RequestContext
from runtime.executor import execute_plan, validate_plan
from runtime.planner import plan as build_plan
from skills.registry import SkillRegistry
from skills.mcp_tools import invalidate_mcp_tools_cache

FALLBACK_TEXT = "Não consegui processar sua mensagem agora."

_registry: SkillRegistry | None = None


def _get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def _reset_registry() -> None:
    global _registry
    _registry = None


def _run_direct_answer_fallback(ctx, registry: SkillRegistry) -> str:
    print(f"[v2.orchestrator] direct_fallback start sender={ctx.sender}")
    skill = registry.get("direct_answer")
    if not skill:
        print("[v2.orchestrator] direct_fallback result=final_fallback reason=skill_missing")
        return FALLBACK_TEXT
    result = skill.run(ctx, {})
    if result.ok and result.user_visible_text:
        print("[v2.orchestrator] direct_fallback result=success")
        return result.user_visible_text
    print(f"[v2.orchestrator] direct_fallback result=final_fallback error={result.error}")
    return FALLBACK_TEXT


def _is_audio_message_event(wa_event: Dict[str, Any]) -> bool:
    if wa_event.get("event") != "messages.upsert":
        return False
    data = wa_event.get("data") or {}
    message = data.get("message") or {}
    return "audioMessage" in message


def _build_audio_request_context(wa_event: Dict[str, Any], instance_name: str) -> RequestContext | None:
    data = wa_event.get("data") or {}
    key = data.get("key") or {}
    sender = key.get("remoteJid") or ""
    if not sender or bool(key.get("fromMe")):
        return None
    return RequestContext(
        sender=sender,
        instance_name=instance_name,
        message_id=key.get("id") or "",
        user_text="",
        urls=[],
        channel_meta={
            "pushName": data.get("pushName"),
            "messageTimestamp": data.get("messageTimestamp"),
            "source": wa_event.get("source", "web"),
            "voice_message": True,
        },
    )


def handle_webhook_v2(
    wa_event: Dict[str, Any],
    instance_name: str,
    send_text: Callable[[str, str, str], Any],
    send_voice: Callable[[str, bytes, str, str], Any] | None = None,
) -> str:
    registry = _get_registry()

    # Voice path: single internal skill, planner is skipped.
    if _is_audio_message_event(wa_event):
        ctx_audio = _build_audio_request_context(wa_event, instance_name=instance_name)
        if not ctx_audio:
            return ""
        skill = registry.get("voice_note_reply")
        if not skill:
            response_text = FALLBACK_TEXT
            send_text(ctx_audio.sender, response_text, instance_name)
            return response_text

        result = skill.run(ctx_audio, {"wa_event": wa_event})
        if result.ok and isinstance(result.output.get("audio_bytes"), (bytes, bytearray)) and send_voice:
            mimetype = str(result.output.get("mimetype") or "audio/mpeg")
            send_voice(ctx_audio.sender, bytes(result.output["audio_bytes"]), instance_name, mimetype)
            return result.user_visible_text or ""

        fallback_text = result.user_visible_text or FALLBACK_TEXT
        if fallback_text:
            send_text(ctx_audio.sender, fallback_text, instance_name)
        return fallback_text

    ctx = build_request_context(wa_event, instance_name=instance_name)
    if not ctx:
        return ""
    print(f"[v2.orchestrator] start sender={ctx.sender} message_id={ctx.message_id}")
    if ctx.user_text.strip().lower() == "/new":
        _reset_registry()
        invalidate_mcp_tools_cache()
        response_text = "Sessao resetada. Contexto anterior limpo."
        print(f"[v2.orchestrator] session_reset sender={ctx.sender}")
        send_text(ctx.sender, response_text, instance_name)
        return response_text

    trace: dict[str, Any] = {
        "sender": ctx.sender,
        "message_id": ctx.message_id,
        "plan": [],
        "results": [],
        "final_mode": "skill_output",
    }

    # Fallback nível 1: planner inválido/falha cai para direct_answer.
    plan = build_plan(ctx, available_skills=registry.planner_catalog())
    trace["plan"] = [{"skill": s.skill, "args": s.args} for s in plan.steps]
    trace["final_mode"] = plan.final_response_mode
    print(f"[v2.orchestrator] plan_skills={[s['skill'] for s in trace['plan']]}")

    valid, error = validate_plan(plan, registry)
    if not valid:
        print(f"[v2.orchestrator] planner_fallback reason={error}")
        response_text = _run_direct_answer_fallback(ctx, registry)
        if response_text:
            print(f"[v2.orchestrator] send_text mode=fallback1 text_len={len(response_text)}")
            send_text(ctx.sender, response_text, instance_name)
        trace["results"] = [{"skill": "direct_answer", "ok": response_text != FALLBACK_TEXT}]
        print(f"[v2.trace] {json.dumps(trace, ensure_ascii=False)}")
        return response_text

    exec_result = execute_plan(ctx, plan, registry)
    trace["results"] = exec_result.get("results") or []
    if exec_result.get("ok"):
        response_text = exec_result.get("response_text") or ""
        if response_text:
            print(f"[v2.orchestrator] send_text mode=plan_success text_len={len(response_text)}")
            send_text(ctx.sender, response_text, instance_name)
        print(f"[v2.trace] {json.dumps(trace, ensure_ascii=False)}")
        return response_text

    print(f"[v2.orchestrator] planner_fallback reason=execution_failed err={exec_result.get('error')}")
    response_text = _run_direct_answer_fallback(ctx, registry)
    if response_text:
        print(f"[v2.orchestrator] send_text mode=fallback2 text_len={len(response_text)}")
        send_text(ctx.sender, response_text, instance_name)

    # Fallback nível 2 já é aplicado dentro de _run_direct_answer_fallback.
    trace["results"].append(
        {
            "skill": "direct_answer",
            "ok": response_text != FALLBACK_TEXT,
            "error": None if response_text != FALLBACK_TEXT else "final_fallback",
        }
    )
    print(f"[v2.trace] {json.dumps(trace, ensure_ascii=False)}")
    return response_text
