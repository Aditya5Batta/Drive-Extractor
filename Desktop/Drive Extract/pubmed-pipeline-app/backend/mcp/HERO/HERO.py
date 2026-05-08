"""
EPA HERO (Health & Environmental Research Online) scraper.

Source: https://hero.epa.gov/
        EPA's curated database of scientific references used in risk
        assessments, IRIS evaluations, PPRTV assessments, and more.

STRATEGY
────────
1. HERO JSON search API:
   GET https://hero.epa.gov/hero/ws/references/search.json
       ?query={chemical}&max={N}&offset=0&sort=score&order=desc
   → returns references with DOI, PubMed ID, title, authors, year, source.

2. For each reference that has a DOI, query Unpaywall:
   GET https://api.unpaywall.org/v2/{doi}?email={EMAIL}
   → returns the best open-access PDF URL (repository, PubMed Central,
     publisher open-access page, etc.)

3. For references that have a direct URL in the HERO record (technical
   reports often do), try that URL directly as an extra candidate.

4. Only include papers where at least one PDF URL was found.

5. download_pdf: direct GET → %PDF magic-byte check → save.

NOTE: Many HERO records are paywalled journal articles.  Unpaywall
      finds PDFs only for open-access or accepted-manuscript copies.
      Technical reports and government publications tend to fare better.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

HERO_SEARCH  = "https://hero.epa.gov/hero/ws/references/search.json"
HERO_REF_URL = "https://hero.epa.gov/reference"
UNPAYWALL    = "https://api.unpaywall.org/v2"
UW_EMAIL     = "research.pipeline@example.com"   # Unpaywall requires an e-mail
TIMEOUT      = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json,text/html,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hero.epa.gov/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", text or "")).strip()


def _best_oa_pdf(uw: dict) -> str:
    """
    Extract the best open-access PDF URL from an Unpaywall record.
    Priority: best_oa_location → any oa_location with url_for_pdf.
    """
    best = uw.get("best_oa_location") or {}
    url  = best.get("url_for_pdf") or best.get("url") or ""
    if url and url.lower().endswith(".pdf") or "pdf" in url.lower():
        return url
    # url_for_pdf is the direct PDF; url may be a landing page — prefer url_for_pdf
    url = best.get("url_for_pdf") or ""
    if url:
        return url
    for loc in (uw.get("oa_locations") or []):
        u = loc.get("url_for_pdf") or ""
        if u:
            return u
    return ""


async def _unpaywall_pdf(
    client: httpx.AsyncClient, doi: str, sem: asyncio.Semaphore
) -> str:
    """Return the best open-access PDF URL for a DOI, or '' if not found."""
    async with sem:
        await asyncio.sleep(0.2)
        try:
            r = await client.get(
                f"{UNPAYWALL}/{quote(doi, safe='')}",
                params={"email": UW_EMAIL},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                return _best_oa_pdf(r.json())
        except Exception:
            pass
    return ""


async def _resolve_ref(
    ref: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> dict | None:
    """
    Given one HERO search result, find its PDF URL and return a paper dict.
    Returns None if no PDF can be found.
    """
    ref_id  = ref.get("referenceid") or ref.get("referenceId") or ""
    doi     = _clean(ref.get("doi") or "")
    pmid    = str(ref.get("pubmedid") or ref.get("pubmedId") or "")
    title   = _clean(ref.get("title") or f"HERO {ref_id}")
    year    = str(ref.get("year") or "")
    journal = _clean(ref.get("source") or "EPA HERO")
    abstract = _clean(ref.get("abstract") or "")[:400]

    # Build authors list
    raw_auth = ref.get("authors") or ref.get("authorList") or ""
    if isinstance(raw_auth, list):
        authors = [_clean(a) for a in raw_auth if a]
    else:
        authors = [a.strip() for a in re.split(r"[;,]", str(raw_auth)) if a.strip()]

    hero_url = f"{HERO_REF_URL}/{ref_id}/" if ref_id else ""

    # Candidate PDF URLs
    pdf_urls: list[str] = []

    # ── 1. Try Unpaywall with DOI ──────────────────────────────────────────────
    if doi:
        uw_url = await _unpaywall_pdf(client, doi, sem)
        if uw_url:
            pdf_urls.append(uw_url)

    # ── 2. Try direct URL from HERO record (technical reports often have one) ──
    direct = _clean(ref.get("url") or ref.get("pdfUrl") or "")
    if direct and direct not in pdf_urls:
        pdf_urls.append(direct)

    if not pdf_urls:
        return None

    return {
        "pmcid":             None,
        "pmid":              pmid or None,
        "doi":               doi or None,
        "title":             title,
        "abstract":          abstract,
        "authors":           authors,
        "journal":           journal,
        "year":              year,
        "epmc_url":          hero_url,
        "pdf_urls":          pdf_urls,
        "has_pdf_from_api":  True,
        "api_pdf_url_count": len(pdf_urls),
    }


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0  = time.monotonic()
    url = (
        f"{HERO_SEARCH}?query={quote(query, safe='')}"
        f"&max={min(max_results * 5, 100)}&offset=0&sort=score&order=desc"
    )

    def _empty(err=None):
        return dict(
            query_sent=query, api_url=url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # ── Step 1: HERO search API ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _empty(str(exc))

    # HERO returns results under "references" key
    refs: list[dict] = (
        data.get("references")
        or data.get("data")
        or data.get("results")
        or []
    )
    if not refs:
        return _empty()

    # ── Step 2: resolve PDF URLs concurrently ─────────────────────────────────
    sem = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        tasks = [
            _resolve_ref(ref, client, sem)
            for ref in refs[:max_results * 3]
        ]
        results = await asyncio.gather(*tasks)

    papers: list[dict] = [p for p in results if p is not None][:max_results]

    return dict(
        query_sent=query, api_url=url,
        papers_returned=len(papers), papers_with_pmcid=0,
        response_time_ms=int((time.monotonic() - t0) * 1000),
        status="ok", error=None, papers=papers,
    )


# ── public: download_pdf ──────────────────────────────────────────────────────

async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    """
    Direct httpx download.  %PDF magic-byte confirms actual PDF content.
    Unpaywall URLs redirect to the actual file — follow_redirects handles it.
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
            await asyncio.sleep(0.3)
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
                    # Build a safe filename from the URL
                    fname = url.rstrip("/").split("/")[-1].split("?")[0]
                    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[:80]
                    if not fname.lower().endswith(".pdf"):
                        fname = f"hero_{i}.pdf"
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
