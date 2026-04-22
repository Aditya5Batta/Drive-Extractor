"""
sources.atsdr — ATSDR (CDC) Toxicological Profile harvester.

ATSDR publishes one "Tox Profile" PDF per chemical; the profile is
authoritative and book-length (150-500 pages, with detailed PK, human
health, and regulatory sections). URL pattern:

    https://www.atsdr.cdc.gov/toxprofiles/tp<NN>.pdf

The mapping from chemical → tp number is well known for ~200 common
substances. We discover it dynamically by scraping the alphabetical index.
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef


INDEX_URL = "https://www.atsdr.cdc.gov/toxprofiles/index.asp"
PROFILE_BASE = "https://www.atsdr.cdc.gov/toxprofiles"
SEARCH_URL = "https://search.cdc.gov/search/"
TP_PATTERN = re.compile(r"/toxprofiles/(tp\d+)\.pdf", re.IGNORECASE)


class ATSDRSource(BaseDatabaseSource):
    name = "ATSDR"
    search_limit = 3   # tp PDFs are huge — cap harvest count

    async def _find_tp_number(self, chemical: str) -> str | None:
        """Scrape the ATSDR index to map chemical → tp number."""
        data, _ct, status, _err = await self.fetcher.fetch(INDEX_URL)
        if data is None or status >= 400:
            return None
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        # The index has rows like:
        #   <a href="/toxprofiles/tp3.pdf">Benzene</a>
        # Be tolerant about surrounding markup.
        patt = re.compile(
            r'<a\s+href=["\']([^"\']+?tp\d+\.pdf)["\'][^>]*>\s*([^<]+?)\s*</a>',
            re.IGNORECASE,
        )
        name_l = chemical.lower().strip()
        for m in patt.finditer(html):
            href, text = m.group(1), m.group(2).strip().lower()
            if name_l == text or name_l in text or text in name_l:
                if href.startswith("/"):
                    return "https://www.atsdr.cdc.gov" + href
                return href
        return None

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []

        # 1. The Tox Profile PDF — the main prize
        tp_url = await self._find_tp_number(chemical)
        if tp_url:
            refs.append(PaperRef(
                source=self.name,
                title=f"ATSDR Toxicological Profile for {chemical}",
                landing_url=tp_url,
                pdf_url=tp_url,
                extra={"atsdr_doc_type": "Toxicological Profile"},
            ))

        # 2. MRL (Minimal Risk Levels) — fetch the master table and ONLY emit
        # a ref when the chemical name or CAS actually appears in the row.
        # Previously we emitted an unconditional ref with the caveat
        # "(contains X if applicable)" which is dishonest: the harvester
        # would claim a reference even when the page was never fetched.
        mrl_index = "https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx"
        mrl_bytes, _ct, mrl_status, _err = await self.fetcher.fetch(mrl_index)
        if mrl_bytes is not None and mrl_status < 400:
            try:
                mrl_html = mrl_bytes.decode("utf-8", errors="replace").lower()
            except Exception:
                mrl_html = ""
            name_l = chemical.lower().strip()
            cas_l = (cas or "").strip().lower()
            if (name_l and name_l in mrl_html) or (cas_l and cas_l in mrl_html):
                refs.append(PaperRef(
                    source=self.name,
                    title=f"ATSDR MRL Listing — {chemical} row present",
                    landing_url=mrl_index,
                    extra={"atsdr_doc_type": "MRL Listing (verified)"},
                ))

        # 3. CDC site-search for additional ATSDR documents
        search_url = (f"{SEARCH_URL}?query={quote(chemical)}&siteLimit=atsdr.cdc.gov")
        data, _ct, status, _err = await self.fetcher.fetch(search_url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            # Pick out direct tp PDFs we haven't already seen
            seen = {r.pdf_url for r in refs if r.pdf_url}
            for m in TP_PATTERN.finditer(html):
                url = "https://www.atsdr.cdc.gov" + m.group(0)
                if url in seen:
                    continue
                seen.add(url)
                refs.append(PaperRef(
                    source=self.name,
                    title=f"ATSDR {m.group(1).upper()} — {chemical} related",
                    landing_url=url,
                    pdf_url=url,
                    extra={"atsdr_doc_type": "Tox Profile (related)"},
                ))
                if len(refs) >= 5:
                    break

        return refs
