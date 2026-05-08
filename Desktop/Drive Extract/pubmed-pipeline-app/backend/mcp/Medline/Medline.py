"""
Medline / NCBI Bookshelf scraper.

Source: https://www.ncbi.nlm.nih.gov/books/
        NLM's database of full-text biomedical books and reports.
        Includes ATSDR toxicological profiles, NTP reports, IARC monographs,
        NLM publications — all with downloadable PDFs.

WHY CHAPTERS ARE RETURNED
──────────────────────────
NCBI db=books esearch returns BOTH root books AND individual chapters as
separate records.  Any field qualifier like [TITL] is silently ignored by
the books database.  So a search for "Benzene" returns:

  NBK591286  root book  "Toxicological Profile for Benzene"        ← want this
  NBK591289  chapter    "Cancer in Humans"  (inside NBK591286)     ← skip
  NBK591291  chapter    "Exposure Characterization"                 ← skip
  ...

FIX: CLIENT-SIDE TITLE WORD FILTER
────────────────────────────────────
After esummary, check whether ANY word from the query appears in the
record's title (case-insensitive whole-word match).

  "Toxicological Profile for Benzene" → contains "benzene" → KEEP ✓
  "Cancer in Humans"                  → no query word    → SKIP ✗
  "Exposure Characterization"         → no query word    → SKIP ✗
  "Table 1.1. Analytical methods…"    → no query word    → SKIP ✗

For multi-word queries like "styrene butadiene", ANY matching word keeps
the record, so relevant books aren't dropped.

PDF DETECTION (4 layers)
─────────────────────────
For each kept UID, fetch its Bookshelf HTML page:
  A. PDF href links  href="/books/.../pdf/...pdf"
  B. Full NCBI PDF URL anywhere in the page
  C. <link rel="alternate" type="application/pdf"> tag
  D. Constructed URL: Bookshelf_NBK{id}.pdf
     + alternative without "Bookshelf_" prefix: {id}.pdf

If the page has NO PDF links (= chapter that slipped through):
  → Detect parent book from breadcrumb nav (first non-self NBK link)
  → Fetch parent page → apply layers A–D there instead

DOWNLOAD
────────
Direct httpx GET — NCBI serves PDFs as plain HTTP, no browser needed.
Detected by Content-Type header OR %PDF magic bytes.
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
    "Referer":         "https://www.ncbi.nlm.nih.gov/books/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# ── regexes ───────────────────────────────────────────────────────────────────
# A: href PDF link e.g.  href="/books/NBK591286/pdf/Bookshelf_NBK591286.pdf"
_PDF_HREF_RE  = re.compile(r'href\s*=\s*["\']\s*(/books/[^"\']+\.pdf)\s*["\']', re.I)
# B: full NCBI PDF URL anywhere in page source
_PDF_FULL_RE  = re.compile(
    r'(https?://www\.ncbi\.nlm\.nih\.gov/books/[^"\'<>\s]+\.pdf)', re.I
)
# C: <link rel="alternate" type="application/pdf" href="...">
_PDF_LINK_TAG = re.compile(
    r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']'
    r'|<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/pdf["\']',
    re.I,
)
# Parent book: any /books/NBK{id}/ link in page breadcrumb
_NBK_LINK_RE  = re.compile(r'/books/(NBK\d+)/', re.I)

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


def _pdf_candidates(nbk_id: str) -> list[str]:
    """
    Two constructed PDF URL patterns NCBI uses (try both):
      1. Bookshelf_NBK{id}.pdf  — most common
      2. NBK{id}.pdf            — used by some older/smaller books
    """
    return [
        f"https://www.ncbi.nlm.nih.gov/books/{nbk_id}/pdf/Bookshelf_{nbk_id}.pdf",
        f"https://www.ncbi.nlm.nih.gov/books/{nbk_id}/pdf/{nbk_id}.pdf",
    ]


def _extract_pdf_links(html: str) -> list[str]:
    """Pull all distinct NCBI PDF URLs from a page's HTML (layers A + B + C)."""
    links: list[str] = []

    # Layer A: /books/.../pdf/...pdf hrefs
    for m in _PDF_HREF_RE.finditer(html):
        href = "https://www.ncbi.nlm.nih.gov" + m.group(1)
        if href not in links:
            links.append(href)

    # Layer B: full NCBI PDF URL anywhere in source
    for m in _PDF_FULL_RE.finditer(html):
        href = m.group(1)
        if href not in links:
            links.append(href)

    # Layer C: <link rel="alternate" type="application/pdf">
    for m in _PDF_LINK_TAG.finditer(html):
        href = m.group(1) or m.group(2) or ""
        if href:
            if href.startswith("/"):
                href = "https://www.ncbi.nlm.nih.gov" + href
            if href not in links:
                links.append(href)

    return links


def _find_parent_nbk(html: str, current_nbk: str) -> str | None:
    """
    Chapter pages have a breadcrumb like:
      Bookshelf > Toxicological Profile for Benzene > Cancer in Humans
    The parent book link /books/NBK{parent}/ appears in the first ~8000 chars.
    Return the first NBK ID that is NOT the current page's own ID.
    """
    for m in _NBK_LINK_RE.finditer(html[:8000]):
        candidate = m.group(1)
        if candidate != current_nbk:
            return candidate
    return None


