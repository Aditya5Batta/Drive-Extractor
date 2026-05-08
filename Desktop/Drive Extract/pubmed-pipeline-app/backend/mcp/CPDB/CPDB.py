"""
CPDB (Carcinogenic Potency Database) scraper.

Source: https://files.toxplanet.com/cpdb/chempages/<CHEMICAL>.html
        Hosted by toxplanet.com (a UC Berkeley / Lawrence Berkeley Lab archive).
        481 chemicals, last updated 2001.

Architecture
────────────
The CPDB is a static S3-hosted site.  There are NO per-chemical PDF files —
each chemical has a single HTML page (tables of TD50 / target-organ data).

Search
  GET https://files.toxplanet.com/cpdb/chemnameindex.html
  → parse all <a href="chempages/…html">Name</a> links (481 total)
  → case-insensitive filter on query string
  → concurrently fetch each matching page to extract CAS number + snippet

PDF download  (the "Ctrl+P" approach the user requested)
  Uses playwright (headless Chromium / installed Chrome) to navigate to the
  CPDB HTML page and call page.pdf(), which is the programmatic equivalent
  of pressing Ctrl+P → "Save as PDF" in the browser.

Result order
  Follows the order in chemnameindex.html (alphabetical within the CPDB),
  filtered to those containing the query string.  The exact-name match is
  moved to position 1 so "Benzene" appears before "Benzene Hexachloride".
"""
from __future__ import annotations
import asyncio, re, time
import html as _html
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, unquote

import httpx

# ── playwright availability ───────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

CPDB_BASE  = "https://files.toxplanet.com/cpdb"
INDEX_URL  = f"{CPDB_BASE}/chemnameindex.html"
TIMEOUT    = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Links in the index:  href="chempages/BENZENE.html"  text="Benzene"
_INDEX_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\']([^"\']*chempages/[^"\']+\.html)["\'][^>]*>([^<]+)</a>',
    re.I,
)
# CAS in page:  e.g.  Benzene (CAS 71-43-2)  or  (71-43-2)
_CAS_RE    = re.compile(r'\((?:CAS\s+)?([\d-]{5,})\)')
_TITLE_RE  = re.compile(r'<title[^>]*>([^<]+)</title>', re.I)
_META_DESC = re.compile(
    r'<meta\b[^>]*\bname=["\']description["\'][^>]*\bcontent=["\']([^"\']+)["\']',
    re.I,
)
_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub("", _html.unescape(text))).strip()


# ── index fetcher / parser ────────────────────────────────────────────────────

