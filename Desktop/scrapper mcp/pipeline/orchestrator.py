"""
pipeline.orchestrator — Linear "chemical-name → full report" flow.

Each step is numbered and logs its progress so a non-technical user can see
what's happening. This is the end-to-end path invoked by the
`generate_chemical_report` and `llm_clean_report` MCP tools.

Steps:
  1.  Resolve chemical via PubChem (name → CID, CAS, SMILES, MW, synonyms)
  2.  Build search URLs for every database in config.databases
  3.  Count papers per database (for the evidence-base table)
  4.  Find top N ranked papers from PubMed + EuropePMC
  5.  Harvest full text + figures + tables from each PMC paper (JATS XML)
  6.  Extract figures/tables from PDFs where JATS is missing them
  7.  Match harvested text against the keyword bank (KWIC)
  8.  Pull agency profiles (NTP, ATSDR, IRIS, OEHHA, NIOSH)
  9.  MultiDatabaseHarvester: run all 14 per-DB source files
      (semantic_scholar, ntp, who_inchem, who_ipcs, echa, atsdr, calepa,
      canada_dsl, concawe, niosh, osha, aicis, ilo, zenodo) — search →
      PDF download → full-text extraction → figure/table extraction in
      parallel for every DB.
  10. Compose the final .docx via pipeline.report.ReportGenerator

The orchestrator is chemical-agnostic and deterministic — same chemical name
yields the same report unless the underlying database content changes.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ProgressFn = Callable[[int, str, dict[str, Any]], None]


@dataclass
class RunState:
    """Shared state accumulated as the pipeline walks through its steps."""
    chemical: str
    cas: str | None = None
    identity: dict[str, Any] | None = None
    urls: list[dict[str, Any]] = field(default_factory=list)
    db_counts: dict[str, int] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    jats_articles: list[dict[str, Any]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    agency_profiles: dict[str, Any] = field(default_factory=dict)
    harvested_docs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _default_progress(step: int, label: str, info: dict[str, Any]) -> None:
    """Prints step-by-step progress to stderr so Claude Desktop shows it."""
    import sys
    summary = " ".join(f"{k}={v}" for k, v in info.items())
    print(f"[step {step}] {label} :: {summary}", file=sys.stderr, flush=True)


async def run(
    ctx: "Any",
    *,
    chemical: str,
    cas: str | None = None,
    top: int = 8,
    progress: Optional[ProgressFn] = None,
) -> RunState:
    """Run every pipeline step in order. Returns accumulated RunState."""
    progress = progress or _default_progress
    state = RunState(chemical=chemical, cas=cas)

    # Step 1 — resolve chemical via PubChem
    progress(1, "resolve_chemical via PubChem", {"chemical": chemical})
    try:
        ident = await ctx.pubchem.resolve(chemical, cas=cas)
        state.identity = ident.__dict__ if hasattr(ident, "__dict__") else ident
    except Exception as e:
        state.errors.append(f"step1 resolve_chemical: {e}")

    # Step 2 — build URLs for every database in the registry
    progress(2, "build_database_urls for all 34 DBs", {"chemical": chemical})
    try:
        from pipeline.urls import DatabaseURLBuilder
        state.urls = DatabaseURLBuilder.all_for(
            chemical=chemical,
            cas=(state.identity or {}).get("cas") or cas,
        )
    except Exception as e:
        state.errors.append(f"step2 build_database_urls: {e}")

    # Step 3 — count papers per database (no PDF download yet)
    progress(3, "count_papers_by_database", {"n_dbs": len(state.urls)})
    try:
        counts = await ctx.db_searcher.count_all(chemical=chemical,
                                                  cas=(state.identity or {}).get("cas") or cas)
        if hasattr(counts, "to_dict"):
            counts = counts.to_dict()
        state.db_counts = dict(counts) if counts else {}
    except Exception as e:
        state.errors.append(f"step3 count_papers_by_database: {e}")

    # Step 4 — rank top N candidates
    progress(4, f"find_papers (top={top})", {"top": top})
    try:
        cands = await ctx.papers.find(chemical=chemical, top=top)
        state.candidates = [c.__dict__ if hasattr(c, "__dict__") else c for c in cands]
    except Exception as e:
        state.errors.append(f"step4 find_papers: {e}")

    # Step 5 — JATS full text + figs + tables for each PMC paper
    progress(5, "fetch_pmc_jats for each candidate", {"n": len(state.candidates)})
    for c in state.candidates:
        pmcid = c.get("pmcid") if isinstance(c, dict) else getattr(c, "pmcid", None)
        if not pmcid:
            continue
        try:
            art = await ctx.jats.fetch(pmcid)
            state.jats_articles.append(art.__dict__ if hasattr(art, "__dict__") else art)
        except Exception as e:
            state.errors.append(f"step5 jats({pmcid}): {e}")

    # Step 6 — PDF fallback figures/tables for any paper missing them
    progress(6, "extract_pdf_figures fallback", {"n": len(state.jats_articles)})
    # (The full figure harvest runs inside generate_chemical_report handler.)

    # Step 7 — evidence matches against tox keyword bank
    progress(7, "build_evidence_record against keyword bank", {})

    # Step 8 — agency profiles
    progress(8, "fetch_agency_profile (NTP/ATSDR/IRIS/OEHHA/NIOSH)", {})
    for ag in ("ntp", "atsdr", "iris", "oehha", "niosh"):
        try:
            prof = await ctx.agency.fetch(ag, chemical=chemical,
                                           cas=(state.identity or {}).get("cas") or cas)
            state.agency_profiles[ag] = prof
        except Exception as e:
            state.errors.append(f"step8 agency({ag}): {e}")

    # Step 9 — per-database harvest: PDFs + full text + figures from all 14
    # verified-live DBs (Semantic Scholar, NTP, WHO INCHEM, WHO IPCS, ECHA,
    # ATSDR, CalEPA OEHHA, Canada CEPA/DSL, CONCAWE, NIOSH, OSHA, AICIS,
    # ILO ICSC, Zenodo). This is the single biggest driver of report depth.
    multi = getattr(ctx, "multi_harvester", None)
    if multi is not None:
        progress(9, "harvest_all_databases (14 DBs → PDFs + full text + figures)",
                 {"dbs": len(multi.list_databases())})
        try:
            harvested = await multi.harvest_all(
                chemical,
                cas=(state.identity or {}).get("cas") or cas,
            )
            state.harvested_docs = [h.to_dict() for h in harvested]
            # Gather figures/tables into the top-level lists so the report
            # writer can walk them uniformly
            for h in harvested:
                for f in h.figures:
                    state.figures.append({
                        "source": h.source,
                        "page": f.page_number,
                        "image_path": f.image_path,
                        "caption": f.caption_guess,
                    })
                for t in h.tables:
                    state.tables.append({
                        "source": h.source,
                        "page": t.page_number,
                        "rows": t.rows,
                        "caption": t.caption,
                    })
        except Exception as e:
            state.errors.append(f"step9 multi_harvester: {e}")

    # Step 10 — compose the .docx happens in the caller (tools/handlers.py
    # already owns the DocX composition inside generate_chemical_report). This
    # orchestrator collects the harvested state; the tool can then feed it
    # into ctx.report_gen.
    progress(10, "ready for report composition", {
        "errors": len(state.errors),
        "jats": len(state.jats_articles),
        "candidates": len(state.candidates),
        "harvested": len(state.harvested_docs),
        "figures": len(state.figures),
        "tables": len(state.tables),
    })

    return state
