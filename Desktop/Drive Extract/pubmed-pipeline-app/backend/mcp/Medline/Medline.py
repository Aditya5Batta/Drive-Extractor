"""
Medline / NCBI Bookshelf scraper.

Source: https://www.ncbi.nlm.nih.gov/books/
        NLM's database of full-text biomedical books and reports.
        Includes ATSDR toxicological profiles, NTP reports, IARC monographs,
        NLM publications — all with downloadable PDFs.

THE CORE PROBLEM WITH db=books
────────────────────────────────
NCBI esearch with db=books returns BOTH root books AND individual chapters
as separate records.  E.g. searching "Benzene" returns:
  NBK591286  = root book  "Toxicological Profile for Benzene"        ← has PDF
  NBK591289  = chapter    "Cancer in Humans"  (inside NBK591286)     ← NO PDF
  NBK591291  = chapter    "Exposure Characterization"                 ← NO PDF

Only root book pages have the PDF sidebar link. Chapter pages have no PDF.

SOLUTION: Two-phase approach
  1. Search using {query}[TITL] — searches title field only, so "Cancer in Humans"
     (chapter title, no chemical name) is excluded; root books like
     "Toxicological Profile for Benzene" are included.
  2. For any chapter that slips through (its title does contain the chemical),
     detect the parent book via the breadcrumb nav in the HTML, fetch the
     parent page, and get the PDF from there.

Search
──────
GET eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
    ?db=books&term={query}[TITL]&retmax={n}&retmode=json

Metadata
────────
GET esummary.fcgi?db=books&id={uid,...}&retmode=json

PDF detection (3 layers)
────────────────────────
  A. Parse HTML sidebar of the Bookshelf page: href="/books/NBK.../pdf/...pdf"
  B. Any full NCBI PDF URL appearing anywhere in the page
  C. If page has NO PDF links → chapter page detected → look for parent book
     NBK ID in breadcrumb header → fetch parent page → repeat A/B there
  D. Last resort: constructed URL  Bookshelf_NBK{id}.pdf

Download
────────
Direct httpx GET — NCBI serves PDFs as plain HTTP, no browser needed.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path

import httpx

ESEARCH_URL    = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
BOOKSHELF_BASE = "https://www.ncbi.nlm.nih.gov/books"
TIMEOUT        = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control":   "no-cache",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# PDF link in the sidebar e.g. href="/books/NBK591286/pdf/Bookshelf_NBK591286.pdf"
_PDF_HREF_RE  = re.compile(r'href=["\'](/books/[^"\']*?\.pdf)["\']', re.I)
# Full NCBI PDF URL anywhere in page
_PDF_FULL_RE  = re.compile(
    r'(https?://www\.ncbi\.nlm\.nih\.gov/books/[^"\'<>\s]+\.pdf)', re.I
)
# Any NBK link in the page — used to find parent book from breadcrumb
# e.g.  href="/books/NBK591286/"  or  href="/books/NBK591286"
_NBK_LINK_RE  = re.compile(r'/books/(NBK\d+)(?:/|")', re.I)

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


def _constructed_pdf(nbk_id: str) -> str:
    """Standard NCBI naming: Bookshelf_NBK{id}.pdf — works on root books."""
    return (
        f"https://www.ncbi.nlm.nih.gov/books/{nbk_id}"
        f"/pdf/Bookshelf_{nbk_id}.pdf"
    )


def _extract_pdf_links(html: str) -> list[str]:
    """Pull all distinct NCBI PDF URLs from a page's HTML."""
    links: list[str] = []
    # sidebar hrefs (/books/.../pdf/...pdf)
    for m in _PDF_HREF_RE.finditer(html):
        href = "https://www.ncbi.nlm.nih.gov" + m.group(1)
        if href not in links:
            links.append(href)
    # full URLs anywhere in page
    for m in _PDF_FULL_RE.finditer(html):
        href = m.group(1)
        if href not in links:
            links.append(href)
    return links


def _find_parent_nbk(html: str, current_nbk: str) -> str | None:
    """
    On a chapter page, the breadcrumb/header links to the parent root book.
    Example breadcrumb:  Bookshelf › Toxicological Profile for Benzene › Cancer in Humans
    The parent link /books/NBK591286/ appears in the first ~8000 chars of the page.

    Find the first NBK ID in the top portion of the HTML that is NOT the
    current page's own NBK ID.
    """
    header_html = html[:8000]  # breadcrumb is always near the top
    for m in _NBK_LINK_RE.finditer(header_html):
        candidate = m.group(1)
        if candidate != current_nbk:
            return candidate
    return None


# ── fetch PDF links for one Bookshelf page ────────────────────────────────────

