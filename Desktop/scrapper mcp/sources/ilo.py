"""
sources.ilo — ILO International Chemical Safety Cards (ICSC) harvester.

ICSC cards are 1-2 page HTML / PDF summaries per chemical covering
hazards, PPE, first aid, storage, and emergency response. The cards are
CAS-indexed.

Card listing (by CAS or name):
  https://www.ilo.org/dyn/icsc/showcard.listCards3?p_lang=en&p_search_text=<q>
  (returns showcard.display?p_card_id=<id> links)

Card display (HTML):
  https://www.ilo.org/dyn/icsc/showcard.display?p_lang=en&p_card_id=<id>

PDF version (predictable slug):
  https://www.ilo.org/dyn/icsc/showcard.<fmt>?p_lang=en&p_card_id=<id>&p_version=2
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef


LIST_URL = "https://www.ilo.org/dyn/icsc/showcard.listCards3"
DISPLAY_URL = "https://www.ilo.org/dyn/icsc/showcard.display"


class ILOSource(BaseDatabaseSource):
    name = "ILO ICSC"
    search_limit = 3

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        q = cas or chemical
        url = f"{LIST_URL}?p_lang=en&p_search_text={quote(q)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        refs: list[PaperRef] = []
        if data is None or status >= 400:
            return refs
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return refs

        # Link pattern (URL-encoded &amp; sometimes)
        patt = re.compile(
            r'showcard\.display\?p_lang=en(?:&amp;|&)p_card_id=(\d+)',
            re.IGNORECASE,
        )
        seen_ids: set[str] = set()
        for m in patt.finditer(html):
            cid = m.group(1)
            if cid in seen_ids: continue
            seen_ids.add(cid)
            card_url = f"{DISPLAY_URL}?p_lang=en&p_card_id={cid}"
            # Try to pull the card title from surrounding markup
            near = html[max(0, m.start() - 200): m.end() + 200]
            title_match = re.search(
                r'<a[^>]+p_card_id=' + cid + r'[^>]*>\s*([^<]{3,200})</a>',
                near, re.IGNORECASE,
            )
            title = (title_match.group(1).strip() if title_match else f"ICSC card {cid}")
            refs.append(PaperRef(
                source=self.name,
                title=f"ILO ICSC {cid} — {title}",
                landing_url=card_url,
                extra={"icsc_id": cid, "ilo_doc_type": "ICSC card"},
            ))
            if len(refs) >= 5:
                break

        return refs
