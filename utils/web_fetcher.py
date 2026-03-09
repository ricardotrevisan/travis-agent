import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlparse
import argparse

from bs4 import BeautifulSoup
from readability import Document
from playwright.async_api import async_playwright

class WebFetcher:
    def __init__(self):
        pass        

    @staticmethod
    def save_html(content: str, filename: str = "page.html"):
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)

    @staticmethod
    async def fetch_article_playwright(url: str) -> Dict:
        """Load page (including SPA), extract main text and metadata."""

        if not url:
            raise ValueError("URL not provided. Set the address you want to fetch.")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)  # run headless
            page = await browser.new_page()
            await page.goto(url, timeout=60000)  # up to 60s to avoid timeout
            await page.wait_for_load_state("domcontentloaded")


            html = await page.content()
            await browser.close()

        soup = BeautifulSoup(html, "html.parser")

        # Title
        title_tag = soup.find("meta", property="og:title") or soup.find("title")
        title = title_tag.get("content") if title_tag and title_tag.has_attr("content") else title_tag.string

        # Summary
        desc_tag = soup.find("meta", {"name": "description"})
        if desc_tag and desc_tag.get("content"):
            summary = desc_tag.get("content")
        else:
            paragraphs = soup.find_all("p")
            summary = " ".join(p.get_text() for p in paragraphs[:2]) if paragraphs else ""

        # Published date
        date_tag = (soup.find("meta", property="article:published_time") or
                    soup.find("meta", {"name": "date"}))
        if date_tag and date_tag.get("content"):
            try:
                published = datetime.fromisoformat(date_tag["content"].replace("Z", "+00:00"))
            except:
                published = datetime.now(timezone.utc)
        else:
            published = datetime.now(timezone.utc)

        domain = urlparse(url).netloc
        source_name = domain.split(".")[-2].capitalize() if domain else "Unknown source"
        # print(soup)
        return {
            "title": title.strip() if title else "",
            "summary": summary.strip() if summary else "",
            "link": url,
            "published": published.isoformat(),
            "source": source_name,
            "content": soup
        }
    
async def _main() -> None:
    parser = argparse.ArgumentParser(description="Fetch article metadata from a SPA URL.")
    parser.add_argument("url", help="Article URL to load")
    args = parser.parse_args()
    
    fetcher = WebFetcher()
    result = await fetcher.fetch_article_playwright(args.url)
    print(result)
    WebFetcher.save_html(str(result["content"]), "article.html")


if __name__ == "__main__":
    asyncio.run(_main())
