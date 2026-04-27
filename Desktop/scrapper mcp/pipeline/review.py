"""
pipeline.review — docx composer for the `build_review` MCP tool.

Turns a HarvestResult (pipeline.harvest) plus a ChemicalIdentity into a finished
Word document. The section order mirrors the Benzene_LitReview_v3 reference:

    Cover page (chemical + CAS + structure image)
    1. Summary
    2. Scope and Methodology of This Review
    3. Key Findings at a Glance
    4. Database Coverage and Retrieval Methodology
    5. Chemical Properties
    6. Environmental Behaviour
    7. Toxicokinetics (ADME)
    8. Sources of Exposure          ← 4 sub-sections from config.sections
    9. Health Effects                ← 8 sub-sections from config.sections
   10. Regulations and Guidelines    ← 5 sub-sections
   11. Risk Assessment               ← 4 sub-sections
   12. Mitigation and Prevention     ← 4 sub-sections
   13. Recent Research Findings
   14. References
   15. Appendix A — Per-Database Extraction Counts
   16. Appendix B — Extraction Ledger

Every section's prose is stitched from verbatim excerpts in the HarvestResult
(no LLM). This preserves traceability: each paragraph is a direct quote from a
harvested document with an inline [n] citation.

The module is pure glue: heavy formatting lives in pipeline.report.ReportGenerator.
"""
from __future__ import annotations
import datetime
import os
from dataclasses import dataclass, field
from typing import Any

from config.sections import (
    LITERATURE_SECTIONS,
    SECTION_GROUPS,
    SECTION_TITLES,
)
from pipeline.harvest import HarvestResult, SectionEvidence
from pipeline.report import (
    HAS_PYTHON_DOCX,
    ReportFigure,
    ReportGenerator,
    ReportReference,
    ReportSection,
    ReportTable,
)
from sources._models import ChemicalIdentity


# ─────────────────────────────────────────────────────────────────────────────
#  Return type
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ReviewResult:
    """Summary of a compose run."""
    output_path: str
    chemical: str
    cas: str | None
    section_count: int
    records_rendered: int
    references_count: int
    figures_count: int = 0
    tables_count: int = 0
    sections_empty: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path":      self.output_path,
            "chemical":         self.chemical,
            "cas":              self.cas,
            "section_count":    self.section_count,
            "records_rendered": self.records_rendered,
            "references_count": self.references_count,
            "figures_count":    self.figures_count,
            "tables_count":     self.tables_count,
            "sections_empty":   self.sections_empty,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Reference numbering
# ─────────────────────────────────────────────────────────────────────────────
class _RefTable:
    """Deduplicates references by (pmid, pmcid, doi, url) and assigns [n] numbers."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str, str], int] = {}
        self._refs: list[ReportReference] = []

    def add(self, record: dict[str, Any]) -> int:
        pmid  = (record.get("pmid") or "")
        pmcid = (record.get("pmcid") or "")
        doi   = (record.get("doi") or "")
        url   = (record.get("url") or "")
        key = (pmid, pmcid, doi, url)
        # Records with no identifiers at all fall back to title|source dedup
        if key == ("", "", "", ""):
            key = (record.get("title") or "", record.get("source_db") or "", "", "")
        if key in self._by_key:
            return self._by_key[key]
        n = len(self._refs) + 1
        self._by_key[key] = n
        self._refs.append(ReportReference(
            number=n,
            authors=record.get("authors") or "",
            year=str(record.get("year") or ""),
            title=record.get("title") or "",
            journal="",
            doi=doi or None,
            pmid=pmid or None,
            pmcid=pmcid or None,
            url=url or "",
            database=record.get("source_db") or "",
        ))
        return n

    @property
    def references(self) -> list[ReportReference]:
        return list(self._refs)


# ─────────────────────────────────────────────────────────────────────────────
#  Prose assembly — each excerpt becomes one justified paragraph
# ─────────────────────────────────────────────────────────────────────────────
_MAX_EXCERPTS_PER_SECTION = 14  # keep each section readable (not a data dump)


def _paragraphs_for_section(evidence: SectionEvidence, refs: _RefTable) -> list[str]:
    """Convert up to N evidence records in a section into verbatim paragraphs."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for rec in evidence.records[:_MAX_EXCERPTS_PER_SECTION * 2]:
        quote = (rec.get("quote") or "").strip()
        if not quote:
            continue
        # Dedupe near-identical excerpts by their first 60 chars (case-insensitive)
        sig = quote.lower()[:60]
        if sig in seen:
            continue
        seen.add(sig)

        n = refs.add(rec)
        db = rec.get("source_db") or ""
        attribution = f" (Source: {db}.)" if db else ""
        paragraphs.append(f"{quote}{attribution} [{n}]")

        if len(paragraphs) >= _MAX_EXCERPTS_PER_SECTION:
            break
    return paragraphs


