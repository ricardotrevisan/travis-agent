import os
import asyncio
from typing import Any, Dict, Optional, List
from utils.text_summary import split_sentences, build_word_freq, summarize_text_locally

from bs4 import BeautifulSoup

from .base import Tool
from utils.web_fetcher import WebFetcher

LOCAL_SUMMARY_MAX_SENTENCES = int(os.getenv("LOCAL_SUMMARY_MAX_SENTENCES", "5"))
LOCAL_SUMMARY_MAX_CHARS = int(os.getenv("LOCAL_SUMMARY_MAX_CHARS", "1500"))


_web_fetcher: Optional[WebFetcher] = None


def get_web_fetcher() -> Optional[WebFetcher]:
    global _web_fetcher
    if _web_fetcher:
        return _web_fetcher
    try:
        _web_fetcher = WebFetcher()
        return _web_fetcher
    except Exception as exc:
        print("[summarize] erro ao inicializar WebFetcher:", exc)
        return None


def truncate_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def split_sentences(text: str) -> List[str]:
    import re

    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]



def fetch_article_summary(url: str, max_chars: int) -> Dict[str, Any]:
    """Usa Playwright via WebFetcher para obter HTML e resumo."""
    fetcher = get_web_fetcher()
    if not fetcher:
        raise RuntimeError("Fetcher Playwright não pôde ser inicializado.")

    result = asyncio.run(fetcher.fetch_article_playwright(url))
    raw_html = ""
    plain_text = ""
    if result.get("content"):
        raw_html = str(result["content"])
        try:
            soup_obj = (
                result["content"]
                if isinstance(result["content"], BeautifulSoup)
                else BeautifulSoup(raw_html, "html.parser")
            )
            plain_text = soup_obj.get_text(" ", strip=True)
        except Exception:
            plain_text = ""

    return {
        "title": (result.get("title") or "").strip(),
        "summary": truncate_text(result.get("summary", "") or "", max_chars),
        "link": result.get("link") or url,
        "published": result.get("published"),
        "source": result.get("source"),
        "raw_html": raw_html,
        "local_summary": summarize_text_locally(plain_text, LOCAL_SUMMARY_MAX_SENTENCES, max_chars) if plain_text else "" ,
    }


class SummarizeURLTool(Tool):
    name = "summarize_url"
    description = "Carregar uma página web (incluindo SPAs) e retornar título, resumo, data e HTML bruto."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL completa da página a ser resumida.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Tamanho máximo do resumo retornado.",
                "minimum": 100,
                "maximum": 4000,
                "default": 2000,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def run(self, args: Dict[str, Any]) -> Any:
        url = args.get("url") or ""
        if not url:
            return {"error": "URL vazia para sumarização."}
        max_chars = int(args.get("max_chars") or 2000)
        return fetch_article_summary(url, max_chars)
