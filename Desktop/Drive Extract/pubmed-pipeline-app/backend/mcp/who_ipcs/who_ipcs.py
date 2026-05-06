"""
WHO IPCS scraper — uses WHO's curated chemical-safety topic pages first.

Primary source:
  https://www.who.int/teams/environment-climate-change-and-health/
       chemical-safety-and-health/health-impacts/chemicals/<chemical>

These topic pages exist for all the chemicals WHO IPCS treats as "of major
public health concern" (benzene, lead, mercury, asbestos, arsenic, cadmium,
dioxins, …).  Each page is a hand-curated list of the WHO publications the
IPCS unit considers most relevant — exactly what the user wants.

We extract:
  • <img src="iris.who.int/.../bitstreams/UUID/content" alt="TITLE">
       → IRIS-hosted publications (the IRIS bitstream URL also serves the PDF
         when GET'd with Accept: application/pdf).
  • <a href="cdn.who.int/.../*.pdf">
       → direct WHO CDN PDFs (e.g. background documents).

For chemicals without a dedicated topic page (e.g. aspirin, toluene,
formaldehyde — those return 404), we fall back to a basic IRIS search.
"""
from __future__ import annotations
import asyncio, html, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

# Curated topic pages
TOPIC_BASE = ("https://www.who.int/teams/environment-climate-change-and-health/"
              "chemical-safety-and-health/health-impacts/chemicals")
# IRIS DSpace API for fallback
API_BASE   = "https://iris.who.int/server/api"
SEARCH_URL = f"{API_BASE}/discover/search/objects"
TIMEOUT    = 30.0
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    # Accept both HTML (topic-page scrape) and JSON (IRIS API)
    "Accept":          "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
