"""
EPA HERO (Health & Environmental Research Online) scraper.

Source: https://hero.epa.gov/
        EPA's curated reference database used in risk assessments (IRIS, PPRTV, etc.).

STRATEGY
────────
1. Scrape HERO HTML search pages (paginated, up to MAX_PAGES) to collect
   reference containers. Each card has ref_id, DOI, PMID, title, authors, year.

2. For EACH reference, try PDF strategies in order (stop at first success):
   A. Europe PMC (DOI or PMID)  → europepmc.org?pdf=render  [no Cloudflare]
   B. OpenAlex   (DOI)          → checks ALL locations, prefers repo copies
   C. Semantic Scholar (DOI/PMID) → openAccessPdf.url

3. Only include papers where a downloadable PDF URL was found.

4. download_pdf: direct GET → %PDF magic-byte check → save.

NOTE: verify=False is required on this machine due to SSL certificate chain issues.
"""
from __future__ import annotations
import asyncio, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

# ── endpoints ─────────────────────────────────────────────────────────────────
HERO_SEARCH_URL = "https://hero.epa.gov/search/"
HERO_REF_URL    = "https://hero.epa.gov/reference"

OPENALEX_API = "https://api.openalex.org/works"
SS_API       = "https://api.semanticscholar.org/graph/v1/paper"
EPMC_API     = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

TIMEOUT   = 30.0
MAX_PAGES = 10   # 10 pages × 25 refs = 250 refs max per search

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/json,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hero.epa.gov/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')

# Cloudflare-protected publisher domains — avoid their PDF URLs
_CF_DOMAINS = {
    "pubs.acs.org", "aem.asm.org", "journals.asm.org",
    "www.tandfonline.com", "www.sciencedirect.com",
    "link.springer.com", "onlinelibrary.wiley.com",
}


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", str(text or ""))).strip()


