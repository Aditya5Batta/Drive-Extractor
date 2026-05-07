"""
NITE (Japan National Institute of Technology and Evaluation) scraper.

Mirrors https://www.nite.go.jp/search_result_en.html?q=<query>

NITE's search uses Google Custom Search (JS-rendered). Many results are
direct PDF links on nite.go.jp. Strategy — concurrent DDG queries:
  1. site:nite.go.jp "{query}" filetype:pdf  → direct PDF files
  2. site:nite.go.jp "{query}" -filetype:pdf → HTML landing pages

Direct PDFs are the primary results since NITE search frequently returns
them; HTML pages augment as fallback.
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

SEARCH_URL = "https://www.nite.go.jp/search_result_en.html?q={query}"
TIMEOUT    = 30.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
    "Referer":         "https://www.nite.go.jp/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

_NITE_HOSTS = ("nite.go.jp",)
_SKIP_PATHS = ("/search_result", "/sitemap", "/index.html")


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub(" ", _html.unescape(text))).strip()


def _is_nite_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _NITE_HOSTS)


def _is_pdf_url(url: str) -> bool:
    return ".pdf" in url.lower().split("?")[0]


def _title(r: dict, pdf_url: str = "") -> str:
    t = _clean(r.get("title", ""))
    t = re.sub(r'\s*[-|]\s*NITE\s*$', '', t, flags=re.I).strip()
    if not t and pdf_url:
        stem = Path(urlparse(pdf_url).path).stem
        t = re.sub(r"[-_]+", " ", stem).strip()
    return (t or "NITE document")[:240]


async def _ddg(query: str, n: int) -> list[dict]:
    try:
        return await _ddg_search(query, n)
    except RuntimeError:
        return []


async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = SEARCH_URL.format(query=quote(query))

    pdf_results, html_results = await asyncio.gather(
        _ddg(f'site:nite.go.jp "{query}" filetype:pdf',  min(max_results * 3, 40)),
        _ddg(f'site:nite.go.jp "{query}" -filetype:pdf', min(max_results * 2, 30)),
    )

    if not pdf_results and not html_results:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    papers: list[dict] = []
    seen_urls: set[str] = set()

    # 1. Direct PDFs first (NITE search typically surfaces these as primary results)
    for r in pdf_results:
        if len(papers) >= max_results:
            break
        href = r.get("href", "")
        if not href or not _is_nite_host(href) or href in seen_urls:
            continue
        seen_urls.add(href)
        if not _is_pdf_url(href):
            continue
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             _title(r, href),
            "abstract":          _clean(r.get("body", ""))[:240],
            "authors":           [],
            "journal":           "NITE",
            "year":              "",
            "epmc_url":          href,
            "pdf_urls":          [href],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    # 2. HTML pages (no associated PDF — listed as informational entries)
    for r in html_results:
        if len(papers) >= max_results:
            break
        href = r.get("href", "")
        if (not href or not _is_nite_host(href) or href in seen_urls
                or _is_pdf_url(href)
                or any(p in href for p in _SKIP_PATHS)):
            continue
        seen_urls.add(href)
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             _title(r),
            "abstract":          _clean(r.get("body", ""))[:240],
            "authors":           [],
            "journal":           "NITE",
            "year":              "",
            "epmc_url":          href,
            "pdf_urls":          [],
            "has_pdf_from_api":  False,
            "api_pdf_url_count": 0,
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
                    stem = Path(unquote(urlparse(url).path)).stem or "nite_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"nite_{stem}.pdf"
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
