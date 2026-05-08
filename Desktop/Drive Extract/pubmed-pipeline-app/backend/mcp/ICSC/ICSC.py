"""
ICSC (International Chemical Safety Cards) scraper.

Source: https://chemicalsafety.ilo.org/dyn/icsc/showcard.listCards3
        ILO/WHO joint programme — ~1,800 safety cards, fully SSR HTML.

Search
──────
POST https://chemicalsafety.ilo.org/dyn/icsc/showcard.listCards3
  p_lang=en  &  p_synonym={query}

Returns a table of matching cards (card_id + main name + synonyms).
The server does NOT rank by relevance — it returns everything whose name OR
synonym contains the query string (e.g. searching "Benzene" returns ANILINE
first because "Benzeneamine" is an Aniline synonym).

We re-rank client-side:
  0 — main name == query (exact, case-insensitive)          → top
  1 — main name starts with query
  2 — main name contains query
  3 — synonym-only match                                    → bottom

Card detail
───────────
GET https://chemicalsafety.ilo.org/dyn/icsc/showcard.display
    ?p_lang=en&p_card_id={NNNN}&p_version=2
Fully SSR. Contains CAS #, UN #, EC Number, hazard tables.

PDF
───
No pre-built PDFs exist.  Uses playwright headless Chrome to call page.pdf()
— equivalent to Ctrl+P → Save as PDF in the browser.
"""
from __future__ import annotations
import asyncio, re, time
import html as _html
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, unquote

import httpx

# ── playwright ────────────────────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

ICSC_BASE   = "https://chemicalsafety.ilo.org"
SEARCH_URL  = f"{ICSC_BASE}/dyn/icsc/showcard.listCards3"
CARD_URL    = f"{ICSC_BASE}/dyn/icsc/showcard.display"
TIMEOUT     = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         ICSC_BASE + "/",
}

# ── regex ─────────────────────────────────────────────────────────────────────
# One result row:
#   <tr valign="top">
#     <td><a href="/dyn/icsc/showcard.display?...&p_card_id=0015&..." ...>0015</a> </td>
#     <td>BENZENE<br /><span ...>synonyms</span></td>
#   </tr>
_ROW_RE = re.compile(
    r'<tr[^>]*valign=["\']?top["\']?[^>]*>\s*'
    r'<td>\s*<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(\d+)</a>.*?</td>\s*'
    r'<td>(.*?)</td>',
    re.I | re.S,
)
_SPAN_RE   = re.compile(r'<span[^>]*>(.*?)</span>', re.I | re.S)
_TAG_STRIP = re.compile(r'<[^>]+>')
_WS_NORM   = re.compile(r'\s+')

_CAS_RE    = re.compile(r'CAS\s*#:\s*([\d-]{5,})', re.I)
_UN_RE     = re.compile(r'UN\s*#:\s*(\d+)', re.I)
_EC_RE     = re.compile(r'EC\s*Number:\s*([\d-]+)', re.I)
_TITLE_RE  = re.compile(r'<title[^>]*>\s*ICSC\s*(\d+)\s*-\s*([^<]+)</title>', re.I)


def _clean(text: str) -> str:
    return _WS_NORM.sub(" ", _TAG_STRIP.sub("", _html.unescape(text))).strip()


def _rank(main_name: str, query: str) -> int:
    """Lower is better. Ranks result rows by relevance to the query."""
    n = main_name.strip().lower()
    q = query.strip().lower()
    if n == q:          return 0   # exact match
    if n.startswith(q): return 1   # starts-with
    if q in n:          return 2   # contains
    return 3                        # synonym-only match


# ── search result parser ──────────────────────────────────────────────────────

def _parse_results(html: str) -> list[dict]:
    """
    Parse the search-results HTML into a list of
    {card_id, href, main_name, synonyms}.
    """
    rows: list[dict] = []
    for m in _ROW_RE.finditer(html):
        href      = m.group(1).strip()
        card_id   = m.group(2).strip().zfill(4)
        td_inner  = m.group(3)

        # main_name is text before the first <br> or <span>
        main_raw = re.split(r'<br|<span', td_inner, maxsplit=1, flags=re.I)[0]
        main_name = _clean(main_raw)

        # synonyms from <span>
        sm = _SPAN_RE.search(td_inner)
        synonyms = _clean(sm.group(1)) if sm else ""

        # absolute href
        if href.startswith("/"):
            href = ICSC_BASE + href
        elif not href.startswith("http"):
            href = urljoin(SEARCH_URL, href)

        if main_name:
            rows.append({
                "card_id":   card_id,
                "href":      href,
                "main_name": main_name,
                "synonyms":  synonyms,
            })
    return rows


# ── card page metadata ────────────────────────────────────────────────────────