async def _get_index(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """
    Fetch chemnameindex.html and return list of (chemical_name, full_page_url).
    """
    try:
        r = await client.get(INDEX_URL, timeout=TIMEOUT)
        html = r.text
    except Exception:
        return []

    results: list[tuple[str, str]] = []
    for m in _INDEX_LINK_RE.finditer(html):
        rel_href = m.group(1).strip()          # e.g. "chempages/BENZENE.html"
        name     = _clean(m.group(2))          # e.g. "Benzene"
        full_url = f"{CPDB_BASE}/{rel_href}"
        results.append((name, full_url))
    return results


# ── per-page metadata fetcher ─────────────────────────────────────────────────

async def _fetch_page_meta(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    sem: asyncio.Semaphore,
) -> dict:
    """
    Fetch one CPDB chem page and extract: title, cas, snippet.
    Falls back to chemical name if the page is unreachable.
    """
    async with sem:
        await asyncio.sleep(0.05)
        try:
            r = await client.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            html = r.text
        except Exception:
            return {"title": f"{name} — CPDB", "cas": "", "snippet": ""}

    # Title
    tm = _TITLE_RE.search(html)
    title = _clean(tm.group(1)).replace(": Carcinogenic Potency Database", "").strip() if tm else name
    if not title:
        title = name

    # CAS
    cas = ""
    cm = _CAS_RE.search(html[:3000])   # CAS near the top of the page
    if cm:
        cas = cm.group(1)

    # Snippet from meta description or first few words of page text
    snippet = ""
    dm = _META_DESC.search(html)
    if dm:
        snippet = _clean(dm.group(1))[:400]
    if not snippet:
        snippet = (
            f"CPDB carcinogenicity data for {name}."
            + (f" CAS: {cas}." if cas else "")
            + " Includes TD₅₀ values and target-organ data for rat and mouse."
        )

    return {"title": title, "cas": cas, "snippet": snippet}


# ── public search ─────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search the CPDB chemical index for names containing `query`.

    Exact-name match is promoted to position 1.
    For each match, fetches the individual chem page concurrently to extract
    CAS number and snippet.  Returns results in CPDB index order (alphabetical
    within matched set), exact match first.
    """
    t0      = time.monotonic()
    api_url = f"{CPDB_BASE}/chemnameindex.html"
    q_low   = query.strip().lower()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:

        # Step 1 – fetch index and filter
        all_chems = await _get_index(client)
        if not all_chems:
            return {
                "query_sent": query, "api_url": api_url,
                "papers_returned": 0, "papers_with_pmcid": 0,
                "response_time_ms": int((time.monotonic() - t0) * 1000),
                "status": "error", "error": "Could not fetch CPDB index",
                "papers": [],
            }

        matched = [(n, u) for n, u in all_chems if q_low in n.lower()]

        # Exact match → first
        exact   = [(n, u) for n, u in matched if n.lower() == q_low]
        partial = [(n, u) for n, u in matched if n.lower() != q_low]
        ordered = (exact + partial)[:max_results]

        if not ordered:
            return {
                "query_sent": query, "api_url": api_url,
                "papers_returned": 0, "papers_with_pmcid": 0,
                "response_time_ms": int((time.monotonic() - t0) * 1000),
                "status": "ok", "error": None, "papers": [],
            }

        # Step 2 – fetch each chem page concurrently for metadata
        sem      = asyncio.Semaphore(6)
        meta_list = await asyncio.gather(
            *[_fetch_page_meta(client, n, u, sem) for n, u in ordered],
            return_exceptions=True,
        )

    papers: list[dict] = []
    for (name, url), meta in zip(ordered, meta_list):
        if isinstance(meta, Exception) or not meta:
            meta = {"title": f"{name} — CPDB", "cas": "", "snippet": ""}

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             meta["title"] or f"{name} — CPDB",
            "abstract":          meta["snippet"],
            "authors":           [],
            "journal":           "CPDB",
            "year":              "2001",   # DB last updated 2001
            "epmc_url":          url,      # CPDB page URL (shown as "View" link)
            # pdf_urls contains the HTML page URL — download_pdf prints it via playwright
            "pdf_urls":          [url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": 0,
        "response_time_ms":  int((time.monotonic() - t0) * 1000),
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


# ── playwright PDF printer ────────────────────────────────────────────────────

async def _print_to_pdf(url: str, path: Path) -> bool:
    """
    Navigate to `url` with headless Chrome and save a PDF — equivalent to
    Ctrl+P → Save as PDF.  Tries system-installed Chrome first (no extra
    download required), falls back to playwright's bundled Chromium.
    Returns True on success.
    """
    async with _async_playwright() as pw:
        # Try system Chrome first (already installed on Windows)
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                if channel:
                    browser = await pw.chromium.launch(
                        channel=channel, headless=True,
                        args=["--no-sandbox", "--disable-setuid-sandbox"],
                    )
                else:
                    browser = await pw.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-setuid-sandbox"],
                    )
                break
            except Exception:
                continue

        if browser is None:
            return False

        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Small pause so any inline JS finishes rendering (tables, etc.)
            await asyncio.sleep(0.5)
            await page.pdf(
                path=str(path),
                format="A4",
                print_background=True,
                margin={"top": "1.5cm", "right": "1cm",
                        "bottom": "1.5cm", "left": "1cm"},
            )
        finally:
            await browser.close()

    return path.exists() and path.stat().st_size > 100


# ── public download ───────────────────────────────────────────────────────────

async def download_pdf(
    pmcid: str, pmid: str, pdf_urls: list[str], pdf_dir: Path
) -> dict:
    """
    For CPDB there are no pre-built PDFs — each URL is the CPDB HTML page.
    We use playwright (headless Chrome) to print the page to PDF, mimicking
    the Ctrl+P → Save as PDF workflow.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": [],
        }

    if not _PLAYWRIGHT_OK:
        return {
            "status": "failed", "pdf_path": None, "pdf_source": None,
            "file_size_kb": None,
            "attempts": [{
                "url": pdf_urls[0], "attempt_no": 1,
                "http_status": None, "content_type": None, "is_pdf": False,
                "success": False, "file_size_kb": None,
                "error": "playwright not installed — run: pip install playwright && playwright install chromium",
                "attempted_at": datetime.utcnow(),
            }],
        }

    attempts = []
    for i, url in enumerate(pdf_urls, 1):
        await asyncio.sleep(0.3)
        attempt = {
            "url":          url,
            "attempt_no":   i,
            "http_status":  None,
            "content_type": "text/html→application/pdf",
            "is_pdf":       False,
            "success":      False,
            "file_size_kb": None,
            "error":        None,
            "attempted_at": datetime.utcnow(),
        }

        # Derive a clean filename from the URL
        stem = Path(unquote(url.rstrip("/").split("/")[-1])).stem or "cpdb_doc"
        stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem)[:80]
        out_path = pdf_dir / f"cpdb_{stem}.pdf"

        try:
            ok = await _print_to_pdf(url, out_path)
            if ok:
                size_kb = out_path.stat().st_size // 1024
                attempt["is_pdf"]       = True
                attempt["success"]      = True
                attempt["file_size_kb"] = size_kb
                attempts.append(attempt)
                return {
                    "status":       "success",
                    "pdf_path":     str(out_path),
                    "pdf_source":   url,
                    "file_size_kb": size_kb,
                    "attempts":     attempts,
                }
            else:
                attempt["error"] = "playwright print produced empty file"
        except Exception as e:
            attempt["error"] = f"{type(e).__name__}: {e}"

        attempts.append(attempt)

    return {
        "status":       "failed",
        "pdf_path":     None,
        "pdf_source":   None,
        "file_size_kb": None,
        "attempts":     attempts,
    }
