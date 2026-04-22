"""
sources._models — Shared dataclasses used across multiple source modules.

Keeping these in one place avoids circular imports between pubmed, pmc,
openalex, and jats.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ChemicalIdentity:
    query: str
    cid: int | None = None
    cas: str | None = None
    iupac_name: str | None = None
    canonical_smiles: str | None = None
    isomeric_smiles: str | None = None
    inchi: str | None = None
    inchikey: str | None = None
    molecular_formula: str | None = None
    molecular_weight: float | None = None
    synonyms: list[str] = field(default_factory=list)
    common_name: str | None = None
    pubchem_url: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperCandidate:
    source: str
    title: str = ""
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    year: str | None = None
    journal: str | None = None
    abstract: str = ""
    has_free_fulltext: bool = False
    has_pdf: bool = False
    fulltext_url: str | None = None
    pdf_url: str | None = None
    landing_url: str | None = None

    def priority_score(self) -> int:
        s = 0
        if self.pmcid: s += 100
        if self.has_free_fulltext: s += 40
        if self.has_pdf: s += 30
        if self.doi: s += 5
        if self.abstract: s += 2
        return s

    def title_relevance(self, chemical: str, keywords: list[str]) -> float:
        """Fraction of [chemical + top-3 keywords] that actually appear in title/abstract.

        Returns 0.0–1.0. Low scores mean the paper was caught by the keyword query
        but isn't actually about the chemical — e.g. a leukemia clinical paper that
        only mentions benzene in the background. Used to filter off-topic hits.
        """
        haystack = (self.title + " " + self.abstract).lower()
        if not haystack.strip():
            return 0.0
        terms = [chemical.lower()] + [k.lower() for k in keywords[:3] if k.strip()]
        hits = sum(1 for t in terms if t and t in haystack)
        # Require chemical name to appear at all — zero it out otherwise
        if chemical.lower() not in haystack:
            return 0.0
        return hits / max(1, len(terms))

    def best_url(self) -> str | None:
        return (self.fulltext_url or self.pdf_url or self.landing_url
                or (f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/" if self.pmid else None))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["priority_score"] = self.priority_score()
        d["best_url"] = self.best_url()
        return d
