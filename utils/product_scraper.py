import re
import unicodedata
from dataclasses import dataclass, field

from utils.jina_fetcher import JinaFetcher
from utils.serper_client import SerperClient

# Lojas que vendem equipamento de piloto (calça Halo).
PANTS_SITES: list[tuple[str, str]] = [
    ("Alpinestars BR", "alpinestarsbr.com.br"),
    ("Zelão", "zelao.com.br"),
    ("Sacramento", "sacramento.com.br"),
    ("Nácar", "nacar.com.br"),
    ("Spinelli Motos", "spinellimotos.com.br"),
    ("Mercado Livre", "mercadolivre.com.br"),
    ("Grid Motors", "gridmotors.com.br"),
    ("Superbike Shop", "superbikeshop.com.br"),
    ("MotoX Wear", "motoxwear.com.br"),
    ("Web Riders", "webriders.com.br"),
]

# Concessionárias Triumph + Power Moto + ML (peça original: bolha A9708606).
WINDSCREEN_SITES: list[tuple[str, str]] = [
    ("Power Moto", "powermoto.com.br"),
    ("Osten Triumph", "triumphosten.com.br"),
    ("Triumph CWB", "triumphcwb.com.br"),
    ("Triumph Rio Preto", "triumphriopreto.com.br"),
    ("Triumph BH", "loja.triumphbh.com.br"),
    ("Triumph BR", "triumphmotorcycles.com.br"),
    ("Mercado Livre", "mercadolivre.com.br"),
]

# Compat: nome antigo aponta para as lojas da calça.
MONITORED_SITES = PANTS_SITES

# Sinais textuais de que o item está disponível para compra.
_IN_STOCK_HINTS = (
    "adicionar ao carrinho",
    "comprar agora",
    "comprar",
    "em estoque",
    "disponivel",
    "disponibilidade imediata",
    "adicionar a sacola",
)
# Sinais textuais de indisponibilidade (têm prioridade sobre os de estoque).
_OUT_OF_STOCK_HINTS = (
    "esgotado",
    "indisponivel",
    "produto indisponivel",
    "avise-me quando chegar",
    "sem estoque",
    "fora de estoque",
)

# Termos que DEVEM aparecer na URL/título do candidato (todos) para confirmar o
# produto certo. Validamos pela URL/título — não pelo corpo, que vem poluído de
# menu e "produtos relacionados" (a luva aparece na página da calça e vice-versa).
_REQUIRED_PRODUCT_TERMS = ("calca", "halo")
# Tipos de produto a EXCLUIR (acessórios da mesma linha Halo que confundem).
_EXCLUDED_PRODUCT_TERMS = (
    "luva",
    "jaqueta",
    "bota",
    "colete",
    "mochila",
    "bag",
    "capacete",
    "camiseta",
    "bone",
    "mascara",
    "oculos",
)


def _normalize(text: str) -> str:
    """Minúsculas + sem acento, para casar termos de forma robusta."""
    lowered = (text or "").lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Equivalências de tamanho conforme a tabela oficial Alpinestars (grade de calças).
# Mapeamos só Código BR <-> Internacional (XL). NÃO usamos numeração Euro:
# número solto na página (preço/prazo) gera falso positivo demais.
#   3G = 2XL ; 4G = 3XL ; 5G = 4XL ; 6G = 5XL
# Atenção: NÃO segue a contagem genérica de "G" (4G ≠ GGGG/XXXL aqui).
# O tamanho principal pedido (ex: "4g") é sempre o primeiro token.
_SIZE_EQUIV: dict[str, list[str]] = {
    "3g": ["3g", "2xl", "xxl"],
    "4g": ["4g", "3xl", "xxxl"],
    "5g": ["5g", "4xl", "xxxxl"],
    "6g": ["6g", "5xl", "xxxxxl"],
}


def _size_tokens(size: str) -> list[str]:
    """Variações de como um tamanho aparece nas páginas (4G, GGGG, 3XL, etc.)."""
    norm = _normalize(size).strip()
    if not norm:
        return []
    compact = norm.replace(" ", "")
    tokens: list[str] = []
    # Tamanho principal primeiro (preserva a referência pedida).
    for token in _SIZE_EQUIV.get(compact, [compact]):
        if token not in tokens:
            tokens.append(token)
    # Inclui também a forma como veio (com/sem espaço), sem duplicar.
    for raw in (norm, compact):
        if raw and raw not in tokens:
            tokens.append(raw)
    return tokens