# ─────────────────────────────────────────────────────────────────────────────
#  Prelude sections (1–4) — summary, scope, key findings, DB coverage
# ─────────────────────────────────────────────────────────────────────────────
def _summary_section(
    chemical: str, cas: str | None, identity: ChemicalIdentity,
    harvest: HarvestResult,
) -> ReportSection:
    formula = identity.molecular_formula or "—"
    mw      = identity.molecular_weight  or "—"
    iupac   = identity.iupac_name        or chemical
    total_docs = harvest.total_documents
    total_records = harvest.total_records
    dbs_hit = sum(1 for v in harvest.per_database.values() if v > 0)

    p1 = (
        f"This review synthesises the peer-reviewed and regulatory literature on "
        f"{chemical.capitalize()} (CAS {cas or '—'}; molecular formula {formula}; "
        f"molecular weight {mw}) as retrieved from {dbs_hit} databases during the "
        f"automated harvest. The IUPAC name of this substance is {iupac}."
    )
    p2 = (
        f"A total of {total_docs:,} documents were harvested across the consulted "
        f"databases, yielding {total_records:,} keyword-anchored evidence records "
        "routed to the 29 canonical sections of this review. Every paragraph in "
        "the body of this document is a verbatim excerpt from a harvested source "
        "with an inline bracketed citation."
    )
    return ReportSection(title="Summary", level=1, paragraphs=[p1, p2])


def _scope_section(chemical: str) -> ReportSection:
    p1 = (
        f"This document provides a comprehensive literature review on the safety, "
        f"toxicology, and regulatory status of {chemical.capitalize()}. The review "
        "was produced by an automated evidence-harvesting pipeline that queries "
        "peer-reviewed databases (PubMed, EuropePMC, OpenAlex) alongside "
        "regulatory and agency sources (ATSDR, NTP, EPA IRIS, NIOSH, OSHA, OEHHA, "
        "IARC, ILO, WHO INCHEM, ECHA, and CONCAWE), downloads available "
        "full-text PDFs, and routes matched passages into the 29 canonical "
        "sections of this review using a deterministic keyword bank."
    )
    p2 = (
        "No prose is generated by language models. Every claim in the body of "
        "this document is a verbatim excerpt from a harvested source with an "
        "inline bracketed citation keyed to the References list at the end. "
        "Sections with zero supporting evidence are explicitly flagged rather "
        "than backfilled with speculative or generic content."
    )
    return ReportSection(title="Scope and Methodology of This Review",
                         level=1, paragraphs=[p1, p2])


