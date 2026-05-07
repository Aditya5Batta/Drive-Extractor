"""
PubMed scraper — NCBI E-utilities (db=pubmed) with "Free full text" filter.

Mirrors https://pubmed.ncbi.nlm.nih.gov/?term=<query>&filter=simsearch2.ffrft
       (Best match / relevance — the website default sort)

PubMed indexes ~30M citations across all biomedical literature; only a
subset has free full text available. We apply the Free Full Text filter
at the API level (`AND free full text[Filter]`), then use the same PMC →
EuropePMC PDF download chain as the PMC module for any article with a
linked PMCID.

Different from the PMC scraper: that one searches `db=pmc` (only the ~6M
articles with full-text in PMC). PubMed searches the broader citation
index but with the free full text filter on, and resolves PMID→PMCID at
result time so we can still fetch the PDF.

Flow:
  1. esearch db=pubmed term="<q> AND free full text[Filter]" sort=relevance
     → list of PMIDs in website Best-match order
  2. esummary db=pubmed → title/authors/journal/year/DOI/PMCID per PMID
  3. For each result, build the PDF URL chain:
       a. https://pmc.ncbi.nlm.nih.gov/articles/PMC<id>/pdf/   (open access)
       b. https://europepmc.org/articles/PMC<id>?pdf=render    (mirror)
"""
from __future__ import annotations
import asyncio, hashlib, os, re, ssl, time
from datetime import datetime
from pathlib import Path

import httpx


# ── PMC Proof-of-Work bot challenge ───────────────────────────────────────────
# pmc.ncbi.nlm.nih.gov gates PDF downloads behind a hashcash-style PoW. The
# response HTML inlines POW_CHALLENGE / POW_DIFFICULTY; we solve the puzzle
# (find a nonce where SHA-256(challenge + nonce) starts with N hex zeros) and
# set the resulting cookie so the same client can fetch the actual PDF.
_POW_CHALLENGE_RE  = re.compile(r'POW_CHALLENGE\s*=\s*["\']([^"\']+)["\']')
_POW_DIFFICULTY_RE = re.compile(r'POW_DIFFICULTY\s*=\s*["\']?(\d+)')
_POW_COOKIE_RE     = re.compile(r'POW_COOKIE_NAME\s*=\s*["\']([^"\']+)["\']')


def _solve_pow(challenge: str, difficulty: int) -> int:
    """SHA-256 hashcash — find nonce N where hash(challenge+N) starts with
    `difficulty` hex zeros. ~0.1s for difficulty=4 on commodity hardware."""
    target = "0" * difficulty
    nonce  = 0
    while True:
        h = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()
        if h.startswith(target):
            return nonce
        nonce += 1


def _try_solve_pow(html: str) -> tuple[str, str, int] | None:
    """Extract & solve a PMC PoW challenge from interstitial HTML.
    Returns (cookie_name, cookie_value, nonce) or None if not a PoW page."""
    m_chal = _POW_CHALLENGE_RE.search(html)
    if not m_chal:
        return None
    m_diff = _POW_DIFFICULTY_RE.search(html)
    m_cook = _POW_COOKIE_RE.search(html)
    challenge  = m_chal.group(1)
    difficulty = int(m_diff.group(1)) if m_diff else 4
    cookie     = m_cook.group(1) if m_cook else "cloudpmc-viewer-pow"
    nonce      = _solve_pow(challenge, difficulty)
    return cookie, f"{challenge},{nonce}", nonce

PUBMED_WEB   = "https://pubmed.ncbi.nlm.nih.gov/"
ESEARCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TIMEOUT      = 30.0

# PubMed search results page lists PMIDs in Best Match order via this attribute
_PMID_FROM_HTML = re.compile(r'data-article-id=["\'](\d+)["\']')

# Optional NCBI API key — raises rate limit from 3 req/s → 10 req/s if set.
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json,application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
PDF_HEADERS = {**HEADERS, "Accept": "application/pdf,*/*"}


