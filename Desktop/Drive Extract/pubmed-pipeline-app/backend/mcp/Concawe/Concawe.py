"""
CONCAWE document scraper.

CONCAWE publishes petroleum-industry science reports (REACH consortium
datasets, workplace exposure, biomonitoring) via a WordPress site.

Search endpoint: https://www.concawe.eu/?s=<chemical>
Publication pages: /publication/<slug>/
PDFs:             /wp-content/uploads/YYYY/MM/<report>.pdf

Results are returned in the same order they appear on the website
(WordPress default relevance / date ordering) — no post-sorting.
"""
from __future__ import annotations
import asyncio, html, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

SEARCH_BASE = "https://www.concawe.eu/"
TIMEOUT     = 30.0
HEADERS     = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_STRIP = re.compile(r"<[^>]+>")
_DATE_RE   = re.compile(
    r"\b(\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# Match direct PDF links from Concawe, capturing href + anchor text
# Concawe search results link directly to PDFs — no intermediate landing pages.
_PDF_ANCHOR_RE = re.compile(
    r'<a\b[^>]+href=["\']'
    r'(https?://(?:www\.)?concawe\.eu/[^"\']*\.pdf(?:\?[^"\']*)?)'
    r'["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Fallback: bare PDF hrefs without anchor text
_PDF_RE = re.compile(
    r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    text = html.unescape(_TAG_STRIP.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()


def _title_from_anchor(anchor_text: str, pdf_url: str) -> str:
    """Extract a readable title from anchor text or the PDF filename."""
    t = _clean(anchor_text)
    if not t or len(t) < 4:
        # Fall back to filename
        filename = pdf_url.split("?")[0].rsplit("/", 1)[-1]
        filename = re.sub(r"\.pdf$", "", filename, flags=re.I)
        t = re.sub(r"[-_]+", " ", filename).strip()
    # If text looks like a raw filename (no spaces), prettify it
    if " " not in t:
        t = re.sub(r"[-_]+", " ", t).strip()
    return t or "CONCAWE document"


def _year_from(text: str) -> str:
    m = _DATE_RE.search(text)
    if m:
        ym = re.search(r"\d{4}", m.group(1))
        if ym:
            return ym.group(0)
    return ""


def _abs_pdf(url: str, base: str = SEARCH_BASE) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.concawe.eu" + url
    if not url.startswith("http"):
        return urljoin(base, url)
    return url


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


async def search(query: str, max_results: int = 20) -> dict:
    """
    Search CONCAWE via WordPress /?s= endpoint.
    Returns papers in the same order as the website.
    """
    url = f"{SEARCH_BASE}?s={quote(query)}"
    t0  = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS,
                                     follow_redirects=True) as client:
            r = await client.get(url)
        r.raise_for_status()
        html_text = r.text
    except Exception as e:
        return {
            "query_sent": query, "api_url": url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": f"{type(e).__name__}: {e}", "papers": [],
        }

    response_ms = int((time.monotonic() - t0) * 1000)

    papers: list[dict] = []
    seen_pdfs: set[str] = set()

    # Concawe search results link directly to PDFs — no intermediate landing pages.
    # We capture anchor text for the title and use the search page URL as epmc_url.
    for m in _PDF_ANCHOR_RE.finditer(html_text):
        if len(papers) >= max_results:
            break
        raw_href   = m.group(1)
        anchor_txt = m.group(2)

        # Normalise PDF URL (strip query params for dedup key)
        pdf_url = _abs_pdf(raw_href)
        pdf_key = pdf_url.split("?")[0]
        if pdf_key in seen_pdfs:
            continue
        seen_pdfs.add(pdf_key)

        title = _title_from_anchor(anchor_txt, pdf_url)
        window = html_text[max(0, m.start() - 200): m.end() + 200]
        year   = _year_from(window)

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          "",
            "authors":           [],
            "journal":           "CONCAWE",
            "year":              year,
            "epmc_url":          url,   # search results page (no landing page exists)
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    return {
        "query_sent":        query,
        "api_url":           url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": sum(1 for p in papers if p["pdf_urls"]),
        "response_time_ms":  response_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Download a single CONCAWE PDF (wp-content/uploads)."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None, "pdf_source": None,
                "file_size_kb": None, "attempts": []}

    url     = pdf_urls[0]
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
        ct     = r.headers.get("content-type", "")
        is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

        attempt["http_status"]  = r.status_code
        attempt["content_type"] = ct[:128]
        attempt["is_pdf"]       = is_pdf

        if r.status_code == 200 and is_pdf:
            from urllib.parse import urlparse, unquote
            stem = Path(unquote(urlparse(str(r.url)).path)).stem or "concawe_doc"
            stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            path = pdf_dir / f"concawe_{stem}.pdf"
            path.write_bytes(r.content)
            size_kb = len(r.content) // 1024
            attempt["success"]      = True
            attempt["file_size_kb"] = size_kb
            return {"status": "success", "pdf_path": str(path),
                    "pdf_source": url, "file_size_kb": size_kb,
                    "attempts": [attempt]}
    except Exception as e:
        attempt["error"] = f"{type(e).__name__}: {e}"

    return {"status": "failed", "pdf_path": None, "pdf_source": None,
            "file_size_kb": None, "attempts": [attempt]}
