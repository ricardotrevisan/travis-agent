import unittest

from runtime.models import RequestContext
from skills.product_monitor import DEFAULT_QUERY, DEFAULT_SIZE, ProductMonitorSkill
from utils.product_scraper import MONITORED_SITES, ProductScraper, windscreen_target

_CTX = RequestContext(
    sender="5511975196655@s.whatsapp.net",
    instance_name="Travis",
    message_id="m1",
    user_text="",
)

_IN_STOCK_PAGE = (
    "Calça AlpineStars Halo Preta - Tamanho 5G\n"
    "Disponível. Adicionar ao carrinho. R$ 1.299,00"
)
_OUT_OF_STOCK_PAGE = (
    "Calça AlpineStars Halo Preta - Tamanho 5G\n"
    "Produto esgotado. Avise-me quando chegar."
)
_WRONG_SIZE_PAGE = (
    "Calça AlpineStars Halo Preta - Tamanho M\n"
    "Disponível. Adicionar ao carrinho."
)
_WRONG_PRODUCT_PAGE = "Jaqueta AlpineStars Andes - Disponível. Comprar agora."


class _FakeSerper:
    """Devolve candidatos por site. Por padrão, a calça Halo (produto-alvo).

    `candidates_by_domain` permite simular candidatos específicos (ex: luva).
    """

    def __init__(
        self,
        with_candidates: bool = True,
        candidates_by_domain: dict | None = None,
    ) -> None:
        self.with_candidates = with_candidates
        self.candidates_by_domain = candidates_by_domain or {}

    def search(self, query, max_results):
        if not self.with_candidates:
            return []
        domain = query.split("site:")[-1].strip()
        if domain in self.candidates_by_domain:
            return self.candidates_by_domain[domain]
        return [
            {
                "title": "Calça Alpinestars Halo Drystar Preto",
                "url": f"https://{domain}/produto/calca-alpinestars-halo-drystar-preto",
                "snippet": "",
            }
        ]


class _FakeJina:
    """Mapeia domínio -> texto da página."""

    def __init__(self, page_by_domain: dict[str, str], default: str = "") -> None:
        self.page_by_domain = page_by_domain
        self.default = default

    def fetch_text(self, url: str) -> str:
        for domain, page in self.page_by_domain.items():
            if domain in url:
                return page
        return self.default


def _make_skill(serper, jina, playwright_fetcher=None) -> ProductMonitorSkill:
    scraper = ProductScraper(
        serper=serper,
        jina=jina,
        playwright_fetcher=playwright_fetcher,
        # Sem fallback real nos testes, salvo quando um fake é injetado.
        use_playwright_fallback=playwright_fetcher is not None,
    )
    return ProductMonitorSkill(scraper=scraper)


def _pants_results(output) -> list:
    """results do alvo da calça (1º report)."""
    return output["reports"][0]["results"]


