"""
OpenAlex scraper.

Mirrors https://openalex.org/works?search=<chem>&filter=open_access.is_oa:true
                                  &sort=relevance_score:desc

OpenAlex (https://openalex.org) is a free, fully-open scholarly index. We use
their official REST API (api.openalex.org). For each work we collect EVERY
known PDF URL across:

  1. best_oa_location.pdf_url       — OpenAlex's pre-computed best OA copy
  2. primary_location.pdf_url        — canonical publisher version
  3. all locations[].pdf_url         — every other OA copy OpenAlex knows about
  4. Unpaywall (api.unpaywall.org)   — independent OA index, often has copies
                                       OpenAlex misses (PMC, repositories)

The downloader then tries each URL in turn. Many publishers return an HTML
"interstitial" landing page when a PDF URL is requested without the right
session — we detect this and parse the HTML for `<meta name="citation_pdf_url">`
(the Highwire / Google Scholar standard) which gives the real PDF URL.

API docs: https://docs.openalex.org/api-entities/works
          https://unpaywall.org/products/api
"""
from __future__ import annotations
import asyncio, os, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

import httpx

API_URL       = "https://api.openalex.org/works"
UNPAYWALL_URL = "https://api.unpaywall.org/v2"
TIMEOUT       = 30.0

_EMAIL = os.getenv("OPENALEX_EMAIL", "pubmed-pipeline@example.com").strip()
_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    f"PubmedPipeline/1.0 (mailto:{_EMAIL})"
)

API_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}
HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept":          "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Upgrade-Insecure-Requests": "1",
}
PDF_HEADERS = {
    "User-Agent": _UA,
    # IMPORTANT: include text/html so publishers like PMC return the full
    # article page (which contains <meta name="citation_pdf_url">) when the
    # PDF endpoint redirects/returns HTML. Pure "application/pdf" Accept
    # makes some publishers serve a stripped page missing the meta tag.
    "Accept":          "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Upgrade-Insecure-Requests": "1",
}

# Patterns to find the real PDF URL inside an HTML interstitial page.
# Permissive: any attribute order, any whitespace, any extra attributes between.
_META_PDF_PATTERNS = [
    re.compile(r'<meta[^>]*\bname\s*=\s*["\']citation_pdf_url["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]*\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*\bname\s*=\s*["\']citation_pdf_url["\']', re.I),
    re.compile(r'<meta[^>]*\bproperty\s*=\s*["\']og:pdf["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']', re.I),
]
_EMBED_PDF_PATTERN = re.compile(
    r'<(?:embed|iframe|object)\s[^>]*\b(?:src|data)\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    re.I,
)
_ANCHOR_PDF_PATTERN = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\'][^>]*>',
    re.I,
)


def _publisher_specific_urls(url: str) -> list[str]:
    """
    Some publishers serve PDFs at predictable URL patterns even when their
    OpenAlex/Unpaywall pdf_url returns an HTML interstitial.
    """
    out: list[str] = []
    p = urlparse(url)
    path = p.path

    # PMC: /pmc/articles/XXXX or /articles/PMCXXXX → add direct PDF endpoint
    pmc_m = re.search(r'/(?:pmc/)?articles/(?:PMC)?(\d+)', path, re.I)
    if pmc_m and ("ncbi.nlm.nih.gov" in p.netloc or "pmc.ncbi.nlm.nih.gov" in p.netloc):
        pmcid = pmc_m.group(1)
        out.append(f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmcid}/pdf/")
        out.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/pdf/")
        out.append(f"https://europepmc.org/article/PMC/PMC{pmcid}?pdf=render")

    # Copernicus (ACP, BG, etc.): the .pdf URL hits an interstitial — the
    # article landing page has a working "Download" button but it's gated.
    # Try the alternate "assets" path which is direct.
    if "copernicus.org" in p.netloc:
        # Pattern: /articles/X/Y/YYYY/<journal>-X-Y-YYYY.pdf
        m = re.match(r'/articles/(\d+)/(\d+)/(\d+)/([\w-]+)\.pdf', path)
        if m:
            vol, page, year, name = m.groups()
            # Direct asset URL pattern used by Copernicus
            out.append(
                f"https://{p.netloc.replace('acp.copernicus.org', 'acp.copernicus.org')}"
                f"/articles/{vol}/{page}/{year}/{name}-print.pdf"
            )
    return out