def _title_matches_query(title: str, query: str) -> bool:
    """
    Return True if ANY word from the query appears as a whole word in the title.
    Case-insensitive.

    Examples (query="Benzene"):
      "Toxicological Profile for Benzene."  → True   (root book ✓)
      "Cancer in Humans"                    → False  (chapter ✗)
      "Table 1.1. Analytical methods…"      → False  (chapter ✗)
    """
    query_words = set(re.findall(r'[a-z]{3,}', query.lower()))  # words ≥3 chars
    title_lower = title.lower()
    return any(
        bool(re.search(rf'\b{re.escape(w)}\b', title_lower))
        for w in query_words
    )


# ── fetch PDF links for one Bookshelf page ────────────────────────────────────

async def _fetch_pdf_links(
    client: httpx.AsyncClient,
    nbk_id: str,
    sem: asyncio.Semaphore,
) -> list[str]:
    """
    Fetch nbk_id's page and return all PDF URLs (HTML extraction + constructed).

    If the page has no PDF links (chapter):
      → find parent book from breadcrumb
      → fetch parent page
      → return parent's PDF links + parent's constructed candidates
    """
    async with sem:
        await asyncio.sleep(0.15)
        try:
            r = await client.get(f"{BOOKSHELF_BASE}/{nbk_id}/", timeout=TIMEOUT)
            html = r.text if r.status_code == 200 else ""
        except Exception:
            html = ""

    # Try to extract PDF links from this page
    pdf_links = _extract_pdf_links(html)
    if pdf_links:
        # Root book — real links found.  Append constructed fallbacks.
        for c in _pdf_candidates(nbk_id):
            if c not in pdf_links:
                pdf_links.append(c)
        return pdf_links

    # No PDF links → likely a chapter.  Find the parent root book.
    parent_nbk = _find_parent_nbk(html, nbk_id) if html else None

    if parent_nbk:
        try:
            rp = await client.get(
                f"{BOOKSHELF_BASE}/{parent_nbk}/", timeout=TIMEOUT
            )
            if rp.status_code == 200:
                parent_links = _extract_pdf_links(rp.text)
                if parent_links:
                    for c in _pdf_candidates(parent_nbk):
                        if c not in parent_links:
                            parent_links.append(c)
                    return parent_links
        except Exception:
            pass
        # Parent page fetch failed — use constructed URLs for parent
        return _pdf_candidates(parent_nbk)

    # No parent found — constructed fallback for current ID
    return _pdf_candidates(nbk_id)


# ── public search ─────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search NCBI Bookshelf for books whose TITLE mentions the chemical.

    After esummary, applies a client-side title-word filter:
    records whose title shares no word with the query (= chapters/sections)
    are silently dropped.  Only genuine book entries proceed.
    """
    t0 = time.monotonic()

    # Over-fetch because many results will be chapters that get filtered out
    search_params = {
        "db":         "books",
        "term":       query,
        "retmax":     min(max_results * 10, 200),
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

    # ── Step 2b: title-word filter — drop chapters before fetching pages ──────
    # Chapters like "Cancer in Humans", "Methods", "Table 1.1…" share no words
    # with the query chemical name → filtered here, saving unnecessary HTTP calls
    filtered_uids: list[str] = []
    for uid in uid_list:
        meta  = summary_result.get(uid, {}) if isinstance(summary_result, dict) else {}
        title = _clean(meta.get("title") or "")
        if not title or _title_matches_query(title, query):
            filtered_uids.append(uid)   # keep if title matches OR if title unknown

    if not filtered_uids:
        # All were chapters — nothing relevant found
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # ── Step 3: fetch only title-filtered pages concurrently ──────────────────
    nbk_ids = [f"NBK{uid}" for uid in filtered_uids]
    sem = asyncio.Semaphore(3)

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        pdf_results = await asyncio.gather(
            *[_fetch_pdf_links(client, nbk_id, sem) for nbk_id in nbk_ids],
            return_exceptions=True,
        )

    # ── Step 4: build papers list with deduplication ──────────────────────────
    seen_first_pdf: set[str] = set()
    papers: list[dict] = []

    for uid, nbk_id, pdf_result in zip(filtered_uids, nbk_ids, pdf_results):
        pdf_links: list[str] = (
            pdf_result if isinstance(pdf_result, list) else []
        )
        if not pdf_links:
            continue

        # Deduplicate: skip if we already have this exact PDF queued
        first_pdf = pdf_links[0]
        if first_pdf in seen_first_pdf:
            continue
        seen_first_pdf.add(first_pdf)

        meta      = summary_result.get(uid, {}) if isinstance(summary_result, dict) else {}
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
                f"Full text: {book_url}"
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
    NCBI serves PDFs as plain HTTP — no browser needed.
    Detected by Content-Type header OR %PDF magic bytes.
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
