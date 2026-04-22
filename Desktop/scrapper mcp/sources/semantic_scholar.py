"""
sources.semantic_scholar — Semantic Scholar harvester.

Uses the public Graph API (no auth needed for modest query rates). Returns
papers with open-access PDF URLs when Semantic Scholar has indexed one, and
falls back to CrossRef DOI / PMID resolution for closed-access hits.
"""
from __future__ import annotations
import json
from typing import Any
from urllib.parse import quote

from sources._base import BaseDatabaseSource, PaperRef


API = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = (
    "paperId,title,abstract,year,authors.name,externalIds,"
    "openAccessPdf,url,venue,journal,citationCount"
)


class SemanticScholarSource(BaseDatabaseSource):
    name = "Semantic Scholar"
    search_limit = 6

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        # Build a keyword-augmented query so we catch tox papers specifically
        q = f'"{chemical}" AND (toxicity OR carcinogenicity OR exposure)'
        url = f"{API}?query={quote(q)}&limit=25&fields={quote(FIELDS)}"
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return []
        try:
            j = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return []
        out: list[PaperRef] = []
        for p in (j.get("data") or [])[:25]:
            ext = p.get("externalIds") or {}
            oapdf = (p.get("openAccessPdf") or {}).get("url") if p.get("openAccessPdf") else None
            authors = ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:4])
            if len((p.get("authors") or [])) > 4:
                authors += " et al."
            journal_obj = p.get("journal") or {}
            out.append(PaperRef(
                source=self.name,
                title=(p.get("title") or "").strip(),
                authors=authors,
                year=str(p.get("year") or ""),
                doi=ext.get("DOI"),
                pmid=ext.get("PubMed"),
                pmcid=ext.get("PubMedCentral"),
                landing_url=p.get("url"),
                pdf_url=oapdf,
                abstract=(p.get("abstract") or "").strip(),
                extra={
                    "venue": p.get("venue") or journal_obj.get("name") or "",
                    "citation_count": p.get("citationCount", 0),
                    "s2_paper_id": p.get("paperId"),
                },
            ))
        # Rank: open-access PDFs first, then high-citation
        out.sort(key=lambda r: (
            0 if r.pdf_url else 1,
            -int(r.extra.get("citation_count") or 0),
        ))
        return out
