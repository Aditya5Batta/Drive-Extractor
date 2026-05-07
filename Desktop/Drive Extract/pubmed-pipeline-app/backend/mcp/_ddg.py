"""
Shared DuckDuckGo helper used by the OEHHA and ATSDR scrapers.

Both scrapers call ddgs to issue `site:<host> <query> [filetype:pdf]` searches.
When the dashboard fires Run All across 9 databases in parallel, both DDG-
backed scrapers hit DuckDuckGo at the same instant and DDG aggressively rate-
limits — returning the homepage 202 / "No results found." for whichever one
landed second.

This module gives them a single shared async lock plus a robust retry loop
with backoff [0, 2, 6, 14] s.  Net effect:
  • DDG requests are serialised across the whole backend process, so OEHHA
    and ATSDR never race each other for DDG quota.
  • Each request retries on empty/exception so transient throttling is
    survived rather than silently turning into a 0-result run.
"""
from __future__ import annotations
import asyncio

from ddgs import DDGS

# One process-wide lock — every DDG call goes through it.
_LOCK = asyncio.Lock()

# Backoff schedule (seconds before each attempt, NOT total elapsed)
_BACKOFFS = (0.0, 2.0, 6.0, 14.0)


def _try_once_sync(query: str, n: int) -> list[dict]:
    """Single ddgs.text() call — try DDG backend first, then auto."""
    with DDGS() as d:
        for backend in ("ddg", "auto"):
            try:
                results = list(d.text(query, max_results=n, backend=backend))
                if results:
                    return results
            except Exception:
                continue
    return []


async def search(query: str, max_results: int) -> list[dict]:
    """
    Run a DuckDuckGo text search with serialisation + retry.
    Returns the raw ddgs results list (each item: title/href/body).
    Raises RuntimeError after all retries fail — caller decides how to
    surface the error to the pipeline.
    """
    async with _LOCK:
        last_err: str = ""
        for delay in _BACKOFFS:
            if delay:
                await asyncio.sleep(delay)
            try:
                results = await asyncio.to_thread(_try_once_sync, query, max_results)
                if results:
                    return results
                last_err = "no results (likely rate-limited)"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        raise RuntimeError(f"DuckDuckGo search exhausted retries — {last_err}")
