import os

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "10"))
DEFAULT_BASE_URL = os.getenv("JINA_BASE_URL", "https://r.jina.ai")


class JinaFetcher:
    """Busca conteúdo de páginas como texto simples via gateway r.jina.ai."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = base_url or DEFAULT_BASE_URL
        self.timeout = float(timeout or DEFAULT_TIMEOUT)

    def fetch_text(self, url: str) -> str:
        if not url:
            return ""

        gateway_url = f"{self.base_url.rstrip('/')}/{url}"
        try:
            resp = requests.get(gateway_url, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.text
            return f"[jina-ai] status {resp.status_code}"
        except Exception as exc:
            return f"[erro ao buscar conteudo: {exc}]"
