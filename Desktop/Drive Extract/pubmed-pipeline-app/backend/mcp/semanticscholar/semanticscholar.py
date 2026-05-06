"""
Semantic Scholar API client — Graph API v1.
https://api.semanticscholar.org/graph/v1/paper/search

IMPORTANT NOTE on ranking:
  The PUBLIC Graph API (this module) and the SS WEBSITE use different ranking
  algorithms. For the same query the website may show paper A first while the
  Graph API shows paper B first. This is by SS's design — the website uses an
  internal ranker (`www.semanticscholar.org/api/1/search`) that is NOT exposed
  publicly: it returns 202/empty for unauthenticated callers and the search
  page is client-rendered (no data in HTML).
  We use `sort=relevance` here, which is the closest the public API allows.

Flow:
  1. Search with `openAccessPdf=` filter — equivalent to website's "Has PDF".
  2. Build PDF URL list per paper (most reliable sources first):
       a. EuropePMC render  (any PMC-indexed paper — never blocks)
       b. ArXiv direct PDF  (any preprint — never blocks)
       c. SS openAccessPdf  (publisher OA link — works for some, fails for NEJM/JAMA)
       d. NCBI PMC native   (last resort)
  3. Caller (pipeline) downloads in order, stops at first success.

Rate limit: 100 req / 5 min without API key. Retries on 429 with backoff.
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime
from pathlib import Path

import httpx

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS     = "title,authors,year,venue,externalIds,openAccessPdf,abstract,publicationDate,isOpenAccess,citationCount"
TIMEOUT    = 30.0
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}


PAGE_SIZE = 100   # SS Graph API hard cap per request


async def _fetch_page(query: str, offset: int) -> tuple[list[dict], str | None, str]:
    """One paginated SS API call.  Returns (data_items, error, api_url)."""
    params = {
        "query":         query,
        "fields":        FIELDS,
        "limit":         PAGE_SIZE,
        "offset":        offset,
        "openAccessPdf": "",   # "Has PDF" filter
    }
    api_url = str(httpx.Request("GET", SEARCH_URL, params=params).url)

    last_error = None
    for attempt, wait in enumerate([1, 3, 6, 12]):
        await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
                r = await c.get(SEARCH_URL, params=params)
            if r.status_code == 429:
                last_error = f"Rate limited (429) — attempt {attempt + 1}"
                if attempt < 3: continue
                return [], last_error, api_url
            r.raise_for_status()
            return r.json().get("data", []), None, api_url
        except Exception as e:
            last_error = str(e)
            if "429" in str(e) and attempt < 3: continue
            return [], last_error, api_url
    return [], last_error, api_url


async def search(query: str, max_results: int = 100) -> dict:
    """
    Search Semantic Scholar for papers — equivalent to website "Has PDF" filter.
    Auto-paginates internally when max_results > PAGE_SIZE (cap = 500).
    """
    target_count = min(max_results, 500)
    pages_needed = (target_count + PAGE_SIZE - 1) // PAGE_SIZE

    t0 = time.monotonic()
    data: list[dict] = []
    last_error = None
    first_api_url = ""

    for page_idx in range(pages_needed):
        items, err, api_url = await _fetch_page(query, page_idx * PAGE_SIZE)
        if not first_api_url:
            first_api_url = api_url
        if err and not items:
            last_error = err
            break
        if not items:
            break          # end of results
        data.extend(items)
        if len(items) < PAGE_SIZE:
            break          # no more pages available

    response_time_ms = int((time.monotonic() - t0) * 1000)

    if not data and last_error:
        return {
            "query_sent": query, "api_url": first_api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "error", "error": last_error, "papers": [],
        }

    papers_with_pdf = 0
    papers = []
    for item in data:
        paper_id = item.get("paperId") or ""
        ext_ids  = item.get("externalIds") or {}

        pmid  = ext_ids.get("PubMed")         or None
        pmcid = ext_ids.get("PubMedCentral")  or None
        doi   = ext_ids.get("DOI")            or None
        arxiv = ext_ids.get("ArXiv")          or None

        if pmcid and not str(pmcid).startswith("PMC"):
            pmcid = f"PMC{pmcid}"

        authors   = [a.get("name", "") for a in item.get("authors", [])]
        year      = str(item.get("year") or "")
        ss_url    = (f"https://www.semanticscholar.org/paper/{paper_id}"
                     if paper_id else None)

        # SS direct OA link
        open_pdf  = item.get("openAccessPdf")
        ss_pdf    = (open_pdf.get("url") or None) if open_pdf else None
        oa_status = (open_pdf.get("status") or "") if open_pdf else ""

        # Build PDF URL list — most reliable sources first
        pdf_urls: list[str] = []

        if pmcid:
            epmc = f"https://europepmc.org/articles/{pmcid}?pdf=render"
            pdf_urls.append(epmc)

        if arxiv:
            arxiv_pdf = f"https://arxiv.org/pdf/{str(arxiv).strip()}.pdf"
            if arxiv_pdf not in pdf_urls:
                pdf_urls.append(arxiv_pdf)

        if ss_pdf and ss_pdf not in pdf_urls:
            pdf_urls.append(ss_pdf)

        if pmcid:
            ncbi = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
            if ncbi not in pdf_urls:
                pdf_urls.append(ncbi)

        has_pdf_from_api = bool(ss_pdf)
        if pdf_urls:
            papers_with_pdf += 1

        papers.append({
            "paper_id":          paper_id,
            "pmcid":             pmcid,
            "pmid":              pmid,
            "doi":               doi,
            "arxiv":             arxiv,
            "title":             (item.get("title") or "").strip(),
            "abstract":          (item.get("abstract") or "").strip(),
            "authors":           authors,
            "journal":           (item.get("venue") or ""),
            "year":              year,
            "epmc_url":          ss_url,
            "ss_url":            ss_url,
            "oa_status":         oa_status,
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  has_pdf_from_api,
            "api_pdf_url_count": len(pdf_urls),
        })

    return {
        "query_sent":        query,
        "api_url":           first_api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": papers_with_pdf,
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(paper_id: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """
    Download a Semantic Scholar paper's PDF.
    Tries each URL in order — EuropePMC render, ArXiv, SS OA link, NCBI.
    Returns full per-URL attempt trace.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    attempts = []

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None,
            "attempts": [],
        }

    safe_id = (paper_id or "unknown")[:16].replace("/", "_")

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.4)
            attempted_at = datetime.utcnow()
            attempt = {
                "url":          url,
                "attempt_no":   i,
                "http_status":  None,
                "content_type": None,
                "is_pdf":       False,
                "success":      False,
                "file_size_kb": None,
                "error":        None,
                "attempted_at": attempted_at,
            }
            try:
                r  = await c.get(url)
                ct = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    path = pdf_dir / f"ss_{safe_id}_{pmid or 'noid'}.pdf"
                    path.write_bytes(r.content)
                    size_kb = len(r.content) // 1024
                    attempt["success"]      = True
                    attempt["file_size_kb"] = size_kb
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
