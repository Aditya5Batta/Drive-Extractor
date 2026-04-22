"""
sources.echa — ECHA (European Chemicals Agency) harvester.

ECHA publishes REACH registration dossiers, CLP harmonized-classification
views, and restriction/authorisation lists for every substance registered
in the EU. The substance landing URL follows a predictable pattern once
we resolve CAS → EC number.

We search in two layers:

1. CAS-indexed substance lookup via the public CHEM Search API:
     https://echa.europa.eu/search-for-chemicals?text=<CAS>&...
   and then scrape the resulting substance info page for document links.

2. Full-site search landing URL as a fallback (useful when the caller
   doesn't have a CAS number yet).
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote, urljoin

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


SEARCH = "https://echa.europa.eu/search"
SUB_INFO_BASE = "https://echa.europa.eu/substance-information/-/substanceinfo"


class EChaSource(BaseDatabaseSource):
    name = "ECHA"
    search_limit = 5

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        q = cas or chemical
        url = f"{SEARCH}?text={quote(q)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        refs: list[PaperRef] = []
        if data is None or status >= 400:
            return refs
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return refs

        # 1. Direct substance-info IDs in the search results
        for m in re.finditer(r'/substanceinfo/(\d+\.\d+\.\d+)', html):
            sid = m.group(1)
            landing = f"{SUB_INFO_BASE}/{sid}"
            if any(r.landing_url == landing for r in refs):
                continue
            refs.append(PaperRef(
                source=self.name,
                title=f"ECHA Substance Information {sid} ({chemical})",
                landing_url=landing,
                extra={"echa_ec_dotted": sid, "echa_doc_type": "Substance Info"},
            ))
            if len(refs) >= 5:
                break

        # 2. Registration dossier links
        for m in re.finditer(r'/registered-dossier/(\d+)', html):
            did = m.group(1)
            url2 = f"https://echa.europa.eu/registration-dossier/-/registered-dossier/{did}"
            if any(r.landing_url == url2 for r in refs):
                continue
            refs.append(PaperRef(
                source=self.name,
                title=f"ECHA REACH Registration Dossier {did} ({chemical})",
                landing_url=url2,
                extra={"echa_doc_type": "REACH Dossier", "dossier_id": did},
            ))
            if len(refs) >= 10:
                break

        # 3. Direct PDF links (CLH reports, RAC opinions, Annex XV dossiers)
        for pdf in _collect_pdf_links(html, "https://echa.europa.eu"):
            if any(r.pdf_url == pdf for r in refs):
                continue
            refs.append(PaperRef(
                source=self.name,
                title=f"ECHA document — {pdf.rsplit('/', 1)[-1]} ({chemical})",
                landing_url=pdf,
                pdf_url=pdf,
                extra={"echa_doc_type": "PDF"},
            ))
            if len(refs) >= 12:
                break

        return refs
