"""
EPA HERO (Health & Environmental Research Online) scraper.

Source: https://hero.epa.gov/
        EPA's curated reference database used in risk assessments (IRIS, PPRTV, etc.).

STRATEGY
────────
1. HERO search JSON API → list of references with DOI / PubMed ID / metadata.
   Fallback: scrape HTML search page if JSON returns 0 results.

2. For EACH reference, try three PDF strategies in order (stop at first success):

   A. DOI   → Unpaywall API  (best for recent open-access journal articles)
   B. DOI/PMID → Semantic Scholar openAccessPdf  (ArXiv, repository copies)
   C. PMID  → Europe PMC     (PubMed Central — government-funded research)

3. Only include papers where a downloadable PDF URL was found.

4. download_pdf: direct GET → %PDF magic-byte check → save.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

# ── endpoints ─────────────────────────────────────────────────────────────────
HERO_SEARCH_API  = "https://hero.epa.gov/hero/ws/references/search.json"
HERO_SEARCH_HTML = "https://hero.epa.gov/search/"
HERO_DETAIL_API  = "https://hero.epa.gov/hero/ws/references/details.json"
HERO_REF_URL     = "https://hero.epa.gov/reference"

UNPAYWALL = "https://api.unpaywall.org/v2"
UW_EMAIL  = "research.pipeline@example.com"

SS_API    = "https://api.semanticscholar.org/graph/v1/paper"
EPMC_API  = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

TIMEOUT = 30.0

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

# Matches "ID: 2598795" in HERO search HTML
_ID_RE    = re.compile(r'\bID[:\s]+(\d{5,10})\b', re.I)
_TAG_RE   = re.compile(r'<[^>]+>')
_WS_RE    = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", str(text or ""))).strip()


# ── PDF strategy A: Unpaywall ─────────────────────────────────────────────────

async def _unpaywall(client: httpx.AsyncClient, doi: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        await asyncio.sleep(0.2)
        try:
            r = await client.get(
                f"{UNPAYWALL}/{quote(doi, safe='')}",
                params={"email": UW_EMAIL}, timeout=TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                best = d.get("best_oa_location") or {}
                url  = best.get("url_for_pdf") or ""
                if url:
                    return url
                for loc in (d.get("oa_locations") or []):
                    url = loc.get("url_for_pdf") or ""
                    if url:
                        return url
        except Exception:
            pass
    return ""


# ── PDF strategy B: Semantic Scholar ─────────────────────────────────────────

async def _semantic_scholar(
    client: httpx.AsyncClient,
    doi: str,
    pmid: str,
    sem: asyncio.Semaphore,
) -> str:
    paper_id = (
        f"DOI:{doi}"  if doi
        else f"PMID:{pmid}" if pmid
        else ""
    )
    if not paper_id:
        return ""
    async with sem:
        await asyncio.sleep(0.25)
        try:
            r = await client.get(
                f"{SS_API}/{paper_id}",
                params={"fields": "openAccessPdf"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                oa = r.json().get("openAccessPdf") or {}
                return oa.get("url") or ""
        except Exception:
            pass
    return ""


# ── PDF strategy C: Europe PMC ────────────────────────────────────────────────

async def _europe_pmc(
    client: httpx.AsyncClient, pmid: str, sem: asyncio.Semaphore
) -> str:
    async with sem:
        await asyncio.sleep(0.2)
        try:
            r = await client.get(
                EPMC_API,
                params={
                    "query":      f"EXT_ID:{pmid} AND SRC:MED",
                    "resultType": "core",
                    "format":     "json",
                },
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                results = r.json().get("resultList", {}).get("result", [])
                if results:
                    paper = results[0]
                    # Check fullTextUrlList for a PDF
                    for entry in (
                        paper.get("fullTextUrlList", {}).get("fullTextUrl") or []
                    ):
                        if entry.get("documentStyle") == "pdf":
                            url = entry.get("url", "")
                            if url:
                                return url
                    # Fallback: PMC PDF URL if PMC ID is present
                    pmcid = paper.get("pmcid", "")
                    if pmcid:
                        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
        except Exception:
            pass
    return ""


# ── resolve one reference to a paper dict ─────────────────────────────────────

async def _resolve_ref(
    ref: dict,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> dict | None:
    ref_id  = str(ref.get("referenceid") or ref.get("referenceId") or ref.get("id") or "")
    doi     = _clean(ref.get("doi") or "").strip()
    pmid    = str(ref.get("pubmedid") or ref.get("pubmedId") or "").strip()
    title   = _clean(ref.get("title") or f"HERO {ref_id}")
    year    = str(ref.get("year") or "")
    journal = _clean(ref.get("source") or "EPA HERO")
    abstract = _clean(ref.get("abstract") or "")[:400]
    hero_url = f"{HERO_REF_URL}/{ref_id}/" if ref_id else ""

    raw_auth = ref.get("authors") or ref.get("authorList") or ""
    if isinstance(raw_auth, list):
        authors = [_clean(a) for a in raw_auth if a]
    else:
        authors = [a.strip() for a in re.split(r"[;,]", str(raw_auth)) if a.strip()]

    pdf_url = ""

    # ── Strategy A: Unpaywall (DOI) ───────────────────────────────────────────
    if doi and not pdf_url:
        pdf_url = await _unpaywall(client, doi, sem)

    # ── Strategy B: Semantic Scholar (DOI or PMID) ────────────────────────────
    if not pdf_url and (doi or pmid):
        pdf_url = await _semantic_scholar(client, doi, pmid, sem)

    # ── Strategy C: Europe PMC (PMID) ─────────────────────────────────────────
    if pmid and not pdf_url:
        pdf_url = await _europe_pmc(client, pmid, sem)

    # ── Strategy D: direct URL from HERO record ───────────────────────────────
    if not pdf_url:
        direct = _clean(ref.get("url") or ref.get("pdfUrl") or "")
        if direct.startswith("http"):
            pdf_url = direct

    if not pdf_url:
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
        "pdf_urls":          [pdf_url],
        "has_pdf_from_api":  True,
        "api_pdf_url_count": 1,
    }


# ── fetch references via JSON API ─────────────────────────────────────────────

async def _json_search(
    client: httpx.AsyncClient, query: str, max_fetch: int
) -> tuple[list[dict], str]:
    """Returns (refs_list, api_url)."""
    url = (
        f"{HERO_SEARCH_API}?query={quote(query, safe='')}"
        f"&max={max_fetch}&offset=0&sortby=score&order=desc"
    )
    try:
        r = await client.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            refs = (
                data.get("references")
                or data.get("data")
                or data.get("results")
                or data.get("searchResults")
                or []
            )
            if isinstance(refs, list) and refs:
                return refs, url
    except Exception:
        pass
    return [], url


# ── fetch reference IDs from HTML search page (fallback) ─────────────────────

async def _html_search_ids(
    client: httpx.AsyncClient, query: str, max_ids: int
) -> list[str]:
    try:
        r = await client.get(
            HERO_SEARCH_HTML,
            params={"query": query, "query_box": query, "is_expanded": "false"},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            ids = list(dict.fromkeys(_ID_RE.findall(r.text)))
            return ids[:max_ids]
    except Exception:
        pass
    return []


async def _fetch_detail(
    client: httpx.AsyncClient, ref_id: str, sem: asyncio.Semaphore
) -> dict:
    async with sem:
        await asyncio.sleep(0.15)
        try:
            r = await client.get(
                HERO_DETAIL_API,
                params={"reference_id": ref_id},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                d = r.json()
                return d.get("data") or d.get("reference") or {}
        except Exception:
            pass
    return {}


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0       = time.monotonic()
    max_fetch = min(max_results * 4, 80)

    def _empty(err=None, url=""):
        return dict(
            query_sent=query, api_url=url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:

        # ── Step 1: get references ────────────────────────────────────────────
        refs, api_url = await _json_search(client, query, max_fetch)

        # Fall back to HTML scraping if JSON API returns nothing
        if not refs:
            detail_sem = asyncio.Semaphore(3)
            ids = await _html_search_ids(client, query, max_fetch)
            if not ids:
                return _empty(url=api_url)
            api_url = f"{HERO_SEARCH_HTML}?query={quote(query, safe='')}"
            # Fetch details for each ID
            detail_tasks = [_fetch_detail(client, rid, detail_sem) for rid in ids]
            details = await asyncio.gather(*detail_tasks)
            refs = [d for d in details if d]

        if not refs:
            return _empty(url=api_url)

        # ── Step 2: resolve PDF URLs (3 strategies) concurrently ─────────────
        sem = asyncio.Semaphore(3)
        tasks = [_resolve_ref(ref, client, sem) for ref in refs[:max_fetch]]
        results = await asyncio.gather(*tasks)

    papers: list[dict] = [p for p in results if p is not None][:max_results]

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
