"""
EPA HERO (Health & Environmental Research Online) scraper.

Source: https://hero.epa.gov/
        EPA's curated reference database used in risk assessments (IRIS, PPRTV, etc.).

STRATEGY
────────
1. Scrape HERO HTML search page → parse reference containers to extract
   reference ID, DOI, PubMed ID, title, authors, year per paper.

2. For EACH reference, try PDF strategies in order (stop at first success):
   A. DOI   → Unpaywall API  (best for recent open-access journal articles)
   B. DOI/PMID → Semantic Scholar openAccessPdf  (ArXiv, repository copies)
   C. PMID  → Europe PMC     (PubMed Central — government-funded research)
   D. Direct URL from HERO record (if present)

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
    "Accept":          "text/html,application/json,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://hero.epa.gov/",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE  = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub("", str(text or ""))).strip()


# ── parse reference containers from HERO search HTML ──────────────────────────

def _parse_search_html(html: str, max_refs: int) -> list[dict]:
    """
    Parse HERO search result HTML.
    Each reference card is a div with id="reference-container-XXXXX".
    Extracts: ref_id, doi, pmid, title, authors, year.
    """
    refs: list[dict] = []

    # Split on reference container boundaries
    blocks = re.split(r'(?=<div id="reference-container-\d+)', html)

    for block in blocks:
        if len(refs) >= max_refs:
            break

        m = re.match(r'<div id="reference-container-(\d+)"', block)
        if not m:
            continue
        ref_id = m.group(1)

        # DOI link: href="https://doi.org/XXXX"
        doi_m = re.search(r'https?://doi\.org/([^\s"\'<>&]+)', block)
        doi = doi_m.group(1).strip().rstrip(").,;") if doi_m else ""

        # PubMed link: href="https://pubmed.ncbi.nlm.nih.gov/XXXXX/"
        pmid_m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', block)
        pmid = pmid_m.group(1) if pmid_m else ""

        # Title: in a <p> with font-size: 1.2rem style
        title_m = re.search(
            r'style="font-size:\s*1\.2rem[^"]*"[^>]*>\s*([^<]{5,300})', block
        )
        title = _clean(title_m.group(1)) if title_m else f"HERO {ref_id}"

        # Author + year: <i>Author et al. YYYY</i>
        author_m = re.search(r'<i>([^<]{5,200})</i>', block)
        author_str = _clean(author_m.group(1)) if author_m else ""

        # Split author string into author list + year
        authors: list[str] = []
        year = ""
        if author_str:
            # Format is typically "Smith AB et al. 2005" or "Smith AB,  Jones C 2003"
            year_m = re.search(r'\b(19|20)\d{2}\b', author_str)
            if year_m:
                year = year_m.group(0)
                author_part = author_str[:year_m.start()].strip().rstrip(",. ")
            else:
                author_part = author_str
            authors = [a.strip() for a in re.split(r"[,;]", author_part) if a.strip()]

        # Abstract: in <p id="reference-XXXXX-abstract">
        abstract_m = re.search(
            rf'id="reference-{ref_id}-abstract"[^>]*>(.*?)</p>', block, re.S
        )
        abstract = ""
        if abstract_m:
            abstract = _clean(abstract_m.group(1))[:400]

        # Publication type (Journal Article, Report, etc.)
        type_m = re.search(r'wbkt-ellipsis-overflow[^>]*>\s*([A-Za-z][^<]{3,60}?)\s*</i>', block)
        journal = _clean(type_m.group(1)) if type_m else "EPA HERO"

        refs.append({
            "ref_id":   ref_id,
            "doi":      doi,
            "pmid":     pmid,
            "title":    title,
            "authors":  authors,
            "year":     year,
            "journal":  journal,
            "abstract": abstract,
        })

    return refs


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
        f"DOI:{doi}"   if doi
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
# Searches by DOI or PMID → prefers europepmc.org?pdf=render (avoids Cloudflare)

async def _europe_pmc(
    client: httpx.AsyncClient,
    doi: str,
    pmid: str,
    sem: asyncio.Semaphore,
) -> str:
    """
    Search Europe PMC by PMID (primary) or DOI (fallback).
    Prefer europepmc.org/articles/PMC...?pdf=render — it's a real PDF download
    and avoids Cloudflare-protected publisher sites.
    NEVER return ncbi.nlm.nih.gov/pmc/.../pdf/ — it returns 1 KB HTML, not PDF.
    """
    queries = []
    if pmid:
        queries.append(f"EXT_ID:{pmid} AND SRC:MED")
    if doi:
        queries.append(f"DOI:{doi}")

    async with sem:
        await asyncio.sleep(0.2)
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
                paper  = results[0]
                pmcid  = paper.get("pmcid", "")
                ft_urls = (
                    paper.get("fullTextUrlList", {}).get("fullTextUrl") or []
                )
                # Prefer europepmc.org?pdf=render — real PDF, no Cloudflare
                for entry in ft_urls:
                    if entry.get("documentStyle") == "pdf":
                        url = entry.get("url", "")
                        if url and "europepmc.org" in url:
                            return url
                # Then try any other pdf URL from the list
                for entry in ft_urls:
                    if entry.get("documentStyle") == "pdf":
                        url = entry.get("url", "")
                        if url:
                            return url
                # Fallback: build europepmc.org URL from PMCID
                if pmcid:
                    return f"https://europepmc.org/articles/{pmcid}?pdf=render"
            except Exception:
                pass
    return ""


# ── resolve one parsed reference to a paper dict ──────────────────────────────

async def _resolve_ref(
    ref: dict,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> dict | None:
    ref_id   = ref["ref_id"]
    doi      = ref["doi"]
    pmid     = ref["pmid"]
    title    = ref["title"]
    year     = ref["year"]
    journal  = ref["journal"]
    abstract = ref["abstract"]
    authors  = ref["authors"]
    hero_url = f"{HERO_REF_URL}/{ref_id}/"

    pdf_url = ""

    # ── Strategy A: Europe PMC (DOI or PMID) — most reliable, no Cloudflare ───
    if (doi or pmid) and not pdf_url:
        pdf_url = await _europe_pmc(client, doi, pmid, sem)

    # ── Strategy B: Semantic Scholar (DOI or PMID) ────────────────────────────
    if not pdf_url and (doi or pmid):
        pdf_url = await _semantic_scholar(client, doi, pmid, sem)

    # ── Strategy C: Unpaywall (DOI) ───────────────────────────────────────────
    if doi and not pdf_url:
        pdf_url = await _unpaywall(client, doi, sem)

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


# ── public: search ────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    t0        = time.monotonic()
    max_fetch = min(max_results * 4, 100)
    api_url   = f"{HERO_SEARCH_URL}?query={quote(query, safe='')}"

    def _empty(err=None):
        return dict(
            query_sent=query, api_url=api_url,
            papers_returned=0, papers_with_pmcid=0,
            response_time_ms=int((time.monotonic() - t0) * 1000),
            status="error" if err else "ok",
            error=err, papers=[],
        )

    # verify=False: required because this machine has SSL certificate chain issues
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=TIMEOUT,
        verify=False,
    ) as client:

        # ── Step 1: scrape HERO search HTML ──────────────────────────────────
        try:
            r = await client.get(
                HERO_SEARCH_URL,
                params={
                    "query":       query,
                    "query_box":   query,
                    "is_expanded": "false",
                },
            )
            if r.status_code != 200:
                return _empty(err=f"HERO search returned HTTP {r.status_code}")
            html = r.text
        except Exception as exc:
            return _empty(err=str(exc))

        refs = _parse_search_html(html, max_fetch)
        if not refs:
            return _empty()

        # ── Step 2: resolve PDF URLs concurrently ─────────────────────────────
        sem   = asyncio.Semaphore(3)
        tasks = [_resolve_ref(ref, client, sem) for ref in refs]
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

    # verify=False: required due to SSL certificate chain issues on this machine
    async with httpx.AsyncClient(
        headers=PDF_HEADERS,
        follow_redirects=True,
        timeout=TIMEOUT,
        verify=False,
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