def _key_findings_section(harvest: HarvestResult) -> ReportSection:
    """Bullet-ish summary of which sections got the most evidence."""
    by_count = sorted(
        ((sid, se) for sid, se in harvest.per_section.items() if se.count > 0),
        key=lambda kv: -kv[1].count,
    )
    paragraphs: list[str] = []
    if not by_count:
        paragraphs.append(
            "No keyword-anchored evidence was routed to any of the 29 sections "
            "of this review; downstream sections are therefore marked as lacking "
            "sufficient evidence. This likely indicates a harvest-time network "
            "failure or an under-represented chemical."
        )
    else:
        p = (
            "Evidence density across the 29 canonical sections of this review "
            "identified the following most-represented topics: "
        )
        top = ", ".join(
            f"{SECTION_TITLES.get(sid, sid)} ({se.count:,})"
            for sid, se in by_count[:8]
        ) + "."
        paragraphs.append(p + top)

        low_cov = [sid for sid, se in harvest.per_section.items() if se.count == 0]
        if low_cov:
            paragraphs.append(
                f"{len(low_cov)} of 29 sections had no keyword-anchored "
                "evidence from the harvest and are rendered with an explicit "
                "insufficient-evidence notice: "
                + ", ".join(SECTION_TITLES.get(s, s) for s in low_cov[:12])
                + ("." if len(low_cov) <= 12 else ", …")
            )
    return ReportSection(title="Key Findings at a Glance",
                         level=1, paragraphs=paragraphs)


