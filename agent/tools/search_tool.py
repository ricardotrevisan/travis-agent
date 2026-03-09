import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from utils.jina_fetcher import JinaFetcher
from utils.serper_client import SerperClient

from .base import Tool

load_dotenv()

SEARCH_MAX_RESULTS_ENV = int(os.getenv("SEARCH_MAX_RESULTS", "3"))
SEARCH_MAX_CHARS = int(os.getenv("SEARCH_MAX_CHARS", "600"))
SERPER_MAX_RESULTS = 5

_serper_client: Optional[SerperClient] = None
_jina_fetcher: Optional[JinaFetcher] = None


def get_serper_client() -> Optional[SerperClient]:
    global _serper_client
    if _serper_client:
        return _serper_client
    try:
        _serper_client = SerperClient()
        return _serper_client
    except Exception as exc:
        print("[search] erro ao inicializar SerperClient:", exc)
        return None


def get_jina_fetcher() -> Optional[JinaFetcher]:
    global _jina_fetcher
    if _jina_fetcher:
        return _jina_fetcher
    try:
        _jina_fetcher = JinaFetcher()
        return _jina_fetcher
    except Exception as exc:
        print("[search] erro ao inicializar JinaFetcher:", exc)
        return None


def truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


# -------------------------------
#  Unified web search
# -------------------------------
def run_web_search(query: str, max_results: int) -> Dict[str, Any]:
    max_results = min(max(max_results, 1), SERPER_MAX_RESULTS)
    serper_client = get_serper_client()
    jina_fetcher = get_jina_fetcher()

    # Tenta SERPER primeiro (Google)
    serper_results = serper_client.search(query, max_results) if serper_client else []

    if serper_results:
        enriched = []
        for r in serper_results:
            url = r.get("url", "")
            snippet = r.get("snippet") or (jina_fetcher.fetch_text(url) if jina_fetcher else "")
            snippet = truncate_text(snippet, SEARCH_MAX_CHARS)
            enriched.append(
                {
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": snippet,
                }
            )
        return {"query": query, "results": enriched}

    # Fallback: tentar capturar o conteúdo do próprio input como URL via Jina
    if jina_fetcher and (query.startswith("http://") or query.startswith("https://")):
        jina_text = jina_fetcher.fetch_text(query)
        snippet = truncate_text(jina_text, SEARCH_MAX_CHARS)
        if snippet:
            return {
                "query": query,
                "results": [
                    {
                        "title": "Conteúdo capturado via Jina",
                        "url": query,
                        "snippet": snippet,
                    }
                ],
            }

    return {"query": query, "results": "nem resultados"}


# -------------------------------
#  Tool class
# -------------------------------
class WebSearchTool(Tool):
    name = "web_search"
    description = "Buscar rapidamente na web e retornar trechos curtos das páginas encontradas."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Termos de busca ou pergunta em linguagem natural.",
            },
            "max_results": {
                "type": "integer",
                "description": "Quantidade máxima de resultados (1-5).",
                "minimum": 1,
                "maximum": 5,
                "default": 1,
            },
        },
        "required": ["query", "max_results"],
        "additionalProperties": False,
    }

    def run(self, args: Dict[str, Any]) -> Any:
        query = args.get("query") or ""
        print(query)
        max_results = int(args.get("max_results") or SEARCH_MAX_RESULTS_ENV)
        return run_web_search(query, max_results)
