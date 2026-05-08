"""
IARC Monographs scraper.

Mirrors https://monographs.iarc.who.int/search
(IARC Monographs on the Identification of Carcinogenic Hazards to Humans)

The search frontend is a WordPress SPA (JS-rendered), but the WordPress REST
API is fully accessible and returns results in the SAME relevance order as
the website:

  Step 1 — GET /wp-json/wp/v2/search?search=<query>&per_page=<N>
    → list[{id, title, url, subtype, _links.self[0].href}] in relevance order

  Step 2 — GET {_links.self[0].href}  (one per result, run concurrently)
    → {title.rendered, link, date, content.rendered, excerpt.rendered?}
    → content.rendered contains <a href="...pdf"> download anchors

  Step 3 — extract all .pdf hrefs from content.rendered
    → resolve relative /wp-content/uploads/... paths to absolute URLs
    → prefer iarc.who.int / who.int domain PDFs first

No DDG, no HTML scraping — clean JSON API throughout.
Result order is guaranteed to match the website's relevance ranking.
"""
from __future__ import annotations
import asyncio, re, time
import html as _html
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import httpx

IARC_BASE  = "https://monographs.iarc.who.int"
SEARCH_URL = f"{IARC_BASE}/wp-json/wp/v2/search"
TIMEOUT    = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         IARC_BASE + "/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# Subtypes that are irrelevant to chemical/toxicology searches
_SKIP_SUBTYPES = frozenset({
    "vacancy", "testimonial", "friend_of_iarc", "staff_member",
})

_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', re.I)
_TAG_STRIP  = re.compile(r'<[^>]+>')
_WS_NORM    = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub("", _html.unescape(text))).strip()


def _extract_pdfs(content_html: str, page_url: str) -> list[str]:
    """
    Extract all .pdf hrefs from content.rendered HTML.
    Relative paths are resolved against IARC_BASE.
    IARC-owned PDFs (iarc.who.int / who.int) are sorted to the front.
    """
    seen: set[str] = set()
    iarc_pdfs: list[str] = []
    ext_pdfs:  list[str] = []

    for m in _ANCHOR_RE.finditer(content_html):
        raw = _html.unescape(m.group(1).strip())
        if not raw or ".pdf" not in raw.lower():
            continue
        # Resolve URL
        if raw.startswith("http"):
            url = raw
        elif raw.startswith("//"):
            url = "https:" + raw
        elif raw.startswith("/"):
            url = IARC_BASE + raw
        else:
            url = urljoin(page_url, raw)

        if url in seen:
            continue
        seen.add(url)
        host = urlparse(url).netloc.lower()
        if "iarc.who.int" in host or "who.int" in host:
            iarc_pdfs.append(url)
        else:
            ext_pdfs.append(url)

    return iarc_pdfs + ext_pdfs


async def _fetch_post(
    client: httpx.AsyncClient,
    api_href: str,
    sem: asyncio.Semaphore,
) -> dict:
    """
    Fetch a single post from its WP REST self-link.
    Returns a dict with: title, link, date, year, content_html, excerpt.
    """
    async with sem:
        await asyncio.sleep(0.1)   # gentle pacing
        try:
            r = await client.get(api_href, timeout=TIMEOUT)
            if r.status_code != 200:
                return {}
            data = r.json()
        except Exception:
            return {}

    title   = _clean(data.get("title", {}).get("rendered", "") or "")
    link    = data.get("link", "") or ""
    date    = data.get("date", "") or ""
    year    = date[:4] if date else ""

    # excerpt — present on some post types (news-events), absent on others (event)
    raw_exc = (data.get("excerpt") or {}).get("rendered", "") or ""
    excerpt = _clean(raw_exc)[:400]

    # content — where PDF links live
    content_html = (data.get("content") or {}).get("rendered", "") or ""

    # ACF metadata (events carry start_date / place)
    acf = data.get("acf") or {}
    if not year:
        start = acf.get("start_date", "") or ""
        if start:
            # format: "10.10.2017"  or "2017-10-10"
            for sep in (".", "-", "/"):
                parts = start.split(sep)
                if len(parts) == 3:
                    candidate = parts[-1] if len(parts[-1]) == 4 else parts[0]
                    if candidate.isdigit() and len(candidate) == 4:
                        year = candidate
                        break

    return {
        "title":        title,
        "link":         link,
        "date":         date,
        "year":         year,
        "content_html": content_html,
        "excerpt":      excerpt,
    }


async def search(query: str, max_results: int = 20) -> dict:
    """
    Search IARC Monographs via the WordPress REST API.

    Results are returned in the same relevance order as the website.
    For each result the individual post endpoint is fetched concurrently
    to get the full content with PDF download links.
    """
    t0      = time.monotonic()
    api_url = (
        f"{IARC_BASE}/search"
        f"?search_api_fulltext={query}"
        f"&sort_by=search_api_relevance"
    )

    # ── Step 1: search results in relevance order ─────────────────────────────
    # Fetch up to 3× max so we have room after filtering irrelevant subtypes.
    params = {
        "search":   query,
        "per_page": min(max_results * 3, 100),
    }

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(SEARCH_URL, params=params, timeout=TIMEOUT)
            if r.status_code != 200:
                return {
                    "query_sent": query, "api_url": api_url,
                    "papers_returned": 0, "papers_with_pmcid": 0,
                    "response_time_ms": int((time.monotonic() - t0) * 1000),
                    "status": "error",
                    "error": f"Search API returned {r.status_code}",
                    "papers": [],
                }
            hits = r.json()
    except Exception as exc:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": str(exc), "papers": [],
        }

    # Filter irrelevant post types, preserve original relevance order
    hits = [
        h for h in hits
        if h.get("subtype", "") not in _SKIP_SUBTYPES
    ]

    if not hits:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # ── Step 2: fetch each post concurrently for content + PDF links ──────────
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        post_data = await asyncio.gather(
            *[
                _fetch_post(
                    client,
                    h["_links"]["self"][0]["href"],
                    sem,
                )
                for h in hits
            ],
            return_exceptions=True,
        )

    # ── Step 3: assemble papers list ─────────────────────────────────────────
    papers: list[dict] = []
    for hit, pd in zip(hits, post_data):
        if len(papers) >= max_results:
            break

        if isinstance(pd, Exception) or not pd:
            # Use search-level data as fallback
            title    = hit.get("title", "")
            link     = hit.get("url", "")
            year     = ""
            excerpt  = ""
            pdf_urls = []
        else:
            title    = pd["title"] or hit.get("title", "")
            link     = pd["link"]  or hit.get("url", "")
            year     = pd["year"]
            excerpt  = pd["excerpt"]
            pdf_urls = _extract_pdfs(pd["content_html"], link)

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             title,
            "abstract":          excerpt,
            "authors":           [],
            "journal":           "IARC Monographs",
            "year":              year,
            "epmc_url":          link,
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  bool(pdf_urls),
            "api_pdf_url_count": len(pdf_urls),
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
    """
    Try each PDF URL in order. Verifies the response is a real PDF.
    IARC-owned PDFs are always listed first by _extract_pdfs.
    """
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
                    stem = Path(unquote(urlparse(url).path)).stem or "iarc_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"iarc_{stem}.pdf"
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
