"""
EuropePMC API client — pure async functions used by the MCP server.
Only EuropePMC sources are used. No NCBI, no fallbacks.
"""
from __future__ import annotations
import asyncio
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


async def search(query: str, max_results: int = 25) -> list[dict]:
    """
    Search EuropePMC for open-access papers.
    Returns only papers that have a PMCID (full text available).
    Each paper includes EuropePMC PDF URLs extracted from the API response.
    """
    params = {
        "query":      f"({query}) AND (OPEN_ACCESS:Y)",
        "format":     "json",
        "pageSize":   max_results,
        "resultType": "core",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        r = await c.get(SEARCH_URL, params=params)
    r.raise_for_status()

    papers = []
    for item in r.json().get("resultList", {}).get("result", []):
        pmcid = item.get("pmcid") or ""
        if not pmcid:
            continue
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"

        pmid = item.get("pmid")

        # EuropePMC PDF URLs only — from fullTextUrlList in the API response
        pdf_urls = []
        for ft in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
            if ft.get("documentStyle") == "pdf":
                url = ft.get("url", "").strip()
                if url:
                    pdf_urls.append(url)
        # EuropePMC native render endpoint
        pdf_urls.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")

        papers.append({
            "pmcid":     pmcid,
            "pmid":      pmid,
            "doi":       item.get("doi"),
            "title":     item.get("title", "").strip(),
            "abstract":  item.get("abstractText", "").strip(),
            "authors":   [a.get("fullName", "")
                          for a in item.get("authorList", {}).get("author", [])],
            "journal":   item.get("journalTitle", ""),
            "year":      str(item.get("pubYear") or ""),
            "epmc_url":  (f"https://europepmc.org/article/MED/{pmid}"
                          if pmid else f"https://europepmc.org/articles/{pmcid}"),
            "pdf_urls":  pdf_urls,
        })

    return papers


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """
    Try each EuropePMC PDF URL until one returns a real PDF file.
    Saves to pdf_dir/epmc_{pmcid}_{pmid}.pdf
    Returns: { status, pdf_path, pdf_source, errors }
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    errors = []

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
    ) as c:
        for url in pdf_urls:
            await asyncio.sleep(0.4)
            try:
                r   = await c.get(url)
                ct  = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                if r.status_code == 200 and is_pdf:
                    path = pdf_dir / f"epmc_{pmcid}_{pmid}.pdf"
                    path.write_bytes(r.content)
                    return {
                        "status":     "success",
                        "pdf_path":   str(path),
                        "pdf_source": url,
                        "errors":     errors,
                    }
                errors.append(f"HTTP {r.status_code} | {ct[:40]} | {url}")

            except Exception as e:
                errors.append(f"{type(e).__name__}: {e} | {url}")

    return {"status": "failed", "pdf_path": None, "pdf_source": None, "errors": errors}
