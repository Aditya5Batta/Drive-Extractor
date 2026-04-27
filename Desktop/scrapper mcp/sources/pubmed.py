"""
sources.pubmed — PubMed + EuropePMC paper finder.

Uses NCBI E-utilities (esearch/efetch) and Europe PMC REST. Prioritises
free-full-text and open-access PDFs so downstream pipeline stages have
something to extract.
"""
from __future__ import annotations
import asyncio
import json
import re
import datetime
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from core.http import HTTPFetcher
from core.keywords import KeywordSearcher
from sources._models import ChemicalIdentity, PaperCandidate


class PaperFinder:
    """Search PubMed (E-utilities) + EuropePMC REST. Prioritize free full text."""

    PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, fetcher: HTTPFetcher) -> None:
        self.fetcher = fetcher

    async def _json(self, url: str) -> dict | None:
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return None
        try:
            return json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None

    async def _fetch_pubmed_abstracts(self, pmids: list[str]) -> dict[str, str]:
        """Use PubMed efetch to pull abstracts as PubMed XML, then parse
        AbstractText elements. Critical for title_relevance filter accuracy —
        without abstracts, on-topic papers whose chemical name only appears
        in the abstract get filtered out.
        """
        if not pmids:
            return {}
        url = (f"{self.PUBMED_EFETCH}?db=pubmed&id={','.join(pmids)}"
               f"&rettype=abstract&retmode=xml")
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return {}
        abstracts: dict[str, str] = {}
        try:
            soup = BeautifulSoup(data, "lxml-xml")
        except Exception:
            try:
                soup = BeautifulSoup(data, "xml")
            except Exception:
                return {}
        for art in soup.find_all("PubmedArticle"):
            pmid_tag = art.find("PMID")
            if not pmid_tag:
                continue
            pmid = pmid_tag.get_text(strip=True)
            # Abstract may have multiple AbstractText sections (Background, Methods, etc.)
            parts: list[str] = []
            for at in art.find_all("AbstractText"):
                label = at.get("Label")
                text = at.get_text(" ", strip=True)
                if label:
                    parts.append(f"{label}: {text}")
                else:
                    parts.append(text)
            if parts:
                abstracts[pmid] = " ".join(parts)[:5000]
        return abstracts

    async def search_pubmed(self, chemical: str, keywords: list[str],
                            retmax: int = 25, free_full_text_only: bool = True) -> list[PaperCandidate]:
        kw_clause = " OR ".join(f'"{k}"' for k in keywords if k.strip()) or "toxicity"
        term = f'("{chemical}"[Title/Abstract]) AND ({kw_clause})'
        if free_full_text_only:
            term += ' AND "free full text"[sb]'
        esearch = (f"{self.PUBMED_ESEARCH}?db=pubmed&retmode=json"
                   f"&retmax={retmax}&sort=relevance&term={quote(term)}")
        data = await self._json(esearch)
        if not data:
            return []
        try:
            pmids = data["esearchresult"]["idlist"]
        except KeyError:
            return []
        if not pmids:
            return []
        esum = f"{self.PUBMED_ESUMMARY}?db=pubmed&retmode=json&id={','.join(pmids)}"
        # Fetch esummary metadata AND efetch abstracts in parallel
        sdata_task = asyncio.create_task(self._json(esum))
        abs_task = asyncio.create_task(self._fetch_pubmed_abstracts(pmids))
        sdata = await sdata_task
        abstracts = await abs_task

        out: list[PaperCandidate] = []
        if not sdata:
            for pmid in pmids:
                out.append(PaperCandidate(
                    source="pubmed", pmid=pmid,
                    abstract=abstracts.get(pmid, ""),
                    landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"))
            return out
        result = sdata.get("result", {})
        for pmid in pmids:
            r = result.get(pmid)
            if not isinstance(r, dict):
                continue
            pmcid = None; doi = None
            for a in r.get("articleids", []):
                if a.get("idtype") == "pmc": pmcid = a.get("value")
                elif a.get("idtype") == "doi": doi = a.get("value")
            cand = PaperCandidate(
                source="pubmed", pmid=pmid, pmcid=pmcid, doi=doi,
                title=r.get("title", ""),
                abstract=abstracts.get(pmid, ""),
                journal=r.get("fulljournalname") or r.get("source"),
                year=(r.get("pubdate", "") or "").split()[0] or None,
                landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
            if pmcid:
                cand.has_free_fulltext = True
                cand.pmcid = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
                cand.fulltext_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{cand.pmcid}/"
                cand.pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{cand.pmcid}/pdf/"
                cand.has_pdf = True
            out.append(cand)
        return out

    async def search_europepmc(self, chemical: str, keywords: list[str],
                               page_size: int = 25, open_access_only: bool = True) -> list[PaperCandidate]:
        kw_clause = " OR ".join(f'"{k}"' for k in keywords if k.strip()) or "toxicity"
        q = f'("{chemical}") AND ({kw_clause})'
        if open_access_only:
            q += " AND (OPEN_ACCESS:Y)"
        url = (f"{self.EUROPEPMC_SEARCH}?query={quote(q)}"
               f"&format=json&pageSize={page_size}&resultType=core")
        data = await self._json(url)
        if not data:
            return []
        out: list[PaperCandidate] = []
        for r in data.get("resultList", {}).get("result", []):
            pmcid = r.get("pmcid"); pmid = r.get("pmid"); doi = r.get("doi")
            has_pdf = (r.get("hasPDF") == "Y") or bool(pmcid)
            has_ft = (r.get("hasFullTextXML") == "Y") or (r.get("inEPMC") == "Y") or bool(pmcid)
            ft_url = None; pdf_url = None
            if pmcid:
                pmcid_norm = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
                ft_url = f"https://europepmc.org/article/PMC/{pmcid_norm}"
                pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid_norm}&blobtype=pdf"
            elif doi:
                ft_url = f"https://doi.org/{doi}"
            out.append(PaperCandidate(
                source="europepmc", pmid=pmid,
                pmcid=(pmcid if pmcid and pmcid.startswith("PMC") else (f"PMC{pmcid}" if pmcid else None)),
                doi=doi, title=r.get("title", ""),
                journal=r.get("journalTitle"),
                year=str(r.get("pubYear") or "") or None,
                abstract=r.get("abstractText", "") or "",
                has_free_fulltext=has_ft, has_pdf=has_pdf,
                fulltext_url=ft_url, pdf_url=pdf_url,
                landing_url=(f"https://europepmc.org/article/MED/{pmid}" if pmid else ft_url),
            ))
        return out

    DEFAULT_KEYWORDS: list[str] = [
        "carcinogenicity", "neurotoxicity", "hepatotoxicity",
        "nephrotoxicity", "reproductive", "developmental",
        "genotoxicity", "immunotoxicity", "metabolism",
        "exposure", "biomarkers", "dose-response", "toxicity",
    ]

    async def find(self, chemical: str, top: int = 8,
                   keywords: list[str] | None = None) -> list[PaperCandidate]:
        """Unified entry point. Runs PubMed and EuropePMC in parallel, merges,
        ranks, and returns the top N candidates. Called by pipeline.harvest
        during the harvest_evidence MCP tool."""
        kws = list(keywords) if keywords else list(self.DEFAULT_KEYWORDS)
        pm_task = asyncio.create_task(self.search_pubmed(chemical, kws, retmax=25))
        ep_task = asyncio.create_task(self.search_europepmc(chemical, kws, page_size=25))
        pm: list[PaperCandidate] = []
        ep: list[PaperCandidate] = []
        try:
            pm = await pm_task
        except Exception:
            pm = []
        try:
            ep = await ep_task
        except Exception:
            ep = []
        merged = self.merge_and_rank(pm, ep)
        return merged[: max(1, int(top))]

    @staticmethod
    def merge_and_rank(a: list[PaperCandidate], b: list[PaperCandidate]) -> list[PaperCandidate]:
        seen: dict[str, PaperCandidate] = {}
        for cand in a + b:
            key = (cand.doi or cand.pmid or cand.pmcid or cand.title or "").lower().strip()
            if not key:
                continue
            if key in seen:
                kept = seen[key]
                if cand.priority_score() > kept.priority_score():
                    for fn in ("abstract", "journal", "year", "fulltext_url", "pdf_url", "landing_url"):
                        if not getattr(cand, fn) and getattr(kept, fn):
                            setattr(cand, fn, getattr(kept, fn))
                    seen[key] = cand
                else:
                    for fn in ("abstract", "journal", "year", "fulltext_url", "pdf_url", "landing_url"):
                        if not getattr(kept, fn) and getattr(cand, fn):
                            setattr(kept, fn, getattr(cand, fn))
            else:
                seen[key] = cand
        merged = list(seen.values())
        merged.sort(key=lambda c: c.priority_score(), reverse=True)
        return merged


# ══════════════════════════════════════════════════════════════════════════
#  OPENALEX CLIENT  — third major OA database (complements PubMed + EuropePMC)
# ══════════════════════════════════════════════════════════════════════════
