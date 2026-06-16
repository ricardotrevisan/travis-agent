import os

from playwright.sync_api import sync_playwright

DEFAULT_TIMEOUT_MS = int(float(os.getenv("PLAYWRIGHT_TIMEOUT", "30")) * 1000)


class PlaywrightFetcher:
    """Renderiza a página com Chromium headless e devolve o texto visível.

    Usado como fallback quando o gateway estático (Jina) não traz a grade de
    tamanhos, que em várias lojas carrega via JavaScript.
    """

    def __init__(self, timeout_ms: int | None = None) -> None:
        self.timeout_ms = int(timeout_ms or DEFAULT_TIMEOUT_MS)

    def fetch_text(self, url: str) -> str:
        if not url:
            return ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    # ignore_https_errors: algumas concessionárias têm cert SSL
                    # vencido (ex: lojas Triumph) — ainda queremos ler a página.
                    context = browser.new_context(ignore_https_errors=True)
                    page = context.new_page()
                    page.goto(url, timeout=self.timeout_ms)
                    # networkidle dá tempo para a grade carregar via JS.
                    try:
                        page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
                    except Exception:
                        # Algumas lojas nunca ficam idle; segue com o que houver.
                        page.wait_for_load_state("domcontentloaded")
                    return page.inner_text("body")
                finally:
                    browser.close()
        except Exception as exc:
            return f"[erro playwright: {exc}]"
