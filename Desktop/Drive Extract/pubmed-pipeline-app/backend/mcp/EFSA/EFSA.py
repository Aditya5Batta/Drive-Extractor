"""
EFSA (European Food Safety Authority) scraper.

Mirrors https://www.efsa.europa.eu/en/search?s=<query>

EFSA's search page is JS-rendered and many outputs are hosted on
efsa.onlinelibrary.wiley.com (Wiley platform, but EFSA outputs there are
fully open-access). Strategy — concurrent DDG queries:
  1. site:efsa.europa.eu "{query}" filetype:pdf
  2. site:efsa.onlinelibrary.wiley.com "{query}" filetype:pdf
  3. site:efsa.europa.eu "{query}" -filetype:pdf  (HTML landing pages)

Pair HTML pages with PDFs in DDG order; remaining direct PDFs become
standalone results.
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

SEARCH_URL = "https://www.efsa.europa.eu/en/search?s={query}"
TIMEOUT    = 30.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.efsa.europa.eu/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

_EFSA_HOSTS = ("efsa.europa.eu", "efsa.onlinelibrary.wiley.com")
_SKIP_PATHS = ("/en/search", "/en/sitemap", "/en/contact")


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub(" ", _html.unescape(text))).strip()


def _is_efsa_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _EFSA_HOSTS)


def _is_pdf_url(url: str) -> bool:
    return ".pdf" in url.lower().split("?")[0]


def _title(r: dict) -> str:
    t = _clean(r.get("title", ""))
    t = re.sub(r'\s*[-|]\s*EFSA\s*$', '', t, flags=re.I).strip()
    return t or "EFSA document"


async def _ddg(query: str, n: int) -> list[dict]:
    try:
        return await _ddg_search(query, n)
    except RuntimeError:
        return []


async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = SEARCH_URL.format(query=quote(query))

    html_results, pdf_results, wiley_pdfs = await asyncio.gather(
        _ddg(f'site:efsa.europa.eu "{query}" -filetype:pdf', min(max_results * 3, 40)),
        _ddg(f'site:efsa.europa.eu "{query}" filetype:pdf',  min(max_results * 2, 30)),
        _ddg(f'site:efsa.onlinelibrary.wiley.com "{query}" filetype:pdf',
             min(max_results * 2, 30)),
    )

    if not html_results and not pdf_results and not wiley_pdfs:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Filter HTML pages
    html_pages: list[dict] = []
    seen_urls:  set[str]   = set()
    for r in html_results:
        href = r.get("href", "")
        if (href and _is_efsa_host(href)
                and not _is_pdf_url(href)
                and not any(p in href for p in _SKIP_PATHS)
                and href not in seen_urls):
            seen_urls.add(href)
            html_pages.append(r)

    # Collect PDFs in order: efsa.europa.eu first, then wiley
    pdf_urls: list[str] = []
    pdf_titles: list[str] = []
    for r in pdf_results + wiley_pdfs:
        href = r.get("href", "")
        if href and _is_efsa_host(href) and href not in seen_urls:
            seen_urls.add(href)
            pdf_urls.append(href)
            pdf_titles.append(_title(r))

    papers: list[dict] = []
    pdf_idx = 0

    # HTML landing pages paired with PDFs in order
    for r in html_pages:
        if len(papers) >= max_results:
            break
        page_url = r.get("href", "")
        title    = _title(r)
        snippet  = _clean(r.get("body", ""))[:240]

        pdf_list: list[str] = []
        if pdf_idx < len(pdf_urls):
            pdf_list = [pdf_urls[pdf_idx]]
            pdf_idx += 1

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title[:240],
            "abstract":          snippet,
            "authors":           [],
            "journal":           "EFSA",
            "year":              "",
            "epmc_url":          page_url,
            "pdf_urls":          pdf_list,
            "has_pdf_from_api":  bool(pdf_list),
            "api_pdf_url_count": len(pdf_list),
        })

    # Remaining PDFs as standalone entries
    while pdf_idx < len(pdf_urls) and len(papers) < max_results:
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             pdf_titles[pdf_idx][:240],
            "abstract":          "",
            "authors":           [],
            "journal":           "EFSA",
            "year":              "",
            "epmc_url":          pdf_urls[pdf_idx],
            "pdf_urls":          [pdf_urls[pdf_idx]],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })
        pdf_idx += 1

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
                "http_status": None, "content_type": None, "is_pdf": False,
                "success": False, "file_size_kb": None, "error": None,
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
                    stem = Path(unquote(urlparse(url).path)).stem or "efsa_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"efsa_{stem}.pdf"
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
