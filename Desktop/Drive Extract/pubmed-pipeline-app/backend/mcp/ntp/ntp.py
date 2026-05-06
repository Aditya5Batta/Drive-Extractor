"""
NTP (National Toxicology Program) document scraper.

The NTP search is powered by Funnelback, exposed at ntpsearch.niehs.nih.gov.
Adding `&Extension=PDF` is the URL-level equivalent of ticking the website's
"Formats → PDF" facet (drops the result count from ~685 to ~639 for "Benzene").

The HTML has TWO result blocks:
  1. "Search suggestions" – ~3 hand-picked links at the top.  Skip.
  2. <div id="results"><ol aria-live="polite" start="1">…</ol></div>  – the
     main result list, in relevance order.  This is what we parse.

Each main <li> contains:
    <a href="URL">title<span class="query">term</span></a>  16.4MB <br/>
    <p class="displayurl">canonical URL</p>
    <p>snippet</p>

URL types we keep:
  • direct *.pdf links
  • /go/<id> redirects (NTP shorthand → resolves to a PDF)
"""
from __future__ import annotations
import asyncio, html, re, time
from datetime import datetime
from pathlib import Path

import httpx

SEARCH_URL = "https://ntpsearch.niehs.nih.gov/"
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

MAIN_OL_RE  = re.compile(r'<ol\s+aria-live="polite"[^>]*>(.*?)</ol>', re.S)
LI_RE       = re.compile(r'<li>\s*(.*?)\s*</li>', re.S)
ANCHOR_RE   = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SIZE_RE     = re.compile(r'</a>\s*([\d.]+\s*(?:K|M|G)?B)\b', re.I)
SNIPPET_RE  = re.compile(r'<p>(.*?)</p>', re.S)
TAG_STRIP   = re.compile(r'<[^>]+>')


def _clean(text: str) -> str:
    return html.unescape(TAG_STRIP.sub("", text)).strip()


def _looks_pdf(url: str) -> bool:
    u = url.lower()
    return u.endswith(".pdf") or ".pdf?" in u or "/go/" in u


async def search(query: str, max_results: int = 20) -> dict:
    """
    Search NTP filtering to PDF-only results (`Extension=PDF`).
    Returns up to `max_results` documents, each with one direct/redirect URL.
    """
    params = {
        "query":     query,
        "Extension": "PDF",      # website's PDF format facet
        "num_ranks": min(max(1, max_results) * 2, 50),  # over-fetch, filter later
    }
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

    main = MAIN_OL_RE.search(r.text)
    if not main:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "ok", "error": None, "papers": [],
        }

    papers: list[dict] = []
    seen_urls: set[str] = set()

    for block in LI_RE.findall(main.group(1)):
        anchor = ANCHOR_RE.search(block)
        if not anchor:
            continue
        url = html.unescape(anchor.group(1))
        if not _looks_pdf(url) or url in seen_urls:
            continue
        seen_urls.add(url)

        title = _clean(anchor.group(2))[:300]
        # snippet = the LAST <p> block (skip the <a> wrapper <p>)
        snippets = SNIPPET_RE.findall(block)
        snippet  = _clean(snippets[-1])[:400] if snippets else ""

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          snippet,
            "authors":           [],
            "journal":           "NTP",
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
    """Download a single NTP PDF (follows /go/ redirects automatically)."""
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
            from urllib.parse import urlparse, unquote
            stem = Path(unquote(urlparse(str(r.url)).path)).stem or "ntp_doc"
            stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
            path = pdf_dir / f"ntp_{stem}.pdf"
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
