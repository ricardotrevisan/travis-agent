import os
from typing import Any

from agent.tools.summarize_tool import SummarizeURLTool
from openai import OpenAI
from runtime.models import RequestContext, SkillResult
from runtime.persona import build_system_persona
from skills.base import BaseSkill

_tool = SummarizeURLTool()
_model = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _build_raw_fallback_text(title: str, summary_text: str) -> str:
    text = summary_text or "Não consegui resumir esse link agora."
    if title:
        return f"{title}\n\n{text}"
    return text


def _synthesize_summary(user_goal: str, url: str, title: str, base_summary: str) -> str:
    prompt = (
        f"{build_system_persona(channel='text')}\n\n"
        "Você vai gerar um resumo final em português do Brasil.\n"
        "Regras:\n"
        "- Responda em 6 a 8 linhas.\n"
        "- Seja objetivo, sem floreio.\n"
        "- Não use opinião pessoal (evite 'acho').\n"
        "- Foque no pedido do usuário.\n"
        "- Não invente fatos fora do conteúdo fornecido.\n\n"
        "Inclua, quando disponível:\n"
        "- fato principal;\n"
        "- contexto/causa do movimento;\n"
        "- números ou variações relevantes;\n"
        "- fechamento com impacto prático em linguagem simples.\n\n"
        f"Pedido do usuário: {user_goal}\n"
        f"URL: {url}\n"
        f"Título: {title}\n"
        f"Resumo base: {base_summary}\n"
    )
    response = _client.responses.create(
        model=_model,
        input=[{"role": "user", "content": prompt}],
    )
    return (response.output_text or "").strip()


class SummarizeURLSkill(BaseSkill):
    name = "summarize_url"
    description = "Ler e resumir uma URL."

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        url = (args.get("url") or (ctx.urls[0] if ctx.urls else "")).strip()
        if not url:
            return SkillResult(ok=False, error="missing url")

        max_chars = int(args.get("max_chars") or 1500)
        try:
            data = _tool.run({"url": url, "max_chars": max_chars})
            print("[skill=summarize_url] phase=tool_call ok=true")

            summary_source = "summary" if (data.get("summary") or "").strip() else "local_summary"
            summary = (data.get("summary") or data.get("local_summary") or "").strip()
            title = (data.get("title") or "").strip()
            raw_fallback_text = _build_raw_fallback_text(title, summary)

            if not summary:
                print("[skill=summarize_url] fallback=raw_summary reason=empty_summary")
                return SkillResult(
                    ok=True,
                    output={
                        "url": url,
                        "data": data,
                        "synthesized_text": "",
                        "synthesis_source": "none",
                    },
                    user_visible_text=raw_fallback_text,
                )

            try:
                synthesized_text = _synthesize_summary(
                    user_goal=ctx.user_text,
                    url=url,
                    title=title,
                    base_summary=summary,
                )
                if not synthesized_text:
                    print("[skill=summarize_url] phase=llm_synthesis ok=false reason=empty_response")
                    print("[skill=summarize_url] fallback=raw_summary reason=empty_synthesis")
                    return SkillResult(
                        ok=True,
                        output={
                            "url": url,
                            "data": data,
                            "synthesized_text": "",
                            "synthesis_source": summary_source,
                        },
                        user_visible_text=raw_fallback_text,
                    )

                print("[skill=summarize_url] phase=llm_synthesis ok=true")
                return SkillResult(
                    ok=True,
                    output={
                        "url": url,
                        "data": data,
                        "synthesized_text": synthesized_text,
                        "synthesis_source": summary_source,
                    },
                    user_visible_text=synthesized_text,
                )
            except Exception as exc:
                print(f"[skill=summarize_url] phase=llm_synthesis ok=false error={exc}")
                print("[skill=summarize_url] fallback=raw_summary reason=llm_error")
                return SkillResult(
                    ok=True,
                    output={
                        "url": url,
                        "data": data,
                        "synthesized_text": "",
                        "synthesis_source": summary_source,
                    },
                    user_visible_text=raw_fallback_text,
                )
        except Exception as exc:
            print(f"[skill=summarize_url] phase=tool_call ok=false error={exc}")
            return SkillResult(ok=False, error=f"summarize_url failed: {exc}")