def _contains_size(text_norm: str, size: str) -> bool:
    for token in _size_tokens(size):
        # \b não casa bem com "5g" colado; usamos lookaround simples.
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text_norm):
            return True
    return False


def _candidate_is_target(
    title: str, url: str, required: tuple[str, ...], excluded: tuple[str, ...]
) -> bool:
    """Confirma o produto pelo título+URL do candidato (não pelo corpo).

    Exige todos os termos `required` e rejeita qualquer `excluded`, evitando que
    acessórios da mesma linha (luva Halo etc.) sejam confundidos com o alvo.
    Para referência (part number), `required` inclui o próprio código.
    """
    signal = _normalize(f"{title} {url}")
    if any(bad in signal for bad in excluded):
        return False
    return all(term in signal for term in required)


@dataclass
class MonitorTarget:
    """Configuração de um produto a monitorar.

    criterion = "size": valida o tamanho (variant) via tabela de equivalências.
    criterion = "name": basta confirmar o produto pelo nome (required_terms);
        não há variante obrigatória. A referência (part number), se presente,
        é registrada como sinal desejável, mas NÃO é exigida para o hit.
    """

    name: str  # rótulo legível no alerta (ex: "Calça Halo Preta")
    query: str  # termos de busca (Serper)
    sites: list[tuple[str, str]]  # [(rótulo, domínio), ...]
    required_terms: tuple[str, ...]  # termos obrigatórios no título/URL do candidato
    excluded_terms: tuple[str, ...] = ()  # tipos a rejeitar (luva, jaqueta...)
    criterion: str = "size"  # "size" | "name"
    variant: str = ""  # tamanho (5G) quando criterion="size"
    reference: str = ""  # part number desejável (A9708606), nunca obrigatório
    enabled: bool = True  # False = config preservada, mas fora do monitoramento ativo

    @property
    def variant_label(self) -> str:
        """Texto do critério para o alerta."""
        return self.variant if self.criterion == "size" else self.reference


@dataclass
class SiteResult:
    site: str
    domain: str
    url: str = ""
    title: str = ""
    matched_product: bool = False
    variant_available: bool = False  # tamanho encontrado (ou N/A p/ criterion=name)
    reference_seen: bool = False  # part number desejável visto (informativo)
    in_stock: bool = False
    note: str = ""

    @property
    def is_hit(self) -> bool:
        """Alerta quando produto certo, critério atendido e em estoque."""
        return self.matched_product and self.variant_available and self.in_stock


@dataclass
class MonitorReport:
    target_name: str
    query: str
    variant: str
    criterion: str = "size"
    results: list[SiteResult] = field(default_factory=list)

    @property
    def hits(self) -> list[SiteResult]:
        return [r for r in self.results if r.is_hit]


