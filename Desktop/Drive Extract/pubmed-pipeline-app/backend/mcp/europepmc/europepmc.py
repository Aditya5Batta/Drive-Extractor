"""
EuropePMC API client — returns full per-URL attempt trace for DB logging.
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime
from pathlib import Path

import httpx

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TIMEOUT    = 30.0
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
}


async def search(query: str, max_results: int = 50) -> dict:
    """
    Search EuropePMC for ALL papers — no open-access filter.
    Every paper returned by EuropePMC is included and logged in activity_logs
    in relevance order.  Papers with a free PDF will be attempted; others
    will be logged as no_url (no free PDF available).

    Returns:
      {
        query_sent, api_url, papers_returned, papers_with_pmcid,
        response_time_ms, status, error, papers: [...]
      }
    """
    params = {
        "query":      query,          # no OPEN_ACCESS filter — return ALL papers
        "format":     "json",
        "pageSize":   max_results,
        "resultType": "core",
    }

    # Build full URL for logging
    req = httpx.Request("GET", SEARCH_URL, params=params)
    api_url = str(req.url)

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(SEARCH_URL, params=params)
        r.raise_for_status()
        response_time_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return {
            "query_sent": params["query"], "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": str(e), "papers": [],
        }

    all_results = r.json().get("resultList", {}).get("result", [])
    papers_returned = len(all_results)

    papers = []
    papers_with_pmcid = 0

    for item in all_results:
        raw_pmcid = (item.get("pmcid") or "").strip()
        if raw_pmcid and not raw_pmcid.startswith("PMC"):
            raw_pmcid = f"PMC{raw_pmcid}"
        pmcid = raw_pmcid or None          # None = no PMC record (paywalled)

        pmid = item.get("pmid") or None

        # Collect PDF URLs explicitly listed in the API response
        api_pdf_urls = []
        for ft in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
            if ft.get("documentStyle") == "pdf":
                url = ft.get("url", "").strip()
                if url and url not in api_pdf_urls:   # deduplicate
                    api_pdf_urls.append(url)

        # has_pdf_from_api = True only when EuropePMC API explicitly provided PDF links
        has_pdf_from_api = bool(api_pdf_urls)

        # Build the list of URLs we will actually try, in reliability order:
        #   1. API-provided PDF links (fullTextUrlList from EuropePMC)
        #   2. EuropePMC render endpoint   (works for any PMC-indexed paper)
        #   3. NCBI's new pmc.ncbi.nlm.nih.gov domain — useful when the
        #      EuropePMC render times out (saw this on placenta-RGDV paper).
        all_pdf_urls = list(api_pdf_urls)
        if pmcid:
            for fallback in (
                f"https://europepmc.org/articles/{pmcid}?pdf=render",
                f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
            ):
                if fallback not in all_pdf_urls:
                    all_pdf_urls.append(fallback)
            papers_with_pmcid += 1

        # EuropePMC article page link
        if pmid:
            epmc_url = f"https://europepmc.org/article/MED/{pmid}"
        elif pmcid:
            epmc_url = f"https://europepmc.org/articles/{pmcid}"
        else:
            epmc_url = None

        papers.append({
            "pmcid":             pmcid,
            "pmid":              pmid,
            "doi":               item.get("doi"),
            "title":             item.get("title", "").strip(),
            "abstract":          item.get("abstractText", "").strip(),
            "authors":           [a.get("fullName", "")
                                  for a in item.get("authorList", {}).get("author", [])],
            "journal":           item.get("journalTitle", ""),
            "year":              str(item.get("pubYear") or ""),
            "epmc_url":          epmc_url,
            "pdf_urls":          all_pdf_urls,        # URLs we will try (empty = no free PDF)
            "has_pdf_from_api":  has_pdf_from_api,    # API explicitly gave PDF links?
            "api_pdf_url_count": len(api_pdf_urls),   # API-provided links only
        })

    return {
        "query_sent":        params["query"],
        "api_url":           api_url,
        "papers_returned":   papers_returned,
        "papers_with_pmcid": papers_with_pmcid,
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """
    Try each EuropePMC PDF URL in order.
    Returns full per-URL trace:
      {
        status, pdf_path, pdf_source, file_size_kb,
        attempts: [
          { url, attempt_no, http_status, content_type,
            is_pdf, success, file_size_kb, error }
        ]
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
            attempted_at = datetime.utcnow()   # captured right before the HTTP call
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
                    path = pdf_dir / f"epmc_{pmcid}_{pmid}.pdf"
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
