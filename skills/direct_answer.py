import os
from typing import Any

from openai import OpenAI

from runtime.models import RequestContext, SkillResult
from runtime.persona import build_system_persona
from skills.base import BaseSkill

MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class DirectAnswerSkill(BaseSkill):
    name = "direct_answer"
    description = "Responder diretamente sem usar ferramentas externas."

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        try:
            messages = [{"role": "system", "content": build_system_persona(channel="text")}]
            messages.extend(ctx.history)
            messages.append({"role": "user", "content": ctx.user_text})
            response = client.responses.create(
                model=MODEL,
                input=messages,
            )
            text = (response.output_text or "").strip()
            if not text:
                return SkillResult(ok=False, error="direct_answer returned empty")
            return SkillResult(ok=True, output={"text": text}, user_visible_text=text)
        except Exception as exc:
            return SkillResult(ok=False, error=f"direct_answer failed: {exc}")
