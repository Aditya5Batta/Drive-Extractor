"""
Medline / NCBI Bookshelf scraper.

Source: https://www.ncbi.nlm.nih.gov/books/

WHY WE SCRAPE THE SEARCH PAGE (not eutils esearch)
────────────────────────────────────────────────────
NCBI eutils db=books returns ALL Bookshelf records — root books AND
individual chapters AND tables AND figures. For a query like "benzene",
fine-grained records with "benzene" literally in their title
  ("Table 11. Results for benzene and 3-methoxybutyl acetate")
rank ABOVE the root books the user actually wants
  ("Addendum to the Toxicological Profile for Benzene" — NBK591286).

The NCBI Bookshelf web search UI (ncbi.nlm.nih.gov/books?term=...) shows
ROOT-LEVEL BOOKS in its results — exactly what a human searching sees.
Scraping that page's NBK IDs is therefore more reliable than eutils.

STRATEGY
────────
1. GET ncbi.nlm.nih.gov/books?term={query}  (server-side rendered HTML)
   → extract unique /books/(NBK\d+)/ hrefs from the result area
   → these are root books with real PDF downloads

2. esummary batch for the found NBK UIDs → titles / authors / dates
   (metadata only; we already have the NBK IDs from the HTML)

3. Construct PDF URL candidates for each root book:
   • Bookshelf_NBK{id}.pdf  (most common — ATSDR, NTP, NLM reports)
   • NBK{id}.pdf             (older / smaller books)

4. download_pdf: GET → %PDF magic-byte check → save.
   Any chapter NBK ID that slips in will 404 naturally.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path

import httpx

BOOKSHELF    = "https://www.ncbi.nlm.nih.gov/books"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TIMEOUT      = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.ncbi.nlm.nih.gov/books/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# /books/(NBK{id})/ links anywhere in a Bookshelf search results page
_NBK_HREF_RE = re.compile(
    r'href=["\'](?:https?://www\.ncbi\.nlm\.nih\.gov)?/books/(NBK\d+)/[^"\']*["\']',
    re.I,
)
# Strip HTML tags / collapse whitespace
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


def _pdf_candidates(nbk_id: str) -> list[str]:
    return [
        f"{BOOKSHELF}/{nbk_id}/pdf/Bookshelf_{nbk_id}.pdf",
        f"{BOOKSHELF}/{nbk_id}/pdf/{nbk_id}.pdf",
    ]


def _get_title(meta: object) -> str:
    if not isinstance(meta, dict):
        return ""
    for field in ("title", "booktitle", "chaptertitle", "bookname"):
        val = _clean(meta.get(field) or "")
        if val:
            return val
    return ""


def _title_matches_query(title: str, query: str) -> bool:
    """True if ANY word ≥3 chars from query appears whole-word in title."""
    words = set(re.findall(r'[a-z]{3,}', query.lower()))
    lo    = title.lower()
    return any(bool(re.search(rf'\b{re.escape(w)}\b', lo)) for w in words)


# ── HTML search scraper ───────────────────────────────────────────────────────

async def _html_search(client: httpx.AsyncClient, query: str, max_ids: int) -> list[str]:
    """
    Fetch the Bookshelf search-results HTML page and extract root-book NBK IDs.

    The search UI returns BOOK-LEVEL cards (not chapters/tables).  We extract
    unique /books/NBK{id}/ hrefs in document order; the first occurrences are
    the result cards.  Navigation / sidebar links are few and deduplicated away.
    """
    try:
        r = await client.get(BOOKSHELF, params={"term": query}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []

    seen: set[str]   = set()
    nbk_ids: list[str] = []
    for m in _NBK_HREF_RE.finditer(html):
        nbk = m.group(1)
        if nbk not in seen:
            seen.add(nbk)
            nbk_ids.append(nbk)
            if len(nbk_ids) >= max_ids:
                break

    return nbk_ids


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = f"{BOOKSHELF}?term={query}"   # for logging

    def _empty(err: str | None = None) -> dict:
        return dict(
            query_sent=query, api_url=api_url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # ── Step 1: Bookshelf HTML search → root-book NBK IDs ────────────────────
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as c:
        nbk_ids = await _html_search(c, query, max_results * 3)

    if not nbk_ids:
        return _empty("Bookshelf HTML search returned no results")

    # ── Step 2: esummary batch for metadata (optional; best-effort) ───────────
    # The numeric part of the NBK ID IS the db=books UID for esummary.
    uids    = [nbk[3:] for nbk in nbk_ids]   # "NBK591286" → "591286"
    summary: dict = {}
    await asyncio.sleep(0.2)
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as c:
            sr = await c.get(ESUMMARY_URL, params={
                "db": "books", "id": ",".join(uids), "retmode": "json",
            })
            sr.raise_for_status()
            summary = sr.json().get("result", {}) or {}
    except Exception:
        summary = {}

    # ── Step 3: build paper records ───────────────────────────────────────────
    seen_pdf: set[str]   = set()
    papers:   list[dict] = []

    for nbk_id, uid in zip(nbk_ids, uids):
        pdf_urls = _pdf_candidates(nbk_id)
        first    = pdf_urls[0]
        if first in seen_pdf:
            continue
        seen_pdf.add(first)

        meta      = summary.get(uid, {}) if isinstance(summary, dict) else {}
        title     = _get_title(meta) or f"NCBI Bookshelf {nbk_id}"
        raw_date  = (meta.get("sortpubdate") or meta.get("pubdate") or "") if isinstance(meta, dict) else ""
        year      = raw_date[:4] if raw_date else ""
        authors: list[str] = [
            a.get("name", "")
            for a in ((meta.get("authors") or []) if isinstance(meta, dict) else [])
            if isinstance(a, dict) and a.get("name")
        ]
        publisher = _clean((meta.get("publishername") or "") if isinstance(meta, dict) else "") or "NCBI Bookshelf"
        book_url  = f"{BOOKSHELF}/{nbk_id}/"
        doi       = (meta.get("doi") if isinstance(meta, dict) else None) or None

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               doi,
            "title":             title,
            "abstract":          f"NCBI Bookshelf. Publisher: {publisher}. Full text: {book_url}"[:400],
            "authors":           authors,
            "journal":           publisher,
            "year":              year,
            "epmc_url":          book_url,
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  True,
            "api_pdf_url_count": len(pdf_urls),
        })

        if len(papers) >= max_results:
            break

    if not papers:
        return _empty()

    return dict(
        query_sent=query, api_url=api_url,
        papers_returned=len(papers), papers_with_pmcid=0,
        response_time_ms=int((time.monotonic() - t0) * 1000),
        status="ok", error=None, papers=papers,
    )


# ── public: download_pdf ──────────────────────────────────────────────────────

async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    """
    Direct httpx download from NCBI Bookshelf.
    Uses %PDF magic-byte check (reliable even when Content-Type is wrong).
    Chapters and missing IDs return 404 — only real root-book PDFs succeed.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return dict(status="no_url", pdf_path=None,
                    pdf_source=None, file_size_kb=None, attempts=[])

    attempts: list[dict] = []

    async with httpx.AsyncClient(
        headers=PDF_HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.2)
            attempt: dict = {
                "url": url, "attempt_no": i,
                "http_status": None, "content_type": None,
                "is_pdf": False, "success": False,
                "file_size_kb": None, "error": None,
                "attempted_at": datetime.utcnow(),
            }
            try:
                r  = await c.get(url)
                ct = r.headers.get("content-type", "")
                # Magic-byte check is most reliable;
                # CDNs sometimes serve PDFs as application/octet-stream
                is_pdf = r.content[:4] == b"%PDF" or "pdf" in ct.lower()

                attempt.update(
                    http_status=r.status_code,
                    content_type=ct[:128],
                    is_pdf=is_pdf,
                )

                if r.status_code == 200 and is_pdf:
                    fname = url.rstrip("/").split("/")[-1].split("?")[0]
                    if not fname.lower().endswith(".pdf"):
                        fname = f"medline_{i}.pdf"
                    path = pdf_dir / fname
                    path.write_bytes(r.content)
                    sz = len(r.content) // 1024
                    attempt.update(success=True, file_size_kb=sz)
                    attempts.append(attempt)
                    return dict(
                        status="success", pdf_path=str(path),
                        pdf_source=url, file_size_kb=sz, attempts=attempts,
                    )

            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"

            attempts.append(attempt)

    return dict(
        status="failed", pdf_path=None,
        pdf_source=None, file_size_kb=None, attempts=attempts,
    )
