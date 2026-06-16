import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill
from utils.product_scraper import (
    MonitorReport,
    MonitorTarget,
    ProductScraper,
    pants_target,
    windscreen_target,
)

# Tamanho-alvo da calça (= 4XL; tamanho do usuário). Override via args["size"].
DEFAULT_SIZE = "5G"
# Compat: nome antigo ainda referenciado em testes.
DEFAULT_QUERY = "Calça AlpineStars Halo Preta"

_TZ_NAME = os.getenv("SCHEDULE_TIMEZONE") or os.getenv("TZ") or "America/Sao_Paulo"


def _local_now_str() -> str:
    try:
        tz = ZoneInfo(_TZ_NAME)
    except Exception:
        tz = timezone(timedelta(hours=-3))
    return datetime.now(tz).strftime("%d/%m %H:%M")


def _format_report(report: MonitorReport) -> str:
    """Bloco de texto de um produto (achou ou não)."""
    label = f"*{report.target_name}*"
    # Mostra a variante no cabeçalho só quando é tamanho (sempre verdadeira).
    if report.variant and report.criterion == "size":
        label += f" ({report.variant})"
    hits = report.hits
    if hits:
        lines = [f"🟢 {label} em estoque:"]
        for hit in hits:
            # Marca a referência (part number) quando confirmada — é desejável.
            ref = f" [ref {report.variant} ✓]" if hit.reference_seen else ""
            lines.append(f"• {hit.site}{ref}: {hit.url}")
        return "\n".join(lines)
    n = len(report.results)
    site_names = ", ".join(r.site for r in report.results)
    return f"🔍 {label}: procurei em {n} sites ({site_names}). Nada em estoque desta vez."


def _report_to_output(report: MonitorReport) -> dict[str, Any]:
    return {
        "target": report.target_name,
        "query": report.query,
        "variant": report.variant,
        "hit_count": len(report.hits),
        "results": [
            {
                "site": r.site,
                "url": r.url,
                "matched_product": r.matched_product,
                "variant_available": r.variant_available,
                "reference_seen": r.reference_seen,
                "in_stock": r.in_stock,
                "is_hit": r.is_hit,
                "note": r.note,
            }
            for r in report.results
        ],
    }


class ProductMonitorSkill(BaseSkill):
    name = "product_monitor"
    description = "Monitorar disponibilidade de produtos no varejo de moto brasileiro."
    # Acionado por endpoint/agendamento, não pelo planner conversacional.
    planner_visible = False

    def __init__(self, scraper: ProductScraper | None = None) -> None:
        self._scraper = scraper or ProductScraper()

    def _targets(self, args: dict[str, Any]) -> list[MonitorTarget]:
        size = (args.get("size") or DEFAULT_SIZE).strip()
        return [pants_target(size), windscreen_target()]

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        try:
            reports = [self._scraper.scan(t) for t in self._targets(args)]
        except Exception as exc:
            return SkillResult(ok=False, error=f"product_monitor failed: {exc}")

        now = _local_now_str()
        blocks = [f"Busca das {now}:"] + [_format_report(r) for r in reports]
        message_text = "\n\n".join(blocks)

        total_hits = sum(len(r.hits) for r in reports)
        output = {
            "hit_count": total_hits,
            "reports": [_report_to_output(r) for r in reports],
        }
        # Sempre informa por WhatsApp: o quê/quando/onde, ache ou não.
        return SkillResult(ok=True, output=output, user_visible_text=message_text)
