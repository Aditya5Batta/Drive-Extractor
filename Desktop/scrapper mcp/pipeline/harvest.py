"""
pipeline.harvest — One-shot evidence harvester.

Backs the `harvest_evidence` MCP tool. Given a chemical identity, this module:

  1. Runs MultiDatabaseHarvester across all 14 agency/industry DBs in parallel.
  2. Searches PubMed + EuropePMC, fetches PMC full-text for any PMCID hits.
  3. Optionally counts OpenAlex matches (extra coverage signal).
  4. Fetches agency profiles (ATSDR, NTP, EPA IRIS, NIOSH, OEHHA, OSHA, ILO).
  5. Scans every harvested document for the 570 section keywords and routes
     each evidence record to its LITERATURE_SECTIONS bucket.
  6. Appends every record to the run's EvidenceLedger and emits trace events.
  7. Returns a HarvestResult with per-section evidence + per-database counts.

Determinism: zero LLM calls. Every excerpt is verbatim from the retrieved page.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any

from config.sections import (
    LITERATURE_SECTIONS,
    SECTION_KEYWORDS,
)
from core.provenance import EvidenceLedger
from core.tracing import Tracer
from sources._base import HarvestedDocument, PaperRef
from sources.agencies import AgencyProfileFetcher
from sources.harvester import MultiDatabaseHarvester
from sources.openalex import OpenAlexClient
from sources.pmc import PMCHarvester
from sources.pubmed import PaperFinder


# ─────────────────────────────────────────────────────────────────────────────
#  Return types
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SectionEvidence:
    """Evidence routed to a single one of the 29 LITERATURE_SECTIONS."""
    section_id: str
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.records)


@dataclass
class HarvestResult:
    """Complete result of a harvest_evidence run."""
    chemical: str
    cas: str | None
    per_database: dict[str, int]            # db_name → paper count
    per_database_errors: dict[str, str]     # db_name → error string (if failed)
    per_section: dict[str, SectionEvidence] # section_id → SectionEvidence
    agency_profiles: list[dict[str, Any]] = field(default_factory=list)
    total_records: int = 0
    total_documents: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chemical":    self.chemical,
            "cas":         self.cas,
            "per_database":        self.per_database,
            "per_database_errors": self.per_database_errors,
            "per_section": {sid: {"count": se.count, "records": se.records}
                            for sid, se in self.per_section.items()},
            "agency_profiles": self.agency_profiles,
            "total_records":   self.total_records,
            "total_documents": self.total_documents,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  KWIC excerpt helper
# ─────────────────────────────────────────────────────────────────────────────
_WINDOW = 280
_MAX_PER_SECTION_PER_DOC = 3   # cap excerpts per section per document


def _excerpt(text: str, position: int, window: int = _WINDOW) -> str:
    lo = max(0, position - window)
    hi = min(len(text), position + window)
    snippet = text[lo:hi]
    if lo > 0:
        sp = snippet.find(" ")
        if sp >= 0:
            snippet = "…" + snippet[sp + 1:]
    if hi < len(text):
        sp = snippet.rfind(" ")
        if sp >= 0:
            snippet = snippet[:sp] + "…"
    return snippet.strip()


def _scan_document_for_sections(full_text: str) -> dict[str, list[tuple[str, str, int]]]:
    """Return {section_id: [(keyword, excerpt, offset), ...]} with per-section cap."""
    out: dict[str, list[tuple[str, str, int]]] = {}
    if not full_text:
        return out
    lower = full_text.lower()
    for section_id in LITERATURE_SECTIONS:
        hits: list[tuple[str, str, int]] = []
        seen_buckets: set[tuple[str, int]] = set()
        for kw in SECTION_KEYWORDS.get(section_id, []):
            start = 0
            while True:
                pos = lower.find(kw, start)
                if pos < 0:
                    break
                bucket = (kw, pos // 500)  # dedupe same kw within 500 chars
                if bucket not in seen_buckets:
                    seen_buckets.add(bucket)
                    hits.append((kw, _excerpt(full_text, pos), pos))
                    if len(hits) >= _MAX_PER_SECTION_PER_DOC:
                        break
                start = pos + max(1, len(kw))
            if len(hits) >= _MAX_PER_SECTION_PER_DOC:
                break
        if hits:
            out[section_id] = hits
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Confidence scoring
# ─────────────────────────────────────────────────────────────────────────────
def _confidence(num_categories: int, num_hits: int) -> float:
    return min(1.0, 0.30 + 0.15 * num_categories + 0.05 * min(5, num_hits))


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────
async def harvest_evidence(
    *,
    chemical: str,
    cas: str | None,
    multi_harvester: MultiDatabaseHarvester,
    papers: PaperFinder,
    openalex: OpenAlexClient,
    pmc: PMCHarvester,
    agency: AgencyProfileFetcher,
    tracer: Tracer | None = None,
    ledger: EvidenceLedger | None = None,
    per_db_limit: int | None = None,
    include_openalex: bool = True,
    include_pubmed: bool = True,
    include_agencies: bool = True,
) -> HarvestResult:
    """Run the full harvest. Returns a HarvestResult."""

    per_database: dict[str, int] = {}
    per_database_errors: dict[str, str] = {}
    per_section: dict[str, SectionEvidence] = {
        sid: SectionEvidence(section_id=sid) for sid in LITERATURE_SECTIONS
    }
    agency_profiles: list[dict[str, Any]] = []
    total_documents = 0

    # ─── 1. 14-DB parallel harvest ──────────────────────────────────────
    try:
        if tracer:
            with tracer.step("harvest.multi_db", chemical=chemical, cas=cas):
                docs = await multi_harvester.harvest_all(chemical, cas, per_db_limit)
        else:
            docs = await multi_harvester.harvest_all(chemical, cas, per_db_limit)
    except Exception as e:
        per_database_errors["multi_db"] = f"{type(e).__name__}: {e}"
        docs = []

    for doc in docs:
        per_database[doc.source] = per_database.get(doc.source, 0) + 1
        if doc.errors:
            per_database_errors.setdefault(doc.source, "; ".join(doc.errors[:3]))
        total_documents += 1
        _route_doc_to_sections(doc, per_section, chemical, ledger)

    # ─── 2. PubMed / EuropePMC ──────────────────────────────────────────
    if include_pubmed:
        try:
            if tracer:
                with tracer.step("harvest.pubmed", chemical=chemical):
                    pm_docs = await _harvest_pubmed(papers, pmc, chemical, cas,
                                                    per_db_limit)
            else:
                pm_docs = await _harvest_pubmed(papers, pmc, chemical, cas,
                                                per_db_limit)
            for doc in pm_docs:
                per_database[doc.source] = per_database.get(doc.source, 0) + 1
                total_documents += 1
                _route_doc_to_sections(doc, per_section, chemical, ledger)
        except Exception as e:
            per_database_errors["PubMed"] = f"{type(e).__name__}: {e}"

    # ─── 3. OpenAlex (count only — too many to deep-harvest) ─────────────
    if include_openalex:
        try:
            if tracer:
                with tracer.step("harvest.openalex", chemical=chemical):
                    oa_hits = await openalex.search(
                        chemical, keywords=["toxicity"], per_page=25, oa_only=True,
                    )
            else:
                oa_hits = await openalex.search(
                    chemical, keywords=["toxicity"], per_page=25, oa_only=True,
                )
            per_database["OpenAlex"] = len(oa_hits or [])
        except Exception as e:
            per_database_errors["OpenAlex"] = f"{type(e).__name__}: {e}"

    # ─── 4. Agency profiles ─────────────────────────────────────────────
    if include_agencies:
        try:
            if tracer:
                with tracer.step("harvest.agencies", chemical=chemical):
                    agency_profiles = await agency.fetch_all(chemical, cas)
            else:
                agency_profiles = await agency.fetch_all(chemical, cas)
            if isinstance(agency_profiles, list):
                for prof in agency_profiles:
                    text = prof.get("text") or prof.get("summary") or ""
                    if text:
                        fake_doc = HarvestedDocument(
                            source=prof.get("agency", "agency"),
                            ref=PaperRef(
                                source=prof.get("agency", "agency"),
                                title=prof.get("title", ""),
                                authors="", year="",
                                doi=None, pmid=None, pmcid=None,
                                landing_url=prof.get("url"),
                                pdf_url=None, abstract="",
                            ),
                            pdf_bytes_len=0, page_count=0,
                            full_text=text, char_count=len(text),
                            tables=[], figures=[], errors=[],
                        )
                        _route_doc_to_sections(fake_doc, per_section, chemical, ledger)
        except Exception as e:
            per_database_errors["Agencies"] = f"{type(e).__name__}: {e}"

    total_records = sum(se.count for se in per_section.values())

    if tracer:
        tracer.event("harvest.complete",
                     total_documents=total_documents,
                     total_records=total_records,
                     per_db_counts=per_database,
                     per_db_errors=per_database_errors)

    return HarvestResult(
        chemical=chemical,
        cas=cas,
        per_database=per_database,
        per_database_errors=per_database_errors,
        per_section=per_section,
        agency_profiles=agency_profiles if isinstance(agency_profiles, list) else [],
        total_records=total_records,
        total_documents=total_documents,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _route_doc_to_sections(
    doc: HarvestedDocument,
    per_section: dict[str, SectionEvidence],
    chemical: str,
    ledger: EvidenceLedger | None,
) -> None:
    """Scan a document; route each hit into the correct section bucket + ledger."""
    if not doc.full_text:
        return
    scanned = _scan_document_for_sections(doc.full_text)
    if not scanned:
        return

    # Confidence: breadth × density
    n_categories = len(scanned)
    n_hits = sum(len(v) for v in scanned.values())
    conf = _confidence(n_categories, n_hits)

    ref = doc.ref
    base_url = ref.landing_url or ref.pdf_url or ""

    for section_id, hits in scanned.items():
        for keyword, excerpt, offset in hits:
            rec = {
                "section_id":  section_id,
                "source_db":   doc.source,
                "title":       ref.title,
                "authors":     ref.authors,
                "year":        ref.year,
                "doi":         ref.doi,
                "pmid":        ref.pmid,
                "pmcid":       ref.pmcid,
                "url":         base_url,
                "keyword":     keyword,
                "quote":       excerpt,
                "char_offset": offset,
                "confidence":  conf,
            }
            per_section[section_id].records.append(rec)

    # Ledger (batch-record via add_from_text — one call per document, one per section)
    if ledger:
        try:
            for section_id, hits in scanned.items():
                keywords = list({h[0] for h in hits})
                ledger.add_from_text(
                    source=doc.source,
                    database_label=doc.source,
                    chemical=chemical,
                    text=doc.full_text,
                    keywords=keywords,
                    section=section_id,
                    url=base_url,
                    doi=ref.doi, pmid=ref.pmid, pmcid=ref.pmcid,
                    title=ref.title,
                    authors=ref.authors, year=ref.year,
                    extraction_method="pdf_fulltext" if doc.pdf_bytes_len else "text",
                    max_excerpts=_MAX_PER_SECTION_PER_DOC,
                    window=_WINDOW,
                )
        except Exception:
            pass  # ledger errors never kill a harvest


async def _harvest_pubmed(
    papers: PaperFinder,
    pmc: PMCHarvester,
    chemical: str,
    cas: str | None,
    per_db_limit: int | None,
) -> list[HarvestedDocument]:
    """Search PubMed + EuropePMC, fetch PMC full text, return HarvestedDocuments."""
    limit = per_db_limit or 15
    candidates = await papers.find(chemical, top=limit)

    out: list[HarvestedDocument] = []
    for pc in candidates[:limit]:
        ref = PaperRef(
            source="PubMed",
            title=pc.title,
            authors="",
            year=pc.year or "",
            doi=pc.doi, pmid=pc.pmid, pmcid=pc.pmcid,
            landing_url=pc.landing_url or pc.best_url(),
            pdf_url=pc.pdf_url,
            abstract=pc.abstract,
            extra={},
        )
        full_text = pc.abstract or ""
        if pc.pmcid:
            try:
                art = await pmc.fetch(pc.pmcid)
                body = getattr(art, "body_text", "") or ""
                if body:
                    full_text = body
            except Exception:
                pass
        out.append(HarvestedDocument(
            source="PubMed", ref=ref,
            pdf_bytes_len=0, page_count=0,
            full_text=full_text,
            char_count=len(full_text),
            tables=[], figures=[], errors=[],
        ))
    return out
