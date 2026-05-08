"""
Medline / NCBI Bookshelf scraper.

Source: https://www.ncbi.nlm.nih.gov/books/
        NLM's full-text biomedical books and reports (ATSDR profiles,
        NTP reports, IARC monographs, NLM publications…).

STRATEGY
────────
1. esearch(db=books, term=query) → up to 200 UIDs
   NCBI returns BOTH root books AND individual chapters as separate records.
   Any [TITL] field qualifier is silently ignored for db=books.

2. esummary batch → grab titles; client-side title-word filter:
   • chapter titles ("Cancer in Humans", "Exposure Characterization") share
     NO words with the chemical name → drop
   • root book titles ("Toxicological Profile for Benzene") match → keep
   • if title unknown → keep (constructed URL will 404 for real chapters)

3. For each kept UID, build TWO constructed PDF URL candidates — NO HTML
   page fetching required; the URL pattern is consistent across Bookshelf:
     https://www.ncbi.nlm.nih.gov/books/NBK{id}/pdf/Bookshelf_NBK{id}.pdf
     https://www.ncbi.nlm.nih.gov/books/NBK{id}/pdf/NBK{id}.pdf

4. download_pdf tries each URL with GET; %PDF magic-byte check confirms a
   real file.  Chapter UIDs naturally 404; root books return 200 + PDF. ✓
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path

import httpx

ESEARCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
BOOKSHELF    = "https://www.ncbi.nlm.nih.gov/books"
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

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


def _pdf_candidates(nbk_id: str) -> list[str]:
    """Ordered PDF URL candidates for a Bookshelf root-book."""
    return [
        f"{BOOKSHELF}/{nbk_id}/pdf/Bookshelf_{nbk_id}.pdf",
        f"{BOOKSHELF}/{nbk_id}/pdf/{nbk_id}.pdf",
    ]


def _title_matches_query(title: str, query: str) -> bool:
    """
    True if ANY word ≥3 chars from `query` appears as a whole word in `title`.

    query="Benzene":
      "Toxicological Profile for Benzene"  → True   keep (root book)
      "Cancer in Humans"                   → False  drop (chapter)
      "Exposure Characterization"          → False  drop (chapter)
    """
    words = set(re.findall(r'[a-z]{3,}', query.lower()))
    lo    = title.lower()
    return any(bool(re.search(rf'\b{re.escape(w)}\b', lo)) for w in words)


def _get_title(meta: object) -> str:
    """Extract title from esummary record; try several field names."""
    if not isinstance(meta, dict):
        return ""
    for field in ("title", "booktitle", "chaptertitle", "bookname"):
        val = _clean(meta.get(field) or "")
        if val:
            return val
    return ""


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search NCBI Bookshelf.  Returns root-book records with constructed PDF URLs.
    No HTML page fetching — the Bookshelf PDF URL pattern is consistent.
    """
    t0 = time.monotonic()

    search_params = {
        "db":         "books",
        "term":       query,
        "retmax":     min(max_results * 10, 200),
        "retmode":    "json",
        "usehistory": "n",
    }
    api_url = str(httpx.Request("GET", ESEARCH_URL, params=search_params).url)

    def _empty(err=None):
        return dict(
            query_sent=query, api_url=api_url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # ── Step 1: esearch ───────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as c:
            er = await c.get(ESEARCH_URL, params=search_params)
            er.raise_for_status()
            uid_list: list[str] = er.json().get("esearchresult", {}).get("idlist", [])
    except Exception as exc:
        return _empty(str(exc))

    if not uid_list:
        return _empty()

    # ── Step 2: esummary batch ────────────────────────────────────────────────
    await asyncio.sleep(0.35)
    summary: dict = {}
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as c:
            sr = await c.get(ESUMMARY_URL, params={
                "db":      "books",
                "id":      ",".join(uid_list),
                "retmode": "json",
            })
            sr.raise_for_status()
            summary = sr.json().get("result", {}) or {}
    except Exception:
        summary = {}

    # ── Step 2b: client-side title-word filter ────────────────────────────────
    # Drop chapters whose titles share no word with the query chemical name.
    # If title is unknown, keep the UID — constructed PDF URL will 404 for
    # real chapters and succeed for root books.
    filtered: list[str] = []
    for uid in uid_list:
        meta  = summary.get(uid, {}) if isinstance(summary, dict) else {}
        title = _get_title(meta)
        if not title or _title_matches_query(title, query):
            filtered.append(uid)

    if not filtered:
        return _empty()

    # ── Step 3: build paper records (NO HTML page fetching) ───────────────────
    # PDF URLs are constructed from the NBK ID — no need to scrape pages.
    # Chapters slip-through → both URLs return 404 in download_pdf (expected).
    seen:   set[str]  = set()
    papers: list[dict] = []

    for uid in filtered:
        nbk_id   = f"NBK{uid}"
        pdf_urls = _pdf_candidates(nbk_id)

        # Deduplicate: same root book can appear as multiple chapter records
        first = pdf_urls[0]
        if first in seen:
            continue
        seen.add(first)

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

    Uses %PDF magic-byte check (more reliable than Content-Type alone —
    some CDNs serve PDFs as application/octet-stream).
    Chapter UIDs return 404; only root books with real PDFs succeed.
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
                # Magic-byte check first — more reliable than Content-Type
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
