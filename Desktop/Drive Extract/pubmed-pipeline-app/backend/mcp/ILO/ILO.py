"""
ILO (International Labour Organization) scraper.

Mirrors https://www.ilo.org/search?search_api_fulltext=<query>&sort_by=search_api_relevance&f[0]=language:en

The ILO search endpoint returns 403 for all non-browser agents (even with
browser-fingerprint headers), so we use DuckDuckGo restricted to ilo.org for
discovery. ILO has several subdomains that host relevant content:
  • www.ilo.org          — main publications/resources
  • natlex.ilo.org       — national legislation database
  • normlex.ilo.org      — international standards database
  • labordoc.ilo.org     — library catalog (may link to documents)
  • chemicalsafety.ilo.org — ICSC chemical safety cards
  • webapps.ilo.org      — web applications

PDFs on ilo.org publication pages are typically under:
  /sites/default/files/YYYY-MM/<filename>.pdf         (new URL scheme)
  /sites/default/files/wcmsp5/groups/public/.../<fn>.pdf  (legacy)
  /dyn/natlex2/natlex2/files/download/...             (NATLEX)

Result order follows the DuckDuckGo relevance order which closely mirrors
the ILO search relevance order.
"""
from __future__ import annotations
import asyncio, re, time
import html as _html
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import httpx
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from _ddg import search as _ddg_search

TIMEOUT = 30.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Dest":  "document",
    "Referer":         "https://www.ilo.org/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TITLE_RE  = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

# ILO PDF URL patterns (relative and absolute)
_ILO_PDF_PATHS = (
    "/sites/default/files/",   # main site (new + legacy wcmsp5)
    "/dyn/natlex2/",           # NATLEX legislation
    "/dyn/nrmlx_en/",          # NORMLEX
    "/static/",                # webapps.ilo.org static files
)
_ILO_HOST_SUFFIX = "ilo.org"


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub("", _html.unescape(text))).strip()


def _is_ilo_host(url: str) -> bool:
    return _ILO_HOST_SUFFIX in urlparse(url).netloc.lower()


def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")


def _href_looks_like_ilo_pdf(href: str) -> bool:
    """True if the href is a PDF file (relative or absolute) from ilo.org."""
    lower = href.lower()
    if ".pdf" not in lower:
        return False
    # Accept relative paths under ILO's well-known file directories
    for prefix in _ILO_PDF_PATHS:
        if lower.startswith(prefix) or lower.startswith("http") and prefix in lower:
            return True
    return False


