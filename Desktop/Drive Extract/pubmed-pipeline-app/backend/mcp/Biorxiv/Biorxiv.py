"""
bioRxiv preprint scraper.

Source: https://www.biorxiv.org/
        Cold Spring Harbor Laboratory's preprint server for biology.

STRATEGY
────────
1. GET https://www.biorxiv.org/search/{query}?numresults={N}&sort=relevance-rank
   → HTML search results page, extract versioned DOI paths from content links
     e.g. /content/10.1101/2025.09.04.674095v2

2. For each DOI, call the bioRxiv API to get clean metadata:
   https://api.biorxiv.org/details/biorxiv/{base_doi}
   Returns title, authors, abstract, date, version.

3. PDF URL = https://www.biorxiv.org/content/{doi}v{version}.full.pdf
   bioRxiv serves PDFs at this predictable URL — no HTML scraping needed.

4. download_pdf: GET + %PDF magic-byte check → save.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

SEARCH_URL = "https://www.biorxiv.org/search"
API_URL    = "https://api.biorxiv.org/details/biorxiv"
BASE_URL   = "https://www.biorxiv.org"
TIMEOUT    = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.biorxiv.org/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

# /content/10.1101/YYYY.MM.DD.NNNNNNvN links in search results
_CONTENT_RE = re.compile(
    r'href=["\']/(content/(10\.\d{4}/[^"\'#?\s]+))["\']',
    re.I,
)
# Strip trailing version: "10.1101/2025.09.04.674095v2" → "10.1101/2025.09.04.674095"
_VERSION_RE = re.compile(r'v\d+$')

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


async def _get_metadata(
    client: httpx.AsyncClient, base_doi: str, versioned_path: str, sem: asyncio.Semaphore
) -> dict:
    """
    Call bioRxiv API for one DOI.  Falls back to minimal metadata on any error.
    """
    async with sem:
        await asyncio.sleep(0.15)
        try:
            r = await client.get(f"{API_URL}/{base_doi}", timeout=TIMEOUT)
            if r.status_code == 200:
                col = r.json().get("collection", [])
                if col:
                    rec     = col[-1]      # latest version
                    version = rec.get("version", "1")
                    doi     = rec.get("doi", base_doi)
                    pdf_url = f"{BASE_URL}/content/{doi}v{version}.full.pdf"
                    authors = [
                        a.strip()
                        for a in re.split(r"[;,]", rec.get("authors", ""))
                        if a.strip()
                    ]
                    return {
                        "pmcid": None, "pmid": None,
                        "doi":   doi,
                        "title": _clean(rec.get("title", "") or versioned_path),
                        "abstract": _clean(rec.get("abstract", "") or "")[:400],
                        "authors":  authors,
                        "journal":  "bioRxiv",
                        "year":     (rec.get("date", "") or "")[:4],
                        "epmc_url": f"{BASE_URL}/content/{doi}v{version}",
                        "pdf_urls": [pdf_url],
                        "has_pdf_from_api":  True,
                        "api_pdf_url_count": 1,
                    }
        except Exception:
            pass

    # Fallback — use path directly
    pdf_url = f"{BASE_URL}/content/{versioned_path}.full.pdf"
    return {
        "pmcid": None, "pmid": None, "doi": base_doi,
        "title": versioned_path, "abstract": "", "authors": [],
        "journal": "bioRxiv", "year": "",
        "epmc_url": f"{BASE_URL}/content/{versioned_path}",
        "pdf_urls": [pdf_url],
        "has_pdf_from_api":  True,
        "api_pdf_url_count": 1,
    }


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0 = time.monotonic()
    search_url = (
        f"{SEARCH_URL}/{quote(query, safe='')}"
        f"?numresults={min(max_results * 2, 75)}&sort=relevance-rank&format_result=condensed"
    )

    def _empty(err=None):
        return dict(
            query_sent=query, api_url=search_url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # ── Step 1: fetch search HTML ─────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as c:
            r = await c.get(search_url)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        return _empty(str(exc))

    # ── Step 2: extract versioned DOI paths ───────────────────────────────────
    seen_doi:   set[str]   = set()
    versioned:  list[str]  = []   # e.g. "10.1101/2025.09.04.674095v2"

    for m in _CONTENT_RE.finditer(html):
        path     = m.group(2)                     # "10.1101/2025.09.04.674095v2"
        base_doi = _VERSION_RE.sub("", path)      # "10.1101/2025.09.04.674095"
        if base_doi not in seen_doi:
            seen_doi.add(base_doi)
            versioned.append(path)
            if len(versioned) >= max_results * 2:
                break

    if not versioned:
        return _empty()

    # ── Step 3: fetch metadata from bioRxiv API (concurrent) ─────────────────
    sem = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        tasks = [
            _get_metadata(client, _VERSION_RE.sub("", v), v, sem)
            for v in versioned[:max_results]
        ]
        papers = list(await asyncio.gather(*tasks))

    return dict(
        query_sent=query, api_url=search_url,
        papers_returned=len(papers), papers_with_pmcid=0,
        response_time_ms=int((time.monotonic() - t0) * 1000),
        status="ok", error=None, papers=papers,
    )


# ── public: download_pdf ──────────────────────────────────────────────────────

async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    """Direct GET download; %PDF magic-byte validates content."""
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
                is_pdf = r.content[:4] == b"%PDF" or "pdf" in ct.lower()
                attempt.update(http_status=r.status_code,
                               content_type=ct[:128], is_pdf=is_pdf)

                if r.status_code == 200 and is_pdf:
                    fname = url.rstrip("/").split("/")[-1].split("?")[0]
                    if not fname.lower().endswith(".pdf"):
                        fname = f"biorxiv_{i}.pdf"
                    path = pdf_dir / fname
                    path.write_bytes(r.content)
                    sz = len(r.content) // 1024
                    attempt.update(success=True, file_size_kb=sz)
                    attempts.append(attempt)
                    return dict(status="success", pdf_path=str(path),
                                pdf_source=url, file_size_kb=sz, attempts=attempts)
            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
            attempts.append(attempt)

    return dict(status="failed", pdf_path=None,
                pdf_source=None, file_size_kb=None, attempts=attempts)
