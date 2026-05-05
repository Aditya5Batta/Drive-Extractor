"""
Semantic Scholar API client — Graph API v1.
https://api.semanticscholar.org/graph/v1/paper/search

Flow:
  1. Search with openAccessPdf field AND isOpenAccess filter
     → equivalent to "Has PDF" filter on the SS website
  2. Build PDF URL list per paper:
       a. openAccessPdf.url  (SS direct OA link)
       b. arxiv.org/pdf/{id} (ArXiv papers — always free)
       c. europepmc.org render URL (PMC-indexed papers)
  3. Download PDFs in order, stop at first success per paper.

Rate limit: 1 req/sec without API key. Retries on 429 with backoff.
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime
from pathlib import Path

import httpx

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
# Request openAccessPdf so SS returns the direct PDF URL when available
FIELDS     = "title,authors,year,venue,externalIds,openAccessPdf,abstract,publicationDate,isOpenAccess"
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


async def search(query: str, max_results: int = 100) -> dict:
    """
    Search Semantic Scholar for papers — equivalent to website "Has PDF" filter.
    Only papers where we can build at least one PDF URL are kept.

    Returns:
      {
        query_sent, api_url, papers_returned, papers_with_pmcid,
        response_time_ms, status, error, papers: [...]
      }
    """
    params = {
        "query":  query,
        "fields": FIELDS,
        "limit":  min(max_results, 100),   # SS API hard cap = 100
    }

    req     = httpx.Request("GET", SEARCH_URL, params=params)
    api_url = str(req.url)

    t0 = time.monotonic()

    # Retry up to 3× on 429 with increasing backoff
    last_error = None
    r          = None
    for attempt, wait in enumerate([1, 3, 6, 12]):
        await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
                r = await c.get(SEARCH_URL, params=params)
            if r.status_code == 429:
                last_error = f"Rate limited (429) — attempt {attempt + 1}"
                if attempt < 3:
                    continue
                break
            r.raise_for_status()
            break   # success
        except Exception as e:
            last_error = str(e)
            if "429" in str(e) and attempt < 3:
                continue
            break

    if r is None or r.status_code != 200:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": last_error or "No response", "papers": [],
        }

    response_time_ms = int((time.monotonic() - t0) * 1000)
    data = r.json().get("data", [])
    papers_returned = len(data)
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

        # Build PDF URL list — priority order:
        #   1. SS openAccessPdf (direct OA)
        #   2. ArXiv PDF        (free for all ArXiv papers)
        #   3. EuropePMC render (free for PMC-indexed papers)
        #   4. NCBI PMC native  (free for open-access PMC articles)
        pdf_urls: list[str] = []

        if ss_pdf:
            pdf_urls.append(ss_pdf)

        if arxiv:
            arxiv_pdf = f"https://arxiv.org/pdf/{str(arxiv).strip()}.pdf"
            if arxiv_pdf not in pdf_urls:
                pdf_urls.append(arxiv_pdf)

        if pmcid:
            epmc = f"https://europepmc.org/articles/{pmcid}?pdf=render"
            ncbi = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
            if epmc not in pdf_urls:
                pdf_urls.append(epmc)
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
        "api_url":           api_url,
        "papers_returned":   papers_returned,
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
    Tries each URL in order — SS OA link, ArXiv, EuropePMC render, NCBI.
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
