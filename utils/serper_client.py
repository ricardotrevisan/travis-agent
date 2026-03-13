import os
from typing import Dict, List

import requests
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT = float(os.getenv("SEARCH_TIMEOUT", "10"))
DEFAULT_BASE_URL = os.getenv("SERPER_BASE_URL", "https://google.serper.dev")


class SerperClient:
    """
    Cliente simples para a Serper.dev (Google Search API unofficial).
    Centraliza configuração e tratamento de erros.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("SERPER_API_KEY") or ""
        self.base_url = base_url or DEFAULT_BASE_URL
        self.timeout = float(timeout or DEFAULT_TIMEOUT)

    def search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        if not self.api_key:
            print("[serper] SERPER_API_KEY ausente.")
            return []

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"q": query}

        try:
            resp = requests.post(
                f"{self.base_url}/search",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[serper] erro na chamada: {exc}")
            return []

        items = data.get("organic", []) or []
        results: List[Dict[str, str]] = []
        for item in items[:max_results]:
            results.append(
                {
                    "title": (item.get("title") or "").strip(),
                    "url": (item.get("link") or "").strip(),
                    "snippet": (item.get("snippet") or "").strip(),
                }
            )
        return results
