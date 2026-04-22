"""
sources.niosh — US NIOSH harvester.

NIOSH publishes per-chemical hazard cards, IDLH documents, criteria
documents, and skin notations — all as PDFs or static HTML pages with
predictable URL patterns:

  Pocket Guide : cdc.gov/niosh/npg/npgd<NNNN>.html
  IDLH         : cdc.gov/niosh/idlh/<CAS-no-dashes>.html   (+ companion PDF)
  Criteria Doc : cdc.gov/niosh/docs/<NN-NNN>/
  Skin Notation: cdc.gov/niosh/docs/<NN-NNN>/pdfs/<slug>-snproposed-<date>.pdf

We also run the CDC site-search restricted to NIOSH to grab anything else
the chemical is tagged in.
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef, _collect_pdf_links


NPG_INDEX = "https://www.cdc.gov/niosh/npg/"
IDLH_INDEX = "https://www.cdc.gov/niosh/idlh/"
SEARCH = "https://search.cdc.gov/search/"


class NIOSHSource(BaseDatabaseSource):
    name = "NIOSH"
    search_limit = 5

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        refs: list[PaperRef] = []

        # 1. IDLH per-chemical page (if CAS is known)
        if cas:
            cas_nd = re.sub(r"[^0-9]", "", cas)
            url = f"{IDLH_INDEX}{cas_nd}.html"
            data, _ct, status, _err = await self.fetcher.fetch(url)
            if data and status < 400 and (b"IDLH" in data[:20000] or b"Immediately Dangerous" in data[:20000]):
                refs.append(PaperRef(
                    source=self.name,
                    title=f"NIOSH IDLH Documentation — {chemical} (CAS {cas})",
                    landing_url=url,
                    extra={"niosh_doc_type": "IDLH"},
                ))
                # Companion PDF often lives at /niosh/idlh/<cas>.pdf
                pdf_url = f"{IDLH_INDEX}{cas_nd}.pdf"
                d2, _c2, s2, _e2 = await self.fetcher.fetch(pdf_url)
                if d2 and s2 < 400 and (b"%PDF-" in d2[:5]):
                    refs[-1].pdf_url = pdf_url

        # 2. Pocket Guide lookup via alphabetical index for first letter
        first = (chemical.strip() or "a")[0].lower()
        if first.isalpha():
            idx_url = f"{NPG_INDEX}npgsyn-{first}.html"
            data, _ct, status, _err = await self.fetcher.fetch(idx_url)
            if data and status < 400:
                try:
                    html = data.decode("utf-8", errors="replace")
                except Exception:
                    html = ""
                # Links look like <a href="npgd0049.html">Benzene</a>
                patt = re.compile(
                    r'<a\s+href=["\'](npgd\d+\.html)["\'][^>]*>\s*([^<]+?)\s*</a>',
                    re.IGNORECASE,
                )
                name_l = chemical.lower().strip()
                for m in patt.finditer(html):
                    href, text = m.group(1), m.group(2).strip().lower()
                    if name_l == text or name_l in text or text in name_l:
                        page_url = NPG_INDEX + href
                        refs.append(PaperRef(
                            source=self.name,
                            title=f"NIOSH Pocket Guide — {chemical}",
                            landing_url=page_url,
                            extra={"niosh_doc_type": "Pocket Guide"},
                        ))
                        break

        # 3. CDC site-search for other NIOSH publications mentioning the chemical
        search_url = (f"{SEARCH}?query={quote(chemical)}&siteLimit=NIOSH")
        data, _ct, status, _err = await self.fetcher.fetch(search_url)
        if data and status < 400:
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = ""
            for pdf in _collect_pdf_links(html, "https://www.cdc.gov"):
                if any(r.pdf_url == pdf for r in refs) or "niosh" not in pdf.lower():
                    continue
                refs.append(PaperRef(
                    source=self.name,
                    title=f"NIOSH — {pdf.rsplit('/', 1)[-1]} ({chemical})",
                    landing_url=pdf,
                    pdf_url=pdf,
                    extra={"niosh_doc_type": "PDF"},
                ))
                if len(refs) >= 8:
                    break

        return refs
