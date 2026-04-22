"""
core.http — Single async HTTP client with retry and caching.

Every network call in the package goes through HTTPFetcher.fetch(). PDF
detection is encapsulated in is_pdf_content() so callers never have to sniff
headers themselves.
"""
from __future__ import annotations
import asyncio
import hashlib
from typing import Any
import httpx

from config.settings import CONFIG, ScraperConfig


class HTTPFetcher:
    """Async HTTP client with retries, sane headers, and per-URL cache."""

    def __init__(self, cfg: ScraperConfig = CONFIG) -> None:
        self.cfg = cfg
        self._cache: dict[str, tuple[bytes, str, int]] = {}  # url → (bytes, content_type, status)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.cfg.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self.cfg.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                limits=httpx.Limits(max_connections=self.cfg.max_concurrent * 2),
            )
        return self._client

    async def fetch(self, url: str) -> tuple[bytes | None, str, int, str | None]:
        """
        Returns (content_bytes, content_type, status_code, error_msg).
        content_bytes is None on failure.
        """
        if url in self._cache:
            b, ct, st = self._cache[url]
            return b, ct, st, None

        client = await self._get_client()
        last_err: str | None = None

        for attempt in range(self.cfg.max_retries + 1):
            try:
                r = await client.get(url)
                ct = (r.headers.get("content-type") or "").lower()
                data = r.content
                self._cache[url] = (data, ct, r.status_code)
                if r.status_code >= 400:
                    return data, ct, r.status_code, f"HTTP {r.status_code}"
                return data, ct, r.status_code, None
            except httpx.TimeoutException:
                last_err = f"timeout after {self.cfg.timeout}s"
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            if attempt < self.cfg.max_retries:
                await asyncio.sleep(1.5 * (attempt + 1))

        return None, "", 0, last_err

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ════════════════════════════════════════════════════════════════════════════
#  HTML EXTRACTOR  — clean main text + outgoing links
# ════════════════════════════════════════════════════════════════════════════
