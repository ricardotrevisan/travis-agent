from typing import Any

from agent.tools.search_tool import WebSearchTool
from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill

_tool = WebSearchTool()


class WebSearchSkill(BaseSkill):
    name = "web_search"
    description = "Buscar informações atuais na web."

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        query = (args.get("query") or ctx.user_text).strip()
        if not query:
            return SkillResult(ok=False, error="missing query")
        max_results = int(args.get("max_results") or 3)

        try:
            data = _tool.run({"query": query, "max_results": max_results})
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                return SkillResult(ok=False, output={"raw": data}, error="invalid search output")

            lines = []
            for item in results[:3]:
                title = item.get("title") or "Sem título"
                url = item.get("url") or ""
                snippet = (item.get("snippet") or "").strip()
                line = f"- {title} {url}".strip()
                if snippet:
                    line += f"\n  {snippet}"
                lines.append(line)

            text = "\n".join(lines).strip() or "Não encontrei resultados úteis agora."
            return SkillResult(
                ok=True,
                output={"results": results, "query": query},
                user_visible_text=text,
            )
        except Exception as exc:
            return SkillResult(ok=False, error=f"web_search failed: {exc}")