class ProductMonitorTests(unittest.TestCase):
    def test_hit_when_size_in_stock(self):
        jina = _FakeJina({"alpinestarsbr.com.br": _IN_STOCK_PAGE}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina)
        result = skill.run(_CTX, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["hit_count"], 1)
        self.assertIn("Alpinestars BR", result.user_visible_text)
        self.assertIn("alpinestarsbr.com.br", result.user_visible_text)

    def test_no_alert_when_out_of_stock(self):
        jina = _FakeJina({"alpinestarsbr.com.br": _OUT_OF_STOCK_PAGE}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina)
        result = skill.run(_CTX, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["hit_count"], 0)
        self.assertIn("Nada em estoque", result.user_visible_text)

    def test_no_alert_when_wrong_size(self):
        jina = _FakeJina({"alpinestarsbr.com.br": _WRONG_SIZE_PAGE}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina)
        result = skill.run(_CTX, {})
        self.assertEqual(result.output["hit_count"], 0)
        self.assertIn("Nada em estoque", result.user_visible_text)

    def test_no_alert_when_no_candidates(self):
        skill = _make_skill(_FakeSerper(with_candidates=False), _FakeJina({}))
        result = skill.run(_CTX, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.output["hit_count"], 0)

    def test_scans_all_sites(self):
        jina = _FakeJina({}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina)
        result = skill.run(_CTX, {})
        self.assertEqual(len(_pants_results(result.output)), len(MONITORED_SITES))

    def test_multiple_hits(self):
        jina = _FakeJina(
            {
                "alpinestarsbr.com.br": _IN_STOCK_PAGE,
                "nacar.com.br": _IN_STOCK_PAGE,
            },
            default=_WRONG_PRODUCT_PAGE,
        )
        skill = _make_skill(_FakeSerper(), jina)
        result = skill.run(_CTX, {})
        self.assertEqual(result.output["hit_count"], 2)

    def test_no_hit_message_has_context(self):
        jina = _FakeJina({}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina)
        text = skill.run(_CTX, {}).user_visible_text
        self.assertIn("Calça AlpineStars Halo Preta", text)
        self.assertIn("5G", text)
        self.assertIn("Alpinestars BR", text)  # lista os sites onde procurou
        self.assertIn("Mercado Livre", text)

    def test_glove_candidate_is_rejected(self):
        # Regressão: a luva Halo (mesma linha) não pode ser confundida com a calça.
        dom = "nacar.com.br"
        glove_page = "Luva Alpinestars Halo Preta tamanho 5G. Adicionar ao carrinho."
        serper = _FakeSerper(
            candidates_by_domain={
                dom: [
                    {
                        "title": "Luva Alpinestars Halo Preta",
                        "url": f"https://{dom}/luvas-moto/luva-alpinestars-halo-preta",
                        "snippet": "",
                    }
                ]
            }
        )
        jina = _FakeJina({dom: glove_page}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(serper, jina)
        result = skill.run(_CTX, {})
        # Nenhum site casa: a luva é rejeitada e os demais usam página errada.
        nacar = next(r for r in _pants_results(result.output) if r["site"] == "Nácar")
        self.assertFalse(nacar["matched_product"])
        self.assertEqual(nacar["url"], "")

    def test_correct_url_when_glove_is_second_candidate(self):
        # A calça (1º) deve vencer mesmo com a luva (2º) na lista de candidatos.
        dom = "alpinestarsbr.com.br"
        serper = _FakeSerper(
            candidates_by_domain={
                dom: [
                    {
                        "title": "Calça Alpinestars Halo Drystar Preto",
                        "url": f"https://{dom}/produto/calca-alpinestars-halo-preto",
                        "snippet": "",
                    },
                    {
                        "title": "Luva Alpinestars Halo Preta",
                        "url": f"https://{dom}/produto/luva-alpinestars-halo-preta",
                        "snippet": "",
                    },
                ]
            }
        )
        jina = _FakeJina({dom: _IN_STOCK_PAGE}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(serper, jina)
        result = skill.run(_CTX, {})
        site = next(r for r in _pants_results(result.output) if r["site"] == "Alpinestars BR")
        self.assertTrue(site["is_hit"])
        self.assertIn("calca", site["url"])
        self.assertNotIn("luva", site["url"])

    def test_playwright_fallback_finds_size(self):
        # Jina não traz a grade; o fallback (browser) encontra o 5G em estoque.
        dom = "nacar.com.br"
        jina_page = "Calça AlpineStars Halo Preta. Produtos relacionados: bone."
        rendered = "Calça AlpineStars Halo Preta tamanho 5G. Adicionar ao carrinho."

        class _FakePlaywright:
            def fetch_text(self, url):
                return rendered

        jina = _FakeJina({dom: jina_page}, default=_WRONG_PRODUCT_PAGE)
        skill = _make_skill(_FakeSerper(), jina, playwright_fetcher=_FakePlaywright())
        result = skill.run(_CTX, {})
        nacar = next(r for r in _pants_results(result.output) if r["site"] == "Nácar")
        self.assertTrue(nacar["is_hit"])

    def test_default_target(self):
        self.assertEqual(DEFAULT_QUERY, "Calça AlpineStars Halo Preta")
        self.assertEqual(DEFAULT_SIZE, "5G")

    def test_4g_equivalents_match(self):
        # Tabela oficial Alpinestars: 4G = 3XL (sem numeração Euro).
        for label in ("4G", "3XL", "XXXL"):
            page = f"Calça AlpineStars Halo Preta {label}. Adicionar ao carrinho."
            jina = _FakeJina({"alpinestarsbr.com.br": page}, default=_WRONG_PRODUCT_PAGE)
            skill = _make_skill(_FakeSerper(), jina)
            result = skill.run(_CTX, {"size": "4G"})
            self.assertEqual(result.output["hit_count"], 1, f"falhou para {label}")

    def test_5g_equivalents_match(self):
        # Tabela oficial: 5G = 4XL (sem numeração Euro).
        for label in ("5G", "4XL", "XXXXL"):
            page = f"Calça AlpineStars Halo Preta {label}. Adicionar ao carrinho."
            jina = _FakeJina({"alpinestarsbr.com.br": page}, default=_WRONG_PRODUCT_PAGE)
            skill = _make_skill(_FakeSerper(), jina)
            result = skill.run(_CTX, {"size": "5G"})
            self.assertEqual(result.output["hit_count"], 1, f"falhou para {label}")

    def test_4g_does_not_match_smaller_or_numbers(self):
        # 4G não casa tamanhos menores nem números soltos (não buscamos Euro).
        for label in ("G", "GG", "2XL", "60", "entrega em 60 dias"):
            page = f"Calça AlpineStars Halo Preta {label}. Adicionar ao carrinho."
            jina = _FakeJina({"alpinestarsbr.com.br": page}, default=_WRONG_PRODUCT_PAGE)
            skill = _make_skill(_FakeSerper(), jina)
            result = skill.run(_CTX, {"size": "4G"})
            self.assertEqual(result.output["hit_count"], 0, f"casou indevidamente com {label}")

    def test_runs_two_targets(self):
        # A skill roda calça + bolha: 2 reports no output.
        skill = _make_skill(_FakeSerper(), _FakeJina({}, default=_WRONG_PRODUCT_PAGE))
        result = skill.run(_CTX, {})
        self.assertEqual(len(result.output["reports"]), 2)
        names = [r["target"] for r in result.output["reports"]]
        self.assertIn("Calça AlpineStars Halo Preta", names[0])
        self.assertIn("Bolha", names[1])


class WindscreenTargetTests(unittest.TestCase):
    def _scraper(self, serper, jina):
        return ProductScraper(serper=serper, jina=jina, use_playwright_fallback=False)

    def test_hit_with_reference_marks_it(self):
        # Anúncio com A9708606 no título + estoque -> hit, e ref marcada.
        dom = "mercadolivre.com.br"
        serper = _FakeSerper(
            candidates_by_domain={
                dom: [
                    {
                        "title": "Parabrisa Alto Scrambler 1200 Original Triumph A9708606",
                        "url": f"https://www.{dom}/parabrisa-scrambler-a9708606",
                        "snippet": "",
                    }
                ]
            }
        )
        jina = _FakeJina({dom: "Parabrisa Scrambler. Comprar. Disponível."})
        report = self._scraper(serper, jina).scan(windscreen_target())
        ml = next(r for r in report.results if r.domain == dom)
        self.assertTrue(ml.is_hit)
        self.assertTrue(ml.reference_seen)

    def test_hit_without_reference(self):
        # Concessionária: bolha Scrambler sem o part number ainda dá hit.
        dom = "loja.triumphbh.com.br"
        serper = _FakeSerper(
            candidates_by_domain={
                dom: [
                    {
                        "title": "Para-brisa Scrambler 1200 Triumph",
                        "url": f"https://{dom}/boutique/parabrisa-scrambler-1200",
                        "snippet": "",
                    }
                ]
            }
        )
        jina = _FakeJina({dom: "Para-brisa Scrambler 1200. Comprar. Em estoque."})
        report = self._scraper(serper, jina).scan(windscreen_target())
        bh = next(r for r in report.results if r.domain == dom)
        self.assertTrue(bh.is_hit)  # hit mesmo sem A9708606
        self.assertFalse(bh.reference_seen)

    def test_rejects_other_model_windscreen(self):
        # Bolha de outro modelo (Rocket) não pode casar.
        dom = "powermoto.com.br"
        serper = _FakeSerper(
            candidates_by_domain={
                dom: [
                    {
                        "title": "Para-brisa Rocket 3 Triumph A9700999",
                        "url": f"https://www.{dom}/parabrisa-rocket-a9700999",
                        "snippet": "",
                    }
                ]
            }
        )
        jina = _FakeJina({dom: "Comprar. Disponível."})
        report = self._scraper(serper, jina).scan(windscreen_target())
        pm = next(r for r in report.results if r.domain == dom)
        self.assertFalse(pm.matched_product)


if __name__ == "__main__":
    unittest.main()