PDF_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept":     "application/pdf,*/*",
}


def _md(metadata: dict, key: str, default: str = "") -> str:
    """Pull the first value from a DSpace metadata dict."""
    vals = metadata.get(key) or []
    if vals and isinstance(vals, list):
        return (vals[0].get("value") or "").strip()
    return default


async def _bitstream_url(client: httpx.AsyncClient, item_uuid: str) -> tuple[str, str]:
    """
    Walk bundles → bitstreams to find the first PDF.
    Returns (download_url, filename).  Empty strings if none found.
    """
    try:
        rb = await client.get(f"{API_BASE}/core/items/{item_uuid}/bundles")
        bundles = (rb.json().get("_embedded", {}) or {}).get("bundles") or []
        # Prefer the "ORIGINAL" bundle which contains the publication PDF
        bundles.sort(key=lambda b: 0 if (b.get("name") == "ORIGINAL") else 1)
        for bundle in bundles:
            bs_link = (bundle.get("_links") or {}).get("bitstreams", {}).get("href")
            if not bs_link:
                continue
            rs = await client.get(bs_link)
            for bs in (rs.json().get("_embedded", {}) or {}).get("bitstreams") or []:
                name = (bs.get("name") or "").strip()
                if name.lower().endswith(".pdf"):
                    content = (bs.get("_links") or {}).get("content", {}).get("href")
                    if content:
                        return content, name
    except Exception:
        pass
    return "", ""


# Real PDF download anchors (NOT <img src> — those are JPEG thumbnails).
# WHO templates use both quoted and UNQUOTED href values, e.g.:
#   <a class="download-url" href="https://iris.who.int/.../UUID/content">
#   <a href=https://iris.who.int/.../UUID/content  aria-label="Download">  ← no quotes!
# Match the URL with or without surrounding quotes.
_DOWNLOAD_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?\shref=["\']?'
    r'(https://iris\.who\.int/server/api/core/bitstreams/[a-f0-9-]+/content)'
    r'["\']?',
    re.I,
)
# A thumbnail <img alt="TITLE"> appears EARLIER in the same publication block;
# we use it as the title for the nearest following download anchor.
_IMG_ALT_RE = re.compile(r'<img\s+[^>]*alt="([^"]+)"[^>]*>', re.I)
# Direct cdn.who.int PDF links (background documents etc.)
_CDN_PDF_RE = re.compile(r'href="(https://cdn\.who\.int/[^"]+\.pdf[^"]*)"', re.I)
# Nearby <h3> title for a CDN PDF link
_NEARBY_TITLE_RE = re.compile(r'<h3[^>]*>(.*?)</h3>', re.S)
# "WHO evaluation and risk assessment documents" arrow links — point at IARC,
# inchem.org and ILO landing pages.  We follow each landing page to find its
# real PDF URL.
_ARROWED_LINK_RE = re.compile(
    r'<div class="arrowed-link"\s*>\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_TAG_STRIP = re.compile(r'<[^>]+>')


async def _resolve_landing_to_pdfs(client: httpx.AsyncClient, landing_url: str
                                    ) -> list[tuple[str, str]]:
    """
    Fetch an HTML landing page and return ALL downloadable PDFs inside,
    as (pdf_url, label) pairs in page order.  Returns [] when no PDFs.

    For IARC monograph pages this yields the full volume PDF + every chapter
    PDF (~18 per monograph).  For inchem.org/htm pages it yields the single
    referenced PDF (often the IARC monograph itself).
    """
    try:
        r = await client.get(landing_url, timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return []
        ct = r.headers.get("content-type", "").lower()
        if "pdf" in ct:           # the URL was itself a PDF
            return [(str(r.url), Path(landing_url.split("?")[0]).stem)]
        if "html" not in ct and "xml" not in ct:
            return []
        # Find every <a href="...pdf"> on the page (with anchor text as label)
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        anchor_re = re.compile(
            r'<a\s+[^>]*?href=["\']?([^"\'\s>]+\.pdf[^"\'\s]*)["\']?[^>]*>(.*?)</a>',
            re.S | re.I)
        for m in anchor_re.finditer(r.text):
            pdf  = urljoin(str(r.url), html.unescape(m.group(1)))
            text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if pdf in seen:
                continue
            seen.add(pdf)
            label = text or Path(pdf.split("?")[0]).stem.replace("-", " ")
            pairs.append((pdf, label))
        return pairs
    except Exception:
        return []


def _slugify(query: str) -> str:
    """Map a chemical name to the WHO topic-page slug."""
    s = query.lower().strip()
    s = re.sub(r'[^a-z0-9-]+', '-', s)   # spaces / underscores → hyphens
    return re.sub(r'-+', '-', s).strip('-')


async def _scrape_topic_page(client: httpx.AsyncClient, query: str
                             ) -> tuple[list[dict], str]:
    """
    Try the WHO chemical-safety topic page for `query`.  Returns (papers, url).
    Empty papers list when no topic page exists for this chemical.
    """
    url = f"{TOPIC_BASE}/{_slugify(query)}"
    try:
        r = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return [], url
        text = r.text
    except Exception:
        return [], url

    papers:    list[dict] = []
    seen_urls: set[str]   = set()

    # Pre-compute every <img alt="..."> position so we can pair each download
    # anchor with the nearest preceding alt (the publication card title).
    alt_positions = [(m.start(), html.unescape(m.group(1)).strip())
                     for m in _IMG_ALT_RE.finditer(text)]

    # 1. IRIS-hosted publications — REAL PDF urls live in <a href=...>
    #    (the matching <img src=...> in the same card is a JPEG thumbnail).
    for m in _DOWNLOAD_ANCHOR_RE.finditer(text):
        pdf_url = html.unescape(m.group(1))
        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)
        # Find the nearest <img alt="..."> that appears before this anchor —
        # that's the publication-card title.
        title = ""
        for pos, alt in reversed(alt_positions):
            if pos < m.start():
                title = alt
                break
        papers.append({
            "pmcid": None, "pmid": None, "doi": None,
            "title":             title,
            "abstract":          "",
            "authors":           [],
            "journal":           "WHO IPCS",
            "year":              "",
            "epmc_url":          url,         # source = topic page itself
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    # 2. Direct cdn.who.int PDFs (background documents)
    for m in _CDN_PDF_RE.finditer(text):
        pdf_url = html.unescape(m.group(1))
        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)
        # Try to find a title in the preceding <h3> block
        pre = text[max(0, m.start() - 1500): m.start()]
        title_matches = _NEARBY_TITLE_RE.findall(pre)
        title = (_TAG_STRIP.sub("", title_matches[-1]).strip()
                 if title_matches else
                 Path(pdf_url.split("?")[0]).stem.replace("-", " "))
        papers.append({
            "pmcid": None, "pmid": None, "doi": None,
            "title":             title,
            "abstract":          "",
            "authors":           [],
            "journal":           "WHO (cdn)",
            "year":              "",
            "epmc_url":          url,
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        })

    # 3. "WHO evaluation and risk assessment documents" arrowed links —
    #    IARC monograph, EHC, PIM, ICSC.  Follow each landing page to find
    #    its real PDF (parallel — small pages, fast).
    arrowed = []
    for m in _ARROWED_LINK_RE.finditer(text):
        link  = html.unescape(m.group(1))
        title = _TAG_STRIP.sub("", m.group(2)).strip()
        if title and link:
            arrowed.append((title, link))

    if arrowed:
        # Each arrowed link → list of (pdf_url, sub_title) pairs.
        # IARC monograph pages typically yield ~18 PDFs (volume + chapters).
        all_pdfs = await asyncio.gather(*[
            _resolve_landing_to_pdfs(client, link) for _, link in arrowed
        ])
        for (parent_title, link), pdfs in zip(arrowed, all_pdfs):
            label = ("IARC"     if "iarc"   in link.lower() else
                     "WHO IPCS" if "inchem" in link.lower() else
                     "ILO ICSC" if "ilo"    in link.lower() else
                     "WHO")
            for pdf_url, sub_title in pdfs:
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                # When an arrowed link expands to many sub-PDFs (chapters),
                # combine the parent doc title with the chapter label.
                title = (f"{parent_title} — {sub_title}"
                         if len(pdfs) > 1 and sub_title
                         and sub_title.lower() not in parent_title.lower()
                         else parent_title)
                papers.append({
                    "pmcid": None, "pmid": None, "doi": None,
                    "title":             title,
                    "abstract":          "",
                    "authors":           [],
                    "journal":           label,
                    "year":              "",
                    "epmc_url":          link,
                    "pdf_urls":          [pdf_url],
                    "has_pdf_from_api":  True,
                    "api_pdf_url_count": 1,
                })

    return papers, url


async def _iris_fallback_papers(client: httpx.AsyncClient, query: str,
                                  max_results: int) -> list[dict]:
    """Basic IRIS search — used when the chemical has no curated topic page."""
    params = [
        ("query",  query),
        ("size",   min(max(max_results * 2, 30), 50)),
        ("f.has_content_in_original_bundle", "true,equals"),
        ("dsoType", "item"),
    ]
    try:
        r = await client.get(SEARCH_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return []
    objs = ((r.json().get("_embedded", {}) or {})
            .get("searchResult", {}).get("_embedded", {}).get("objects") or [])

    items = [(obj.get("_embedded") or {}).get("indexableObject") or {}
             for obj in objs]
    items = [it for it in items if it.get("type") == "item" and it.get("uuid")]

    async def _build(item: dict) -> dict | None:
        uuid = item.get("uuid") or ""
        md   = item.get("metadata") or {}
        pdf_url, _ = await _bitstream_url(client, uuid)
        if not pdf_url:
            return None
        handle = item.get("handle") or ""
        return {
            "pmcid": None, "pmid": None, "doi": None,
            "title":             _md(md, "dc.title"),
            "abstract":          _md(md, "dc.description.abstract"),
            "authors":           [v.get("value", "")
                                  for v in (md.get("dc.contributor.author") or [])
                                  if v.get("value")],
            "journal":           _md(md, "dc.relation.ispartofseries")
                                 or _md(md, "dc.type") or "WHO publication",
            "year":              _md(md, "dc.date.issued")[:4],
            "epmc_url":          (f"https://iris.who.int/handle/{handle}"
                                  if handle else pdf_url),
            "pdf_urls":          [pdf_url],
            "has_pdf_from_api":  True,
            "api_pdf_url_count": 1,
        }

    resolved = await asyncio.gather(*[_build(it) for it in items])
    return [p for p in resolved if p][:max_results]


async def search(query: str, max_results: int = 20) -> dict:
    """
    Primary: scrape WHO's chemical-safety topic page (curated PDFs in WHO order).
    Fallback: basic IRIS search if the chemical has no dedicated topic page.

    Both paths return the same standard pipeline shape.
    """
    t0 = time.monotonic()
    api_url = ""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS,
                                     follow_redirects=True) as c:
            # 1. Try the curated topic page (preferred — matches website ordering)
            topic_papers, topic_url = await _scrape_topic_page(c, query)
            api_url = topic_url
            papers = topic_papers[:max_results]

            # 2. If no topic page (404 or no PDFs), fall back to IRIS search
            if not papers:
                papers = await _iris_fallback_papers(c, query, max_results)
                if not api_url:
                    api_url = SEARCH_URL + f"?query={query}"
    except Exception as e:
        return {
            "query_sent": query, "api_url": api_url,
            "papers_returned": 0, "papers_with_pmcid": 0,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "status": "error", "error": f"{type(e).__name__}: {e}", "papers": [],
        }

    response_time_ms = int((time.monotonic() - t0) * 1000)
    return {
        "query_sent":        query,
        "api_url":           api_url,
        "papers_returned":   len(papers),
        "papers_with_pmcid": len(papers),
        "response_time_ms":  response_time_ms,
        "status":            "ok",
        "error":             None,
        "papers":            papers,
    }


async def download_pdf(pmcid: str, pmid: str, pdf_urls: list[str],
                       pdf_dir: Path) -> dict:
    """Download a single WHO IRIS bitstream PDF.  No fallbacks."""
    pdf_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_urls:
        return {"status": "no_url", "pdf_path": None, "pdf_source": None,
                "file_size_kb": None, "attempts": []}

    url = pdf_urls[0]
    attempt = {
        "url": url, "attempt_no": 1,
        "http_status": None, "content_type": None, "is_pdf": False,
        "success": False, "file_size_kb": None, "error": None,
        "attempted_at": datetime.utcnow(),
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers=PDF_HEADERS) as c:
            r = await c.get(url)
        ct = r.headers.get("content-type", "")
        is_pdf = "pdf" in ct.lower() or r.content[:4] == b"%PDF"

        attempt["http_status"]  = r.status_code
        attempt["content_type"] = ct[:128]
        attempt["is_pdf"]       = is_pdf

        if r.status_code == 200 and is_pdf:
            # bitstream UUID is the segment before /content
            uuid_part = url.rstrip("/").split("/")[-2] if "/content" in url else "doc"
            path = pdf_dir / f"who_{uuid_part[:32]}.pdf"
            path.write_bytes(r.content)
            size_kb = len(r.content) // 1024
            attempt["success"]      = True
            attempt["file_size_kb"] = size_kb
            return {"status": "success", "pdf_path": str(path), "pdf_source": url,
                    "file_size_kb": size_kb, "attempts": [attempt]}
    except Exception as e:
        attempt["error"] = f"{type(e).__name__}: {e}"

    return {"status": "failed", "pdf_path": None, "pdf_source": None,
            "file_size_kb": None, "attempts": [attempt]}
