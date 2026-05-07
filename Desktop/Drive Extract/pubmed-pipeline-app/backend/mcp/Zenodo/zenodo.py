"""
Zenodo (CERN open-research repository) scraper.

Mirrors https://zenodo.org/search?q=<query>&sort=bestmatch

Flow:
  1. DuckDuckGo site:zenodo.org "{query}" — returns record/file URLs in
     Zenodo's relevance order (same Bing index). Avoids the Zenodo bulk-search
     API which is aggressively rate-limited.
  2. For each DDG result URL:
       a. If the URL is already a /files/*.pdf link → use it directly.
       b. Otherwise extract the record ID and call /api/records/{id} or
          the /files endpoint to find the PDF URL.
  3. Return in DDG order. PDFs from (a) are always available; (b) falls back
     gracefully when the Zenodo API is temporarily rate-limited.
"""
from __future__ import annotations
import asyncio, os, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from _ddg import search as _ddg_search

API_URL  = "https://zenodo.org/api/records"
TIMEOUT  = 30.0

ZENODO_API_TOKEN = os.getenv("ZENODO_API_TOKEN", "").strip()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://zenodo.org/",
    "Origin":          "https://zenodo.org/",
    **({"Authorization": f"Bearer {ZENODO_API_TOKEN}"} if ZENODO_API_TOKEN else {}),
}
PDF_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://zenodo.org/",
    **({"Authorization": f"Bearer {ZENODO_API_TOKEN}"} if ZENODO_API_TOKEN else {}),
}

# Matches /record/12345 and /records/12345 anywhere in the URL
_RECORD_RE = re.compile(r'zenodo\.org/records?/(\d+)', re.I)
# Direct file URL: .../records/12345/files/something.pdf
_FILE_RE   = re.compile(r'zenodo\.org/records?/(\d+)/files/[^?#]+\.pdf', re.I)


def _pdf_url_from_files(files_field) -> str:
    """Extract the first PDF download URL from a Zenodo `files` field."""
    if not files_field:
        return ""
    if isinstance(files_field, dict):
        entries = files_field.get("entries") or {}
        file_list = list(entries.values()) if isinstance(entries, dict) else (
            entries if isinstance(entries, list) else []
        )
    else:
        file_list = files_field if isinstance(files_field, list) else []

    for f in file_list:
        if not isinstance(f, dict):
            continue
        key = (f.get("key") or "").lower()
        if not key.endswith(".pdf"):
            continue
        links = f.get("links") or {}
        url = links.get("content") or links.get("self") or ""
        if url:
            return url
    return ""


async def _fetch_record(client: httpx.AsyncClient, record_id: str) -> dict | None:
    """Fetch a single Zenodo record metadata via API. Returns None on failure."""
    for delay in [0, 3]:
        if delay:
            await asyncio.sleep(delay)
        try:
            r = await client.get(f"{API_URL}/{record_id}")
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                continue
        except Exception:
            continue
    return None


async def _fetch_files_url(client: httpx.AsyncClient, record_id: str) -> str:
    """Fallback: /files endpoint for InvenioRDM. Returns PDF URL or ""."""
    try:
        r = await client.get(f"{API_URL}/{record_id}/files")
        if r.status_code != 200:
            return ""
        data = r.json()
        entries = data.get("entries") or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        for f in entries:
            if not isinstance(f, dict):
                continue
            if (f.get("key") or "").lower().endswith(".pdf"):
                links = f.get("links") or {}
                return links.get("content") or links.get("self") or ""
    except Exception:
        pass
    return ""


