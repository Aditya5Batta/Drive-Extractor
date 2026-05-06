"""
ECHA (European Chemicals Agency) document scraper.

How the website's "Has PDF" filter works:
  https://echa.europa.eu/search?q=<chem>&type=com.liferay.document.library.kernel.model.DLFileEntry
  → restricts results to documents (PDFs) only — every hit is a direct PDF URL.

Each search result has a single direct PDF URL — no fallbacks needed.
"""
from __future__ import annotations
import asyncio, html, re, time
from datetime import datetime
from pathlib import Path

import httpx

SEARCH_URL = "https://echa.europa.eu/search"
DOC_TYPE   = "com.liferay.document.library.kernel.model.DLFileEntry"
TIMEOUT    = 30.0
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# Match a search result <li> block.  Each block contains:
#   • a download link  <a href="...PDF" download class="search-result-link">FILE.pdf</a>
#   • a "Modified date: DD-MMM-YYYY HH:MM" line
#   • a snippet line in <span class="subtext-item">…</span>
ITEM_RE     = re.compile(r'<li class="list-group-item[^"]*"[^>]*>(.*?)</li>', re.S)
LINK_RE     = re.compile(
    r'<a\s+href="([^"]+)"\s+download[^>]*class="search-result-link"[^>]*>\s*(.+?)\s*</a>', re.S)
DATE_RE     = re.compile(r'Modified date:\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4})')
SNIPPET_RE  = re.compile(
    r'<p class="list-group-subtext">.*?<span class="subtext-item">(.*?)</span>', re.S)
TAG_STRIP   = re.compile(r'<[^>]+>')


def _clean(text: str) -> str:
    return html.unescape(TAG_STRIP.sub("", text)).strip()


async def search(query: str, max_results: int = 20) -> dict:
    """
    Search ECHA for documents matching `query`.  Returns the standard pipeline
    shape so the generic _run_pipeline driver can consume it directly.
    """
    params = {"q": query, "type": DOC_TYPE}
    api_url = str(httpx.Request("GET", SEARCH_URL, params=params).url)

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(SEARCH_URL, params=params)
        r.raise_for_status()
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": f"{type(e).__name__}: {e}", "papers": [],
        }
    response_time_ms = int((time.monotonic() - t0) * 1000)

    items = ITEM_RE.findall(r.text)
    papers: list[dict] = []
    seen_urls: set[str] = set()

    for block in items:
        link = LINK_RE.search(block)
        if not link:
            continue
        pdf_url, raw_title = link.group(1), link.group(2)
        # Decode HTML entities ECHA leaves in the URL (e.g. &amp;)
        pdf_url = html.unescape(pdf_url)
        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)

        title = _clean(raw_title)
        date  = (DATE_RE.search(block) or [None, ""])[1]
        snip  = SNIPPET_RE.search(block)
        snippet = _clean(snip.group(1)) if snip else ""

        # Year from "27-Oct-2014"
        year = (date or "")[-4:] if date and len(date) >= 4 else ""

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          snippet,
            "authors":           [],
            "journal":           "Document",        # ECHA result type
            "year":              year,
            "epmc_url":          pdf_url,           # source URL (= PDF URL)
            "pdf_urls":          [pdf_url],         # single direct PDF
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })
        if len(papers) >= max_results:
            break

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": len(papers),    # all docs are downloadable
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Download a single ECHA document PDF.  No fallbacks — one URL per doc."""
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
                                     headers=PDF_HEADERS) as c:
            r = await c.get(url)
        ct = r.headers.get("content-type", "")
        is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

        attempt["http_status"]  = r.status_code
        attempt["content_type"] = ct[:128]
        attempt["is_pdf"]       = is_pdf

        if r.status_code == 200 and is_pdf:
            # Filename from URL path (ECHA URLs end in /<filename>.pdf/<uuid>?...)
            from urllib.parse import urlparse, unquote
            stem = Path(unquote(urlparse(url).path)).stem or "echa_doc"
            stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
            path = pdf_dir / f"echa_{stem}.pdf"
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
