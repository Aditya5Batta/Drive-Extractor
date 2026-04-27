"""
sources.openalex — OpenAlex works API client.

Returns citation counts, PDF URLs, and related papers.
"""
from __future__ import annotations
import json
from typing import Any
from urllib.parse import quote

from core.http import HTTPFetcher
from sources._models import PaperCandidate


class OpenAlexClient:
    """Query OpenAlex Works API — https://api.openalex.org/works

    OpenAlex indexes ~240M works; unlike PubMed it includes preprints, reports,
    and many OA papers EuropePMC misses. No API key required (polite pool
    by providing mailto= is free).
    """

    BASE = "https://api.openalex.org/works"
    MAILTO = "aditya@scitoxsynthesis.local"  # polite pool identifier

    def __init__(self, fetcher: HTTPFetcher) -> None:
        self.fetcher = fetcher

    async def search(self, chemical: str, keywords: list[str],
                     per_page: int = 25, oa_only: bool = True) -> list[PaperCandidate]:
        # OpenAlex full-text search with filters for OA status
        kw = " ".join(keywords[:5]) if keywords else ""
        query = f"{chemical} {kw}".strip()
        filters = []
        if oa_only:
            filters.append("is_oa:true")
        filter_str = ",".join(filters) if filters else ""
        url = (f"{self.BASE}?search={quote(query)}"
               f"&per-page={per_page}&mailto={quote(self.MAILTO)}")
        if filter_str:
            url += f"&filter={quote(filter_str)}"

        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return []
        try:
            j = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return []

        out: list[PaperCandidate] = []
        for w in j.get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
            pmid = None
            # OpenAlex stores PMID in ids dict
            ids = w.get("ids", {})
            if ids.get("pmid"):
                pmid_raw = ids["pmid"]
                pmid = pmid_raw.rsplit("/", 1)[-1] if "/" in pmid_raw else pmid_raw
            pmcid = None
            if ids.get("pmcid"):
                pmcid_raw = ids["pmcid"]
                pmcid = pmcid_raw.rsplit("/", 1)[-1] if "/" in pmcid_raw else pmcid_raw

            oa = w.get("open_access", {}) or {}
            pdf_url = oa.get("oa_url")
            ft_url = pdf_url
            if pmcid:
                ft_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"

            # Reconstruct abstract from inverted index if present
            abstract = ""
            inv = w.get("abstract_inverted_index")
            if inv:
                positions: list[tuple[int, str]] = []
                for word, posns in inv.items():
                    for p in posns:
                        positions.append((p, word))
                positions.sort()
                abstract = " ".join(w for _, w in positions)

            out.append(PaperCandidate(
                source="openalex",
                pmid=pmid,
                pmcid=pmcid,
                doi=doi,
                title=w.get("title", "") or "",
                journal=(w.get("primary_location") or {}).get("source", {}).get("display_name"),
                year=str(w.get("publication_year", "")) or None,
                abstract=abstract[:3000],
                has_free_fulltext=bool(oa.get("is_oa")),
                has_pdf=bool(pdf_url),
                fulltext_url=ft_url,
                pdf_url=pdf_url,
                landing_url=(f"https://openalex.org/{w.get('id', '').rsplit('/', 1)[-1]}"
                             if w.get("id") else None),
            ))
        return out


# ══════════════════════════════════════════════════════════════════════════
#  PMC FULL-TEXT HARVESTER  — clean text + figure URLs + reference network
# ══════════════════════════════════════════════════════════════════════════