async def search(query: str, max_results: int = 20) -> dict:
    """Search Zenodo. Uses DDG for URL discovery, Zenodo API for metadata."""
    t0      = time.monotonic()
    api_url = f"{API_URL}?q={query}&size={max_results}&sort=bestmatch"

    # Step 1: DDG to get Zenodo URLs in relevance order
    ddg_query = f'site:zenodo.org "{query}"'
    try:
        ddg_results = await _ddg_search(ddg_query, min(max_results * 5, 80))
    except RuntimeError as e:
        try:
            ddg_results = await _ddg_search(f"site:zenodo.org {query}", min(max_results * 5, 80))
        except RuntimeError:
            return {
                "query_sent": query, "api_url": api_url,
                "papers_returned": 0, "papers_with_pmcid": 0,
                "response_time_ms": int((time.monotonic() - t0) * 1000),
                "status": "error", "error": str(e), "papers": [],
            }

    if not ddg_results:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Step 2: Group DDG results by record ID, keeping first occurrence per ID
    # Each entry: (record_id, ddg_result_dict, direct_pdf_url_or_empty)
    ordered: list[tuple[str, dict, str]] = []
    seen_ids: set[str] = set()

    for r in ddg_results:
        href = r.get("href", "")
        m    = _RECORD_RE.search(href)
        if not m:
            continue
        rid = m.group(1)
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        # Check if DDG gave us a direct PDF file URL
        direct_pdf = href if _FILE_RE.search(href) else ""
        # Ensure the PDF URL ends with .pdf (strip query params if needed)
        if direct_pdf and "?" in direct_pdf:
            direct_pdf = direct_pdf.split("?")[0]
        ordered.append((rid, r, direct_pdf))

    if not ordered:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Step 3: Fetch record metadata + resolve PDF URL concurrently
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=HEADERS, follow_redirects=True
    ) as client:

        async def _resolve(rid: str, ddg_r: dict, direct_pdf: str) -> tuple[str, dict | None, str]:
            # Try API for metadata (and PDF URL as bonus)
            hit = await _fetch_record(client, rid)

            # Determine PDF URL
            pdf_url = direct_pdf
            if not pdf_url and hit is not None:
                pdf_url = _pdf_url_from_files(hit.get("files"))
                if not pdf_url:
                    access = (
                        (hit.get("access") or {}).get("status")
                        or (hit.get("metadata") or {}).get("access_right")
                        or ""
                    )
                    if access in ("open", "embargoed", ""):
                        pdf_url = await _fetch_files_url(client, rid)

            return rid, hit, pdf_url

        resolved = await asyncio.gather(
            *[_resolve(rid, ddg_r, dp) for rid, ddg_r, dp in ordered[:max_results * 2]],
            return_exceptions=True,
        )

    # Step 4: Build papers in DDG order
    papers: list[dict] = []
    for item, (rid, ddg_r, _direct) in zip(resolved, ordered[:max_results * 2]):
        if isinstance(item, Exception):
            continue

        _rid, hit, pdf_url = item
        if not pdf_url:
            continue

        # Use API metadata when available, else fall back to DDG snippet
        if hit is not None:
            meta     = hit.get("metadata") or {}
            creators = meta.get("creators") or []
            authors  = [
                (c.get("name") or c.get("person_or_org", {}).get("name") or "").strip()
                for c in creators
            ]
            authors = [a for a in authors if a]
            title       = (meta.get("title") or "").strip() or ddg_r.get("title", "")
            description = (meta.get("description") or "").strip()
            snippet     = re.sub(r'<[^>]+>', '', description).strip()[:400] or ddg_r.get("body", "")[:400]
            year        = (meta.get("publication_date") or "")[:4]
            doi         = meta.get("doi") or hit.get("doi") or None
            rec_links   = hit.get("links") or {}
            record_url  = (
                rec_links.get("html")
                or rec_links.get("self_html")
                or rec_links.get("self")
                or f"https://zenodo.org/records/{rid}"
            )
            res_type = meta.get("resource_type") or {}
            journal  = res_type.get("title") or res_type.get("type") or "Zenodo"
        else:
            # API unavailable — use DDG metadata
            authors    = []
            title      = ddg_r.get("title", "")
            snippet    = ddg_r.get("body", "")[:400]
            year       = ""
            doi        = None
            record_url = f"https://zenodo.org/records/{rid}"
            journal    = "Zenodo"

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               doi,
            "title":             title,
            "abstract":          snippet,
            "authors":           authors,
            "journal":           journal,
            "year":              year,
            "epmc_url":          record_url,
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

        if len(papers) >= max_results:
            break

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": len(papers),
        "response_time_ms":  int((time.monotonic() - t0) * 1000),
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Download a Zenodo PDF. Tries each URL in order."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None,
                "pdf_source": None, "file_size_kb": None, "attempts": []}

    attempts = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.3)
            attempt = {
                "url": url, "attempt_no": i,
                "http_status": None, "content_type": None, "is_pdf": False,
                "success": False, "file_size_kb": None, "error": None,
                "attempted_at": datetime.utcnow(),
            }
            try:
                r  = await c.get(url)
                ct = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    stem = Path(unquote(urlparse(url).path)).stem or "zenodo_doc"
                    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                    path = pdf_dir / f"zenodo_{stem}.pdf"
                    path.write_bytes(r.content)
                    size_kb = len(r.content) // 1024
                    attempt["success"]      = True
                    attempt["file_size_kb"] = size_kb
                    attempts.append(attempt)
                    return {
                        "status": "success", "pdf_path": str(path),
                        "pdf_source": url, "file_size_kb": size_kb,
                        "attempts": attempts,
                    }
            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
            attempts.append(attempt)

    return {"status": "failed", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": attempts}
