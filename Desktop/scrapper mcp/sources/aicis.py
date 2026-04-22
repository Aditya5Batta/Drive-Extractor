"""
sources.aicis — Australia AICIS (Industrial Chemicals Introduction Scheme).

Formerly NICNAS. AICIS publishes IMAPs (Inventory Multi-tiered Assessment
and Prioritisation) and evaluation statements for every registered
industrial chemical, at industrialchemicals.gov.au.

Search:
  https://www.industrialchemicals.gov.au/search/node/<chemical>

Direct assessment pages live under:
  /chemicals/<slug>
  /business/search-assessments
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SEARCH = "https://www.industrialchemicals.gov.au/search/node"
CHEM_BASE = "https://www.industrialchemicals.gov.au/chemicals"


class AICISSource(BaseDatabaseSource):
    name = "Australia AICIS"
    search_limit = 4

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []
        q = cas or chemical

        # 1. Drupal site search
        url = f"{SEARCH}/{quote(q)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            # Find /chemicals/<slug> and /business/* result links
            patt = re.compile(
                r'<a[^>]+href=["\'](/(?:chemicals|business)/[^"\']+)["\'][^>]*>([^<]{5,300})</a>',
                re.IGNORECASE,
            )
            seen: set[str] = set()
            for m in patt.finditer(html):
                href, text = m.group(1), m.group(2).strip()
                if href in seen: continue
                seen.add(href)
                refs.append(PaperRef(
                    source=self.name,
                    title=text,
                    landing_url="https://www.industrialchemicals.gov.au" + href,
                    extra={"aicis_doc_type": "Assessment or Chemical page"},
                ))
                if len(refs) >= 6:
                    break

            # Pull any PDF links directly off the search results page
            for pdf in _collect_pdf_links(html, "https://www.industrialchemicals.gov.au"):
                if any(r.pdf_url == pdf for r in refs):
                    continue
                refs.append(PaperRef(
                    source=self.name,
                    title=f"AICIS — {pdf.rsplit('/', 1)[-1]} ({chemical})",
                    landing_url=pdf,
                    pdf_url=pdf,
                    extra={"aicis_doc_type": "PDF"},
                ))
                if len(refs) >= 10:
                    break

        # 2. Direct chemical page by slug
        slug = chemical.lower().replace(" ", "-")
        chem_page = f"{CHEM_BASE}/{slug}"
        d2, _c2, s2, _e2 = await self.fetcher.fetch(chem_page)
        if d2 and s2 < 400 and not any(r.landing_url == chem_page for r in refs):
            refs.append(PaperRef(
                source=self.name,
                title=f"AICIS Chemical Page — {chemical}",
                landing_url=chem_page,
                extra={"aicis_doc_type": "Chemical page"},
            ))

        return refs
