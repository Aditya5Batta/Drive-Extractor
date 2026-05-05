"""
PubMed Central (PMC) scraper — NCBI E-utilities API.
Completely separate from EuropePMC. Uses NCBI Entrez API.

Flow:
  1. esearch  → get list of PMC IDs in relevance order
  2. esummary → get title, authors, journal, year, DOI for each ID
  3. PDF URL  → https://pmc.ncbi.nlm.nih.gov/articles/PMC{id}/pdf/
               (returns real PDF for open-access; fails for embargoed)
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime
from pathlib import Path

import httpx

ESEARCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TIMEOUT = 30.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
}


async def search(query: str, max_results: int = 50) -> dict:
    """
    Search PubMed Central via NCBI E-utilities.
    Returns all results in relevance order — including embargoed articles.
    Papers with a free PDF are attempted; others logged as no_url.

    Returns:
      {
        query_sent, api_url, papers_returned, papers_with_pmcid,
        response_time_ms, status, error, papers: [...]
      }
    """
    esearch_params = {
        "db":      "pmc",
        "term":    query,
        "retmax":  max_results,
        "retmode": "json",
        "sort":    "relevance",
    }

    req     = httpx.Request("GET", ESEARCH_URL, params=esearch_params)
    api_url = str(req.url)

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(ESEARCH_URL, params=esearch_params)
        r.raise_for_status()
        response_time_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": str(e), "papers": [],
        }

    id_list = r.json().get("esearchresult", {}).get("idlist", [])
    papers_returned = len(id_list)

    if not id_list:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "ok", "error": None, "papers": [],
        }

    # ── Fetch article summaries (title, authors, journal, IDs) ───────────────
    await asyncio.sleep(0.35)   # respect NCBI rate limit (3 req/s without API key)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            sr = await c.get(ESUMMARY_URL, params={
                "db": "pmc", "id": ",".join(id_list), "retmode": "json",
            })
        sr.raise_for_status()
        summaries = sr.json().get("result", {})
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": papers_returned, "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "error", "error": f"esummary failed: {e}", "papers": [],
        }

    # ── Build paper list in original relevance order ──────────────────────────
    papers = []
    for pmc_num in id_list:
        s = summaries.get(pmc_num)
        if not s:
            continue

        pmcid = f"PMC{pmc_num}"

        # Extract pmid and doi from articleids list
        pmid = None
        doi  = None
        for aid in s.get("articleids", []):
            t = aid.get("idtype", "")
            if t == "pmid":
                pmid = aid.get("value") or None
            elif t == "doi":
                doi = aid.get("value") or None

        authors = [a.get("name", "") for a in s.get("authors", [])]

        # Year from pubdate ("2024 Jan 15" → "2024")
        pubdate = s.get("pubdate", "")
        year    = pubdate[:4] if pubdate else ""

        # Try two PDF sources in order:
        # 1. NCBI PMC native URL  (works for fully open-access articles)
        # 2. EuropePMC render URL (same PMCID, better availability — proven fallback)
        pdf_urls = [
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
            f"https://europepmc.org/articles/{pmcid}?pdf=render",
        ]

        papers.append({
            "pmcid":             pmcid,
            "pmid":              pmid,
            "doi":               doi,
            "title":             s.get("title", "").strip(),
            "abstract":          "",   # esummary doesn't include abstract text
            "authors":           authors,
            "journal":           s.get("source", ""),
            "year":              year,
            "epmc_url":          f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  False,
            "api_pdf_url_count": 0,
        })

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   papers_returned,
        "papers_with_pmcid": len(papers),
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """
    Try each PMC PDF URL in order.
    Returns full per-URL trace:
      {
        status, pdf_path, pdf_source, file_size_kb,
        attempts: [ { url, attempt_no, http_status, content_type,
                      is_pdf, success, file_size_kb, error } ]
      }
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    attempts = []

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None,
            "attempts": [],
        }

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
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
                r   = await c.get(url)
                ct  = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    path = pdf_dir / f"pmc_{pmcid}_{pmid}.pdf"
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