async def _get_html(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """GET url → (html_text, final_url). Returns ("", url) on failure or non-HTML."""
    try:
        r = await client.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return "", str(r.url)
        ct = r.headers.get("content-type", "").lower()
        if "html" in ct or "xml" in ct or not ct:
            return r.text, str(r.url)
        return "", str(r.url)
    except Exception:
        return "", url


async def _pdfs_from_page(
    client: httpx.AsyncClient,
    page_url: str,
    ddg_title: str,
) -> tuple[str, list[str], str]:
    """
    Fetch one ILO result page and return (title, pdf_urls, snippet).
    If the URL is itself a PDF, return it directly.
    """
    # Direct PDF URL from DDG
    if _is_pdf_url(page_url):
        if _is_ilo_host(page_url):
            return ddg_title, [page_url], ""
        return ddg_title, [], ""

    text, final_url = await _get_html(client, page_url)
    if not text:
        return ddg_title, [], ""

    # Page title — strip " | ILO" / " | International Labour Organization" suffix
    page_title = ddg_title
    tm = _TITLE_RE.search(text)
    if tm:
        t = re.sub(
            r'\s*[|–\-]\s*(ILO|International Labour Organization).*$',
            '', _clean(tm.group(1)), flags=re.I,
        ).strip()
        if t:
            page_title = t

    # Snippet from meta description
    snippet = ""
    dm = re.search(
        r'<meta\b[^>]*\bname=["\']description["\'][^>]*\bcontent=["\']([^"\']{10,})["\']',
        text, re.I,
    )
    if not dm:
        # Try property="og:description"
        dm = re.search(
            r'<meta\b[^>]*\bproperty=["\']og:description["\'][^>]*\bcontent=["\']([^"\']{10,})["\']',
            text, re.I,
        )
    if dm:
        snippet = _clean(dm.group(1))[:400]

    # Collect PDF links on the page
    pdf_urls: list[str] = []
    seen:     set[str]  = set()
    base      = final_url or page_url
    parsed_base = urlparse(base)
    ilo_root  = f"{parsed_base.scheme}://{parsed_base.netloc}"

    for m in _ANCHOR_RE.finditer(text):
        raw_href = _html.unescape(m.group(1).strip())
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:")):
            continue
        if ".pdf" not in raw_href.lower():
            continue

        # Resolve URL
        if raw_href.startswith("http"):
            pdf_url = raw_href
        elif raw_href.startswith("//"):
            pdf_url = parsed_base.scheme + ":" + raw_href
        elif raw_href.startswith("/"):
            # Relative to ilo.org root
            pdf_url = "https://www.ilo.org" + raw_href
        else:
            pdf_url = urljoin(base, raw_href)

        if not _is_ilo_host(pdf_url):
            continue
        if pdf_url not in seen:
            seen.add(pdf_url)
            pdf_urls.append(pdf_url)

    return page_title, pdf_urls, snippet


async def search(query: str, max_results: int = 20) -> dict:
    """
    Search ILO via two concurrent DDG queries:
      1. site:ilo.org "{query}" -filetype:pdf  → HTML pages (best-match order)
      2. site:ilo.org "{query}" filetype:pdf   → direct PDF files
    HTML pages are fetched to extract their PDF links.
    Direct PDFs from query 2 are appended as extra results.
    """
    t0      = time.monotonic()
    api_url = (
        f"https://www.ilo.org/search"
        f"?search_api_fulltext={query}"
        f"&sort_by=search_api_relevance"
        f"&f%5B0%5D=language%3Aen"
    )

    async def _ddg(q: str, n: int) -> list[dict]:
        try:
            return await _ddg_search(q, n)
        except RuntimeError:
            return []

    html_results, pdf_results = await asyncio.gather(
        _ddg(f'site:ilo.org "{query}" -filetype:pdf', min(max_results * 3, 30)),
        _ddg(f'site:ilo.org "{query}" filetype:pdf',  min(max_results * 2, 20)),
    )

    # Fallback: unquoted query
    if not html_results and not pdf_results:
        html_results = await _ddg(
            f"site:ilo.org {query} -filetype:pdf",
            min(max_results * 3, 30),
        )
        if not html_results:
            return {
                "query_sent":        query,
                "api_url":           api_url,
                "papers_returned":   0,
                "papers_with_pmcid": 0,
                "response_time_ms":  int((time.monotonic() - t0) * 1000),
                "status":            "ok",
                "error":             None,
                "papers":            [],
            }

    # Fetch each HTML result page to extract its PDF links
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=HEADERS, follow_redirects=True
    ) as client:
        page_results = await asyncio.gather(
            *[
                _pdfs_from_page(client, r["href"], r.get("title", ""))
                for r in html_results
            ],
            return_exceptions=True,
        )

    papers: list[dict] = []
    seen_pages: set[str] = set()
    seen_pdfs:  set[str] = set()

    # ── 1. HTML pages (primary, in DDG relevance order) ───────────────────────
    for r, pr in zip(html_results, page_results):
        page_url = r.get("href", "")
        if not page_url or page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        if isinstance(pr, Exception):
            page_title = r.get("title", "")
            pdf_urls   = []
            snippet    = r.get("body", "")[:400]
        else:
            page_title, pdf_urls, snippet = pr
            if not snippet:
                snippet = r.get("body", "")[:400]

        for u in pdf_urls:
            seen_pdfs.add(u)

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             page_title or r.get("title", ""),
            "abstract":          snippet,
            "authors":           [],
            "journal":           "ILO",
            "year":              "",
            "epmc_url":          page_url,
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  bool(pdf_urls),
            "api_pdf_url_count": len(pdf_urls),
        })

        if len(papers) >= max_results:
            break

    # ── 2. Direct PDFs from DDG filetype:pdf query (extra coverage) ───────────
    for r in pdf_results:
        if len(papers) >= max_results:
            break
        pdf_url  = r.get("href", "")
        page_url = pdf_url
        if not pdf_url or page_url in seen_pages:
            continue
        if pdf_url in seen_pdfs:
            continue
        if not _is_ilo_host(pdf_url):
            continue
        seen_pages.add(page_url)
        seen_pdfs.add(pdf_url)

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             r.get("title", "") or pdf_url.rsplit("/", 1)[-1],
            "abstract":          r.get("body", "")[:400],
            "authors":           [],
            "journal":           "ILO",
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
    """Try each PDF URL in order. Verifies the response is actually a PDF."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": [],
        }

    attempts = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.3)
            attempt = {
                "url":          url,
                "attempt_no":   i,
                "http_status":  None,
                "content_type": None,
                "is_pdf":       False,
                "success":      False,
                "file_size_kb": None,
                "error":        None,
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
                    stem = Path(unquote(urlparse(url).path)).stem or "ilo_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"ilo_{stem}.pdf"
                    path.write_bytes(r.content)
                    size_kb = len(r.content) // 1024
                    attempt["success"]      = True
                    attempt["file_size_kb"] = size_kb
                    attempts.append(attempt)
                    return {
                        "status":       "success",
                        "pdf_path":     str(path),
                        "pdf_source":   url,
                        "file_size_kb": size_kb,
                        "attempts":     attempts,
                    }
            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
            attempts.append(attempt)

    return {
        "status":       "failed",
        "pdf_path":     None,
        "pdf_source":   None,
        "file_size_kb": None,
        "attempts":     attempts,
    }
