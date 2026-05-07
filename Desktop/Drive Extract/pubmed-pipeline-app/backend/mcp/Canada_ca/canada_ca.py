"""
Canada.ca scraper.

Mirrors https://www.canada.ca/en/sr/srb.html?q=<query>

Canada.ca uses Akamai CDN which blocks programmatic page fetches, and its
search results are JS-rendered. Strategy:
  1. DDG site:canada.ca "{query}" — result pages in relevance order.
  2. DDG site:publications.gc.ca "{query}" filetype:pdf — direct PDF files.
  3. Merge both, dedup, and return in DDG order with pdf_urls set where found.
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

SEARCH_URL = (
    "https://www.canada.ca/en/sr/srb.html"
    "?cdn=canada&st=s&num=10&langs=en&q={query}"
)
TIMEOUT = 30.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding":           "gzip, deflate, br",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Upgrade-Insecure-Requests": "1",
}

_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

# URL hosts considered "Canada.ca ecosystem"
_CANADA_HOSTS = (
    "canada.ca", "publications.gc.ca", "bac-lac.gc.ca",
    "gazette.gc.ca", "epe.lac-bac.gc.ca", "recalls-rappels.canada.ca",
)
_SKIP_PATHS = ("/en/sr/srb", "/en.html", "/en/index.html", "/fr/")

# Regex to detect a direct PDF URL
_IS_PDF = re.compile(r'\.pdf(\?.*)?$', re.I)


def _clean(text: str) -> str:
    text = _html.unescape(_TAG_STRIP.sub(" ", text))
    return _WS_NORM.sub(" ", text).strip()


def _is_canada_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _CANADA_HOSTS)


def _is_pdf_url(url: str) -> bool:
    return bool(_IS_PDF.search(url.split("?")[0]))


async def search(query: str, max_results: int = 50) -> dict:
    t0      = time.monotonic()
    api_url = SEARCH_URL.format(query=quote(query))

    # ── Query 1: HTML result pages from canada.ca ─────────────────────────────
    pages_ddg: list[dict] = []
    try:
        pages_ddg = await _ddg_search(
            f'site:canada.ca "{query}"', min(max_results * 3, 60)
        )
    except RuntimeError:
        try:
            pages_ddg = await _ddg_search(
                f"site:canada.ca {query}", min(max_results * 2, 40)
            )
        except RuntimeError:
            pass

    # ── Query 2: Direct PDFs from canada.ca/content/dam (these download cleanly,
    #            unlike publications.gc.ca which serves a JS-rendered archive page)
    pdfs_ddg: list[dict] = []
    try:
        pdfs_ddg = await _ddg_search(
            f'site:canada.ca "{query}" filetype:pdf',
            min(max_results * 2, 40),
        )
    except RuntimeError:
        pass
    # ── Query 3: publications.gc.ca PDFs as fallback ──────────────────────────
    try:
        more_pdfs = await _ddg_search(
            f'site:publications.gc.ca "{query}" filetype:pdf',
            min(max_results * 2, 30),
        )
        pdfs_ddg = pdfs_ddg + more_pdfs
    except RuntimeError:
        pass

    # ── Also check direct PDF hrefs in the HTML pages DDG results themselves ──
    # DDG sometimes returns direct .pdf URLs in the href field
    direct_from_pages = [r for r in pages_ddg if _is_pdf_url(r.get("href", ""))]
    html_pages        = [r for r in pages_ddg if not _is_pdf_url(r.get("href", ""))
                         and _is_canada_host(r.get("href", ""))
                         and not any(p in r.get("href", "") for p in _SKIP_PATHS)]

    all_pdfs_ddg = pdfs_ddg + direct_from_pages

    if not html_pages and not all_pdfs_ddg:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Build a lookup: page_url → list of PDF URLs from the PDF query
    # We try to match PDFs to pages by title similarity (best effort)
    pdf_urls_from_ddg: list[str] = []
    seen_pdf_urls: set[str] = set()
    for r in all_pdfs_ddg:
        href = r.get("href", "")
        if href and href not in seen_pdf_urls and _is_canada_host(href):
            seen_pdf_urls.add(href)
            pdf_urls_from_ddg.append(href)

    # ── Build papers ───────────────────────────────────────────────────────────
    papers: list[dict] = []
    seen_pages: set[str] = set()

    # First: HTML result pages (the main ranked list)
    pdf_pool = list(pdf_urls_from_ddg)  # consume PDFs into pages where possible
    pdf_idx  = 0

    for r in html_pages:
        if len(papers) >= max_results:
            break
        page_url = r.get("href", "")
        if not page_url or page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        title   = _clean(r.get("title", ""))
        snippet = _clean(r.get("body", ""))[:400]

        # Assign next unmatched PDF (rough association in order)
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
            "journal":           "Canada.ca",
            "year":              "",
            "epmc_url":          page_url,
            "pdf_urls":          pdf_list,
            "has_pdf_from_api":  bool(pdf_list),
            "api_pdf_url_count": len(pdf_list),
        })

    # Second: any remaining PDFs from publications.gc.ca not yet assigned
    for pdf_url in pdf_pool[pdf_idx:]:
        if len(papers) >= max_results:
            break
        filename = pdf_url.split("?")[0].rsplit("/", 1)[-1]
        stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
        title = re.sub(r"[-_]+", " ", stem).strip() or "Canada.ca document"
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          "",
            "authors":           [],
            "journal":           "Canada.ca",
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
    attempts = []

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None,
                "pdf_source": None, "file_size_kb": None, "attempts": []}

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
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

                # publications.gc.ca returns a JS-rendered HTML interstitial for
                # PDF URLs. Try a /pub/ URL variant which often serves the file.
                if (not is_pdf and r.status_code == 200
                        and "publications.gc.ca" in url
                        and "html" in ct.lower()):
                    # Try alternate path: /pub/publication/ instead of /collections/
                    alt = url.replace("/collections/", "/pub/")
                    try:
                        r2 = await c.get(alt)
                        ct2 = r2.headers.get("content-type", "")
                        if "pdf" in ct2.lower() or r2.content[:4] == b"%PDF":
                            r, ct, is_pdf = r2, ct2, True
                    except Exception:
                        pass

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    stem = Path(unquote(urlparse(url).path)).stem or "canada_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"canada_{stem}.pdf"
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
