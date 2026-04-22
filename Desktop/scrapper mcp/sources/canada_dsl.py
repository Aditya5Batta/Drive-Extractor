"""
sources.canada_dsl — Canada CEPA / Domestic Substances List harvester.

Environment & Climate Change Canada and Health Canada publish CEPA
risk assessments at canada.ca/en/health-canada/services/chemical-substances/
and canada.ca/en/environment-climate-change/services/management-toxic-
substances/ — each as one or more downloadable PDFs.

Canada.ca runs an Akamai bot-challenge on some paths; we bypass it by
hitting the stable publications search and substance group landing pages.
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SR_SEARCH = "https://www.canada.ca/en/sr/srb.html"
HC_CHEM = "https://www.canada.ca/en/health-canada/services/chemical-substances/challenge/batch-6.html"
ECC_CEPA = "https://www.canada.ca/en/environment-climate-change/services/management-toxic-substances/list-canadian-environmental-protection-act.html"


class CanadaDSLSource(BaseDatabaseSource):
    name = "Canada CEPA/DSL"
    search_limit = 4

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []
        q = cas or chemical

        # 1. Site-search JSON endpoint
        url = (f"{SR_SEARCH}?q={quote(q)}&cdn=canada&st=s&num=25&langs=en")
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            # Pull result hrefs restricted to canada.ca toxic-substances pages
            patt = re.compile(
                r'<a[^>]+href=["\'](https?://[^"\']*canada\.ca/[^"\']+)["\'][^>]*>([^<]{5,200})</a>',
                re.IGNORECASE,
            )
            seen: set[str] = set()
            for m in patt.finditer(html):
                href, text = m.group(1), m.group(2).strip()
                if href in seen: continue
                if not any(k in href for k in (
                    "chemical-substances", "environment-climate-change",
                    "health-canada", "toxic-substances",
                )):
                    continue
                seen.add(href)
                refs.append(PaperRef(
                    source=self.name,
                    title=text,
                    landing_url=href,
                    pdf_url=href if href.lower().endswith(".pdf") else None,
                    extra={"canada_doc_type": "CEPA/HC publication"},
                ))
                if len(refs) >= 8:
                    break

        # 2. CEPA Schedule 1 master list — fetch it and only emit a ref if
        # the chemical actually appears. Previously we emitted an unconditional
        # ref that implied inclusion without ever fetching the list.
        cepa_bytes, _ct, cepa_status, _err2 = await self.fetcher.fetch(ECC_CEPA)
        if cepa_bytes is not None and cepa_status < 400:
            try:
                cepa_html = cepa_bytes.decode("utf-8", errors="replace").lower()
            except Exception:
                cepa_html = ""
            name_l = chemical.lower().strip()
            cas_l = (cas or "").strip().lower()
            if (name_l and name_l in cepa_html) or (cas_l and cas_l in cepa_html):
                refs.append(PaperRef(
                    source=self.name,
                    title=f"CEPA Schedule 1 — {chemical} listed",
                    landing_url=ECC_CEPA,
                    extra={"canada_doc_type": "Schedule 1 (verified)"},
                ))

        return refs
