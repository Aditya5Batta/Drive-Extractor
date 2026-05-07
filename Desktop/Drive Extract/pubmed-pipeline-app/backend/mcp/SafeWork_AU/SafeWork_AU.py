"""
Safe Work Australia scraper.

Mirrors https://www.safeworkaustralia.gov.au/search?search=<query>

The search page is JS-rendered and the CDN blocks programmatic HTML fetches.
Strategy — two DDG queries run concurrently:
  1. site:safeworkaustralia.gov.au "{query}" -filetype:pdf
       → HTML landing pages in website relevance order.
  2. site:safeworkaustralia.gov.au "{query}" filetype:pdf
       → Direct PDF files (/system/files/documents/…).
Pair them: first PDF gets assigned to first HTML page, second to second, etc.
Remaining un-paired PDFs become standalone entries (epmc_url = PDF URL).
Download is a plain GET — succeeds for open PDFs, fails for blocked ones.
"""
from __future__ import annotations
import asyncio, re, sys, time
import html as _html
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote, unquote

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from _ddg import search as _ddg_search

SEARCH_URL = "https://www.safeworkaustralia.gov.au/search?search={query}"
TIMEOUT    = 30.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer":         "https://www.safeworkaustralia.gov.au/",
}
PDF_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "application/pdf,*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer":         "https://www.safeworkaustralia.gov.au/",
}

_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

_SWA_HOSTS = ("safeworkaustralia.gov.au", "hcis.safeworkaustralia.gov.au")
_SKIP_PATHS = ("/search", "/about", "/contact", "/sitemap")

# Patterns that identify a direct file/PDF URL vs an HTML page
_IS_PDF_URL = re.compile(
    r'(/system/files/|/sites/default/files/|\.pdf(\?.*)?$)', re.I
)


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub(" ", _html.unescape(text))).strip()


def _is_swa_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _SWA_HOSTS)


def _is_pdf_url(url: str) -> bool:
    return bool(_IS_PDF_URL.search(url))


def _title_from_ddg(r: dict) -> str:
    t = _clean(r.get("title", ""))
    t = re.sub(r'\s*[-|]\s*Safe Work Australia\s*$', '', t, flags=re.I).strip()
    return t or "Safe Work Australia document"


async def _ddg(query: str, n: int) -> list[dict]:
    try:
        return await _ddg_search(query, n)
    except RuntimeError:
        return []


async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = SEARCH_URL.format(query=quote(query))

    # Run two DDG queries concurrently
    html_results, pdf_results = await asyncio.gather(
        _ddg(f'site:safeworkaustralia.gov.au "{query}" -filetype:pdf',
             min(max_results * 3, 40)),
        _ddg(f'site:safeworkaustralia.gov.au "{query}" filetype:pdf',
             min(max_results * 2, 30)),
    )

    # Fallback if both empty
    if not html_results and not pdf_results:
        html_results = await _ddg(
            f"site:safeworkaustralia.gov.au {query}",
            min(max_results * 3, 40),
        )

    if not html_results and not pdf_results:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Separate DDG HTML results into actual HTML pages vs embedded PDF links
    html_pages: list[dict] = []
    extra_pdfs: list[dict] = []
    seen_urls:  set[str]   = set()

    for r in html_results:
        href = r.get("href", "")
        if not href or not _is_swa_host(href):
            continue
        if any(p in href for p in _SKIP_PATHS):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        if _is_pdf_url(href):
            extra_pdfs.append(r)
        else:
            html_pages.append(r)

    # Collect all PDF URLs from filetype:pdf query
    pdf_pool: list[str] = []
    pdf_title_pool: list[str] = []
    for r in (pdf_results + extra_pdfs):
        href = r.get("href", "")
        if href and href not in seen_urls and _is_swa_host(href):
            seen_urls.add(href)
            pdf_pool.append(href)
            pdf_title_pool.append(_title_from_ddg(r))

    # Build papers in HTML landing-page order, pairing with PDFs by index
    papers: list[dict] = []
    pdf_idx = 0

    for r in html_pages:
        if len(papers) >= max_results:
            break
        page_url = r.get("href", "")
        title    = _title_from_ddg(r)
        snippet  = _clean(r.get("body", ""))[:400]

        # Assign next available PDF
        pdf_list: list[str] = []
        if pdf_idx < len(pdf_pool):
            pdf_list = [pdf_pool[pdf_idx]]
            pdf_idx += 1

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          snippet,
            "authors":           [],
            "journal":           "Safe Work Australia",
            "year":              "",
            "epmc_url":          page_url,
            "pdf_urls":          pdf_list,
            "has_pdf_from_api":  bool(pdf_list),
            "api_pdf_url_count": len(pdf_list),
        })

    # Remaining un-paired PDFs become standalone entries
    for pdf_url, pdf_title in zip(pdf_pool[pdf_idx:], pdf_title_pool[pdf_idx:]):
        if len(papers) >= max_results:
            break
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             pdf_title,
            "abstract":          "",
            "authors":           [],
            "journal":           "Safe Work Australia",
            "year":              "",
            "epmc_url":          pdf_url,
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": 0,
        "response_time_ms":  int((time.monotonic() - t0) * 1000),
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None,
                "pdf_source": None, "file_size_kb": None, "attempts": []}

    attempts = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.3)
            attempt = {
                "url": url, "attempt_no": i,
                "http_status": None, "content_type": None,
                "is_pdf": False, "success": False,
                "file_size_kb": None, "error": None,
                "attempted_at": datetime.utcnow(),
            }
            try:
                r  = await c.get(url)
                ct = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    stem = Path(unquote(urlparse(url).path)).stem or "safework_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"safework_{stem}.pdf"
                    path.write_bytes(r.content)
                    size_kb = len(r.content) // 1024
                    attempt["success"]      = True
                    attempt["file_size_kb"] = size_kb
                    attempts.append(attempt)
                    return {
                        "status": "success", "pdf_path": str(path),
                        "pdf_source": url, "file_size_kb": size_kb,
                        "attempts": attempts,
                    }
            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
            attempts.append(attempt)

    return {"status": "failed", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": attempts}