def _db_coverage_section(harvest: HarvestResult) -> ReportSection:
    rows = sorted(harvest.per_database.items(), key=lambda kv: -kv[1])
    table = ReportTable(
        title="Per-database document counts from this harvest.",
        headers=["#", "Database", "Documents", "Errors"],
        rows=[
            [str(i), db, f"{n:,}",
             (harvest.per_database_errors.get(db) or "")[:60]]
            for i, (db, n) in enumerate(rows, 1)
        ],
        placement="Database Coverage and Retrieval Methodology",
    )
    err_rows = [
        [db, msg[:120]]
        for db, msg in harvest.per_database_errors.items()
        if harvest.per_database.get(db, 0) == 0
    ]
    err_table = None
    if err_rows:
        err_table = ReportTable(
            title="Databases that produced no documents during this harvest.",
            headers=["Database", "Error"],
            rows=err_rows,
            placement="Database Coverage and Retrieval Methodology",
        )

    p = (
        "The harvest layer consulted peer-reviewed and regulatory databases in "
        "parallel, capping each at a configurable per-database limit. The table "
        "below shows the document count contributed by each consulted database "
        "during this run; a second table (where applicable) lists databases that "
        "failed to return any documents, with the recorded error message."
    )

    tables = [table] + ([err_table] if err_table else [])
    return ReportSection(
        title="Database Coverage and Retrieval Methodology",
        level=1,
        paragraphs=[p],
        inline_tables=tables,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Chapter assembly — walk SECTION_GROUPS, render each H1 + sub-sections
# ─────────────────────────────────────────────────────────────────────────────
_EMPTY_SECTION_NOTE = (
    "No keyword-anchored evidence was routed to this section during the harvest. "
    "Per the review's zero-hallucination policy, no generated prose is provided."
)


def _build_body_sections(
    harvest: HarvestResult, refs: _RefTable,
) -> tuple[list[ReportSection], list[str]]:
    """Render SECTION_GROUPS into ReportSections. Returns (sections, empty_ids)."""
    out: list[ReportSection] = []
    empty_ids: list[str] = []

    # Start at chapter #5 (the first four are Summary/Scope/Key Findings/DB Coverage).
    chapter_num = 5
    for chapter_title, section_ids in SECTION_GROUPS:
        children: list[ReportSection] = []
        if len(section_ids) == 1:
            sid = section_ids[0]
            ev = harvest.per_section.get(sid, SectionEvidence(section_id=sid))
            paras = _paragraphs_for_section(ev, refs)
            if not paras:
                paras = [_EMPTY_SECTION_NOTE]
                empty_ids.append(sid)
            # Single-section chapter: body goes directly on the chapter heading.
            out.append(ReportSection(
                title=chapter_title, level=1, paragraphs=paras,
                chapter_number=chapter_num, chapter_subtitle=None,
            ))
        else:
            for sid in section_ids:
                ev = harvest.per_section.get(sid, SectionEvidence(section_id=sid))
                paras = _paragraphs_for_section(ev, refs)
                if not paras:
                    paras = [_EMPTY_SECTION_NOTE]
                    empty_ids.append(sid)
                children.append(ReportSection(
                    title=SECTION_TITLES.get(sid, sid),
                    level=2,
                    paragraphs=paras,
                ))
            out.append(ReportSection(
                title=chapter_title, level=1, paragraphs=[],
                subsections=children,
                chapter_number=chapter_num, chapter_subtitle=None,
            ))
        chapter_num += 1
    return out, empty_ids


def _db_counts_for_appendix(harvest: HarvestResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for db, n in sorted(harvest.per_database.items(), key=lambda kv: -kv[1]):
        err = harvest.per_database_errors.get(db)
        rows.append({
            "database":     db,
            "status":       "error" if err and n == 0 else "ok",
            "result_count": n,
            "url":          "",
            "error":        err or "",
        })
    # DBs that errored with zero count
    for db, err in harvest.per_database_errors.items():
        if db in harvest.per_database:
            continue
        rows.append({
            "database":     db,
            "status":       "error",
            "result_count": 0,
            "url":          "",
            "error":        err,
        })
    return rows


def _ledger_entries_for_appendix(
    harvest: HarvestResult,
) -> dict[str, list[dict[str, Any]]]:
    """Group unique (title, url, pmid, pmcid, doi) rows per source database."""
    out: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[tuple[str, str, str, str, str]]] = {}
    for ev in harvest.per_section.values():
        for r in ev.records:
            src = r.get("source_db") or "unknown"
            key = (
                r.get("title") or "",
                r.get("url") or "",
                r.get("pmid") or "",
                r.get("pmcid") or "",
                r.get("doi") or "",
            )
            bucket = seen.setdefault(src, set())
            if key in bucket:
                continue
            bucket.add(key)
            out.setdefault(src, []).append({
                "title": r.get("title") or "",
                "url":   r.get("url"),
                "pmid":  r.get("pmid"),
                "pmcid": r.get("pmcid"),
                "doi":   r.get("doi"),
                "year":  r.get("year"),
            })
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def build_review(
    *,
    chemical: str,
    identity: ChemicalIdentity,
    harvest: HarvestResult,
    output_path: str,
    report_gen: ReportGenerator,
    cover_image_path: str | None = None,
    include_appendices: bool = True,
) -> ReviewResult:
    """Compose a Benzene-v3-style docx from harvest evidence. Zero LLM calls."""
    if not HAS_PYTHON_DOCX:
        raise RuntimeError("python-docx not installed (pip install python-docx)")

    cas = identity.cas

    # 1. Prelude: Summary / Scope / Key Findings / DB Coverage
    prelude: list[ReportSection] = [
        _summary_section(chemical, cas, identity, harvest),
        _scope_section(chemical),
        _key_findings_section(harvest),
        _db_coverage_section(harvest),
    ]

    # 2. Body chapters → references table is populated as a side effect
    refs = _RefTable()
    body_sections, empty_ids = _build_body_sections(harvest, refs)

    all_sections = prelude + body_sections

    # 3. Output directory must exist
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    report_gen.generate(
        output_path=output_path,
        chemical=chemical,
        cas=cas,
        sections=all_sections,
        references=refs.references,
        db_counts=_db_counts_for_appendix(harvest),
        ledger_entries=_ledger_entries_for_appendix(harvest) if include_appendices else None,
        generated_date=datetime.date.today().isoformat(),
        figures=[],
        tables=[],
        cover_image_path=cover_image_path,
        include_appendices=include_appendices,
    )

    return ReviewResult(
        output_path=output_path,
        chemical=chemical,
        cas=cas,
        section_count=len(all_sections),
        records_rendered=sum(se.count for se in harvest.per_section.values()),
        references_count=len(refs.references),
        sections_empty=empty_ids,
    )


__all__ = ["ReviewResult", "build_review"]
