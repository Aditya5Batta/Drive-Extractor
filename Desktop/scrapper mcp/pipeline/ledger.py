"""
pipeline.ledger — Per-session papers-per-database tracker.

Maintains a count of how many papers each database has returned this session,
so the report can show "PubMed: 142 | ECHA: 23 | NTP: 11 | ..." tables.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


class ExtractionLedger:
    """Per-session accounting: which papers were pulled from which source.

    Required for regulatory dossiers where provenance matters. Automatically
    records every paper/document retrieved by find_papers, find_papers_openalex,
    fetch_pmc_article, and fetch_agency_profile. Query via get_extraction_ledger.
    """

    def __init__(self) -> None:
        # source -> list of {title, url, pmid, pmcid, doi, retrieved_at}
        self._entries: dict[str, list[dict[str, Any]]] = {}

    def record(self, source: str, entry: dict[str, Any]) -> None:
        if source not in self._entries:
            self._entries[source] = []
        # Dedupe by (pmid, pmcid, doi, url) composite key
        key = (entry.get("pmid"), entry.get("pmcid"),
               entry.get("doi"), entry.get("url"))
        for existing in self._entries[source]:
            ek = (existing.get("pmid"), existing.get("pmcid"),
                  existing.get("doi"), existing.get("url"))
            if key == ek:
                return
        self._entries[source].append(entry)

    def record_paper(self, source: str, paper: PaperCandidate) -> None:
        self.record(source, {
            "title": paper.title,
            "pmid": paper.pmid,
            "pmcid": paper.pmcid,
            "doi": paper.doi,
            "year": paper.year,
            "journal": paper.journal,
            "url": paper.best_url(),
            "has_free_fulltext": paper.has_free_fulltext,
            "has_pdf": paper.has_pdf,
        })

    def record_agency(self, agency: str, url: str, ok: bool, title: str = "") -> None:
        self.record(agency, {"title": title, "url": url, "ok": ok})

    def reset(self) -> None:
        self._entries = {}

    def summary(self) -> dict[str, Any]:
        total = sum(len(v) for v in self._entries.values())
        counts = {src: len(entries) for src, entries in self._entries.items()}
        return {
            "total_extractions": total,
            "unique_sources": len(self._entries),
            "counts_by_source": counts,
            "full_entries": self._entries,
        }

    def markdown(self) -> str:
        lines = ["# Extraction Ledger — Per-Source Provenance", ""]
        total = sum(len(v) for v in self._entries.values())
        lines.append(f"- Total extractions: **{total}**")
        lines.append(f"- Unique sources: **{len(self._entries)}**")
        lines.append("")
        lines.append("## Counts by source")
        lines.append("| Source | Papers/Docs | |")
        lines.append("|--------|-------------|-|")
        for src, entries in sorted(self._entries.items(),
                                   key=lambda kv: -len(kv[1])):
            lines.append(f"| {src} | {len(entries)} | |")
        lines.append("")
        lines.append("## Full entries (by source)")
        for src, entries in self._entries.items():
            lines.append(f"\n### {src} ({len(entries)})")
            for i, e in enumerate(entries, 1):
                title = (e.get("title") or "(no title)")[:100]
                url = e.get("url") or ""
                ids = []
                if e.get("pmid"): ids.append(f"PMID:{e['pmid']}")
                if e.get("pmcid"): ids.append(e["pmcid"])
                if e.get("doi"): ids.append(f"DOI:{e['doi']}")
                id_str = f" [{', '.join(ids)}]" if ids else ""
                lines.append(f"{i}. {title}{id_str}  \n   {url}")
        return "\n".join(lines)