def _is_cf_blocked(url: str) -> bool:
    """Return True if the URL is on a known Cloudflare-protected publisher domain."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in _CF_DOMAINS)
    except Exception:
        return False


# ── parse one HTML page of HERO search results ───────────────────────────────

def _parse_page(html: str) -> list[dict]:
    refs: list[dict] = []
    blocks = re.split(r'(?=<div id="reference-container-\d+)', html)

    for block in blocks:
        m = re.match(r'<div id="reference-container-(\d+)"', block)
        if not m:
            continue
        ref_id = m.group(1)

        doi_m  = re.search(r'https?://doi\.org/([^\s"\'<>&]+)', block)
        doi    = doi_m.group(1).strip().rstrip(").,;") if doi_m else ""

        pmid_m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', block)
        pmid   = pmid_m.group(1) if pmid_m else ""

        title_m = re.search(
            r'style="font-size:\s*1\.2rem[^"]*"[^>]*>\s*([^<]{5,300})', block
        )
        title = _clean(title_m.group(1)) if title_m else f"HERO {ref_id}"

        author_m  = re.search(r'<i>([^<]{5,200})</i>', block)
        author_str = _clean(author_m.group(1)) if author_m else ""

        authors: list[str] = []
        year = ""
        if author_str:
            year_m = re.search(r'\b(19|20)\d{2}\b', author_str)
            if year_m:
                year = year_m.group(0)
                author_part = author_str[:year_m.start()].strip().rstrip(",. ")
            else:
                author_part = author_str
            authors = [a.strip() for a in re.split(r"[,;]", author_part) if a.strip()]

        abstract_m = re.search(
            rf'id="reference-{ref_id}-abstract"[^>]*>(.*?)</p>', block, re.S
        )
        abstract = _clean(abstract_m.group(1))[:400] if abstract_m else ""

        type_m = re.search(
            r'wbkt-ellipsis-overflow[^>]*>\s*([A-Za-z][^<]{3,60}?)\s*</i>', block
        )
        journal = _clean(type_m.group(1)) if type_m else "EPA HERO"

        if doi or pmid:   # only keep refs we can look up
            refs.append({
                "ref_id": ref_id, "doi": doi, "pmid": pmid,
                "title": title, "authors": authors, "year": year,
                "journal": journal, "abstract": abstract,
            })

    return refs


# ── PDF strategy A: Europe PMC ────────────────────────────────────────────────

async def _europe_pmc(
    client: httpx.AsyncClient,
    doi: str,
    pmid: str,
    sem: asyncio.Semaphore,
) -> str:
    queries = []
    if pmid:
        queries.append(f"EXT_ID:{pmid} AND SRC:MED")
    if doi:
        queries.append(f"DOI:{doi}")

    async with sem:
        await asyncio.sleep(0.15)
        for q in queries:
            try:
                r = await client.get(
                    EPMC_API,
                    params={"query": q, "resultType": "core", "format": "json"},
                    timeout=TIMEOUT,
                )
                if r.status_code != 200:
                    continue
                results = r.json().get("resultList", {}).get("result", [])
                if not results:
                    continue
                paper = results[0]
                pmcid = paper.get("pmcid", "")
                ft    = paper.get("fullTextUrlList", {}).get("fullTextUrl") or []

                # Prefer europepmc.org — real PDF, no Cloudflare
                for entry in ft:
                    if entry.get("documentStyle") == "pdf":
                        url = entry.get("url", "")
                        if url and "europepmc.org" in url:
                            return url
                # Any other listed PDF
                for entry in ft:
                    if entry.get("documentStyle") == "pdf":
                        url = entry.get("url", "")
                        if url and not _is_cf_blocked(url):
                            return url
                # Build europepmc URL from PMCID
                if pmcid:
                    return f"https://europepmc.org/articles/{pmcid}?pdf=render"
            except Exception:
                pass
    return ""


# ── PDF strategy B: OpenAlex ──────────────────────────────────────────────────

async def _openalex(
    client: httpx.AsyncClient, doi: str, sem: asyncio.Semaphore
) -> str:
    """
    Query OpenAlex for all OA locations for this DOI.
    Prefer non-Cloudflare repository copies over publisher PDFs.
    """
    if not doi:
        return ""
    async with sem:
        await asyncio.sleep(0.15)
        try:
            r = await client.get(
                f"{OPENALEX_API}/doi:{doi}",
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                return ""
            d = r.json()
            all_locs = d.get("locations") or []

            # 1st pass: repository copies (universities, arXiv, PMC…) — no Cloudflare
            for loc in all_locs:
                pdf = loc.get("pdf_url") or ""
                if pdf and not _is_cf_blocked(pdf):
                    return pdf

            # 2nd pass: any PDF (publisher) — may be Cloudflare, but try
            for loc in all_locs:
                pdf = loc.get("pdf_url") or ""
                if pdf:
                    return pdf
        except Exception:
            pass
    return ""


# ── PDF strategy C: Semantic Scholar ─────────────────────────────────────────

async def _semantic_scholar(
    client: httpx.AsyncClient,
    doi: str,
    pmid: str,
    sem: asyncio.Semaphore,
) -> str:
    paper_id = (
        f"DOI:{doi}"   if doi
        else f"PMID:{pmid}" if pmid
        else ""
    )
    if not paper_id:
        return ""
    async with sem:
        await asyncio.sleep(0.2)
        try:
            r = await client.get(
                f"{SS_API}/{paper_id}",
                params={"fields": "openAccessPdf"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                oa  = r.json().get("openAccessPdf") or {}
                url = oa.get("url") or ""
                # Skip Cloudflare-protected publisher URLs
                if url and not _is_cf_blocked(url):
                    return url
                if url:          # return anyway as last resort
                    return url
        except Exception:
            pass
    return ""


# ── resolve one reference to a paper dict ─────────────────────────────────────

async def _resolve_ref(
    ref: dict,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> dict | None:
    doi      = ref["doi"]
    pmid     = ref["pmid"]
    ref_id   = ref["ref_id"]
    hero_url = f"{HERO_REF_URL}/{ref_id}/"

    pdf_url = ""

    # A: Europe PMC — most reliable, no Cloudflare
    if (doi or pmid) and not pdf_url:
        pdf_url = await _europe_pmc(client, doi, pmid, sem)

    # B: OpenAlex — checks ALL locations, prefers repo copies
    if doi and not pdf_url:
        pdf_url = await _openalex(client, doi, sem)

    # C: Semantic Scholar — fallback
    if not pdf_url and (doi or pmid):
        pdf_url = await _semantic_scholar(client, doi, pmid, sem)

    if not pdf_url:
        return None

    return {
        "pmcid":             None,
        "pmid":              pmid or None,
        "doi":               doi or None,
        "title":             ref["title"],
        "abstract":          ref["abstract"],
        "authors":           ref["authors"],
        "journal":           ref["journal"],
        "year":              ref["year"],
        "epmc_url":          hero_url,
        "pdf_urls":          [pdf_url],
        "has_pdf_from_api":  True,
        "api_pdf_url_count": 1,
    }


# ── fetch one search page ─────────────────────────────────────────────────────

async def _fetch_page(
    client: httpx.AsyncClient, query: str, page: int, page_sem: asyncio.Semaphore
) -> list[dict]:
    async with page_sem:
        await asyncio.sleep(0.3 * (page - 1))   # stagger page requests
        params = {"query": query, "query_box": query, "is_expanded": "false"}
        if page > 1:
            params["page"] = page
        try:
            r = await client.get(HERO_SEARCH_URL, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return _parse_page(r.text)
        except Exception:
            pass
    return []


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0      = time.monotonic()
    api_url = f"{HERO_SEARCH_URL}?query={quote(query, safe='')}"

    def _empty(err=None):
        return dict(
            query_sent=query, api_url=api_url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # How many pages do we need?
    # Assume ~30% of refs have open-access PDFs → need ~3.5× refs vs wanted PDFs
    pages_needed = min(MAX_PAGES, max(1, (max_results * 4 + 24) // 25))

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT, verify=False
    ) as client:

        # ── Step 1: fetch all pages concurrently ─────────────────────────────
        page_sem  = asyncio.Semaphore(3)
        page_tasks = [
            _fetch_page(client, query, p, page_sem)
            for p in range(1, pages_needed + 1)
        ]
        page_results = await asyncio.gather(*page_tasks)

        # Deduplicate refs by ref_id
        seen: set[str] = set()
        refs: list[dict] = []
        for page_refs in page_results:
            for ref in page_refs:
                if ref["ref_id"] not in seen:
                    seen.add(ref["ref_id"])
                    refs.append(ref)

        if not refs:
            return _empty()

        # ── Step 2: resolve PDF URLs concurrently ─────────────────────────────
        pdf_sem  = asyncio.Semaphore(4)
        pdf_tasks = [_resolve_ref(ref, client, pdf_sem) for ref in refs]
        results   = await asyncio.gather(*pdf_tasks)

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
        headers=PDF_HEADERS, follow_redirects=True, timeout=TIMEOUT, verify=False
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
