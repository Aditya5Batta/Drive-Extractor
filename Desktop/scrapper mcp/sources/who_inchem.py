"""
sources.who_inchem — WHO / IPCS INCHEM harvester.

INCHEM hosts the WHO Environmental Health Criteria (EHC) and Concise
International Chemical Assessment Documents (CICAD) — all in predictable
HTML directories under inchem.org/documents/. We don't rely on the broken
keyword-search page; instead we walk the collection indexes and match
chemical names against the file-link text.

Collections:
  EHC   — /documents/ehc/ehc/  (behaves like ehc001 .. ehc240+)
  CICAD — /documents/cicads/cicads/
  HSG   — /documents/hsg/hsg/
  PIM   — /documents/pims/pims/
  ICSC  — /documents/icsc/icsc/
"""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef


COLLECTIONS: list[tuple[str, str]] = [
    ("EHC",   "https://www.inchem.org/pages/ehc.html"),
    ("CICAD", "https://www.inchem.org/pages/cicads.html"),
    ("HSG",   "https://www.inchem.org/pages/hsg.html"),
    ("PIM",   "https://www.inchem.org/pages/pims.html"),
    ("ICSC",  "https://www.inchem.org/pages/icsc.html"),
]


class WHOInchemSource(BaseDatabaseSource):
    name = "WHO INCHEM"
    search_limit = 4

    async def _walk_collection(
        self, label: str, index_url: str, chemical: str,
    ) -> list[PaperRef]:
        data, _ct, status, _err = await self.fetcher.fetch(index_url)
        if data is None or status >= 400:
            return []
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            return []
        refs: list[PaperRef] = []
        name_l = chemical.lower().strip()
        # Links look like <a href="../documents/ehc/ehc/ehc150.htm">Benzene</a>
        patt = re.compile(
            r'<a\s+href=["\']([^"\']+?/(?:ehc|cicad|hsg|pim|pims|icsc)[^"\']+?)["\'][^>]*>\s*([^<]{3,200}?)\s*</a>',
            re.IGNORECASE,
        )
        for m in patt.finditer(html):
            href, text = m.group(1), m.group(2).strip()
            tl = text.lower()
            if name_l in tl or any(tok in tl for tok in name_l.split()):
                if href.startswith("../"):
                    url = "https://www.inchem.org" + href.replace("../", "/")
                elif href.startswith("/"):
                    url = "https://www.inchem.org" + href
                elif not href.startswith("http"):
                    url = "https://www.inchem.org/" + href.lstrip("./")
                else:
                    url = href
                refs.append(PaperRef(
                    source=self.name,
                    title=f"WHO {label}: {text}",
                    landing_url=url,
                    extra={"inchem_collection": label},
                ))
                if len(refs) >= 5:
                    break
        return refs

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        out: list[PaperRef] = []
        for label, url in COLLECTIONS:
            try:
                out.extend(await self._walk_collection(label, url, chemical))
            except Exception:  # noqa: BLE001
                continue
        # De-duplicate by landing_url
        seen: set[str] = set()
        uniq: list[PaperRef] = []
        for r in out:
            key = r.landing_url or r.title
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        return uniq