def _is_blocked_html(content: bytes, ct: str) -> bool:
    """Detect generic 'access denied' / Cloudflare / Akamai blocks."""
    if "html" not in ct.lower():
        return False
    head = content[:4000].lower()
    return any(s in head for s in (
        b"access denied", b"cloudflare", b"captcha",
        b"are you a robot", b"blocked", b"forbidden",
    ))


# ──────────────────────────────────────────────────────────────────────────────
# OpenAlex helpers
# ──────────────────────────────────────────────────────────────────────────────
def _reconstruct_abstract(inv_idx: dict | None) -> str:
    if not inv_idx or not isinstance(inv_idx, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, pos_list in inv_idx.items():
        if isinstance(pos_list, list):
            for p in pos_list:
                if isinstance(p, int):
                    positions.append((p, word))
    positions.sort()
    # DB column is varchar(256); leave headroom for safety
    return " ".join(w for _, w in positions)[:240]


def _all_pdf_urls(work: dict) -> list[str]:
    """Collect every PDF URL OpenAlex knows about for this work, deduped."""
    urls: list[str] = []
    seen: set[str]  = set()

    def _add(u: str | None):
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    best = work.get("best_oa_location") or {}
    if isinstance(best, dict):
        _add(best.get("pdf_url"))

    primary = work.get("primary_location") or {}
    if isinstance(primary, dict):
        _add(primary.get("pdf_url"))

    for loc in (work.get("locations") or []):
        if isinstance(loc, dict):
            _add(loc.get("pdf_url"))

    # OA URL — sometimes a PDF, sometimes a landing page
    oa = work.get("open_access") or {}
    if isinstance(oa, dict):
        _add(oa.get("oa_url"))

    return urls


def _landing_url(work: dict) -> str:
    primary = work.get("primary_location") or {}
    if isinstance(primary, dict):
        url = primary.get("landing_page_url")
        if url:
            return url
    best = work.get("best_oa_location") or {}
    if isinstance(best, dict):
        url = best.get("landing_page_url")
        if url:
            return url
    return work.get("id") or ""


def _journal(work: dict) -> str:
    primary = work.get("primary_location") or {}
    if isinstance(primary, dict):
        src = primary.get("source") or {}
        if isinstance(src, dict) and src.get("display_name"):
            # DB column is varchar(256) — truncate
            return src["display_name"][:240]
    return (work.get("type") or "OpenAlex")[:240]


def _authors(work: dict) -> list[str]:
    out: list[str] = []
    for a in (work.get("authorships") or []):
        if isinstance(a, dict):
            au = a.get("author") or {}
            if isinstance(au, dict):
                name = (au.get("display_name") or "").strip()[:80]
                if name:
                    out.append(name)
    # The pipeline joins authors with ", "; keep total joined length ≤200.
    result: list[str] = []
    total = 0
    for n in out:
        cost = len(n) + (2 if result else 0)
        if total + cost > 200:
            result.append("et al.")
            break
        result.append(n)
        total += cost
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Unpaywall fallback — independent OA index. Often has PMC/preprint copies
# OpenAlex doesn't surface as the "best" location.
# ──────────────────────────────────────────────────────────────────────────────
async def _unpaywall_pdfs(client: httpx.AsyncClient, doi: str) -> list[str]:
    if not doi:
        return []
    try:
        r = await client.get(
            f"{UNPAYWALL_URL}/{doi}",
            params={"email": _EMAIL},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _add(u: str | None):
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    best = data.get("best_oa_location") or {}
    if isinstance(best, dict):
        _add(best.get("url_for_pdf"))

    for loc in (data.get("oa_locations") or []):
        if isinstance(loc, dict):
            _add(loc.get("url_for_pdf"))

    return out


# ──────────────────────────────────────────────────────────────────────────────
# search()
# ──────────────────────────────────────────────────────────────────────────────
async def search(query: str, max_results: int = 25) -> dict:
    t0 = time.monotonic()

    per_page = min(max(max_results, 25), 200)
    # IMPORTANT: openalex.org/works uses `title_and_abstract.search` (NOT
    # the plain `search` param which also searches fulltext/references and
    # produces different relevance ordering). Match the website exactly:
    #   openalex.org/works?search.title_and_abstract=<q>&filter=open_access.is_oa:true
    params = {
        "filter":   f"title_and_abstract.search:{query},open_access.is_oa:true",
        "sort":     "relevance_score:desc",
        "per_page": per_page,
        "page":     1,
        "mailto":   _EMAIL,
        "select": ",".join([
            "id", "doi", "title", "display_name", "publication_year",
            "type", "authorships", "abstract_inverted_index",
            "open_access", "best_oa_location", "primary_location", "locations",
        ]),
    }
    api_url = (
        f"{API_URL}?filter=title_and_abstract.search:{query},open_access.is_oa:true"
        f"&sort=relevance_score:desc&per_page={per_page}"
    )

    payload, last_error = None, None
    for attempt, wait in enumerate([0, 2, 5]):
        if wait:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, headers=API_HEADERS, follow_redirects=True
            ) as c:
                r = await c.get(API_URL, params=params)
            if r.status_code == 200:
                payload = r.json()
                break
            if r.status_code in (429, 503):
                last_error = f"HTTP {r.status_code} (rate limited)"
                continue
            last_error = f"HTTP {r.status_code}: {r.text[:150]}"
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

    if payload is None:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": last_error or "no response", "papers": [],
        }

    works = payload.get("results") or []
    papers: list[dict] = []

    # Enrich with Unpaywall in parallel (one HTTP request per DOI; throttled)
    sem = asyncio.Semaphore(8)

    async def _enrich(work: dict) -> dict:
        if not isinstance(work, dict):
            return work
        doi_full = (work.get("doi") or "").strip()
        doi      = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi_full, flags=re.I)
        unpaywall_urls: list[str] = []
        if doi:
            async with sem:
                async with httpx.AsyncClient(
                    timeout=TIMEOUT, headers=API_HEADERS, follow_redirects=True
                ) as c:
                    unpaywall_urls = await _unpaywall_pdfs(c, doi)
        work["_unpaywall_pdfs"] = unpaywall_urls
        work["_doi_clean"]      = doi
        return work

    works_with_unpaywall = await asyncio.gather(
        *[_enrich(w) for w in works[:max_results * 2]],
        return_exceptions=True,
    )

    for w in works_with_unpaywall:
        if len(papers) >= max_results:
            break
        if isinstance(w, Exception) or not isinstance(w, dict):
            continue

        title = (w.get("title") or w.get("display_name") or "").strip()
        if not title:
            continue
        # DB column varchar(256) — truncate aggressively
        title = title[:240]

        # Combined PDF URL list: OpenAlex first (its best guess), then Unpaywall.
        # DB stores "\n".join(pdf_urls) in varchar(256). Cap at 2 URLs and
        # ensure the joined string fits — download_pdf will further expand
        # each URL with publisher-specific alternatives at runtime.
        pdf_urls = _all_pdf_urls(w) + (w.get("_unpaywall_pdfs") or [])
        seen, deduped = set(), []
        for u in pdf_urls:
            if u and u not in seen:
                seen.add(u)
                deduped.append(u)
            if len(deduped) >= 2:
                break
        # Final guard: if joined string >250 chars, keep only the first URL
        if len(deduped) > 1 and sum(len(u) for u in deduped) + len(deduped) - 1 > 250:
            deduped = deduped[:1]

        # Defensive truncation: every varchar(256) DB column gets ≤240 chars,
        # every Text column also capped to keep responses sane.
        doi = (w.get("_doi_clean") or "")[:120] or None
        year = str(w.get("publication_year") or "")[:8]

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               doi,
            "title":             title[:240],
            "abstract":          _reconstruct_abstract(w.get("abstract_inverted_index"))[:240],
            "authors":           _authors(w),
            "journal":           _journal(w)[:240],
            "year":              year,
            "epmc_url":          _landing_url(w),
            "pdf_urls":          deduped,
            "has_pdf_from_api":  bool(deduped),
            "api_pdf_url_count": len(deduped),
        })

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": sum(1 for p in papers if p["pdf_urls"]),
        "response_time_ms":  int((time.monotonic() - t0) * 1000),
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


