"""
OECD eChemPortal scraper.

Mirrors https://www.echemportal.org/echemportal/substance-search

eChemPortal aggregates chemical data from many sources (AGRITOX, AICIS,
APVMA, EU CLP, ECHA, OECD HPV, etc.). The portal itself is JS-rendered,
so we use DDG to discover both:
  1. PDFs hosted on echemportal.org or its linked sources
  2. Substance pages — checked for embedded PDFs at download time

For maximum coverage we run three concurrent DDG queries:
  • site:echemportal.org "{query}" filetype:pdf
  • site:echemportal.org "{query}"
  • "{query}" "echemportal" filetype:pdf  (cross-domain reports that cite
    echemportal as their source)
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
    "https://www.echemportal.org/echemportal/substance-search?searchTerm={query}"
)
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
    "Referer":         "https://www.echemportal.org/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

# Trusted hosts for eChemPortal results (the portal + its source databases
# that publish open chemical assessment PDFs).
_OECD_HOSTS = (
    "echemportal.org",
    "oecd.org",
    "echa.europa.eu",      # ECHA
    "agritox.anses.fr",    # AGRITOX
    "industrialchemicals.gov.au",   # AICIS
    "apvma.gov.au",        # APVMA
    "ipcs.who.int",        # WHO IPCS
    "inchem.org",
)
_SKIP_PATHS = ("/echemportal/substance-search", "/echemportal/property-search")


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub(" ", _html.unescape(text))).strip()


def _is_oecd_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _OECD_HOSTS)


def _is_pdf_url(url: str) -> bool:
    return ".pdf" in url.lower().split("?")[0]


def _title(r: dict, pdf_url: str = "") -> str:
    t = _clean(r.get("title", ""))
    t = re.sub(r'\s*[-|]\s*(eChemPortal|OECD)\s*$', '', t, flags=re.I).strip()
    if not t and pdf_url:
        stem = Path(urlparse(pdf_url).path).stem
        t = re.sub(r"[-_]+", " ", stem).strip()
    return (t or "OECD eChemPortal document")[:240]


async def _ddg(query: str, n: int) -> list[dict]:
    try:
        return await _ddg_search(query, n)
    except RuntimeError:
        return []


async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = SEARCH_URL.format(query=quote(query))

    # eChemPortal itself is JS-rendered with little indexed content, so we
    # also search oecd.org directly (OECD HPV/SIAR/SIDS assessments) and
    # AGRITOX (a key portal source) for direct PDF reports.
    pdf_results, html_results, oecd_pdfs, agritox_pdfs = await asyncio.gather(
        _ddg(f'site:echemportal.org "{query}" filetype:pdf', min(max_results, 20)),
        _ddg(f'site:echemportal.org "{query}"',              min(max_results, 20)),
        _ddg(f'site:oecd.org "{query}" filetype:pdf',        min(max_results * 2, 30)),
        _ddg(f'site:agritox.anses.fr "{query}" filetype:pdf', min(max_results, 20)),
    )
    cross_pdfs = oecd_pdfs + agritox_pdfs

    if not pdf_results and not html_results and not cross_pdfs:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    papers: list[dict] = []
    seen_urls: set[str] = set()

    # 1. Direct PDFs (DDG already filtered to filetype:pdf, so we trust the
    # result is a PDF even if URL has no .pdf extension — e.g. OECD HPV's
    # /UI/handler.axd?id=... endpoint serves application/pdf content).
    for r in pdf_results + cross_pdfs:
        if len(papers) >= max_results:
            break
        href = r.get("href", "")
        if (not href or href in seen_urls
                or not _is_oecd_host(href)):
            continue
        seen_urls.add(href)
        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             _title(r, href),
            "abstract":          _clean(r.get("body", ""))[:240],
            "authors":           [],
            "journal":           "OECD eChemPortal",
            "year":              "",
            "epmc_url":          href,
            "pdf_urls":          [href],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    # 2. HTML pages from echemportal.org (substance/property records)
    for r in html_results:
        if len(papers) >= max_results:
            break
        href = r.get("href", "")
        if (not href or href in seen_urls
                or _is_pdf_url(href)
                or not _is_oecd_host(href)
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
            "journal":           "OECD eChemPortal",
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
                    stem = Path(unquote(urlparse(url).path)).stem or "oecd_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"oecd_{stem}.pdf"
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
