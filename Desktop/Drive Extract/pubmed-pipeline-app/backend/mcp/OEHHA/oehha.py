"""
OEHHA (California Office of Environmental Health Hazard Assessment) scraper.

Why this scraper looks different from the other 7
==================================================
The OEHHA website (oehha.ca.gov) sits behind Incapsula bot protection.
Plain GETs to HTML pages get a 951-byte Incapsula challenge instead of
the actual page — even with full Chrome-like headers and HTTP/2.

BUT — OEHHA's actual PDF files at
    https://oehha.ca.gov/sites/default/files/...
    https://oehha.ca.gov/media/downloads/...
are *not* bot-protected.  Once we have a PDF's URL, we can download it
directly with no challenge.

So we discover PDF URLs the same way a human would: by searching.  We use
the `ddgs` library, which queries DuckDuckGo (and falls back across other
engines automatically) with the operator chain
    site:oehha.ca.gov  <chemical>  filetype:pdf
returning ~10 OEHHA-hosted PDFs per query, each with title and snippet.
This matches what the user gets by typing the chemical name into OEHHA's
own search box (which is itself a Google CSE) — but lets us bypass
Incapsula entirely.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx
from _ddg import search as _ddg_search   # shared lock + retry across OEHHA/ATSDR

TIMEOUT = 30.0
# Full Chrome-like headers — OEHHA's CDN occasionally returns an Incapsula
# JS challenge instead of the PDF when the request looks bot-like.  Mimicking
# a real browser request (Sec-* hints, full Accept-Encoding) avoids that.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/pdf,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua":         '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile":   "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest":     "document",
    "Sec-Fetch-Mode":     "navigate",
    "Sec-Fetch-Site":     "none",
    "Sec-Fetch-User":     "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer":            "https://oehha.ca.gov/",
}


def _canonical_pdf_url(url: str) -> str:
    """
    OEHHA serves the same PDF from two URL paths:
        oehha.ca.gov/media/downloads/crnr/benzenerelsjune2014.pdf            (short)
        oehha.ca.gov/sites/default/files/media/downloads/crnr/...june2014.pdf (long)
    Both 200 OK with the same content.  This helper normalises both forms
    to the same string so dedup catches the duplicate.
    """
    u = url.lower()
    u = u.replace("://www.", "://")                     # strip leading www.
    u = u.replace("/sites/default/files/", "/")         # strip drupal prefix
    u = u.split("?", 1)[0].split("#", 1)[0]             # strip ?query and #frag
    return u


async def search(query: str, max_results: int = 10) -> dict:
    """
    Search OEHHA-hosted PDFs via the `ddgs` library + `site:` operator.
    Returns the standard pipeline shape so the generic _run_pipeline driver
    can consume it.
    """
    ddg_query = f"site:oehha.ca.gov {query} filetype:pdf"
    api_url   = f"https://duckduckgo.com/?q={ddg_query.replace(' ', '+')}"

    t0 = time.monotonic()
    try:
        # Shared OEHHA/ATSDR DDG helper — single global lock + retry.
        raw = await _ddg_search(ddg_query, min(max(1, max_results), 30))
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": f"{type(e).__name__}: {e}", "papers": [],
        }
    response_time_ms = int((time.monotonic() - t0) * 1000)

    papers:        list[dict] = []
    seen_canonical: set[str]   = set()

    for hit in raw:
        url = (hit.get("href") or "").strip()
        # Sanity: must be on oehha.ca.gov AND end in .pdf
        if "oehha.ca.gov" not in url.lower():
            continue
        if ".pdf" not in url.lower():
            continue
        # Dedup by canonical URL — OEHHA serves the same PDF from two paths
        canon = _canonical_pdf_url(url)
        if canon in seen_canonical:
            continue
        seen_canonical.add(canon)

        title = (hit.get("title") or "").strip()
        # Some search engines prefix file results with "PDF " — strip it
        if title.upper().startswith("PDF "):
            title = title[4:].strip()
        snippet = (hit.get("body") or "").strip()[:400]

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          snippet,
            "authors":           [],
            "journal":           "OEHHA",
            "year":              "",
            "epmc_url":          url,
            "pdf_urls":          [url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })
        if len(papers) >= max_results:
            break

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": len(papers),
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Download a single OEHHA PDF.  No fallbacks — one URL per document."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None, "pdf_source": None,
                "file_size_kb": None, "attempts": []}

    url = pdf_urls[0]
    attempt = {
        "url": url, "attempt_no": 1,
        "http_status": None, "content_type": None, "is_pdf": False,
        "success": False, "file_size_kb": None, "error": None,
        "attempted_at": datetime.utcnow(),
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers=HEADERS) as c:
            # Warm-up GET to oehha.ca.gov — sets the Incapsula cookie that
            # subsequent PDF requests need to bypass the JS challenge.
            try:
                await c.get("https://oehha.ca.gov/", timeout=10)
            except Exception:
                pass
            r = await c.get(url)
            # If we got HTML instead of PDF (Incapsula), retry once with the
            # cookies now in the client.
            ct = r.headers.get("content-type", "")
            is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"
            if r.status_code == 200 and not is_pdf and "Incapsula" in r.text[:1500]:
                await asyncio.sleep(1.5)
                r = await c.get(url)
                ct = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

        attempt["http_status"]  = r.status_code
        attempt["content_type"] = ct[:128]
        attempt["is_pdf"]       = is_pdf

        if r.status_code == 200 and is_pdf:
            stem = Path(unquote(urlparse(url).path)).stem or "oehha_doc"
            stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
            path = pdf_dir / f"oehha_{stem}.pdf"
            path.write_bytes(r.content)
            size_kb = len(r.content) // 1024
            attempt["success"]      = True
            attempt["file_size_kb"] = size_kb
            return {"status": "success", "pdf_path": str(path), "pdf_source": url,
                    "file_size_kb": size_kb, "attempts": [attempt]}
    except Exception as e:
        attempt["error"] = f"{type(e).__name__}: {e}"

    return {"status": "failed", "pdf_path": None, "pdf_source": None,
            "file_size_kb": None, "attempts": [attempt]}