# ──────────────────────────────────────────────────────────────────────────────
# download_pdf — robust multi-strategy fetcher
# ──────────────────────────────────────────────────────────────────────────────
def _pdf_from_html(html: str, base_url: str) -> str:
    """
    When a publisher returns an HTML page instead of a PDF, look for the
    canonical PDF link. Tries (in order):
      1. <meta name="citation_pdf_url"> — academic standard (Highwire,
         Google Scholar, Crossref) used by most major publishers
      2. <meta property="og:pdf">
      3. <embed>/<iframe>/<object> src pointing to a .pdf
      4. First <a href="…pdf"> on the page
    """
    for pat in _META_PDF_PATTERNS:
        m = pat.search(html)
        if m:
            return urljoin(base_url, m.group(1).strip())

    m = _EMBED_PDF_PATTERN.search(html)
    if m:
        return urljoin(base_url, m.group(1).strip())

    m = _ANCHOR_PDF_PATTERN.search(html)
    if m:
        return urljoin(base_url, m.group(1).strip())

    return ""


def _publisher_referer(url: str) -> str:
    """Return a sensible Referer for `url` — the publisher's own homepage."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


async def _try_one_url(
    client: httpx.AsyncClient, url: str, follow_html: bool = True,
) -> tuple[bytes | None, str, dict]:
    """
    Try to fetch `url` and return (pdf_bytes, source_url, attempt_meta).
    If we get HTML, parse it for embedded PDF URL and recurse once.
    """
    meta = {
        "url": url, "http_status": None, "content_type": None,
        "is_pdf": False, "error": None,
    }
    try:
        headers = dict(PDF_HEADERS)
        headers["Referer"] = _publisher_referer(url)
        r = await client.get(url, headers=headers)
        ct = r.headers.get("content-type", "")
        meta["http_status"]  = r.status_code
        meta["content_type"] = ct[:128]

        # Real PDF — done (even on non-200; some servers return 200 vs 206)
        if r.status_code in (200, 206) and (
            "pdf" in ct.lower() or r.content[:4] == b"%PDF"
        ):
            meta["is_pdf"] = True
            return r.content, str(r.url), meta

        # HTML fallback: works for 200 OR 403 (some publishers return 403
        # with the citation_pdf_url meta tag still set).
        if (follow_html
                and r.status_code in (200, 403)
                and "html" in ct.lower()
                and not _is_blocked_html(r.content, ct)):
            embedded = _pdf_from_html(r.text, str(r.url))
            if embedded and embedded != url and embedded != str(r.url):
                bytes_, src, meta2 = await _try_one_url(client, embedded, follow_html=False)
                if bytes_:
                    return bytes_, src, meta2
        return None, url, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return None, url, meta


async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None,
                "pdf_source": None, "file_size_kb": None, "attempts": []}

    # Expand each input URL with publisher-specific alternatives (PMC direct
    # PDF endpoint, Copernicus print-PDF, etc.) interleaved after the original
    expanded: list[str] = []
    seen_exp:  set[str] = set()
    for u in pdf_urls:
        if u and u not in seen_exp:
            seen_exp.add(u)
            expanded.append(u)
            for alt in _publisher_specific_urls(u):
                if alt not in seen_exp:
                    seen_exp.add(alt)
                    expanded.append(alt)

    attempts = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS
    ) as c:
        for i, url in enumerate(expanded, 1):
            if i > 1:
                await asyncio.sleep(0.3)
            data, src, meta = await _try_one_url(c, url, follow_html=True)

            attempt = {
                "url":          url,
                "attempt_no":   i,
                "http_status":  meta["http_status"],
                "content_type": meta["content_type"],
                "is_pdf":       meta["is_pdf"],
                "success":      False,
                "file_size_kb": None,
                "error":        meta["error"],
                "attempted_at": datetime.utcnow(),
            }

            if data and data[:4] == b"%PDF":
                stem = Path(unquote(urlparse(src).path)).stem or "openalex_doc"
                stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
                path = pdf_dir / f"openalex_{stem}.pdf"
                path.write_bytes(data)
                size_kb = len(data) // 1024
                attempt["success"]      = True
                attempt["file_size_kb"] = size_kb
                attempts.append(attempt)
                return {
                    "status": "success", "pdf_path": str(path),
                    "pdf_source": src, "file_size_kb": size_kb,
                    "attempts": attempts,
                }
            attempts.append(attempt)

    return {"status": "failed", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": attempts}
