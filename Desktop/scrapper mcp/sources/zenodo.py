"""
sources.zenodo — Zenodo open-research-data harvester.

Zenodo hosts preprints, datasets, and technical reports. Every record has a
direct JSON endpoint and typically exposes a downloadable PDF in files[].
"""
from __future__ import annotations
import json
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef


API = "https://zenodo.org/api/records"


class ZenodoSource(BaseDatabaseSource):
    name = "Zenodo"
    search_limit = 5

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        q = f'"{chemical}" AND (toxicity OR exposure OR carcinogenicity OR risk)'
        url = f"{API}?q={quote(q)}&size=25&sort=mostrecent"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return []
        try:
            j = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return []
        hits = (j.get("hits") or {}).get("hits") or []
        out: list[PaperRef] = []
        for rec in hits[:25]:
            md = rec.get("metadata") or {}
            # Prefer openly-licensed PDF in files[]
            pdf_url = None
            for f in (rec.get("files") or []):
                key = (f.get("key") or "").lower()
                if key.endswith(".pdf"):
                    pdf_url = (f.get("links") or {}).get("self")
                    break
            authors = ", ".join(
                a.get("name", "") for a in (md.get("creators") or [])[:4]
            )
            if len(md.get("creators") or []) > 4:
                authors += " et al."
            pub_date = (md.get("publication_date") or "")[:4]
            out.append(PaperRef(
                source=self.name,
                title=(md.get("title") or "").strip(),
                authors=authors,
                year=pub_date,
                doi=md.get("doi"),
                landing_url=rec.get("links", {}).get("html") or rec.get("links", {}).get("self_html"),
                pdf_url=pdf_url,
                abstract=(md.get("description") or "").strip()[:2000],
                extra={
                    "zenodo_id": rec.get("id"),
                    "resource_type": (md.get("resource_type") or {}).get("type"),
                },
            ))
        out.sort(key=lambda r: 0 if r.pdf_url else 1)
        return out
