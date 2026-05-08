"""
Medline / NCBI Bookshelf scraper.

Source: https://www.ncbi.nlm.nih.gov/books/
        NLM's database of full-text biomedical books and reports.
        Includes ATSDR toxicological profiles, NTP reports, NLM publications,
        and thousands of reference textbooks — all with downloadable PDFs.

Search
──────
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
    ?db=books&term={query}&retmax={n}&retmode=json

Returns a list of numeric UIDs.  Each UID maps to NBK{UID} on Bookshelf
e.g. UID 591286  →  https://www.ncbi.nlm.nih.gov/books/NBK591286/

Metadata
────────
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi
    ?db=books&id={uid1,uid2,...}&retmode=json

Returns title, authors, pubdate, publishername per book.

PDF detection
─────────────
Fetch https://www.ncbi.nlm.nih.gov/books/NBK{uid}/
The right-hand sidebar contains PDF links like:
  <a href="/books/NBK591286/pdf/Bookshelf_NBK591286.pdf">PDF version of this title (14M)</a>

We extract all /books/.../pdf/...pdf hrefs from the page HTML.
Papers with NO PDF link are excluded (per user request: ignore those
without a PDF).

Download
────────
Direct httpx GET on the PDF URL — no browser needed, pure HTTP.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

ESEARCH_URL   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
BOOKSHELF_BASE = "https://www.ncbi.nlm.nih.gov/books"
TIMEOUT       = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# Matches any /books/.../pdf/...pdf href inside the Bookshelf page
# e.g. href="/books/NBK591286/pdf/Bookshelf_NBK591286.pdf"
_PDF_RE  = re.compile(r'href=["\'](/books/[^"\']+\.pdf)["\']', re.I)
_TAG_RE  = re.compile(r'<[^>]+>')
_WS_RE   = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


# ── fetch one Bookshelf page ──────────────────────────────────────────────────

async def _fetch_pdf_links(
    client: httpx.AsyncClient,
    nbk_id: str,
    sem: asyncio.Semaphore,
) -> list[str]:
    """
    Fetch the Bookshelf HTML page for nbk_id and return all absolute PDF URLs
    found in the sidebar (links whose href matches /books/.../pdf/...pdf).
    """
    url = f"{BOOKSHELF_BASE}/{nbk_id}/"
    async with sem:
        await asyncio.sleep(0.15)   # stay within NCBI 3 req/s limit
        try:
            r = await client.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                return []
            html = r.text
        except Exception:
            return []

    pdf_links: list[str] = []
    for m in _PDF_RE.finditer(html):
        href = "https://www.ncbi.nlm.nih.gov" + m.group(1)
        if href not in pdf_links:
            pdf_links.append(href)

    return pdf_links


# ── public search ─────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search NCBI Bookshelf for books/reports containing the query.
    Only returns papers that have at least one PDF download link on their page.
    """
    t0 = time.monotonic()

    search_params = {
        "db":       "books",
        "term":     query,
        "retmax":   min(max_results * 3, 100),  # over-fetch; many won't have PDFs
        "retmode":  "json",
        "usehistory": "n",
    }

    # ── Step 1: esearch ───────────────────────────────────────────────────────
    req_obj = httpx.Request("GET", ESEARCH_URL, params=search_params)
    api_url = str(req_obj.url)

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as client:
            r = await client.get(ESEARCH_URL, params=search_params)
            r.raise_for_status()
            esearch = r.json()
    except Exception as exc:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": str(exc), "papers": [],
        }

    uid_list: list[str] = esearch.get("esearchresult", {}).get("idlist", [])
    if not uid_list:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # ── Step 2: esummary — all UIDs in one request ────────────────────────────
    await asyncio.sleep(0.35)   # NCBI rate-limit courtesy
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as client:
            rs = await client.get(ESUMMARY_URL, params={
                "db":      "books",
                "id":      ",".join(uid_list),
                "retmode": "json",
            })
            rs.raise_for_status()
            summary_result = rs.json().get("result", {})
    except Exception:
        summary_result = {}

    # ── Step 3: fetch each Bookshelf page concurrently to find PDF links ──────
    nbk_ids = [f"NBK{uid}" for uid in uid_list]
    sem = asyncio.Semaphore(3)   # max 3 simultaneous page fetches

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        pdf_results = await asyncio.gather(
            *[_fetch_pdf_links(client, nbk_id, sem) for nbk_id in nbk_ids],
            return_exceptions=True,
        )

    # ── Step 4: build papers list — skip those with no PDF ────────────────────
    papers: list[dict] = []
    for uid, nbk_id, pdf_result in zip(uid_list, nbk_ids, pdf_results):
        pdf_links: list[str] = (
            pdf_result if isinstance(pdf_result, list) else []
        )
        # User request: skip papers with no PDF at all
        if not pdf_links:
            continue

        meta = summary_result.get(uid, {}) if isinstance(summary_result, dict) else {}

        title = _clean(meta.get("title") or f"NCBI Bookshelf {nbk_id}")

        # Year: "sortpubdate" = "2023/07/01 00:00" or "pubdate" = "2023 Jul"
        raw_date = meta.get("sortpubdate") or meta.get("pubdate") or ""
        year = raw_date[:4] if raw_date else ""

        # Authors: list of {"name": "...", "authtype": "..."}
        authors_raw = meta.get("authors") or []
        authors = [
            a.get("name", "") for a in authors_raw
            if isinstance(a, dict) and a.get("name")
        ]

        publisher = (meta.get("publishername") or "NCBI Bookshelf").strip()
        bookshelf_url = f"{BOOKSHELF_BASE}/{nbk_id}/"

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               meta.get("doi") or None,
            "title":             title,
            "abstract":          (
                f"NCBI Bookshelf. Publisher: {publisher}. "
                f"Full text available at {bookshelf_url}"
            )[:400],
            "authors":           authors,
            "journal":           publisher,
            "year":              year,
            "epmc_url":          bookshelf_url,
            "pdf_urls":          pdf_links,
            "has_pdf_from_api":  True,
            "api_pdf_url_count": len(pdf_links),
        })

        # Stop once we have enough
        if len(papers) >= max_results:
            break

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


# ── public download ───────────────────────────────────────────────────────────

async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    """
    Direct httpx download of a Bookshelf PDF.
    No browser needed — NCBI serves PDFs as plain HTTP.
    Detects real PDFs by content-type OR %PDF magic bytes.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": [],
        }

    attempts: list[dict] = []

    async with httpx.AsyncClient(
        headers=PDF_HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.3)
            attempt: dict = {
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
                    # Use the filename from the URL path e.g. "Bookshelf_NBK591286.pdf"
                    fname   = url.rstrip("/").split("/")[-1].split("?")[0]
                    if not fname.lower().endswith(".pdf"):
                        fname = f"medline_{i}.pdf"
                    path    = pdf_dir / fname
                    path.write_bytes(r.content)
                    size_kb = len(r.content) // 1024
                    attempt.update(success=True, file_size_kb=size_kb)
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
        "status": "failed", "pdf_path": None,
        "pdf_source": None, "file_size_kb": None,
        "attempts": attempts,
    }