async def _fetch_pdf_links(
    client: httpx.AsyncClient,
    nbk_id: str,
    sem: asyncio.Semaphore,
) -> list[str]:
    """
    Fetch nbk_id's page and return all PDF URLs found.

    If the page has no PDF links (= chapter, not root book):
      → look in the breadcrumb for the parent root book's NBK ID
      → fetch the parent page and get PDFs from there

    Always appends a constructed fallback URL so the list is never empty.
    """
    page_url = f"{BOOKSHELF_BASE}/{nbk_id}/"

    async with sem:
        await asyncio.sleep(0.15)   # NCBI 3 req/s courtesy
        try:
            r = await client.get(page_url, timeout=TIMEOUT)
            html = r.text if r.status_code == 200 else ""
        except Exception:
            html = ""

    # ── Layer A+B: direct PDF links on this page ──────────────────────────────
    pdf_links = _extract_pdf_links(html)
    if pdf_links:
        # Root book: has PDF links. Keep them + add constructed as last fallback
        if _constructed_pdf(nbk_id) not in pdf_links:
            pdf_links.append(_constructed_pdf(nbk_id))
        return pdf_links

    # ── Layer C: no PDF links → this is a chapter page ───────────────────────
    # Find parent root book NBK ID from breadcrumb
    parent_nbk = _find_parent_nbk(html, nbk_id) if html else None

    if parent_nbk:
        # Fetch the parent (root) book page — this WILL have PDF links
        try:
            rp = await client.get(
                f"{BOOKSHELF_BASE}/{parent_nbk}/", timeout=TIMEOUT
            )
            if rp.status_code == 200:
                parent_pdfs = _extract_pdf_links(rp.text)
                if parent_pdfs:
                    # Add parent's constructed fallback too
                    if _constructed_pdf(parent_nbk) not in parent_pdfs:
                        parent_pdfs.append(_constructed_pdf(parent_nbk))
                    return parent_pdfs
        except Exception:
            pass
        # Parent page fetch failed — use constructed URL for parent
        return [_constructed_pdf(parent_nbk)]

    # ── Layer D: absolute last resort — constructed URL for current ID ─────────
    return [_constructed_pdf(nbk_id)]


# ── public search ─────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search NCBI Bookshelf for root books/reports whose TITLE mentions the query.

    Using [TITL] field qualifier means:
      - "Toxicological Profile for Benzene"  → matched  (chemical in title)
      - "Cancer in Humans" (chapter)         → NOT matched (no chemical in title)
      - "Table 1.1. Analytical methods…"     → NOT matched

    For any chapter that slips through (its title also contains the chemical),
    _fetch_pdf_links() detects it has no PDF and climbs to the parent root book.
    """
    t0 = time.monotonic()

    # [TITL] = title field only → returns books, not individual chapters
    search_params = {
        "db":         "books",
        "term":       f"{query}[TITL]",
        "retmax":     min(max_results * 3, 60),
        "retmode":    "json",
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

    # ── Step 2: esummary — all UIDs in one batch request ─────────────────────
    await asyncio.sleep(0.35)
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

    # ── Step 3: fetch each page concurrently (Semaphore=3) ───────────────────
    nbk_ids = [f"NBK{uid}" for uid in uid_list]
    sem = asyncio.Semaphore(3)   # 3 concurrent, respects NCBI 3 req/s limit

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        pdf_results = await asyncio.gather(
            *[_fetch_pdf_links(client, nbk_id, sem) for nbk_id in nbk_ids],
            return_exceptions=True,
        )

    # ── Step 4: build papers list ─────────────────────────────────────────────
    seen_pdfs: set[str] = set()   # deduplicate by first PDF URL (same parent book)
    papers: list[dict] = []

    for uid, nbk_id, pdf_result in zip(uid_list, nbk_ids, pdf_results):
        pdf_links: list[str] = (
            pdf_result if isinstance(pdf_result, list) else []
        )
        if not pdf_links:
            continue

        # Deduplicate: skip if we've already queued this exact PDF
        first_pdf = pdf_links[0]
        if first_pdf in seen_pdfs:
            continue
        seen_pdfs.add(first_pdf)

        meta = summary_result.get(uid, {}) if isinstance(summary_result, dict) else {}

        title     = _clean(meta.get("title") or f"NCBI Bookshelf {nbk_id}")
        raw_date  = meta.get("sortpubdate") or meta.get("pubdate") or ""
        year      = raw_date[:4] if raw_date else ""
        authors   = [
            a.get("name", "") for a in (meta.get("authors") or [])
            if isinstance(a, dict) and a.get("name")
        ]
        publisher = (meta.get("publishername") or "NCBI Bookshelf").strip()
        book_url  = f"{BOOKSHELF_BASE}/{nbk_id}/"

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               meta.get("doi") or None,
            "title":             title,
            "abstract":          (
                f"NCBI Bookshelf. Publisher: {publisher}. "
                f"Full text available at {book_url}"
            )[:400],
            "authors":           authors,
            "journal":           publisher,
            "year":              year,
            "epmc_url":          book_url,
            "pdf_urls":          pdf_links,
            "has_pdf_from_api":  True,
            "api_pdf_url_count": len(pdf_links),
        })

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
    NCBI serves PDFs as plain HTTP responses — no browser needed.
    Detected by content-type header OR %PDF magic bytes.
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
                    # filename e.g. "Bookshelf_NBK591286.pdf"
                    fname = url.rstrip("/").split("/")[-1].split("?")[0]
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
