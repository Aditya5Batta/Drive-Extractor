"""
sources.ntp — NTP (US National Toxicology Program) harvester.

NTP publishes Report on Carcinogens (RoC), Long-Term Studies (TR/LT-RPT),
and Monographs — all as PDFs with predictable URL patterns. The search SPA
at ntpsearch.niehs.nih.gov is JS-rendered, so we discover documents two
ways:

1. Probe the RoC profile directory (substance-slug.pdf) — covers every
   chemical listed as known/reasonably-anticipated to be a human carcinogen.
2. Walk the publications landing page for the chemical slug.
3. Fall back to site-search scraping for any remaining leads.

Each hit comes back with a real pdf_url so full-text and figure extraction
"just works".
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


ROC_PROFILE_BASE = "https://ntp.niehs.nih.gov/ntp/roc/content/profiles"
PUBS_INDEX = "https://ntp.niehs.nih.gov/publications"
TR_BASE = "https://ntp.niehs.nih.gov/ntp/htdocs/lt_rpts"


def _slugs(chemical: str) -> list[str]:
    """Generate likely RoC-profile URL slugs for a chemical name."""
    base = chemical.lower().strip()
    out = [re.sub(r"[^a-z0-9]+", "", base)]               # benzene
    out.append(re.sub(r"[^a-z0-9]+", "-", base).strip("-"))  # 1-3-butadiene
    out.append(re.sub(r"\s+", "", base))                   # 13butadiene
    # De-duplicate
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


class NTPSource(BaseDatabaseSource):
    name = "NTP"
    search_limit = 6

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []
        seen: set[str] = set()

        # 1. RoC profile PDF (e.g. benzene.pdf)
        for slug in _slugs(chemical):
            url = f"{ROC_PROFILE_BASE}/{slug}.pdf"
            data, ct, status, _ = await self.fetcher.fetch(url)
            if data and status < 400 and (b"%PDF-" in data[:10] or "pdf" in ct):
                if url in seen: continue
                seen.add(url)
                refs.append(PaperRef(
                    source=self.name,
                    title=f"NTP Report on Carcinogens — {chemical} profile",
                    year="",
                    landing_url=url,
                    pdf_url=url,
                    extra={"ntp_doc_type": "RoC profile"},
                ))
                break  # one hit per chemical

        # 2. Walk the publications site search for this chemical
        pubs_url = f"{PUBS_INDEX}?search_api_fulltext={quote(chemical)}"
        data, ct, status, _ = await self.fetcher.fetch(pubs_url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            for pdf in _collect_pdf_links(html, PUBS_INDEX):
                if pdf in seen or "ntp.niehs.nih.gov" not in pdf:
                    continue
                seen.add(pdf)
                # Extract a nearby title (crude but non-hallucinating)
                m = re.search(
                    r'href=["\'][^"\']*?' + re.escape(pdf.split("/")[-1]) + r'["\'][^>]*>([^<]{4,200})',
                    html)
                title = (m.group(1).strip() if m else pdf.rsplit("/", 1)[-1])
                refs.append(PaperRef(
                    source=self.name,
                    title=title,
                    landing_url=pdf,
                    pdf_url=pdf,
                    extra={"ntp_doc_type": "publication"},
                ))
                if len(refs) >= 10:
                    break

        # 3. Technical Report (TR) lookup — if CAS is known we can sometimes
        # guess a TR number directory, but without that we rely on the search.
        return refs
