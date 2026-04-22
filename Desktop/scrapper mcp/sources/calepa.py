"""
sources.calepa — California EPA / OEHHA harvester.

OEHHA publishes:
  * Proposition 65 chemical listings (with the underlying toxicity basis)
  * Reference Exposure Levels (RELs) — acute, chronic, 8-hour
  * Public Health Goals (PHGs) for drinking water
  * Notification levels / public health assessments

Every assessment is a PDF on oehha.ca.gov. The search at
oehha.ca.gov/about/search-results?q=<chemical> returns a clean Drupal
results page we can parse reliably.
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SEARCH = "https://oehha.ca.gov/about/search-results"
PROP65 = "https://oehha.ca.gov/proposition-65/chemicals"


class CalEPASource(BaseDatabaseSource):
    name = "CalEPA OEHHA"
    search_limit = 5

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        url = f"{SEARCH}?q={quote(chemical)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        refs: list[PaperRef] = []
        if data is None or status >= 400:
            return refs
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return refs

        # 1. Direct PDF links in the search results — REL, PHG, TSD, etc.
        for pdf in _collect_pdf_links(html, "https://oehha.ca.gov"):
            if any(r.pdf_url == pdf for r in refs):
                continue
            # Derive a title from the filename
            fname = pdf.rsplit("/", 1)[-1]
            refs.append(PaperRef(
                source=self.name,
                title=f"OEHHA {fname} ({chemical})",
                landing_url=pdf,
                pdf_url=pdf,
                extra={"oehha_doc_type": "PDF", "filename": fname},
            ))
            if len(refs) >= 8:
                break

        # 2. Proposition 65 listing page for the chemical, if one exists
        p65 = f"{PROP65}/{chemical.lower().replace(' ', '-')}"
        d2, _c2, s2, _e2 = await self.fetcher.fetch(p65)
        if d2 and s2 < 400:
            refs.append(PaperRef(
                source=self.name,
                title=f"OEHHA Proposition 65 listing — {chemical}",
                landing_url=p65,
                extra={"oehha_doc_type": "Prop 65 Listing"},
            ))

        # 3. Chemical page (facts + regulatory summary)
        chem_page = f"https://oehha.ca.gov/chemicals/{chemical.lower().replace(' ', '-')}"
        d3, _c3, s3, _e3 = await self.fetcher.fetch(chem_page)
        if d3 and s3 < 400:
            refs.append(PaperRef(
                source=self.name,
                title=f"OEHHA Chemical Profile — {chemical}",
                landing_url=chem_page,
                extra={"oehha_doc_type": "Chemical Profile"},
            ))

        return refs
