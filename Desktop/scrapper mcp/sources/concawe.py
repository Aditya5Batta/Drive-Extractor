"""
sources.concawe — CONCAWE (petroleum industry science consortium).

CONCAWE publishes REACH consortium reports, workplace exposure reviews,
and biomonitoring studies for refinery-related chemicals — heavy on
benzene, toluene, PAHs, naphthalene, MTBE, etc.

Site layout:
  concawe.eu/publications          → master landing (paginated)
  concawe.eu/?s=<chemical>         → WordPress full-site search
  Every downloadable report lives under /wp-content/uploads/<YYYY>/<MM>/

We search via the built-in WordPress /?s= endpoint and walk resulting
publication tiles + direct PDF links.
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SEARCH = "https://www.concawe.eu/"
PUBS = "https://www.concawe.eu/publications/"


class ConcaweSource(BaseDatabaseSource):
    name = "CONCAWE"
    search_limit = 4

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        url = f"{SEARCH}?s={quote(chemical)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        refs: list[PaperRef] = []
        if data is None or status >= 400:
            return refs
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return refs

        # 1. Publication tiles: <a href="https://www.concawe.eu/publication/...">Title</a>
        tile_patt = re.compile(
            r'<a[^>]+href=["\'](https?://(?:www\.)?concawe\.eu/publication/[^"\']+)["\'][^>]*>([^<]{5,300})</a>',
            re.IGNORECASE,
        )
        seen: set[str] = set()
        for m in tile_patt.finditer(html):
            href, text = m.group(1), m.group(2).strip()
            if href in seen: continue
            seen.add(href)
            refs.append(PaperRef(
                source=self.name,
                title=text,
                landing_url=href,
                extra={"concawe_doc_type": "Publication landing"},
            ))

        # 2. Direct PDFs (usually /wp-content/uploads/YYYY/MM/<report>.pdf)
        for pdf in _collect_pdf_links(html, "https://www.concawe.eu"):
            if any(r.pdf_url == pdf for r in refs):
                continue
            refs.append(PaperRef(
                source=self.name,
                title=f"CONCAWE — {pdf.rsplit('/', 1)[-1]} ({chemical})",
                landing_url=pdf,
                pdf_url=pdf,
                extra={"concawe_doc_type": "PDF report"},
            ))

        # If landing tiles came back without a PDF URL, try to enrich by
        # fetching the landing and looking for the canonical "Download"
        # PDF link (only do the top 3 to keep runtime bounded).
        for r in refs[:3]:
            if r.pdf_url:
                continue
            d2, _c2, s2, _e2 = await self.fetcher.fetch(r.landing_url or "")
            if not d2 or s2 >= 400:
                continue
            try:
                html2 = d2.decode("utf-8", errors="replace")
            except Exception:
                continue
            pdfs = _collect_pdf_links(html2, r.landing_url or "")
            if pdfs:
                r.pdf_url = pdfs[0]

        return refs
