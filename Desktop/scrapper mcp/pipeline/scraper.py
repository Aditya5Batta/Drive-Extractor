"""
pipeline.scraper — Scraper + BatchProcessor + PageResult.

Scraper wraps HTTPFetcher with MIME dispatch (HTML vs PDF). BatchProcessor
runs N URLs × M keywords concurrently and returns ranked hits. Pure glue
code — no chemistry logic here.
"""
from __future__ import annotations
import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Any

from config.settings import CONFIG
from core.http import HTTPFetcher
from core.html import HTMLExtractor
from core.pdf import PDFExtractor, is_pdf_content
from core.keywords import KeywordSearcher, KeywordHit, KeywordSearchResult


@dataclass
class PageResult:
    url: str
    ok: bool
    kind: str                        # "html" | "pdf" | "error"
    title: str = ""
    text: str = ""
    page_count: int = 0
    char_count: int = 0
    status: int = 0
    content_type: str = ""
    error: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "kind": self.kind,
            "title": self.title,
            "char_count": self.char_count,
            "page_count": self.page_count,
            "status": self.status,
            "content_type": self.content_type,
            "error": self.error,
        }


class Scraper:
    """High-level: fetch URL → detect type → extract clean text + links."""

    def __init__(self, fetcher: HTTPFetcher, cfg: ScraperConfig = CONFIG) -> None:
        self.fetcher = fetcher
        self.cfg = cfg

    async def scrape(self, url: str, want_links: bool = False) -> PageResult:
        data, ct, status, err = await self.fetcher.fetch(url)

        if data is None:
            return PageResult(
                url=url, ok=False, kind="error",
                status=status, content_type=ct, error=err or "fetch failed",
            )

        # PDF?
        if is_pdf_content(ct, data, url):
            if len(data) > self.cfg.max_pdf_bytes:
                return PageResult(
                    url=url, ok=False, kind="error",
                    status=status, content_type=ct,
                    error=f"PDF too large ({len(data)} bytes, limit {self.cfg.max_pdf_bytes})",
                )
            text, pages, pdf_err = PDFExtractor.extract(data)
            truncated = text[: self.cfg.max_content_chars]
            return PageResult(
                url=url,
                ok=bool(truncated) and pdf_err is None,
                kind="pdf",
                title=url.rsplit("/", 1)[-1],
                text=truncated,
                page_count=pages,
                char_count=len(truncated),
                status=status,
                content_type=ct or "application/pdf",
                error=pdf_err,
            )

        # HTML (or XML / text)
        try:
            text = HTMLExtractor.extract_text(data, url)
            title = HTMLExtractor.extract_title(data)
            links = HTMLExtractor.extract_links(data, url) if want_links else []
        except Exception as e:  # noqa: BLE001
            return PageResult(
                url=url, ok=False, kind="error",
                status=status, content_type=ct, error=f"html parse: {e}",
            )

        truncated = text[: self.cfg.max_content_chars]
        return PageResult(
            url=url,
            ok=bool(truncated),
            kind="html",
            title=title,
            text=truncated,
            char_count=len(truncated),
            status=status,
            content_type=ct,
            links=links,
            error=None if truncated else "empty extraction",
        )


# ════════════════════════════════════════════════════════════════════════════
#  BATCH PROCESSOR  — N URLs × M keywords in parallel
# ════════════════════════════════════════════════════════════════════════════

class BatchProcessor:
    """Parallel scraping of many URLs with per-URL keyword scoring."""

    def __init__(self, scraper: Scraper, cfg: ScraperConfig = CONFIG) -> None:
        self.scraper = scraper
        self.cfg = cfg

    async def run(
        self,
        urls: list[str],
        keywords: list[str],
        max_hits_per_keyword: int = 3,
        min_relevance: float = 0.0,
    ) -> dict[str, Any]:
        sem = asyncio.Semaphore(self.cfg.max_concurrent)

        async def one(u: str) -> dict[str, Any]:
            async with sem:
                page = await self.scraper.scrape(u, want_links=False)
                if not page.ok or not page.text:
                    return {
                        "url": u,
                        "ok": False,
                        "kind": page.kind,
                        "error": page.error,
                        "status": page.status,
                    }
                ksr = KeywordSearcher.search(
                    page.text, keywords,
                    context_chars=self.cfg.context_chars,
                    max_hits_per_keyword=max_hits_per_keyword,
                )
                return {
                    "url": u,
                    "ok": True,
                    "kind": page.kind,
                    "title": page.title,
                    "char_count": page.char_count,
                    "page_count": page.page_count,
                    "status": page.status,
                    **ksr.to_dict(),
                }

        results = await asyncio.gather(*(one(u) for u in urls), return_exceptions=False)

        # Filter by min relevance and rank
        ranked = [r for r in results if r.get("ok") and r.get("relevance_score", 0) >= min_relevance]
        ranked.sort(key=lambda r: (r.get("relevance_score", 0), r.get("total_hits", 0)), reverse=True)
        failed = [r for r in results if not r.get("ok")]

        return {
            "requested": len(urls),
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
            "keywords": keywords,
            "ranked_results": ranked,
            "failures": failed,
        }


# ════════════════════════════════════════════════════════════════════════════
#  OUTPUT FORMATTING  — markdown renderers for MCP text responses
# ════════════════════════════════════════════════════════════════════════════