async def _fetch_card_meta(
    client: httpx.AsyncClient,
    card_id: str,
    sem: asyncio.Semaphore,
) -> dict:
    """Fetch one ICSC card page and extract CAS, UN, EC numbers."""
    url = f"{CARD_URL}?p_lang=en&p_card_id={card_id}&p_version=2"
    async with sem:
        await asyncio.sleep(0.05)
        try:
            r = await client.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                return {}
            html = r.text
        except Exception:
            return {}

    cas = (m := _CAS_RE.search(html[:4000])) and m.group(1) or ""
    un  = (m := _UN_RE.search(html[:4000]))  and m.group(1) or ""
    ec  = (m := _EC_RE.search(html[:4000]))  and m.group(1) or ""

    snippet_parts = []
    if cas: snippet_parts.append(f"CAS: {cas}")
    if un:  snippet_parts.append(f"UN: {un}")
    if ec:  snippet_parts.append(f"EC: {ec}")
    snippet = "International Chemical Safety Card. " + "  ·  ".join(snippet_parts)

    return {"cas": cas, "un": un, "ec": ec, "snippet": snippet, "card_url": url}


# ── public search ─────────────────────────────────────────────────────────────

async def search(query: str, max_results: int = 20) -> dict:
    """
    Search ICSC cards by chemical name/synonym.
    Re-ranks so exact name matches appear before synonym-only hits.
    Fetches the top-N card pages concurrently for CAS/UN metadata.
    """
    t0      = time.monotonic()
    api_url = f"{ICSC_BASE}/dyn/icsc/showcard.listCards3"

    # ── Step 1: POST search ───────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
        ) as client:
            r = await client.post(
                SEARCH_URL,
                data={"p_lang": "en", "p_synonym": query},
            )
            if r.status_code != 200:
                return {
                    "query_sent": query, "api_url": api_url,
                    "papers_returned": 0, "papers_with_pmcid": 0,
                    "response_time_ms": int((time.monotonic() - t0) * 1000),
                    "status": "error",
                    "error": f"Search returned HTTP {r.status_code}",
                    "papers": [],
                }
            search_html = r.text
    except Exception as exc:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": str(exc), "papers": [],
        }

    # ── Step 2: parse + re-rank ───────────────────────────────────────────────
    rows = _parse_results(search_html)
    if not rows:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "ok", "error": None, "papers": [],
        }

    # Sort: lower rank score = better match
    rows.sort(key=lambda r: _rank(r["main_name"], query))
    rows = rows[:max_results]

    # ── Step 3: fetch card pages concurrently for metadata ────────────────────
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=TIMEOUT
    ) as client:
        meta_list = await asyncio.gather(
            *[_fetch_card_meta(client, row["card_id"], sem) for row in rows],
            return_exceptions=True,
        )

    # ── Step 4: build papers ──────────────────────────────────────────────────
    papers: list[dict] = []
    for row, meta in zip(rows, meta_list):
        if isinstance(meta, Exception) or not meta:
            meta = {
                "cas": "", "un": "", "ec": "",
                "snippet": f"International Chemical Safety Card for {row['main_name']}.",
                "card_url": row["href"],
            }

        synonyms = row["synonyms"]
        abstract = meta["snippet"]
        if synonyms:
            abstract += f"  Synonyms: {synonyms}."

        papers.append({
            "pmcid":             None,
            "pmid":              None,
            "doi":               None,
            "title":             f"ICSC {row['card_id']}: {row['main_name']}",
            "abstract":          abstract[:400],
            "authors":           [],
            "journal":           "ICSC",
            "year":              "",
            "epmc_url":          row["href"],
            # HTML URL → playwright prints it to PDF in download_pdf()
            "pdf_urls":          [row["href"]],
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


# ── playwright PDF ─────────────────────────────────────────────────────────────

async def _print_to_pdf(url: str, path: Path) -> bool:
    """Ctrl+P → Save as PDF using headless Chrome/Edge/Chromium."""
    async with _async_playwright() as pw:
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = dict(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                if channel:
                    kwargs["channel"] = channel
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if browser is None:
            return False
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
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
    pdf_urls[0] is the ICSC card HTML page URL.
    Uses playwright (Ctrl+P equivalent) to render and save as PDF.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {
            "status": "no_url", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None, "attempts": [],
        }

    if not _PLAYWRIGHT_OK:
        return {
            "status": "failed", "pdf_path": None,
            "pdf_source": None, "file_size_kb": None,
            "attempts": [{
                "url": pdf_urls[0], "attempt_no": 1,
                "http_status": None, "content_type": None,
                "is_pdf": False, "success": False, "file_size_kb": None,
                "error": (
                    "playwright not installed — "
                    "run: pip install playwright && playwright install chromium"
                ),
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
        # card_id from URL param p_card_id=NNNN
        m   = re.search(r'p_card_id=(\d+)', url)
        cid = m.group(1) if m else "icsc"
        out = pdf_dir / f"icsc_{cid}.pdf"

        try:
            ok = await _print_to_pdf(url, out)
            if ok:
                size_kb = out.stat().st_size // 1024
                attempt.update(is_pdf=True, success=True, file_size_kb=size_kb)
                attempts.append(attempt)
                return {
                    "status":       "success",
                    "pdf_path":     str(out),
                    "pdf_source":   url,
                    "file_size_kb": size_kb,
                    "attempts":     attempts,
                }
            attempt["error"] = "playwright PDF was empty"
        except Exception as e:
            attempt["error"] = f"{type(e).__name__}: {e}"
        attempts.append(attempt)

    return {
        "status": "failed", "pdf_path": None,
        "pdf_source": None, "file_size_kb": None,
        "attempts": attempts,
    }