class ProductScraper:
    """Busca um produto+tamanho nos sites monitorados via Serper + Jina."""

    def __init__(
        self,
        serper: SerperClient | None = None,
        jina: JinaFetcher | None = None,
        max_candidates_per_site: int = 2,
        playwright_fetcher=None,
        use_playwright_fallback: bool = True,
    ) -> None:
        self.serper = serper or SerperClient()
        self.jina = jina or JinaFetcher()
        self.max_candidates_per_site = max_candidates_per_site
        self.use_playwright_fallback = use_playwright_fallback
        # Lazy: só instancia o browser fetcher se/quando for necessário.
        self._playwright_fetcher = playwright_fetcher

    def _get_playwright_fetcher(self):
        if self._playwright_fetcher is None:
            from utils.playwright_fetcher import PlaywrightFetcher

            self._playwright_fetcher = PlaywrightFetcher()
        return self._playwright_fetcher

    def _evaluate_page(
        self, target: "MonitorTarget", page_text: str, candidate_signal: str = ""
    ) -> tuple[bool, bool, bool]:
        """Avalia a página: (variante_ok, em_estoque, referência_vista).

        Produto já confirmado pelo título/URL do candidato. Para criterion="name",
        não há variante obrigatória (variante_ok = True) e a referência é só um
        sinal informativo. Para "size", valida o tamanho na página.
        """
        text_norm = _normalize(page_text)
        signal_norm = _normalize(candidate_signal)
        ref = _normalize(target.reference)
        reference_seen = bool(ref) and (ref in text_norm or ref in signal_norm)
        if target.criterion == "name":
            variant_ok = True
        else:
            variant_ok = _contains_size(text_norm, target.variant)
        out_of_stock = any(h in text_norm for h in _OUT_OF_STOCK_HINTS)
        in_stock = (not out_of_stock) and any(h in text_norm for h in _IN_STOCK_HINTS)
        return variant_ok, in_stock, reference_seen

    def _scan_site(self, target: "MonitorTarget", site: str, domain: str) -> SiteResult:
        result = SiteResult(site=site, domain=domain)
        try:
            candidates = self.serper.search(
                f"{target.query} site:{domain}", max_results=self.max_candidates_per_site
            )
        except Exception as exc:
            result.note = f"erro na busca: {exc}"
            return result

        if not candidates:
            result.note = "nenhum candidato na busca"
            return result

        saw_target = False
        for candidate in candidates:
            url = (candidate.get("url") or "").strip()
            if not url:
                continue
            title = (candidate.get("title") or "").strip()
            # Filtra pelo título/URL: só o produto-alvo, nunca acessórios.
            if not _candidate_is_target(
                title, url, target.required_terms, target.excluded_terms
            ):
                continue
            saw_target = True
            signal = f"{title} {url}"
            page_text = self.jina.fetch_text(url)
            variant_ok, in_stock, ref_seen = self._evaluate_page(target, page_text, signal)

            # Fallback: variação E sinais de estoque costumam carregar via JS e
            # não aparecem no texto estático da Jina. Se faltar qualquer um dos
            # dois, renderizamos a página com browser e reavaliamos.
            if self.use_playwright_fallback and not (variant_ok and in_stock):
                rendered = self._get_playwright_fetcher().fetch_text(url)
                if rendered and not rendered.startswith("[erro playwright"):
                    pw_variant_ok, pw_in_stock, pw_ref = self._evaluate_page(
                        target, rendered, signal
                    )
                    # Mantém o que a Jina já confirmou; o browser só acrescenta.
                    variant_ok = variant_ok or pw_variant_ok
                    in_stock = in_stock or pw_in_stock
                    ref_seen = ref_seen or pw_ref
                    result.note = "variação/estoque via playwright"

            # Registra este candidato como o produto confirmado deste site.
            result.url = url
            result.title = title
            result.matched_product = True
            result.variant_available = variant_ok
            result.reference_seen = ref_seen
            result.in_stock = in_stock
            if result.is_hit:
                # Melhor caso possível; não precisa olhar mais candidatos.
                return result

        if not saw_target:
            result.note = "produto-alvo não encontrado entre os candidatos"
        return result

    def scan(self, target: "MonitorTarget") -> MonitorReport:
        report = MonitorReport(
            target_name=target.name,
            query=target.query,
            variant=target.variant_label,
            criterion=target.criterion,
        )
        for site, domain in target.sites:
            report.results.append(self._scan_site(target, site, domain))
        return report


# ---------------------------------------------------------------------------
# Alvos concretos monitorados.
# ---------------------------------------------------------------------------
def pants_target(size: str, enabled: bool = True) -> MonitorTarget:
    """Calça Alpinestars Halo Preta, validada por tamanho."""
    return MonitorTarget(
        name="Calça AlpineStars Halo Preta",
        query="Calça AlpineStars Halo Preta",
        sites=PANTS_SITES,
        required_terms=_REQUIRED_PRODUCT_TERMS,
        excluded_terms=_EXCLUDED_PRODUCT_TERMS,
        criterion="size",
        variant=size,
        enabled=enabled,
    )


def windscreen_target() -> MonitorTarget:
    """Bolha/windscreen Triumph Scrambler 1200 (peça original A9708606).

    Confirma o produto pelo NOME (scrambler) no título/URL e rejeita outros
    modelos e acessórios não-bolha. A referência A9708606 é DESEJÁVEL (marcada
    quando presente), mas NÃO obrigatória — assim as concessionárias, que
    raramente citam o part number, também disparam o alerta.
    """
    return MonitorTarget(
        name="Bolha Triumph Scrambler 1200",
        # Query curta: com o operador site:, queries longas zeram resultados em
        # vários domínios (testado). "parabrisa Scrambler" cobre ML + boutiques.
        query="parabrisa Scrambler",
        sites=WINDSCREEN_SITES,
        required_terms=("scrambler",),
        excluded_terms=(
            # Outros modelos Triumph.
            "rocket",
            "tiger",
            "bonneville",
            "trident",
            "speed twin",
            "street twin",
            "900",
            "400",
            # Acessórios da Scrambler que não são a bolha.
            "banco",
            "escapamento",
            "manopla",
            "pedaleira",
            "protetor",
            "alforje",
            "bagageiro",
            "farol",
            "retrovisor",
        ),
        criterion="name",
        reference="A9708606",
    )