async def _ncbi_get(url: str, params: dict) -> httpx.Response:
    """
    GET an NCBI E-utilities URL with SSL verification, falling back to
    unverified TLS only if the system cert store rejects NCBI's certificate
    (common on Windows where revocation check sometimes fails for *.nih.gov).
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            return await c.get(url, params=params)
    except (httpx.ConnectError, ssl.SSLError, OSError) as e:
        if "ssl" in str(e).lower() or "certificate" in str(e).lower():
            async with httpx.AsyncClient(
                timeout=TIMEOUT, headers=HEADERS, verify=False
            ) as c:
                return await c.get(url, params=params)
        raise


def _with_api_key(params: dict) -> dict:
    if NCBI_API_KEY:
        return {**params, "api_key": NCBI_API_KEY}
    return params


async def search(query: str, max_results: int = 25) -> dict:
    """
    Search PubMed for free-full-text articles in the website's exact Best
    Match order. The E-utilities API only exposes the OLD relevance algorithm
    (sort=relevance), which differs from the website's newer ML-based Best
    Match. To match the website 1:1 we scrape the HTML search page (which is
    server-rendered with PMIDs in correct order) and only use E-utilities
    for the metadata enrichment step.
    """
    t0 = time.monotonic()

    # ── Step 1: Get PMIDs in PubMed's relevance order ───────────────────────
    # Strategy: Try the website first (Best Match ML ranking, matches the
    # user-facing site exactly). If blocked (403/timeout/etc.), fall back to
    # E-utilities esearch with sort=relevance — slightly different ranking
    # but always works and uses the official API.
    web_params = {"term": query, "filter": "simsearch2.ffrft"}
    api_url = str(httpx.Request("GET", PUBMED_WEB, params=web_params).url)

    web_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "none",
        "Sec-Fetch-User":  "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control":   "max-age=0",
    }

    pmids: list[str] = []

    # Try website scrape (best ordering)
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, headers=web_headers,
            follow_redirects=True, verify=False,
        ) as c:
            r = await c.get(PUBMED_WEB, params=web_params)
        if r.status_code == 200:
            seen: set[str] = set()
            for pmid in _PMID_FROM_HTML.findall(r.text):
                if pmid not in seen:
                    seen.add(pmid)
                    pmids.append(pmid)
                if len(pmids) >= max_results:
                    break
    except Exception:
        pass  # Fall through to E-utilities

    # Fallback: E-utilities esearch (always works, slightly different order)
    if not pmids:
        esearch_params = _with_api_key({
            "db":      "pubmed",
            "term":    f"{query} AND free full text[Filter]",
            "retmax":  max_results,
            "retmode": "json",
            "sort":    "relevance",
        })
        try:
            er = await _ncbi_get(ESEARCH_URL, esearch_params)
            er.raise_for_status()
            pmids = er.json().get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            return {
                "query_sent": query, "api_url": api_url,
                "papers_returned": 0, "papers_with_pmcid": 0,
                "response_time_ms": int((time.monotonic() - t0) * 1000),
                "status": "error",
                "error": f"both website scrape and esearch failed: {e}",
                "papers": [],
            }

    response_time_ms = int((time.monotonic() - t0) * 1000)

    if not pmids:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "ok", "error": None, "papers": [],
        }

    # ── Step 2: esummary → metadata + PMCID per PMID ─────────────────────────
    # NCBI rate limit: 3 req/s without key, 10 req/s with key
    await asyncio.sleep(0.35 if not NCBI_API_KEY else 0.1)

    # version=2.0 is required for db=pubmed json — v1 returns HTTP 500
    summary_params = _with_api_key({
        "db":      "pubmed",
        "id":      ",".join(pmids),
        "retmode": "json",
        "version": "2.0",
    })
    try:
        sr = await _ncbi_get(ESUMMARY_URL, summary_params)
        sr.raise_for_status()
        summaries = sr.json().get("result", {})
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": len(pmids), "papers_with_pmcid": 0,
            "response_time_ms": response_time_ms,
            "status": "error", "error": f"esummary failed: {e}", "papers": [],
        }

    # ── Step 3: build paper list in PubMed relevance order ──────────────────
    papers: list[dict] = []
    for pmid in pmids:
        s = summaries.get(pmid)
        if not s or not isinstance(s, dict):
            continue

        # Walk articleids — PubMed summaries include pmc, doi, pii, etc.
        pmcid    = None
        doi      = None
        for aid in s.get("articleids", []):
            t = aid.get("idtype", "")
            v = aid.get("value", "") or ""
            if t == "pmc":
                pmcid = v if v.upper().startswith("PMC") else f"PMC{v}"
            elif t == "doi":
                doi = v or None

        title   = (s.get("title") or "").strip()
        # PubMed wraps titles in [brackets] for non-English articles; trim them
        title   = re.sub(r'^\[(.+)\]\.?$', r'\1', title).strip()
        journal = (s.get("source") or s.get("fulljournalname") or "PubMed").strip()
        pubdate = s.get("pubdate") or s.get("epubdate") or ""
        year    = pubdate[:4] if pubdate else ""

        authors = [
            (a.get("name") or "").strip()
            for a in (s.get("authors") or [])
        ]
        authors = [a for a in authors if a]

        # PDF: use the PMC native /pdf/ endpoint. The download_pdf function
        # solves NCBI's proof-of-work bot challenge and fetches the real PDF.
        # We deliberately use ONE URL per paper to avoid the multi-PDF
        # expansion path producing duplicate downloads of the same article.
        pdf_urls: list[str] = (
            [f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"]
            if pmcid else []
        )

        # Landing page: prefer the PubMed citation page (always exists)
        epmc_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        papers.append({
            "pmcid":             pmcid,
            "pmid":              pmid,
            "doi":               doi,
            "title":             title[:240],
            "abstract":          "",   # esummary doesn't return abstract text
            "authors":           authors,
            "journal":           journal[:240],
            "year":              year[:8],
            "epmc_url":          epmc_url,
            "pdf_urls":          pdf_urls,
            "has_pdf_from_api":  bool(pdf_urls),
            "api_pdf_url_count": len(pdf_urls),
        })

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(pmids),
        "papers_with_pmcid": sum(1 for p in papers if p["pmcid"]),
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Try each PDF URL in order — PMC native first, then EuropePMC mirror."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    attempts = []

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None,
                "pdf_source": None, "file_size_kb": None, "attempts": []}

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=PDF_HEADERS,
        verify=False,  # NCBI cert revocation flakiness on Windows
    ) as c:
        for i, url in enumerate(pdf_urls, 1):
            await asyncio.sleep(0.4)
            attempt = {
                "url": url, "attempt_no": i,
                "http_status": None, "content_type": None,
                "is_pdf": False, "success": False,
                "file_size_kb": None, "error": None,
                "attempted_at": datetime.utcnow(),
            }
            try:
                r  = await c.get(url)
                ct = r.headers.get("content-type", "")
                is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                # PMC's bot challenge: small HTML interstitial with PoW JS.
                # Solve the puzzle, set the cookie, retry once on the same client.
                if (not is_pdf and r.status_code == 200
                        and "html" in ct.lower()
                        and "pmc.ncbi.nlm.nih.gov" in str(r.url)):
                    pow_result = _try_solve_pow(r.text)
                    if pow_result:
                        cookie_name, cookie_value, _nonce = pow_result
                        # Set on the parent domain so it applies to /articles/.../pdf/
                        c.cookies.set(
                            cookie_name, cookie_value,
                            domain="pmc.ncbi.nlm.nih.gov", path="/",
                        )
                        r  = await c.get(url)
                        ct = r.headers.get("content-type", "")
                        is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

                attempt["http_status"]  = r.status_code
                attempt["content_type"] = ct[:128]
                attempt["is_pdf"]       = is_pdf

                if r.status_code == 200 and is_pdf:
                    safe_pmcid = (pmcid or "unknown").replace("/", "_")
                    safe_pmid  = (pmid or "noid").replace("/", "_")
                    path = pdf_dir / f"pubmed_{safe_pmcid}_{safe_pmid}.pdf"
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
