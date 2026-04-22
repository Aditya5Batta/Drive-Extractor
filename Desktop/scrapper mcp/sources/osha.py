"""
sources.osha — US OSHA harvester.

OSHA publishes:
  * Substance-specific standards (29 CFR 1910.NNNN)
  * PEL / Annotated PEL tables
  * Chemical Sampling Information sheets (chemicaldata/<slug>)
  * Standards Interpretation letters

Search endpoint (stable April 2025):
  https://www.osha.gov/laws-regs/standardinterpretations/search?search_api_fulltext=<chemical>
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SI_SEARCH = "https://www.osha.gov/laws-regs/standardinterpretations/search"
CHEMDATA_BASE = "https://www.osha.gov/chemicaldata"


class OSHASource(BaseDatabaseSource):
    name = "OSHA"
    search_limit = 4

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []

        # 1. Standards Interpretation letters (the best-indexed OSHA content)
        url = f"{SI_SEARCH}?search_api_fulltext={quote(chemical)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            # OSHA search results have <article class="search-result"> tiles
            tile = re.compile(
                r'<a[^>]+href=["\'](/laws-regs/standardinterpretations/[^"\']+)["\'][^>]*>([^<]{5,300})</a>',
                re.IGNORECASE,
            )
            seen: set[str] = set()
            for m in tile.finditer(html):
                href, text = m.group(1), m.group(2).strip()
                if href in seen: continue
                seen.add(href)
                refs.append(PaperRef(
                    source=self.name,
                    title=text,
                    landing_url="https://www.osha.gov" + href,
                    extra={"osha_doc_type": "Standards Interpretation"},
                ))
                if len(refs) >= 6:
                    break

        # 2. Chemical Sampling Info — try the slug form
        slug = re.sub(r"[^a-z0-9]+", "", chemical.lower())
        cd_url = f"{CHEMDATA_BASE}/{slug}"
        d2, _c2, s2, _e2 = await self.fetcher.fetch(cd_url)
        if d2 and s2 < 400:
            refs.append(PaperRef(
                source=self.name,
                title=f"OSHA Chemical Sampling Information — {chemical}",
                landing_url=cd_url,
                extra={"osha_doc_type": "Chemical Sampling Info"},
            ))

        # 3. PEL / Annotated PEL tables — fetch and only emit a ref when
        # the chemical is verified to appear in the table. Avoids the prior
        # dishonest "(contains X if listed)" unconditional ref.
        pel_url = "https://www.osha.gov/annotated-pels"
        pd, _pc, pst, _pe = await self.fetcher.fetch(pel_url)
        if pd is not None and pst < 400:
            try:
                pel_html = pd.decode("utf-8", errors="replace").lower()
            except Exception:
                pel_html = ""
            name_l = chemical.lower().strip()
            cas_l = (cas or "").strip().lower()
            if (name_l and name_l in pel_html) or (cas_l and cas_l in pel_html):
                refs.append(PaperRef(
                    source=self.name,
                    title=f"OSHA Annotated PELs — {chemical} row present",
                    landing_url=pel_url,
                    extra={"osha_doc_type": "Annotated PELs (verified)"},
                ))

        return refs
