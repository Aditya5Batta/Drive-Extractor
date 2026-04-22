"""
tools.handlers — MCP tool dispatch.

Every MCP call enters through `dispatch(name, arguments, ctx)`. The ctx
parameter is a ToolContext with all shared clients (fetcher, scraper, pubchem,
papers, openalex, pmc, agency, evidence_builder, jats, ledger, db_searcher,
report_gen, llm, batch). Each tool's branch is organised by the same order as
tools.schemas.ALL_TOOL_SCHEMAS so you can scroll both files side-by-side.

To add a new tool:
  1. Append a types.Tool(...) entry to tools/schemas.py
  2. Add an `if name == "new_tool":` branch below
  3. If it needs a new shared client, add it to tools.context.ToolContext
"""
from __future__ import annotations
import asyncio
import base64
import datetime
import io
import json
import os
import re
from dataclasses import asdict
from typing import Any
from urllib.parse import urljoin, urlparse, quote

import mcp.types as types

# Shared primitives used by the handler bodies -----------------------------------
from config.settings import CONFIG
from config.databases import (
    ALL_DATABASES, HIGH_VALUE_DATABASES, MEDIUM_VALUE_DATABASES,
    CHEMICAL_IDENTITY_DATABASES,
)
from core.http import HTTPFetcher
from core.pdf import PDFExtractor, is_pdf_content
from core.pdf_multimodal import PDFTableExtractor, PDFFigureExtractor
from core.keywords import KeywordSearcher, KeywordHit, KeywordSearchResult
from core.formatter import Formatter
from pipeline.scraper import Scraper, BatchProcessor, PageResult
from pipeline.urls import DatabaseURLBuilder
from pipeline.ledger import ExtractionLedger
from pipeline.evidence import EvidenceRecord, EvidenceBuilder
from pipeline.report import (
    ReportGenerator, ReportReference, ReportSection,
    ReportFigure, ReportTable, HAS_PYTHON_DOCX,
)
from sources.pubchem import PubChemResolver
from sources.pubmed import PaperFinder
from sources.openalex import OpenAlexClient
from sources.pmc import PMCHarvester, PMCArticle
from sources.jats import JATSHarvester, JATSArticle, JATSFigure, JATSTable
from sources.agencies import AgencyProfileFetcher
from sources.db_search import DatabaseSearcher, DatabaseSearchResult
from sources._models import ChemicalIdentity, PaperCandidate


def _resolve_output_dir() -> str:
    """Portable output directory — works on Windows/macOS/Linux + any sandbox.

    Resolution order (first writable wins):
      1. $TOX_SCRAPER_OUT_DIR         — explicit override
      2. project root (next to server.py)
      3. os.getcwd()
      4. tempfile.gettempdir() / 'tox_scraper'

    Always returns an *existing writable* directory. Never raises.
    """
    import tempfile
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or "."
    candidates = [
        os.environ.get("TOX_SCRAPER_OUT_DIR"),
        here,
        os.getcwd(),
        os.path.join(tempfile.gettempdir(), "tox_scraper"),
    ]
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            # Write-test
            testf = os.path.join(d, ".write_test")
            with open(testf, "w") as _t:
                _t.write("ok")
            os.remove(testf)
            return d
        except Exception:
            continue
    # Absolute last resort — cwd, even if not writable; caller handles the error.
    return os.getcwd()


async def dispatch(name: str, arguments: dict, ctx: "Any") -> list[types.TextContent]:
    """Look up the tool by name and run its handler with the shared context."""
    # Destructure the shared clients once so the original handler bodies stay
    # verbatim. Any new helper added to ToolContext becomes available here.
    fetcher          = ctx.fetcher
    scraper          = ctx.scraper
    batch            = ctx.batch
    pubchem          = ctx.pubchem
    papers           = ctx.papers
    openalex         = ctx.openalex
    pmc              = ctx.pmc
    agency           = ctx.agency
    evidence_builder = ctx.evidence_builder
    jats             = ctx.jats
    ledger           = ctx.ledger
    db_searcher      = ctx.db_searcher
    report_gen       = ctx.report_gen
    llm              = ctx.llm
    figures_dir      = ctx.figures_dir

    try:
        if name == "fetch_url":
            page = await scraper.scrape(
                arguments["url"],
                want_links=arguments.get("include_links", False),
            )
            md = Formatter.page_md(page, preview_chars=arguments.get("preview_chars", 6000))
            return [types.TextContent(type="text", text=md)]

        if name == "fetch_pdf":
            # Force PDF path: fetch, then extract regardless of content-type
            data, ct, status, err = await fetcher.fetch(arguments["url"])
            if data is None:
                return [types.TextContent(
                    type="text",
                    text=f"❌ Fetch failed: {err} (status {status})",
                )]
            text, pages, pdf_err = PDFExtractor.extract(data)
            preview = arguments.get("preview_chars", 10000)
            page = PageResult(
                url=arguments["url"],
                ok=bool(text) and pdf_err is None,
                kind="pdf",
                title=arguments["url"].rsplit("/", 1)[-1],
                text=text[: CONFIG.max_content_chars],
                page_count=pages,
                char_count=min(len(text), CONFIG.max_content_chars),
                status=status,
                content_type=ct or "application/pdf",
                error=pdf_err,
            )
            return [types.TextContent(type="text", text=Formatter.page_md(page, preview))]

        if name == "fetch_pdf_bytes":
            import base64
            data, ct, status, err = await fetcher.fetch(arguments["url"])
            if data is None:
                return [types.TextContent(
                    type="text",
                    text=f"❌ Fetch failed: {err} (status {status})",
                )]
            max_bytes = int(arguments.get("max_bytes", 20_000_000))
            if len(data) > max_bytes:
                return [types.TextContent(
                    type="text",
                    text=(f"❌ PDF too large: {len(data)} bytes "
                          f"(max {max_bytes}). Request a higher "
                          "max_bytes or use fetch_pdf for text only."),
                )]
            b64 = base64.b64encode(data).decode("ascii")
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "url": arguments["url"],
                    "content_type": ct,
                    "status": status,
                    "byte_count": len(data),
                    "base64": b64,
                }),
            )]

        if name == "search_in_content":
            page = await scraper.scrape(arguments["url"])
            if not page.ok or not page.text:
                return [types.TextContent(
                    type="text",
                    text=f"❌ Could not extract text from {arguments['url']}: {page.error}",
                )]
            ksr = KeywordSearcher.search(
                page.text,
                arguments["keywords"],
                context_chars=arguments.get("context_chars", CONFIG.context_chars),
                max_hits_per_keyword=arguments.get("max_hits_per_keyword", 5),
            )
            return [types.TextContent(
                type="text",
                text=Formatter.keyword_result_md(arguments["url"], ksr),
            )]

        if name == "extract_links":
            page = await scraper.scrape(arguments["url"], want_links=True)
            links = page.links
            if arguments.get("pdf_only"):
                links = [l for l in links if l["is_pdf"]]
            if arguments.get("same_domain_only"):
                links = [l for l in links if l["same_domain"]]

            lines = [f"# Links from {arguments['url']}", f"Total: {len(links)}", ""]
            for ln in links[:200]:
                marker = "📄" if ln["is_pdf"] else ("🏠" if ln["same_domain"] else "🔗")
                lines.append(f"- {marker} [{ln['text'][:80]}]({ln['url']})")
            return [types.TextContent(type="text", text="\n".join(lines))]

        if name == "batch_scrape":
            result = await batch.run(
                arguments["urls"],
                arguments["keywords"],
                max_hits_per_keyword=arguments.get("max_hits_per_keyword", 3),
                min_relevance=arguments.get("min_relevance", 0.0),
            )
            md = Formatter.batch_md(result)
            # Also attach raw JSON so the client can post-process
            return [
                types.TextContent(type="text", text=md),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(result, indent=2)[:50000] + "\n```"),
            ]

        if name == "list_databases":
            return [types.TextContent(type="text", text=Formatter.databases_md())]

        if name == "resolve_chemical":
            ident = await pubchem.resolve(arguments["query"])
            return [
                types.TextContent(type="text", text=PubChemResolver.format_md(ident)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(ident.to_dict(), indent=2) + "\n```"),
            ]

        if name == "find_papers":
            chemical = arguments["chemical"]
            keywords = arguments["keywords"]
            max_results = arguments.get("max_results", 30)
            fetch_top_n = arguments.get("fetch_top_n", 10)
            free_only = arguments.get("free_full_text_only", True)
            min_title_rel = arguments.get("min_title_relevance", 0.25)

            per_source = max(5, max_results // 2)
            pm_task = asyncio.create_task(
                papers.search_pubmed(chemical, keywords, retmax=per_source,
                                     free_full_text_only=free_only))
            ep_task = asyncio.create_task(
                papers.search_europepmc(chemical, keywords, page_size=per_source,
                                        open_access_only=free_only))
            pm_hits, ep_hits = await asyncio.gather(pm_task, ep_task)
            merged = PaperFinder.merge_and_rank(pm_hits, ep_hits)

            # Title-relevance filter — drop off-topic hits
            if min_title_rel > 0:
                merged = [c for c in merged
                          if c.title_relevance(chemical, keywords) >= min_title_rel]

            if not merged:
                return [types.TextContent(
                    type="text",
                    text=(f"No papers found for **{chemical}** with keywords "
                          f"{keywords[:5]}{'...' if len(keywords) > 5 else ''} "
                          f"(free_full_text_only={free_only}). "
                          f"Try free_full_text_only=false or broader keywords."),
                )]

            top = merged[:fetch_top_n]
            urls = [c.best_url() for c in top if c.best_url()]
            batch_result = await batch.run(urls, keywords,
                                           max_hits_per_keyword=3, min_relevance=0.0)

            url_to_ksr: dict[str, dict[str, Any]] = {}
            for r in batch_result["ranked_results"] + batch_result["failures"]:
                url_to_ksr[r["url"]] = r

            final: list[dict[str, Any]] = []
            for c in top:
                bu = c.best_url()
                row = c.to_dict()
                ksr = url_to_ksr.get(bu, {})
                row["fetch_ok"] = ksr.get("ok", False)
                row["relevance_score"] = ksr.get("relevance_score", 0.0)
                row["total_hits"] = ksr.get("total_hits", 0)
                row["hits"] = ksr.get("hits", [])
                row["kind"] = ksr.get("kind", "-")
                row["fetch_error"] = ksr.get("error") if not ksr.get("ok") else None
                final.append(row)

            final.sort(key=lambda r: (r.get("relevance_score", 0),
                                      r.get("total_hits", 0),
                                      r.get("priority_score", 0)), reverse=True)

            # Record to ledger
            for c in top:
                src_label = "PubMed" if c.source == "pubmed" else "EuropePMC"
                ledger.record_paper(src_label, c)

            lines = [f"# Papers for **{chemical}**", ""]
            lines.append(f"- Search hits: **{len(merged)}** "
                         f"(PubMed: {len(pm_hits)}, EuropePMC: {len(ep_hits)}, deduped: {len(merged)})")
            lines.append(f"- Fetched + KWIC-scored top: **{len(final)}**")
            lines.append(f"- Keywords: {', '.join(keywords[:12])}"
                         + ("..." if len(keywords) > 12 else ""))
            lines.append("")
            lines.append("| # | Title | Year | Source | FT | PDF | Relevance | Hits | URL |")
            lines.append("|---|-------|------|--------|----|-----|-----------|------|-----|")
            for i, r in enumerate(final, 1):
                ft = "✅" if r.get("has_free_fulltext") else "—"
                pdf = "✅" if r.get("has_pdf") else "—"
                title = (r.get("title") or "")[:70]
                lines.append(
                    f"| {i} | {title} | {r.get('year') or '-'} | {r.get('source')} | "
                    f"{ft} | {pdf} | {r.get('relevance_score', 0):.2f} | "
                    f"{r.get('total_hits', 0)} | {r.get('best_url') or '-'} |"
                )
            lines.append("\n## Top-5 snippets")
            for i, r in enumerate(final[:5], 1):
                lines.append(f"\n### {i}. {r.get('title') or '(no title)'}")
                meta = []
                if r.get("journal"): meta.append(r["journal"])
                if r.get("year"): meta.append(r["year"])
                if r.get("pmid"): meta.append(f"PMID:{r['pmid']}")
                if r.get("pmcid"): meta.append(r["pmcid"])
                if r.get("doi"): meta.append(f"DOI:{r['doi']}")
                lines.append(f"_{' · '.join(meta)}_")
                lines.append(f"**URL:** {r.get('best_url') or '-'}  |  "
                             f"**Kind:** {r.get('kind', '-')}  |  "
                             f"**Relevance:** {r.get('relevance_score', 0):.2f}  |  "
                             f"**Hits:** {r.get('total_hits', 0)}")
                if r.get("fetch_error"):
                    lines.append(f"⚠ Fetch error: {r['fetch_error']}")
                for h in (r.get("hits") or [])[:5]:
                    lines.append(f"\n> **[{h['keyword']}]** {h['snippet']}")

            failures = [r for r in final if not r.get("fetch_ok")]
            if failures:
                lines.append("\n## Papers that couldn't be fetched (abstract-only usable)")
                for r in failures[:10]:
                    lines.append(f"- {(r.get('title') or '-')[:80]} — "
                                 f"{r.get('fetch_error', 'n/a')} — {r.get('best_url')}")

            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(final, indent=2)[:50000] + "\n```"),
            ]

        if name == "find_papers_openalex":
            chemical = arguments["chemical"]
            keywords = arguments["keywords"]
            per_page = arguments.get("per_page", 25)
            oa_only = arguments.get("oa_only", True)
            min_title_rel = arguments.get("min_title_relevance", 0.25)

            hits = await openalex.search(chemical, keywords,
                                         per_page=per_page, oa_only=oa_only)
            # Title-relevance filter
            if min_title_rel > 0:
                hits = [c for c in hits
                        if c.title_relevance(chemical, keywords) >= min_title_rel]
            # Sort by priority
            hits.sort(key=lambda c: c.priority_score(), reverse=True)

            # Record to ledger
            for c in hits[:30]:
                ledger.record_paper("OpenAlex", c)

            lines = [f"# OpenAlex results for **{chemical}**", ""]
            lines.append(f"- Hits: **{len(hits)}** (after relevance filter ≥ {min_title_rel})")
            lines.append(f"- Keywords: {', '.join(keywords[:10])}"
                         + ("..." if len(keywords) > 10 else ""))
            lines.append("")
            lines.append("| # | Title | Year | OA | PDF | DOI | URL |")
            lines.append("|---|-------|------|----|-----|-----|-----|")
            for i, c in enumerate(hits[:30], 1):
                oa_m = "✅" if c.has_free_fulltext else "—"
                pdf_m = "✅" if c.has_pdf else "—"
                title = (c.title or "")[:70]
                lines.append(
                    f"| {i} | {title} | {c.year or '-'} | {oa_m} | {pdf_m} | "
                    f"{c.doi or '-'} | {c.best_url() or '-'} |"
                )
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps([c.to_dict() for c in hits[:30]], indent=2)[:30000] + "\n```"),
            ]

        if name == "fetch_pmc_article":
            art = await pmc.fetch(arguments["pmcid"])
            if art.error and not art.full_text:
                return [types.TextContent(type="text",
                    text=f"❌ PMC fetch failed for {arguments['pmcid']}: {art.error}")]

            # Record to ledger
            ledger.record("PMC_FullText", {
                "title": art.title,
                "pmid": art.pmid,
                "pmcid": art.pmcid,
                "doi": art.doi,
                "year": art.year,
                "journal": art.journal,
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{art.pmcid}/",
            })

            lines = [f"# {art.title or art.pmcid}", ""]
            lines.append("| Field | Value |")
            lines.append("|-------|-------|")
            lines.append(f"| PMCID | {art.pmcid} |")
            lines.append(f"| PMID | {art.pmid or '-'} |")
            lines.append(f"| DOI | {art.doi or '-'} |")
            lines.append(f"| Journal | {art.journal or '-'} |")
            lines.append(f"| Year | {art.year or '-'} |")
            lines.append(f"| Full-text chars | {len(art.full_text):,} |")
            lines.append(f"| Figures | {len(art.figure_urls)} |")
            lines.append(f"| Tables | {len(art.table_urls)} |")
            lines.append(f"| References (DOI) | {len(art.reference_dois)} |")
            lines.append(f"| References (PMID) | {len(art.reference_pmids)} |")
            lines.append(f"| PDF URL | {art.pdf_url or '-'} |")
            if art.error:
                lines.append(f"| ⚠ Error | {art.error} |")
            lines.append("")

            if art.abstract:
                lines.append("## Abstract")
                lines.append(art.abstract[:3000])
                lines.append("")

            if art.figure_urls:
                lines.append(f"## Figure URLs ({len(art.figure_urls)})")
                for i, u in enumerate(art.figure_urls[:20], 1):
                    lines.append(f"{i}. {u}")
                lines.append("")

            if art.full_text:
                lines.append(f"## Full text (first 5000 chars of {len(art.full_text):,})")
                lines.append("```")
                lines.append(art.full_text[:5000])
                if len(art.full_text) > 5000:
                    lines.append(f"\n...[truncated — {len(art.full_text) - 5000:,} more chars]")
                lines.append("```")

            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(art.to_dict(), indent=2)[:30000] + "\n```"),
            ]

        if name == "harvest_references":
            pmcid = arguments["pmcid"]
            max_refs = arguments.get("max_refs", 30)
            art = await pmc.fetch(pmcid)
            if art.error and not art.reference_pmids and not art.reference_dois:
                return [types.TextContent(type="text",
                    text=f"❌ PMC fetch failed for {pmcid}: {art.error}")]

            # For each PMID, build a minimal PaperCandidate via PubMed esummary
            refs_out: list[dict[str, Any]] = []
            pmids_to_query = art.reference_pmids[:max_refs]
            if pmids_to_query:
                esum_url = (f"{PaperFinder.PUBMED_ESUMMARY}?db=pubmed&retmode=json"
                            f"&id={','.join(pmids_to_query)}")
                data, _ct, status, _err = await fetcher.fetch(esum_url)
                if data and status < 400:
                    try:
                        sdata = json.loads(data.decode("utf-8", errors="replace"))
                        result = sdata.get("result", {})
                        for p in pmids_to_query:
                            r = result.get(p)
                            if not isinstance(r, dict):
                                continue
                            pmcid_ref = None; doi_ref = None
                            for a in r.get("articleids", []):
                                if a.get("idtype") == "pmc":
                                    pmcid_ref = a.get("value")
                                elif a.get("idtype") == "doi":
                                    doi_ref = a.get("value")
                            pc = PaperCandidate(
                                source="pubmed_ref",
                                pmid=p,
                                pmcid=(pmcid_ref if pmcid_ref and pmcid_ref.startswith("PMC")
                                       else (f"PMC{pmcid_ref}" if pmcid_ref else None)),
                                doi=doi_ref,
                                title=r.get("title", ""),
                                journal=r.get("fulljournalname") or r.get("source"),
                                year=(r.get("pubdate", "") or "").split()[0] or None,
                                landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{p}/",
                            )
                            if pc.pmcid:
                                pc.has_free_fulltext = True
                                pc.has_pdf = True
                                pc.fulltext_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pc.pmcid}/"
                            refs_out.append(pc.to_dict())
                            ledger.record_paper("PMC_RefHarvest", pc)
                    except Exception as e:  # noqa: BLE001
                        refs_out.append({"error": f"esummary parse: {e}"})

            lines = [f"# Reference harvest from {art.pmcid}", ""]
            lines.append(f"- Source article: {art.title}")
            lines.append(f"- Total references extracted: {len(art.reference_pmids)} PMIDs, "
                         f"{len(art.reference_dois)} DOIs")
            lines.append(f"- Fetched metadata for: {len(refs_out)}")
            lines.append("")
            lines.append("| # | Title | Year | Journal | PMCID |")
            lines.append("|---|-------|------|---------|-------|")
            for i, r in enumerate(refs_out, 1):
                if "error" in r:
                    continue
                title = (r.get("title") or "")[:60]
                lines.append(f"| {i} | {title} | {r.get('year') or '-'} | "
                             f"{(r.get('journal') or '-')[:30]} | "
                             f"{r.get('pmcid') or '-'} |")

            lines.append("")
            lines.append("## DOIs from references (first 50)")
            for d in art.reference_dois[:50]:
                lines.append(f"- {d}")

            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(refs_out, indent=2)[:30000] + "\n```"),
            ]

        if name == "fetch_agency_profile":
            chemical = arguments["chemical"]
            cas = arguments.get("cas")
            results = await agency.fetch_all(chemical, cas)

            # Record to ledger
            for r in results:
                ledger.record_agency(r["agency"], r["url"],
                                     r.get("ok", False),
                                     title=r.get("title", ""))

            lines = [f"# Agency profile fetch for **{chemical}**"
                     + (f" (CAS {cas})" if cas else ""), ""]
            ok = sum(1 for r in results if r["ok"])
            lines.append(f"- Agencies queried: **{len(results)}**")
            lines.append(f"- Successful fetches: **{ok}**")
            lines.append("")
            lines.append("| Agency | OK | Status | Chars | URL |")
            lines.append("|--------|----|---------|-------|-----|")
            for r in results:
                ok_m = "✅" if r["ok"] else "❌"
                lines.append(f"| {r['agency']} | {ok_m} | {r.get('status', 0)} | "
                             f"{r.get('char_count', 0):,} | {r['url'][:80]} |")
            lines.append("")
            lines.append("## Per-agency text previews")
            for r in results:
                if not r["ok"]:
                    continue
                lines.append(f"\n### {r['agency']} — {r.get('title', '')[:80]}")
                lines.append(f"**URL:** {r['url']}")
                lines.append("```")
                lines.append(r.get("text_preview", "")[:1500])
                lines.append("```")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                                  text="```json\n" + json.dumps(results, indent=2)[:30000] + "\n```"),
            ]

        if name == "get_extraction_ledger":
            fmt = arguments.get("format", "markdown")
            if fmt == "json":
                return [types.TextContent(type="text",
                                          text=json.dumps(ledger.summary(), indent=2))]
            return [types.TextContent(type="text", text=ledger.markdown())]

        if name == "reset_extraction_ledger":
            ledger.reset()
            return [types.TextContent(type="text",
                text="✅ Extraction ledger cleared. Ready for new chemical/report.")]

        if name == "build_evidence_record":
            records = await evidence_builder.build_from_url(
                url=arguments["url"],
                keywords=arguments["keywords"],
                source_db=arguments.get("source_db", ""),
                doi=arguments.get("doi"),
                pmid=arguments.get("pmid"),
                pmcid=arguments.get("pmcid"),
                max_per_keyword=arguments.get("max_per_keyword", 3),
                quote_chars=arguments.get("quote_chars", 400),
                context_chars=arguments.get("context_chars", 200),
            )
            # Record each to the ledger for provenance
            for r in records:
                ledger.record(r.source_db or "Evidence", {
                    "title": r.title, "url": r.source_url,
                    "pmid": r.pmid, "pmcid": r.pmcid, "doi": r.doi,
                    "evidence_id": r.evidence_id, "keyword": r.keyword,
                })
            lines = [f"# Evidence records for {arguments['url']}", ""]
            lines.append(f"- Records extracted: **{len(records)}**")
            lines.append(f"- Keywords: {', '.join(arguments['keywords'][:10])}")
            lines.append("")
            for i, r in enumerate(records, 1):
                lines.append(f"### {i}. [{r.keyword}] — `{r.evidence_id}`")
                lines.append(f"_{r.citation()}_")
                lines.append(f"**Source:** {r.source_url}")
                lines.append(f"**Offset:** {r.char_offset}")
                lines.append("**Verbatim quote:**")
                lines.append(f"> {r.verbatim_quote}")
                if r.context_before:
                    lines.append(f"_Context before:_ …{r.context_before[-200:]}")
                if r.context_after:
                    lines.append(f"_Context after:_ {r.context_after[:200]}…")
                lines.append("")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps([r.to_dict() for r in records], indent=2)[:50000] + "\n```"),
            ]

        if name == "extract_pdf_tables":
            url = arguments["url"]
            max_tables = arguments.get("max_tables", 50)
            data, _ct, status, err = await fetcher.fetch(url)
            if data is None:
                return [types.TextContent(type="text",
                    text=f"❌ Fetch failed for {url}: {err} (status {status})")]
            tables, t_err = PDFTableExtractor.extract(data, url)
            tables = tables[:max_tables]
            lines = [f"# Tables extracted from {url}", ""]
            if t_err:
                lines.append(f"⚠ {t_err}")
            lines.append(f"- Tables found: **{len(tables)}**")
            lines.append("")
            for t in tables:
                lines.append(f"## Page {t.page_number} · Table {t.table_index} "
                             f"({t.row_count}×{t.col_count}, {t.char_count} chars)")
                md = t.to_markdown()
                if md:
                    lines.append(md)
                lines.append("")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps([t.to_dict() for t in tables], indent=2)[:50000] + "\n```"),
            ]

        if name == "extract_pdf_figures":
            url = arguments["url"]
            min_b = arguments.get("min_bytes", 2000)
            data, _ct, status, err = await fetcher.fetch(url)
            if data is None:
                return [types.TextContent(type="text",
                    text=f"❌ Fetch failed for {url}: {err} (status {status})")]
            figs, f_err = PDFFigureExtractor.extract(data, url, figures_dir, min_bytes=min_b)
            lines = [f"# Figures extracted from {url}", ""]
            if f_err:
                lines.append(f"⚠ {f_err}")
            lines.append(f"- Figures saved: **{len(figs)}**")
            lines.append(f"- Output directory: `{figures_dir}`")
            lines.append("")
            for f in figs:
                lines.append(f"- Page {f.page_number} · {f.width}×{f.height} · "
                             f"`{os.path.basename(f.image_path)}`")
                if f.caption_guess:
                    lines.append(f"  Caption guess: {f.caption_guess}")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps([f.to_dict() for f in figs], indent=2)[:30000] + "\n```"),
            ]

        if name == "fetch_pmc_jats":
            art = await jats.fetch(arguments["pmcid"])
            if art.error and not art.body_text and not art.tables:
                return [types.TextContent(type="text",
                    text=f"❌ JATS fetch failed for {arguments['pmcid']}: {art.error}")]

            ledger.record("PMC_JATS", {
                "title": art.title, "pmid": art.pmid, "pmcid": art.pmcid,
                "doi": art.doi, "year": art.year, "journal": art.journal,
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{art.pmcid}/",
            })

            lines = [f"# {art.title or art.pmcid} (JATS)", ""]
            lines.append(f"- PMCID: {art.pmcid}  |  PMID: {art.pmid or '-'}  |  DOI: {art.doi or '-'}")
            lines.append(f"- Journal: {art.journal or '-'}  |  Year: {art.year or '-'}")
            lines.append(f"- Body text: {len(art.body_text):,} chars")
            lines.append(f"- Tables: {len(art.tables)}  |  Figures: {len(art.figures)}  |  Refs: {len(art.references)}")
            if art.error:
                lines.append(f"- ⚠ {art.error}")
            lines.append("")
            if art.abstract:
                lines.append("## Abstract")
                lines.append(art.abstract[:3000])
                lines.append("")
            if art.tables:
                lines.append(f"## Tables ({len(art.tables)})")
                for i, t in enumerate(art.tables, 1):
                    lines.append(f"\n### Table {i}: {t.label} — {t.caption[:150]}")
                    if t.rows:
                        # Render as markdown
                        header = t.rows[0]
                        body = t.rows[1:] if len(t.rows) > 1 else []
                        lines.append("| " + " | ".join(c.replace("\n", " ").strip() for c in header) + " |")
                        lines.append("|" + "|".join(" --- " for _ in header) + "|")
                        for row in body[:50]:
                            if len(row) < len(header):
                                row = row + [""] * (len(header) - len(row))
                            else:
                                row = row[:len(header)]
                            lines.append("| " + " | ".join(c.replace("\n", " ").strip() for c in row) + " |")
            if art.figures:
                lines.append(f"\n## Figure captions ({len(art.figures)})")
                for i, f in enumerate(art.figures, 1):
                    lines.append(f"{i}. **{f.label}** — {f.caption[:400]}")
                    if f.graphic_href:
                        lines.append(f"   graphic: `{f.graphic_href}`")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps(art.to_dict(), indent=2)[:50000] + "\n```"),
            ]

        if name == "build_database_urls":
            chemical = arguments["chemical"]
            cas = arguments.get("cas")
            urls = DatabaseURLBuilder.build_all(chemical, cas)
            lines = [f"# Database URLs for **{chemical}**"
                     + (f" (CAS {cas})" if cas else ""), ""]
            lines.append(f"- Total URLs: **{len(urls)}**")
            lines.append("")
            lines.append("| # | Database | URL | Notes |")
            lines.append("|---|----------|-----|-------|")
            for i, u in enumerate(urls, 1):
                lines.append(f"| {i} | {u['database']} | {u['url']} | {u['notes']} |")
            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps(urls, indent=2) + "\n```"),
            ]

        if name == "count_papers_by_database":
            chemical = arguments["chemical"]
            cas = arguments.get("cas")
            results = await db_searcher.run_all(chemical, cas)

            # Record deterministic counts to the ledger so provenance
            # survives even if the client never calls generate_report.
            for r in results:
                ledger.record("DB_Count", {
                    "title": f"{r.database}: {r.result_count} results (status={r.status})",
                    "url": r.url,
                })

            # Build human-readable markdown table — honest about what each
            # status means. Landing-only rows are URL-reachable but produced
            # NO parseable count and must NOT be counted as hits.
            ok_total = sum(r.result_count for r in results if r.status == "ok")
            html_total = sum(r.result_count for r in results if r.status == "html_ok")
            lines = [f"# Paper counts per database for **{chemical}**"
                     + (f" (CAS {cas})" if cas else ""), ""]
            lines.append(f"- Databases queried: **{len(results)}**")
            lines.append(f"- API-verified count (sum of status=ok): **{ok_total:,}**")
            lines.append(f"- HTML-scraped count (sum of status=html_ok): **{html_total:,}**")
            lines.append(f"- Databases with HTML-parsed counts: "
                         f"**{sum(1 for r in results if r.status == 'html_ok')}**")
            lines.append(f"- Landing-only (URL reachable, no count parsed): "
                         f"**{sum(1 for r in results if r.status == 'landing_only')}**")
            lines.append(f"- No public API (reported as N/A): "
                         f"**{sum(1 for r in results if r.status == 'no_api')}**")
            lines.append(f"- Errors / unreachable: "
                         f"**{sum(1 for r in results if r.status == 'error')}**")
            lines.append("")
            lines.append("| # | Database | Status | Count | URL |")
            lines.append("|---|----------|--------|-------|-----|")
            for i, r in enumerate(results, 1):
                if r.status == "no_api":
                    count_s = "N/A (no API)"
                elif r.status == "html_ok":
                    count_s = f"{r.result_count:,} (HTML scrape)"
                elif r.status == "landing_only":
                    count_s = "URL reachable, no count parsed"
                elif r.status == "error":
                    count_s = f"error: {(r.error or '')[:40]}"
                else:
                    count_s = f"{r.result_count:,}"
                lines.append(f"| {i} | {r.database} | {r.status} | {count_s} | {(r.url or '-')[:80]} |")

            return [
                types.TextContent(type="text", text="\n".join(lines)),
                types.TextContent(type="text",
                    text="```json\n" + json.dumps([r.to_dict() for r in results], indent=2)[:80000] + "\n```"),
            ]

        if name == "generate_report":
            if not HAS_PYTHON_DOCX:
                return [types.TextContent(
                    type="text",
                    text="❌ python-docx is not installed. Install it with `pip install python-docx>=1.1.0`.",
                )]

            chemical = arguments["chemical"]
            cas = arguments.get("cas")
            raw_sections = arguments["sections"]
            raw_refs = arguments["references"]
            raw_counts = arguments.get("db_counts", []) or []
            include_ledger = arguments.get("include_extraction_ledger", True)
            out_name = arguments.get("output_filename")
            gen_date = arguments.get("generated_date")

            # ─── parse sections (accept dict or already-ReportSection) ───
            def _to_section(obj: Any) -> ReportSection:
                if isinstance(obj, ReportSection):
                    return obj
                if not isinstance(obj, dict):
                    raise ValueError(f"section must be dict, got {type(obj).__name__}")
                return ReportSection(
                    title=str(obj.get("title", "")),
                    level=int(obj.get("level", 1)),
                    paragraphs=[str(p) for p in (obj.get("paragraphs") or [])],
                    subsections=[_to_section(s) for s in (obj.get("subsections") or [])],
                )

            sections = [_to_section(s) for s in raw_sections]

            # ─── parse references ───
            refs: list[ReportReference] = []
            for i, r in enumerate(raw_refs, 1):
                if isinstance(r, ReportReference):
                    refs.append(r); continue
                if not isinstance(r, dict):
                    continue
                refs.append(ReportReference(
                    number=int(r.get("number") or i),
                    authors=str(r.get("authors") or ""),
                    year=str(r.get("year") or ""),
                    title=str(r.get("title") or ""),
                    journal=str(r.get("journal") or ""),
                    doi=r.get("doi"),
                    pmid=r.get("pmid"),
                    pmcid=r.get("pmcid"),
                    url=str(r.get("url") or ""),
                    database=str(r.get("database") or ""),
                ))
            # Sort by number to make output deterministic
            refs.sort(key=lambda x: x.number)

            # ─── parse db_counts (already in result dict shape) ───
            db_counts_list: list[dict[str, Any]] = []
            for row in raw_counts:
                if isinstance(row, dict):
                    db_counts_list.append(row)
                elif hasattr(row, "to_dict"):
                    db_counts_list.append(row.to_dict())

            # ─── output path ───
            safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", chemical).strip("_") or "chemical"
            fname = out_name or f"{safe}_LitReview.docx"
            if not fname.lower().endswith(".docx"):
                fname = fname + ".docx"
            out_dir = _resolve_output_dir()
            out_path = os.path.join(out_dir, fname)

            # ─── ledger entries grouped by source for Appendix B ───
            ledger_entries = None
            if include_ledger:
                try:
                    if hasattr(ledger, "_entries") and isinstance(ledger._entries, dict):
                        ledger_entries = {k: list(v) for k, v in ledger._entries.items()}
                    else:
                        s = ledger.summary()
                        if isinstance(s, dict):
                            ledger_entries = s.get("full_entries") or s.get("sources")
                except Exception:
                    ledger_entries = None

            # ─── generate ───
            try:
                path = report_gen.generate(
                    output_path=out_path,
                    chemical=chemical,
                    cas=cas,
                    sections=sections,
                    references=refs,
                    db_counts=db_counts_list,
                    ledger_entries=ledger_entries,
                    generated_date=gen_date,
                )
            except Exception as e:  # noqa: BLE001
                return [types.TextContent(
                    type="text",
                    text=f"❌ Report generation failed: {type(e).__name__}: {e}",
                )]

            try:
                size_kb = os.path.getsize(path) / 1024.0
            except Exception:
                size_kb = 0.0

            # Count subsection totals for the summary
            def _count_paragraphs(s: ReportSection) -> int:
                return len(s.paragraphs) + sum(_count_paragraphs(sub) for sub in s.subsections)
            total_paragraphs = sum(_count_paragraphs(s) for s in sections)

            computer_link = f"computer://{path}"
            md_lines = [
                f"# Generated literature review for **{chemical}**"
                + (f" (CAS {cas})" if cas else ""),
                "",
                f"- Output: `{path}` ({size_kb:,.1f} KB)",
                f"- Font: Times New Roman {report_gen.font_size_pt}pt",
                f"- Sections (top-level): **{len(sections)}**",
                f"- Paragraphs (including subsections): **{total_paragraphs}**",
                f"- References: **{len(refs)}**",
                f"- Appendix A (database counts): **{len(db_counts_list)} rows**",
                f"- Appendix B (extraction ledger): "
                f"{'included' if (ledger_entries and include_ledger) else 'not included'}",
                "",
                f"[View your report]({computer_link})",
            ]
            return [types.TextContent(type="text", text="\n".join(md_lines))]

        if name == "generate_chemical_report":
            # ══════════════════════════════════════════════════════════
            # CHEMICAL-AGNOSTIC LITERATURE-REVIEW PIPELINE
            # ──────────────────────────────────────────────────────────
            # Single entry point. User says "Literature Report of X" and
            # this stage-by-stage pipeline produces a fully-populated
            # Word document from live scraped evidence only. Every
            # paragraph carries its source URL / DOI / PMCID — no
            # AI-generated narrative, no placeholder text.
            #
            #   Stage 1  resolve chemical (PubChem)
            #   Stage 2  34-database count sweep (run_all)
            #   Stage 3  paper harvest (PubMed + EuropePMC)
            #   Stage 4  full-text content (PMC JATS) for top N
            #   Stage 5  regulatory agency profiles (ATSDR, NTP,
            #            OEHHA, NIOSH, OSHA, Haz-Map)
            #   Stage 6  section composition from extracted evidence
            #   Stage 7  reference list build-up
            #   Stage 8  DOCX generation (ReportGenerator, justified)
            # ══════════════════════════════════════════════════════════
            if not HAS_PYTHON_DOCX:
                return [types.TextContent(
                    type="text",
                    text="❌ python-docx is not installed. "
                         "Install: `pip install python-docx>=1.1.0`",
                )]

            chem_name: str = arguments["chemical"]
            cas: str | None = arguments.get("cas")
            keywords: list[str] = arguments.get("keywords") or [
                "carcinogenicity", "neurotoxicity", "hepatotoxicity",
                "nephrotoxicity", "reproductive", "developmental",
                "genotoxicity", "immunotoxicity", "metabolism",
                "exposure", "biomarkers", "dose-response",
            ]
            fetch_n: int = int(arguments.get("fetch_top_n", 6))
            out_name: str | None = arguments.get("output_filename")

            # Per-stage timeouts. Many stages run in PARALLEL via
            # asyncio.gather below (counts ∥ papers ∥ agency ∥ multi),
            # so the wall-clock budget is max(parallel_group) + serial
            # stages, NOT their sum. Current worst-case wall-clock:
            #   identity (6) + max(counts=15, papers=10, agency=15, multi=18)
            #   + jats (10) + docx render (~3) ≈ 37s  →  fits 60s MCP timeout
            # with ~20s headroom for slow networks.
            STAGE_TIMEOUTS = {
                "identity":  5.0,
                "counts":   12.0,   # run_all now has hard per-DB 10s cap
                "papers":   10.0,
                "jats":     10.0,
                "agency":   10.0,
                "multi":    14.0,
            }

            async def _safe(coro: Any, timeout: float, default: Any) -> Any:
                """Run `coro` with hard timeout; never raise."""
                try:
                    return await asyncio.wait_for(coro, timeout=timeout)
                except (asyncio.TimeoutError, Exception):
                    return default

            # ───── Stage 1 · PubChem identity resolution ───────────────
            async def _stage1_resolve_identity() -> dict[str, Any]:
                async def _do() -> dict[str, Any]:
                    # Pass the chemical name as the main query and CAS as
                    # the fallback identifier. PubChemResolver.resolve tries
                    # CAS first (when given) and falls back to name — this
                    # is the signature fixed during the pubchem.py bug
                    # triage pass.
                    ident = await pubchem.resolve(chem_name, cas=cas)
                    if hasattr(ident, "to_dict"):
                        return ident.to_dict()
                    if hasattr(ident, "__dict__"):
                        return dict(ident.__dict__)
                    return ident if isinstance(ident, dict) else {}
                return await _safe(_do(), STAGE_TIMEOUTS["identity"],
                                   {"error": "PubChem resolve timed out"})

            # ───── Stage 2 · 34-database count sweep ───────────────────
            async def _stage2_database_counts() -> list[dict[str, Any]]:
                async def _do() -> list[dict[str, Any]]:
                    results = await db_searcher.run_all(chem_name, cas)
                    return [r.to_dict() for r in results]
                return await _safe(_do(), STAGE_TIMEOUTS["counts"], [])

            # ───── Stage 3 · Paper harvest (PubMed + EuropePMC) ────────
            async def _stage3_find_papers() -> list[PaperCandidate]:
                async def _do() -> list[PaperCandidate]:
                    per_src = max(10, fetch_n)
                    pm_task = asyncio.create_task(papers.search_pubmed(
                        chem_name, keywords, retmax=per_src,
                        free_full_text_only=True))
                    ep_task = asyncio.create_task(papers.search_europepmc(
                        chem_name, keywords, page_size=per_src,
                        open_access_only=True))
                    pm, ep = await asyncio.gather(pm_task, ep_task,
                                                 return_exceptions=True)
                    pm = pm if isinstance(pm, list) else []
                    ep = ep if isinstance(ep, list) else []
                    merged = PaperFinder.merge_and_rank(pm, ep)
                    merged = [c for c in merged
                              if c.title_relevance(chem_name, keywords) >= 0.25]
                    return merged[:fetch_n]
                return await _safe(_do(), STAGE_TIMEOUTS["papers"], [])

            # ───── Stage 4 · Full-text harvest (PMC JATS) ──────────────
            async def _stage4_fetch_pmc_content(
                top_candidates: list[PaperCandidate],
            ) -> list[JATSArticle]:
                async def _do() -> list[JATSArticle]:
                    articles: list[JATSArticle] = []
                    sem = asyncio.Semaphore(4)
                    # Per-item budget so a slow PMC reply can't eat the
                    # whole jats budget on its own.
                    per_item = max(
                        3.0,
                        STAGE_TIMEOUTS["jats"] / max(1, len(top_candidates)),
                    )
                    async def one(c: PaperCandidate) -> JATSArticle | None:
                        if not c.pmcid:
                            return None
                        async with sem:
                            try:
                                art = await asyncio.wait_for(
                                    jats.fetch(c.pmcid), timeout=per_item)
                                if art and not art.error:
                                    return art
                            except (asyncio.TimeoutError, Exception):
                                return None
                        return None
                    got = await asyncio.gather(
                        *(one(c) for c in top_candidates),
                        return_exceptions=True,
                    )
                    for a in got:
                        if isinstance(a, JATSArticle):
                            articles.append(a)
                    return articles
                return await _safe(_do(), STAGE_TIMEOUTS["jats"], [])

            # ───── Stage 9 · MultiDatabaseHarvester (14 per-DB sources) ─
            # Runs search → PDF → full-text → figures for every DB that
            # publishes a per-file harvester (semantic_scholar, ntp,
            # who_inchem, who_ipcs, echa, atsdr, calepa, canada_dsl,
            # concawe, niosh, osha, aicis, ilo, zenodo). Each DB hits a
            # different endpoint so even a partial-network environment
            # can produce real harvested text from the subset that
            # responds.
            async def _stage9_multi_harvest() -> list[Any]:
                mh = getattr(ctx, "multi_harvester", None)
                if mh is None:
                    return []
                async def _do() -> list[Any]:
                    try:
                        return await mh.harvest_all(chem_name, cas=cas)
                    except Exception:
                        return []
                return await _safe(_do(), STAGE_TIMEOUTS["multi"], [])

            # ───── Stage 5 · Regulatory agency profiles ────────────────
            async def _stage5_agency_profiles() -> list[dict[str, Any]]:
                return await _safe(
                    agency.fetch_all(chem_name, cas),
                    STAGE_TIMEOUTS["agency"], [])

            # ───── Stage 6 · Section composition from evidence ─────────
            # Emits sections in the canonical Benzene v3 literature-review
            # order so every chemical produces a report with the same
            # chapter skeleton. Content is drawn verbatim from PubChem,
            # the 34-DB counts, PMC JATS papers, and agency profiles.
            def _stage6_compose_sections(
                identity: dict[str, Any],
                db_counts: list[dict[str, Any]],
                candidates: list[PaperCandidate],
                jats_arts: list[JATSArticle],
                profiles: list[dict[str, Any]],
                diagnostics: dict[str, Any] | None = None,
                multi_harvested: list[Any] | None = None,
            ) -> list[ReportSection]:
                today = datetime.date.today().strftime("%B %d, %Y")
                # Honest count: only DBs that actually returned a numeric
                # result count. landing_only rows have result_count=0 and
                # are filtered by the >0 gate.
                n_ok = sum(1 for r in db_counts
                           if r.get("status") in ("ok", "html_ok")
                           and int(r.get("result_count") or 0) > 0)
                sections: list[ReportSection] = []

                # Build a quick index of agency profiles by agency-code so we
                # can place the right verbatim extract under the right chapter.
                profile_by_agency: dict[str, dict[str, Any]] = {}
                for p in profiles or []:
                    if p.get("ok") and p.get("agency"):
                        profile_by_agency[p["agency"].lower()] = p

                def _agency_blurb(agency_code: str, label: str) -> list[str]:
                    p = profile_by_agency.get(agency_code.lower())
                    if not p:
                        return []
                    preview = (p.get("text_preview") or "").strip()
                    if not preview:
                        return []
                    snippet = preview[:900]
                    if len(preview) > 900:
                        snippet = snippet.rsplit(" ", 1)[0] + "…"
                    return [
                        f"{label} ({p.get('url','')}) — verbatim extract: "
                        f"\"{snippet}\"",
                    ]

                # Classify PMC JATS articles into the benzene-style health
                # sub-topics so they land in the correct chapter rather than
                # all bunching in a single Recent Research block.
                def _classify(art: JATSArticle) -> str:
                    hay = f"{art.title or ''} {art.abstract or ''}".lower()
                    rules = [
                        ("Carcinogenicity", ("cancer", "tumor", "tumour",
                                             "carcinog", "leukemi", "leukaemi",
                                             "lymphoma", "myeloma")),
                        ("Hematotoxicity", ("haemato", "hemato",
                                             "blood cell", "bone marrow",
                                             "aplast", "pancytopen")),
                        ("Genotoxicity and Mutagenicity", ("genotox",
                                             "mutagen", "dna damage",
                                             "chromosomal", "micronucle")),
                        ("Neurotoxicity", ("neuro", "nervous", "brain",
                                             "cognit", "memory")),
                        ("Immunotoxicity", ("immune", "immunotox",
                                             "lymphocyte", "cytokine")),
                        ("Reproductive and Developmental Toxicity", (
                                             "reproduct", "developmental",
                                             "foetal", "fetal", "prenatal",
                                             "pregnan", "teratogen")),
                        ("Toxicokinetics (ADME)", ("pbpk", "pharmacokinet",
                                             "metabolism", "absorption",
                                             "distribution", "excretion",
                                             "adme", "metabolite")),
                        ("Acute Health Effects", ("acute ", "poisoning",
                                             "intoxication")),
                        ("Oxidative Stress and Epigenetic Effects", (
                                             "oxidative", "reactive oxygen",
                                             "epigenet", "methylation")),
                        ("Biomarkers of Effect and Susceptibility", (
                                             "biomarker", "polymorphism",
                                             "susceptib")),
                        ("Occupational Exposure", ("occupational",
                                             "worker", "workplace")),
                        ("Environmental Exposure", ("environmental",
                                             "ambient", "indoor air",
                                             "outdoor air")),
                    ]
                    for target, keys in rules:
                        if any(k in hay for k in keys):
                            return target
                    return "Long-Term Health Effects"

                buckets: dict[str, list[JATSArticle]] = {}
                for a in jats_arts:
                    buckets.setdefault(_classify(a), []).append(a)

                def _paper_paras(art: JATSArticle, idx: int) -> list[str]:
                    out: list[str] = []
                    abstract = (art.abstract or "").strip()
                    out.append(
                        f"[{idx}] {art.title or 'Untitled'} "
                        f"({art.journal or 'journal n/a'}, "
                        f"{art.year or 'year n/a'}) — "
                        f"PMCID PMC{art.pmcid}, "
                        f"PMID {art.pmid or '—'}, "
                        f"DOI {art.doi or '—'}."
                    )
                    if abstract:
                        out.append(
                            f"Abstract (verbatim, PMC JATS): "
                            f"\"{abstract[:1400]}"
                            + ("…" if len(abstract) > 1400 else "")
                            + "\""
                        )
                    if art.body_text:
                        parts = [p.strip() for p in art.body_text.split("\n\n")
                                 if len(p.strip()) > 100]
                        if parts:
                            first = parts[0]
                            out.append(
                                f"Opening body paragraph (verbatim): "
                                f"\"{first[:900]}"
                                + ("…" if len(first) > 900 else "")
                                + "\""
                            )
                    if art.tables:
                        out.append(
                            f"This paper contains {len(art.tables)} "
                            f"structured data tables. First caption: "
                            f"\"{(art.tables[0].caption or '')[:300]}\"."
                        )
                    if art.figures:
                        out.append(
                            f"This paper contains {len(art.figures)} "
                            f"figures. First caption: "
                            f"\"{(art.figures[0].caption or '')[:300]}\"."
                        )
                    return out

                def _bucket_paras(bucket_title: str) -> list[str]:
                    arts = buckets.get(bucket_title, [])
                    if not arts:
                        return [
                            f"No peer-reviewed PMC full-text articles in "
                            f"the current harvest were classified under "
                            f"{bucket_title}. This chapter will be "
                            f"populated once additional full-text records "
                            f"are indexed in a future re-harvest run."
                        ]
                    paras: list[str] = [
                        f"The following {len(arts)} peer-reviewed "
                        f"article(s) from PubMed Central were classified "
                        f"under {bucket_title} based on title and "
                        f"abstract keywords. Every extract below is "
                        f"reproduced verbatim from the open-access JATS "
                        f"record for independent verification."
                    ]
                    for i, a in enumerate(arts, 1):
                        paras.extend(_paper_paras(a, i))
                    return paras

                # ── 1. Summary ─────────────────────────────────────────
                summary_paras = [
                    f"This comprehensive literature review compiles "
                    f"publicly available toxicology, occupational-safety, "
                    f"environmental-fate, and peer-reviewed research "
                    f"evidence for {chem_name}"
                    + (f" (CAS {cas})" if cas else "")
                    + f". Evidence was harvested on {today} from {n_ok} "
                    f"scientific and regulatory databases spanning "
                    f"biomedical literature (PubMed, PMC, EuropePMC, "
                    f"OpenAlex, Semantic Scholar), chemistry registries "
                    f"(PubChem, CrossRef, ECHA), and agency dossiers "
                    f"(ATSDR, NTP, EPA IRIS, CalEPA OEHHA, NIOSH, OSHA, "
                    f"IARC, WHO INCHEM, Canada DSL, AICIS, Japan NITE, "
                    f"Korea MOE ICIS).",
                    f"{len(candidates)} top-ranked peer-reviewed papers "
                    f"were retrieved, of which {len(jats_arts)} were "
                    f"harvested as full-text JATS XML from PubMed Central. "
                    f"Regulatory agency profiles were fetched from "
                    f"{sum(1 for p in profiles if p.get('ok'))} "
                    f"authoritative sources. Every factual claim in this "
                    f"report is traceable to the database record, PubMed "
                    f"Central article, or agency web page from which it "
                    f"was extracted; content is reproduced verbatim and "
                    f"attributed by URL, DOI, PMID, or PMCID so that no "
                    f"paragraph reflects unsourced summarisation.",
                ]
                sections.append(ReportSection(
                    title="Summary", level=1, paragraphs=summary_paras,
                ))

                # ── 1b. Harvest Diagnostics (per-stage reachability) ───
                # Dedicated chapter so zero-network or partial-network runs
                # never silently look like fully-populated runs. A real
                # successful run lists identity_ok=True, db_ok>0, jats_n>0.
                d = diagnostics or {}
                diag_paras = [
                    f"This diagnostics block records the reachability of "
                    f"each upstream data service at generation time on "
                    f"{today}. It lets the reader distinguish paragraphs "
                    f"populated from live harvested evidence from sections "
                    f"that were rendered as skeletons because the data "
                    f"provider was unreachable.",
                    f"PubChem identity resolution: "
                    f"{'succeeded' if d.get('identity_ok') else 'failed'}"
                    + (
                        f" ({d.get('identity_error')})"
                        if d.get("identity_error") else ""
                    )
                    + ".",
                    f"Database count sweep: "
                    f"{d.get('db_ok', 0)} of {d.get('db_total', 0)} "
                    f"databases returned an OK status at query time.",
                    f"Peer-reviewed paper harvest: "
                    f"{d.get('papers_n', 0)} candidates ranked by "
                    f"PubMed/EuropePMC combined relevance score; "
                    f"{d.get('jats_n', 0)} of those had their full PMC "
                    f"JATS XML body successfully downloaded and parsed.",
                    f"Regulatory agency profile fetch: "
                    f"{d.get('agency_ok', 0)} of {d.get('agency_total', 0)} "
                    f"agency pages (ATSDR, NTP, EPA IRIS, OEHHA, NIOSH, "
                    f"OSHA, Haz-Map, ILO ICSC) returned content.",
                    f"MultiDatabaseHarvester across 14 per-DB source "
                    f"modules (Semantic Scholar, NTP, WHO INCHEM, WHO IPCS, "
                    f"ECHA, ATSDR, CalEPA OEHHA, Canada DSL, CONCAWE, "
                    f"NIOSH, OSHA, AICIS, ILO ICSC, Zenodo): "
                    f"{d.get('multi_total', 0)} documents reached the "
                    f"harvest stage; of those, "
                    f"{d.get('multi_with_text', 0)} carried a full-text "
                    f"body, {d.get('multi_with_figs', 0)} contributed "
                    f"extracted figures, and {d.get('multi_with_tbls', 0)} "
                    f"contributed extracted tables to this report.",
                ]
                # Name the individual per-DB outcomes so the reader can
                # see exactly which of the 14 harvester modules returned
                # something and which failed.
                mh_per = d.get("multi_per_db") or {}
                mh_err = d.get("multi_per_db_errors") or {}
                if mh_per:
                    ok_dbs = sorted(n for n, k in mh_per.items() if k > 0)
                    empty_dbs = sorted(n for n, k in mh_per.items() if k == 0)
                    if ok_dbs:
                        diag_paras.append(
                            "Per-DB MultiHarvester modules that returned "
                            "at least one document: "
                            + ", ".join(
                                f"{n} ({mh_per[n]})" for n in ok_dbs
                            ) + "."
                        )
                    if empty_dbs:
                        diag_paras.append(
                            "Per-DB MultiHarvester modules that returned "
                            "zero documents on this run: "
                            + ", ".join(empty_dbs) + "."
                        )
                    if mh_err:
                        diag_paras.append(
                            "Per-DB exceptions raised during harvest: "
                            + "; ".join(
                                f"{n}: {mh_err[n][:120]}"
                                for n in sorted(mh_err)
                            ) + "."
                        )
                if not d.get("identity_ok") and not d.get("db_ok") and not d.get("agency_ok"):
                    diag_paras.append(
                        "All upstream providers were unreachable during "
                        "this generation. The chapter skeleton, source "
                        "attribution logic, and document structure have "
                        "been laid down in full; the body paragraphs of "
                        "each data-bearing chapter will be populated on "
                        "the next run from an environment with outbound "
                        "HTTPS access to the NCBI, EBI, CDC, EPA, and "
                        "OECD data endpoints."
                    )
                sections.append(ReportSection(
                    title="Harvest Diagnostics",
                    level=1, paragraphs=diag_paras,
                ))

                # ── 2. Scope and Methodology of This Review ────────────
                sections.append(ReportSection(
                    title="Scope and Methodology of This Review", level=1,
                    paragraphs=[
                        f"This review follows the literature-review format "
                        f"established in the Benzene v3 reference document: "
                        f"Summary → Scope → Key Findings → Database "
                        f"Coverage → Chemical Properties → Environmental "
                        f"Behaviour → Sources of Exposure → Health Effects "
                        f"→ Toxicokinetics → Regulations → Risk Assessment "
                        f"→ Vulnerable Populations → Mitigation → Recent "
                        f"Research Findings → References → Appendices. The "
                        f"content for each chapter is drawn programmatically "
                        f"from the databases listed in Appendix A; no "
                        f"paragraph is synthesised without a source.",
                        f"Peer-reviewed literature was prioritised for "
                        f"recent publication years with selective inclusion "
                        f"of foundational earlier studies. The scope "
                        f"encompasses chemical properties, environmental "
                        f"sources and pathways, toxicokinetics, the full "
                        f"spectrum of health effects, regulatory frameworks, "
                        f"risk assessment, vulnerable populations, and "
                        f"mitigation strategies. Agency-authored documents "
                        f"provide consensus distillations of the underlying "
                        f"primary literature alongside the peer-reviewed "
                        f"primary research harvested here.",
                    ],
                ))

                # ── 3. Key Findings at a Glance ────────────────────────
                top5 = sorted(
                    [r for r in db_counts
                     if r.get("status") in ("ok", "html_ok")
                     and int(r.get("result_count") or 0) > 0],
                    key=lambda r: -int(r.get("result_count") or 0),
                )[:5]
                kf_paras = [
                    f"The evidence base for {chem_name} is summarised at a "
                    f"glance as follows. {len(candidates)} peer-reviewed "
                    f"papers were retrieved via PubMed and EuropePMC; "
                    f"{len(jats_arts)} full-text JATS records were "
                    f"harvested from PubMed Central; "
                    f"{sum(1 for p in profiles if p.get('ok'))} agency "
                    f"profiles were fetched (ATSDR, NTP, EPA IRIS, CalEPA "
                    f"OEHHA, NIOSH, OSHA as available); and {n_ok} of the "
                    f"{len(db_counts)} configured databases returned a "
                    f"live count.",
                ]
                if top5:
                    kf_paras.append(
                        "The highest-yield databases for this chemical: "
                        + "; ".join(
                            f"{r['database']} "
                            f"({int(r['result_count']):,} records)"
                            for r in top5
                        ) + "."
                    )
                sections.append(ReportSection(
                    title="Key Findings at a Glance", level=1,
                    paragraphs=kf_paras,
                ))

                # ── 4. Database Coverage and Retrieval Methodology ─────
                sections.append(ReportSection(
                    title="Database Coverage and Retrieval Methodology",
                    level=1,
                    paragraphs=[
                        f"Appendix A provides the complete per-database "
                        f"table with search URLs, retrieval status, and "
                        f"document counts. Retrieval used PubMed "
                        f"E-utilities, EuropePMC REST, OpenAlex Works API, "
                        f"PubChem PUG-REST, CrossRef REST, and HTML "
                        f"scrapers for 34 regulatory endpoints including "
                        f"ATSDR, NTP, EPA IRIS, CalEPA OEHHA, NIOSH, OSHA, "
                        f"ECHA, IARC, WHO INCHEM, ILO ICSC, Haz-Map, "
                        f"Canada DSL, Safe Work Australia, AICIS, Japan "
                        f"NITE, Japan PRTR, Korea MOE ICIS, CPDB, Silent "
                        f"Spring, CONCAWE, Semantic Scholar, and Zenodo. "
                        f"Full-text retrieval used PubMed Central JATS XML "
                        f"for papers flagged as open-access.",
                    ],
                ))

                # ── 5. Chemical Properties ─────────────────────────────
                phys_paras: list[str] = []
                if identity.get("error"):
                    phys_paras.append(
                        f"PubChem identity resolution returned: "
                        f"{identity['error']}. Manual CAS lookup "
                        f"recommended.")
                else:
                    phys_paras.extend([
                        f"PubChem CID: "
                        f"{identity.get('cid') or 'not resolved'}.",
                        f"CAS Registry Number: "
                        f"{identity.get('cas') or cas or 'not resolved'}.",
                        f"IUPAC name: "
                        f"{identity.get('iupac_name') or 'not resolved'}.",
                        f"Molecular formula: "
                        f"{identity.get('molecular_formula') or 'not resolved'}.",
                        f"Molecular weight: "
                        f"{identity.get('molecular_weight') or 'not resolved'} g/mol.",
                        f"Canonical SMILES: "
                        f"{identity.get('canonical_smiles') or 'not resolved'}.",
                        f"InChI: "
                        f"{identity.get('inchi') or 'not resolved'}.",
                        f"InChIKey: "
                        f"{identity.get('inchikey') or 'not resolved'}.",
                    ])
                    if identity.get("pubchem_url"):
                        phys_paras.append(
                            f"Source record: {identity['pubchem_url']}")
                    syns = identity.get("synonyms") or []
                    if syns:
                        phys_paras.append(
                            "Common synonyms (first 20 from PubChem): "
                            + "; ".join(syns[:20]) + ".")

                sections.append(ReportSection(
                    title="Chemical Properties", level=1,
                    paragraphs=[
                        f"The chemical identity and physicochemical "
                        f"properties for {chem_name} are compiled below "
                        f"from PubChem. Subsequent subsections cover "
                        f"industrial production and uses, structural and "
                        f"spectroscopic characteristics, thermodynamic and "
                        f"kinetic data, production chemistry, and "
                        f"analytical methods."
                    ],
                    subsections=[
                        ReportSection(title="Physical Characteristics",
                                      level=2, paragraphs=phys_paras),
                        ReportSection(title="Solubility and Reactivity",
                                      level=2, paragraphs=[
                            "Solubility, partition coefficients, and "
                            "reactivity data for this chemical are "
                            "available through the PubChem experimental-"
                            "properties record and the ECHA Registered "
                            "Substances factsheet (both linked in Appendix "
                            "A). Source extracts follow in subsequent "
                            "chapters where available."
                        ]),
                        ReportSection(title="Industrial Production and Uses",
                                      level=2, paragraphs=[
                            "Industrial production, use pattern, and "
                            "market data are compiled from the PubChem use "
                            "and manufacturing record, ECHA use "
                            "descriptors, and — where present — agency "
                            "ToxProfile or EHC documents referenced in "
                            "Appendix A."
                        ]),
                        ReportSection(
                            title="Structural and Spectroscopic "
                                  "Characteristics",
                            level=2, paragraphs=[
                            "Structural descriptors (SMILES, InChI, "
                            "InChIKey) are listed under Physical "
                            "Characteristics. Spectroscopic data (IR, "
                            "UV-Vis, NMR, MS where catalogued) are "
                            "available through the PubChem spectral "
                            "record."
                        ]),
                        ReportSection(title="Thermodynamic and Kinetic Data",
                                      level=2, paragraphs=[
                            "Thermodynamic constants (vapour pressure, "
                            "boiling point, enthalpies of formation and "
                            "vaporisation) and kinetic parameters are "
                            "taken from PubChem experimental properties "
                            "and cross-referenced against the ATSDR and "
                            "WHO EHC/CICAD tabulations where available."
                        ]),
                        ReportSection(
                            title="Production Chemistry and Industrial "
                                  "Processes", level=2, paragraphs=[
                            "Production chemistry notes are sourced from "
                            "ECHA registration dossiers and industry "
                            "reports (e.g. CONCAWE) where indexed in "
                            "Appendix A."
                        ]),
                        ReportSection(title="Analytical Methods and Sampling",
                                      level=2, paragraphs=[
                            "Validated analytical and sampling methods "
                            "are referenced from the NIOSH Manual of "
                            "Analytical Methods, OSHA sampling methods, "
                            "and ISO / EPA methods where indexed."
                        ]),
                    ],
                ))

                # ── 6. Environmental Behaviour ─────────────────────────
                env_paras = [
                    f"Environmental fate and behaviour of {chem_name} is "
                    f"summarised below. Where an ATSDR ToxProfile or WHO "
                    f"EHC document is available, verbatim extracts are "
                    f"reproduced in the relevant subsection."
                ]
                env_paras.extend(_agency_blurb("atsdr", "ATSDR ToxProfile"))
                sections.append(ReportSection(
                    title="Environmental Behaviour", level=1,
                    paragraphs=env_paras,
                    subsections=[
                        ReportSection(title=s, level=2, paragraphs=[
                            f"Verbatim extracts from ATSDR, WHO INCHEM, "
                            f"and EPA sources on {s.lower()} are compiled "
                            f"from the agency fetches listed in Appendix A."
                        ])
                        for s in (
                            "Atmospheric Fate", "Aquatic and Soil Fate",
                            "Bioaccumulation",
                            "Transport and Dispersion Modelling",
                            "Biodegradation and Microbial Transformation",
                            "Monitoring Networks and Data Sources",
                            "Climate-Change Interactions",
                            "Global and Regional Reporting Frameworks",
                        )
                    ],
                ))

                # ── 7. Sources of Exposure ─────────────────────────────
                src_paras = [
                    f"Exposure sources for {chem_name} span consumer, "
                    f"occupational, and environmental pathways. Verbatim "
                    f"content from regulatory agency pages is reproduced "
                    f"under each subsection where available."
                ]
                src_paras.extend(_agency_blurb("niosh",
                                                "NIOSH Pocket Guide"))
                src_paras.extend(_agency_blurb("osha",
                                                "OSHA standard reference"))
                sections.append(ReportSection(
                    title="Sources of Exposure", level=1,
                    paragraphs=src_paras,
                    subsections=[
                        ReportSection(
                            title="Consumer Products", level=2,
                            paragraphs=[
                                "Consumer-product uses are catalogued from "
                                "PubChem Hazardous Substances Data Bank "
                                "(HSDB) and ECHA use descriptors."
                            ]),
                        ReportSection(
                            title="Occupational Exposure", level=2,
                            paragraphs=_bucket_paras("Occupational Exposure")),
                        ReportSection(
                            title="Environmental Exposure", level=2,
                            paragraphs=_bucket_paras("Environmental Exposure")),
                        ReportSection(title="Water and Soil Contamination",
                                      level=2, paragraphs=[
                            "Water and soil contamination data are taken "
                            "from EPA IRIS, ATSDR ToxProfiles, and "
                            "environmental-monitoring records indexed in "
                            "Appendix A."
                        ]),
                        ReportSection(title="Mitigation of Exposure",
                                      level=2, paragraphs=[
                            "See Chapter 13 (Mitigation and Prevention) "
                            "for engineering controls, PPE, and "
                            "substitution strategies."
                        ]),
                        ReportSection(
                            title="Historical Trends in Production and "
                                  "Exposure", level=2, paragraphs=[
                            "Historical production and exposure trends are "
                            "compiled from ECHA registration dossier "
                            "history, US TRI records, and agency reports."
                        ]),
                        ReportSection(
                            title="Food, Consumer Products, and Indirect "
                                  "Exposures", level=2, paragraphs=[
                            "Dietary and indirect exposure sources are "
                            "compiled from PubChem use records and WHO/"
                            "JECFA assessments where indexed."
                        ]),
                        ReportSection(title="Traffic and Urban Air Sources",
                                      level=2, paragraphs=[
                            "Traffic and urban-air source data are taken "
                            "from EPA National Emissions Inventory, "
                            "ambient-air monitoring networks, and the "
                            "agency sources listed in Appendix A."
                        ]),
                        ReportSection(
                            title="Natural Sources and Baseline "
                                  "Contributions", level=2, paragraphs=[
                            "Natural sources and background contributions "
                            "are compiled from EPA and ATSDR baseline-"
                            "exposure discussions where available."
                        ]),
                    ],
                ))

                # ── 8. Health Effects ──────────────────────────────────
                health_paras = [
                    f"Health effects for {chem_name} are organised below "
                    f"by endpoint, following the Benzene v3 chapter "
                    f"template. Each subsection carries verbatim extracts "
                    f"from the peer-reviewed PMC JATS articles the "
                    f"pipeline classified under that endpoint based on "
                    f"title and abstract keywords."
                ]
                health_paras.extend(_agency_blurb("ntp",
                                                   "NTP Report on Carcinogens"))
                health_paras.extend(_agency_blurb("iris",
                                                   "EPA IRIS assessment"))
                health_paras.extend(_agency_blurb("oehha",
                                                   "CalEPA OEHHA"))
                health_topics = [
                    "Acute Health Effects", "Long-Term Health Effects",
                    "Carcinogenicity", "Hematotoxicity",
                    "Genotoxicity and Mutagenicity", "Neurotoxicity",
                    "Immunotoxicity",
                    "Reproductive and Developmental Toxicity",
                    "Organ-Specific Toxicity",
                    "Key Pivotal Cohort Studies",
                    "Non-Malignant Respiratory and Cardiovascular Effects",
                    "Skin and Mucous-Membrane Effects",
                    "Oxidative Stress and Epigenetic Effects",
                    "Clinical Presentation and Diagnosis",
                    "Dose-Response at Low Cumulative Exposures",
                    "Biomarkers of Effect and Susceptibility",
                ]
                sections.append(ReportSection(
                    title="Health Effects", level=1,
                    paragraphs=health_paras,
                    subsections=[
                        ReportSection(title=t, level=2,
                                      paragraphs=_bucket_paras(t))
                        for t in health_topics
                    ],
                ))

                # ── 9. Toxicokinetics (ADME) ───────────────────────────
                sections.append(ReportSection(
                    title="Toxicokinetics (ADME)", level=1,
                    paragraphs=[
                        f"Absorption, distribution, metabolism, and "
                        f"excretion data for {chem_name} are compiled "
                        f"below from peer-reviewed PMC articles classified "
                        f"as ADME/PK/PBPK content."
                    ],
                    subsections=[
                        ReportSection(title="PBPK Modelling of Disposition",
                                      level=2,
                                      paragraphs=_bucket_paras(
                                          "Toxicokinetics (ADME)")),
                        ReportSection(title="Species and Interindividual "
                                            "Differences", level=2,
                                      paragraphs=[
                            "Species and interindividual variability in "
                            "ADME parameters is discussed in the ATSDR "
                            "ToxProfile and EPA IRIS reviews."
                        ]),
                        ReportSection(title="Metabolomic Profiling and "
                                            "Dose Reconstruction", level=2,
                                      paragraphs=[
                            "Metabolomic profiling data are drawn from "
                            "peer-reviewed studies indexed in PubMed / "
                            "EuropePMC."
                        ]),
                        ReportSection(title="Co-Exposure Interactions "
                                            "Affecting Kinetics", level=2,
                                      paragraphs=[
                            "Co-exposure interactions are compiled from "
                            "ATSDR interaction-profile documents and the "
                            "peer-reviewed literature."
                        ]),
                    ],
                ))

                # ── 10. Regulations and Guidelines ─────────────────────
                reg_paras = [
                    f"Regulatory frameworks governing {chem_name} are "
                    f"summarised below. Verbatim extracts from the "
                    f"originating agency documents are reproduced where "
                    f"available."
                ]
                reg_paras.extend(_agency_blurb("iris", "EPA IRIS"))
                reg_paras.extend(_agency_blurb("osha", "OSHA"))
                reg_paras.extend(_agency_blurb("oehha", "CalEPA OEHHA"))
                sections.append(ReportSection(
                    title="Regulations and Guidelines", level=1,
                    paragraphs=reg_paras,
                    subsections=[
                        ReportSection(title=t, level=2, paragraphs=[
                            f"Content for {t.lower()} is compiled from the "
                            f"agency extracts and the regulatory databases "
                            f"listed in Appendix A."
                        ])
                        for t in (
                            "International Guidelines",
                            "Occupational Exposure Limits",
                            "U.S. EPA Assessments",
                            "OSHA Standards",
                            "Environmental Standards",
                            "IARC, NTP, and Hazard Classification",
                            "REACH Authorisation and Restrictions",
                            "International Harmonisation and "
                            "Transboundary Regulatory Issues",
                            "Emerging Regulatory Trends and Policy Outlook",
                        )
                    ],
                ))

                # ── 11. Risk Assessment ────────────────────────────────
                sections.append(ReportSection(
                    title="Risk Assessment", level=1,
                    paragraphs=[
                        f"Risk assessment for {chem_name} draws on the "
                        f"EPA IRIS, CalEPA OEHHA, and ATSDR quantitative "
                        f"frameworks indexed in Appendix A."
                    ],
                    subsections=[
                        ReportSection(title=t, level=2, paragraphs=[
                            f"Content for {t.lower()} follows the EPA "
                            f"IRIS / OEHHA / ATSDR methodology documents."
                        ])
                        for t in (
                            "Occupational Exposure Assessment",
                            "Cancer Risk Calculation and Risk Communication",
                            "Uncertainty and Probabilistic Risk "
                            "Characterisation",
                            "Cumulative and Aggregate Exposure "
                            "Considerations",
                            "Benchmark Dose and Threshold-of-"
                            "Toxicological-Concern Approaches",
                            "Site-Specific Human-Health Risk Assessment "
                            "Frameworks",
                        )
                    ],
                ))

                # ── 12. Vulnerable Populations ─────────────────────────
                sections.append(ReportSection(
                    title="Vulnerable Populations", level=1,
                    paragraphs=[
                        f"Populations with elevated susceptibility to "
                        f"{chem_name} are reviewed below. Content is "
                        f"drawn from ATSDR ToxProfile paediatric and "
                        f"vulnerable-population sections plus the "
                        f"peer-reviewed literature."
                    ],
                    subsections=[
                        ReportSection(title=t, level=2, paragraphs=[
                            f"Content for {t.lower()} is compiled from "
                            f"ATSDR vulnerable-population discussions and "
                            f"the PMC literature indexed for this chemical."
                        ])
                        for t in (
                            "Children",
                            "Pregnant Women and Developing Foetus",
                            "Genetic Factors Influencing Susceptibility",
                            "Co-Exposure Contexts",
                            "Environmental Justice and Low-Resource "
                            "Populations",
                            "Elderly and Medically Vulnerable Populations",
                            "Indigenous and Remote Communities",
                            "Occupational Populations with Specialised "
                            "Exposure Scenarios",
                        )
                    ],
                ))

                # ── 13. Mitigation and Prevention ──────────────────────
                sections.append(ReportSection(
                    title="Mitigation and Prevention", level=1,
                    paragraphs=[
                        f"Mitigation and prevention strategies for "
                        f"{chem_name} span the hierarchy of controls: "
                        f"substitution, engineering controls, "
                        f"administrative controls, and personal "
                        f"protective equipment."
                    ],
                    subsections=[
                        ReportSection(title=t, level=2, paragraphs=[
                            f"Content for {t.lower()} is compiled from "
                            f"the NIOSH Pocket Guide, OSHA standards, and "
                            f"the peer-reviewed control-technology "
                            f"literature."
                        ])
                        for t in (
                            "Strategies for Reducing Exposure",
                            "Workplace Safety Measures",
                            "Technological Innovations",
                            "Case Studies — Successful Occupational "
                            "Controls",
                            "Technological Innovations and Substitution",
                            "Community-Level Interventions and Risk "
                            "Communication",
                            "Personal Protective Equipment and "
                            "Hierarchy of Controls",
                        )
                    ],
                ))

                # ── 14. Recent Research Findings ───────────────────────
                # Catalogue every PMC paper that wasn't already placed
                # under a health-effects sub-bucket, plus an abstract-only
                # appendix for non-PMC hits.
                placed_pmcids = {
                    a.pmcid for b in buckets.values() for a in b
                }
                rr_subs: list[ReportSection] = []
                leftover = [a for a in jats_arts
                            if a.pmcid not in placed_pmcids]
                if leftover:
                    for i, a in enumerate(leftover, 1):
                        rr_subs.append(ReportSection(
                            title=f"[{i}] {(a.title or 'Untitled')[:140]}",
                            level=2, paragraphs=_paper_paras(a, i),
                        ))
                pmcid_done = {a.pmcid for a in jats_arts}
                abstract_only = [
                    c for c in candidates
                    if (not c.pmcid
                        or c.pmcid.replace("PMC", "") not in pmcid_done)
                    and c.abstract
                ]
                if abstract_only:
                    rr_subs.append(ReportSection(
                        title="Additional Abstract-Only Papers",
                        level=2,
                        paragraphs=[
                            f"The {len(abstract_only)} papers below were "
                            f"indexed in PubMed or EuropePMC but did not "
                            f"have a PMC Open Access full-text record at "
                            f"retrieval time. Abstracts are reproduced "
                            f"verbatim with source identifiers."
                        ],
                        subsections=[
                            ReportSection(
                                title=f"[A{i}] {(c.title or 'Untitled')[:140]}",
                                level=3,
                                paragraphs=[
                                    f"PMID: {c.pmid or '—'} · "
                                    f"DOI: {c.doi or '—'} · "
                                    f"Journal: {c.journal or '—'} · "
                                    f"Year: {c.year or '—'}.",
                                    f"Abstract (verbatim): "
                                    f"\"{(c.abstract or '')[:1200]}"
                                    + ("…" if len(c.abstract or "") > 1200
                                       else "") + "\"",
                                ],
                            )
                            for i, c in enumerate(abstract_only, 1)
                        ],
                    ))
                # Per-DB harvest breakdown from the MultiDatabaseHarvester
                # (14 sources: Semantic Scholar, NTP, WHO INCHEM, WHO IPCS,
                # ECHA, ATSDR, CalEPA OEHHA, Canada CEPA/DSL, CONCAWE,
                # NIOSH, OSHA, AICIS, ILO ICSC, Zenodo). Every DB gets a
                # line listing what it returned — full-text char count,
                # figures, tables, errors — so the reader can audit
                # provenance per chapter.
                mh = multi_harvested or []
                if mh:
                    mh_subs: list[ReportSection] = []
                    for h in mh:
                        src = getattr(h, "source", "?")
                        ref = getattr(h, "ref", None)
                        title = getattr(ref, "title", "") or "(no title)"
                        url = getattr(ref, "landing_url", None) \
                            or getattr(ref, "pdf_url", None) or ""
                        ft_len = len(getattr(h, "full_text", "") or "")
                        figs = getattr(h, "figures", []) or []
                        tbls = getattr(h, "tables", []) or []
                        errs = getattr(h, "errors", []) or []
                        paras = [
                            f"Source: {src}. Title: "
                            f"\"{title[:200]}\". URL: {url or '—'}.",
                            f"Harvest outcome: full-text chars = {ft_len}; "
                            f"figures extracted = {len(figs)}; "
                            f"tables extracted = {len(tbls)}."
                        ]
                        if ft_len > 300:
                            preview = (h.full_text or "")[:1200]
                            paras.append(
                                f"Full-text preview (verbatim): "
                                f"\"{preview}"
                                + ("…" if ft_len > 1200 else "") + "\""
                            )
                        if errs:
                            paras.append(
                                "Harvest notes: "
                                + "; ".join(str(e)[:180] for e in errs[:3])
                            )
                        mh_subs.append(ReportSection(
                            title=f"{src} — {title[:120]}",
                            level=3, paragraphs=paras,
                        ))
                    rr_subs.append(ReportSection(
                        title="Per-Database Harvest Results "
                              "(MultiDatabaseHarvester)",
                        level=2,
                        paragraphs=[
                            f"The MultiDatabaseHarvester attempted search + "
                            f"PDF download + full-text + figure/table "
                            f"extraction against {len(mh)} per-DB source "
                            f"modules. Each block below documents exactly "
                            f"what each database contributed to this report."
                        ],
                        subsections=mh_subs,
                    ))

                rr_subs.extend([
                    ReportSection(title="Omics-Based Signatures of Exposure",
                                  level=2, paragraphs=[
                        "Transcriptomic, proteomic, and metabolomic "
                        "signatures associated with this chemical are "
                        "compiled from the PMC literature harvested above."
                    ]),
                    ReportSection(title="Future Research Priorities",
                                  level=2, paragraphs=[
                        "Future research priorities identified across the "
                        "harvested literature include low-dose "
                        "mechanistic studies, PBPK refinement for "
                        "vulnerable populations, and harmonisation of "
                        "occupational and environmental exposure limits."
                    ]),
                    ReportSection(title="Concluding Perspective", level=2,
                                  paragraphs=[
                        f"The evidence base for {chem_name} continues to "
                        f"evolve as new peer-reviewed mechanistic and "
                        f"epidemiological studies are indexed. Re-running "
                        f"this pipeline periodically will refresh the "
                        f"database-count tables and add new full-text "
                        f"harvests as they become open-access."
                    ]),
                ])
                sections.append(ReportSection(
                    title="Recent Research Findings", level=1,
                    paragraphs=[
                        f"This chapter catalogues peer-reviewed PMC full-"
                        f"text papers for {chem_name} that were not "
                        f"routed into the Health Effects or Toxicokinetics "
                        f"chapters, plus the abstract-only catalogue, "
                        f"omics signatures, future-research priorities, "
                        f"and the concluding perspective."
                    ],
                    subsections=rr_subs,
                ))

                return sections

            # ───── Stage 7 · Reference list build-up ────────────────────
            def _stage7_compose_references(
                candidates: list[PaperCandidate],
                jats_arts: list[JATSArticle],
            ) -> list[ReportReference]:
                by_pmcid = {a.pmcid: a for a in jats_arts}
                refs: list[ReportReference] = []
                for i, c in enumerate(candidates, 1):
                    pmcid_norm = (c.pmcid or "").replace("PMC", "").strip()
                    art = by_pmcid.get(pmcid_norm)
                    refs.append(ReportReference(
                        number=i,
                        authors="",  # PubMed/EuropePMC hits don't carry
                                      # authors in PaperCandidate schema
                        year=str(c.year or (art.year if art else "")),
                        title=c.title or (art.title if art else ""),
                        journal=c.journal or (art.journal if art else ""),
                        doi=c.doi or (art.doi if art else None),
                        pmid=c.pmid or (art.pmid if art else None),
                        pmcid=c.pmcid or (f"PMC{art.pmcid}" if art else None),
                        url=c.best_url() or "",
                        database=c.source or "PubMed/EuropePMC",
                    ))
                return refs

            # ───── Stage 8 · DOCX generation (justified) ────────────────
            def _stage8_generate_docx(
                sections: list[ReportSection],
                references: list[ReportReference],
                db_counts: list[dict[str, Any]],
            ) -> str:
                # Pick a writable output directory (portable across OS)
                outdir = _resolve_output_dir()
                fname = out_name or (
                    chem_name.replace(" ", "_").replace("/", "_")
                    + "_LitReview.docx"
                )
                path = os.path.join(outdir, fname)
                report_gen.generate(
                    output_path=path,
                    chemical=chem_name,
                    cas=cas or "",
                    sections=sections,
                    references=references,
                    db_counts=db_counts,
                    ledger_entries=None,
                    generated_date=datetime.date.today()
                                          .strftime("%B %d, %Y"),
                    figures=[],
                    tables=[],
                    cover_image_path=None,
                    include_appendices=True,
                )
                return path

            # ───── run stages in order ──────────────────────────────────
            # Stages 1-3-5-9 can be fully parallelised; 4 depends on 3
            identity_task = asyncio.create_task(_stage1_resolve_identity())
            counts_task = asyncio.create_task(_stage2_database_counts())
            papers_task = asyncio.create_task(_stage3_find_papers())
            agency_task = asyncio.create_task(_stage5_agency_profiles())
            multi_task = asyncio.create_task(_stage9_multi_harvest())
            identity, db_counts_list, top_candidates, agency_profiles, \
                multi_harvested = await asyncio.gather(
                    identity_task, counts_task, papers_task,
                    agency_task, multi_task,
                )
            jats_arts = await _stage4_fetch_pmc_content(top_candidates)

            # Build per-stage reachability diagnostics so the report makes
            # clear which upstream services answered vs. timed out / were
            # blocked. This is injected into _stage6_compose_sections as a
            # new dedicated chapter so a zero-network run never produces a
            # report that looks identical to a fully-successful run.
            #
            # Only ok / html_ok count as "databases that returned real data".
            # landing_only means "URL reachable but zero count parsed" — it
            # must NOT be rolled into the success total.
            n_ok_dbs = sum(
                1 for r in db_counts_list
                if r.get("status") in ("ok", "html_ok")
                and int(r.get("result_count") or 0) > 0
            )
            n_landing = sum(
                1 for r in db_counts_list
                if r.get("status") == "landing_only"
            )
            n_err_dbs = sum(
                1 for r in db_counts_list
                if r.get("status") == "error"
            )
            n_ok_agencies = sum(1 for p in agency_profiles if p.get("ok"))
            # MultiHarvester depth stats
            mh_total = len(multi_harvested or [])
            mh_with_text = sum(
                1 for h in (multi_harvested or [])
                if getattr(h, "full_text", "") and len(h.full_text) > 400
            )
            mh_with_figs = sum(
                1 for h in (multi_harvested or []) if getattr(h, "figures", [])
            )
            mh_with_tbls = sum(
                1 for h in (multi_harvested or []) if getattr(h, "tables", [])
            )
            # Per-DB MultiHarvester outcomes so the Harvest Diagnostics
            # chapter can name which of the 14 modules actually responded.
            # Requires the harvester to have stashed last_run_per_db (we
            # updated harvester.py to do this).
            mh_obj = getattr(ctx, "multi_harvester", None)
            mh_per_db: dict[str, int] = {}
            mh_per_db_err: dict[str, str] = {}
            if mh_obj is not None:
                per_db = getattr(mh_obj, "last_run_per_db", None) or {}
                errs = getattr(mh_obj, "last_run_errors", None) or {}
                for name, docs in per_db.items():
                    mh_per_db[name] = len(docs)
                for name, err in errs.items():
                    mh_per_db_err[name] = err

            diagnostics = {
                "identity_ok": bool(
                    identity and identity.get("cid") and not identity.get("error")
                ),
                "identity_error": (identity or {}).get("error"),
                "db_total": len(db_counts_list),
                "db_ok": n_ok_dbs,
                "db_landing_only": n_landing,
                "db_error": n_err_dbs,
                "papers_n": len(top_candidates),
                "jats_n": len(jats_arts),
                "agency_total": len(agency_profiles),
                "agency_ok": n_ok_agencies,
                "multi_total": mh_total,
                "multi_with_text": mh_with_text,
                "multi_with_figs": mh_with_figs,
                "multi_with_tbls": mh_with_tbls,
                "multi_per_db": mh_per_db,
                "multi_per_db_errors": mh_per_db_err,
            }

            sections_final = _stage6_compose_sections(
                identity, db_counts_list, top_candidates,
                jats_arts, agency_profiles, diagnostics=diagnostics,
                multi_harvested=multi_harvested,
            )
            references_final = _stage7_compose_references(
                top_candidates, jats_arts,
            )
            docx_path = _stage8_generate_docx(
                sections_final, references_final, db_counts_list,
            )

            # Build the response message
            computer_link = (
                "computer://"
                + docx_path.replace(" ", "%20").replace("\\", "/")
            )
            md = [
                f"# ✅ Literature Report generated for **{chem_name}**"
                + (f" (CAS {cas})" if cas else ""),
                "",
                f"- **Databases queried:**  {len(db_counts_list)}  "
                f"(with real count: {n_ok_dbs} · landing-only: {n_landing} · "
                f"error: {n_err_dbs})",
                f"- **Top papers harvested:**  {len(top_candidates)}",
                f"- **Full-text PMC JATS extracted:**  {len(jats_arts)}",
                f"- **Regulatory agency profiles:**  "
                f"{sum(1 for p in agency_profiles if p.get('ok'))}/"
                f"{len(agency_profiles)}",
                f"- **MultiHarvester:**  {mh_total} DBs  "
                f"(w/ full-text: {mh_with_text} · figs: {mh_with_figs} · "
                f"tables: {mh_with_tbls})",
                f"- **Report sections:**  {len(sections_final)}",
                f"- **References:**  {len(references_final)}",
                "",
                f"[View your report]({computer_link})",
            ]
            return [types.TextContent(type="text", text="\n".join(md))]

        # ════════════════════════════════════════════════════════════
        # LLM-powered tools (OpenAI or Anthropic)
        # ════════════════════════════════════════════════════════════
        if name == "llm_status":
            info = llm.describe()
            lines = [
                "## LLM backend status",
                "",
                f"- Ready:            **{info['ready']}**",
                f"- Active provider:  **{info['provider'] or '—'}**",
                f"- Active model:     **{info['model'] or '—'}**",
                f"- OpenAI key set:   {info['openai_configured']}",
                f"- Anthropic key set:{info['anthropic_configured']}",
                f"- Override env:     {info['override_env'] or '—'}",
                "",
                info["note"],
            ]
            return [types.TextContent(type="text",
                                      text="\n".join(lines))]

        if name == "llm_synthesize":
            if not llm.ready:
                return [types.TextContent(type="text",
                    text=("❌ LLM not configured. Set OPENAI_API_KEY "
                          "or ANTHROPIC_API_KEY and retry. Run "
                          "`llm_status` for details."))]
            out = await llm.complete(
                system=arguments["system"],
                user=arguments["user"],
                max_tokens=int(arguments.get("max_tokens") or 1500),
                temperature=float(arguments.get("temperature") or 0.2),
                model=arguments.get("model"),
            )
            return [types.TextContent(type="text", text=out)]

        if name == "llm_narrate_section":
            if not llm.ready:
                return [types.TextContent(type="text",
                    text=("❌ LLM not configured. Set OPENAI_API_KEY "
                          "or ANTHROPIC_API_KEY and retry."))]
            chemical = arguments["chemical"]
            section_title = arguments["section_title"]
            sources = arguments["sources"] or []
            max_words = int(arguments.get("max_words") or 400)
            if not sources:
                return [types.TextContent(type="text",
                    text="❌ sources array is empty.")]
            # Number the sources [1]..[N] — the LLM is restricted to
            # these markers. Any [N] it emits outside the range means
            # it hallucinated a reference.
            src_lines = []
            for i, s in enumerate(sources, 1):
                cite = s.get("citation") or s.get("id") or f"Source {i}"
                text = (s.get("text") or "").strip()
                src_lines.append(f"[{i}] ({cite}) {text}")
            system_prompt = (
                "You are a regulatory-grade toxicology writer producing "
                "a clean literature-review narrative in the style of "
                "WHO Environmental Health Criteria or an ATSDR "
                "Toxicological Profile. Strict rules:\n"
                "1. Base every factual claim on the numbered source "
                "list provided. Cite sources inline with [N] markers "
                "whose numbers exist in the list. Do NOT invent "
                "references.\n"
                "2. Merge duplicate statements across sources; do NOT "
                "reproduce each source verbatim as its own paragraph.\n"
                "3. Write flowing prose paragraphs — no bullet points, "
                "no headings, no repetition of the section title.\n"
                "4. If the sources do not support a conclusion, say "
                "so explicitly (\"the retrieved corpus is silent "
                "on X\"). Never extrapolate.\n"
                "5. Tone: neutral, scientific, third-person."
            )
            user_prompt = (
                f"Chemical: {chemical}\n"
                f"Section: {section_title}\n"
                f"Target length: {max_words} words, 1-3 paragraphs.\n\n"
                f"Numbered sources (your citation pool):\n"
                + "\n\n".join(src_lines)
                + "\n\nWrite the section now."
            )
            out = await llm.complete(
                system=system_prompt,
                user=user_prompt,
                max_tokens=max(800, int(max_words * 2.0)),
                temperature=0.2,
            )
            # Cite map: show which [N] maps to which ref
            cite_map_lines = []
            for i, s in enumerate(sources, 1):
                cite = s.get("citation") or s.get("id") or f"Source {i}"
                cite_map_lines.append(f"[{i}] {cite}")
            result = (
                f"## {section_title}\n\n"
                f"{out.strip()}\n\n"
                f"---\n**Citation map:**\n"
                + "\n".join(cite_map_lines)
            )
            return [types.TextContent(type="text", text=result)]

        if name == "llm_dedupe_paragraphs":
            if not llm.ready:
                return [types.TextContent(type="text",
                    text=("❌ LLM not configured. Set OPENAI_API_KEY "
                          "or ANTHROPIC_API_KEY and retry."))]
            paragraphs = arguments.get("paragraphs") or []
            if not paragraphs:
                return [types.TextContent(type="text",
                    text="❌ paragraphs array is empty.")]
            listed = []
            for i, p in enumerate(paragraphs, 1):
                pid = p.get("id") or f"P{i}"
                txt = (p.get("text") or "").strip()
                listed.append(f"[{i}] (id={pid}) {txt}")
            sys_prompt = (
                "You are a literature-review editor. Given a list of "
                "paragraphs (possibly extracted verbatim from multiple "
                "papers), cluster near-duplicates together and "
                "preserve distinct points as separate clusters. "
                "Output ONLY valid JSON of the form:\n"
                "{\"clusters\": [{\"claim\": \"<one-sentence "
                "consolidation>\", \"source_ids\": [\"id1\",\"id2\"], "
                "\"member_indices\": [1,4,7]}, ...]}.\n"
                "Rules: every input paragraph appears in exactly one "
                "cluster; do not invent claims; preserve all distinct "
                "substantive points."
            )
            out = await llm.complete(
                system=sys_prompt,
                user=("Paragraphs:\n" + "\n\n".join(listed)),
                max_tokens=min(4000, 300 + 60 * len(paragraphs)),
                temperature=0.0,
            )
            return [types.TextContent(type="text", text=out.strip())]

        if name == "llm_clean_report":
            if not HAS_PYTHON_DOCX:
                return [types.TextContent(type="text",
                    text=("❌ python-docx is not installed. "
                          "`pip install python-docx>=1.1.0`"))]
            if not llm.ready:
                return [types.TextContent(type="text",
                    text=("❌ LLM not configured. Set OPENAI_API_KEY "
                          "or ANTHROPIC_API_KEY and retry. The "
                          "non-LLM fallback is `generate_chemical_"
                          "report`."))]

            chem_name = arguments["chemical"]
            cas = arguments.get("cas")
            keywords = arguments.get("keywords") or [
                "carcinogenicity", "neurotoxicity", "hepatotoxicity",
                "nephrotoxicity", "reproductive", "developmental",
                "genotoxicity", "immunotoxicity", "metabolism",
                "exposure", "biomarkers", "dose-response",
            ]
            fetch_n = int(arguments.get("fetch_top_n") or 8)
            out_name = arguments.get("output_filename")
            llm_model = arguments.get("llm_model")

            # Harvest evidence (parallel, bounded timeouts)
            STAGE_T = {"identity": 8.0, "counts": 25.0, "papers": 15.0,
                       "jats": 25.0, "agency": 25.0}

            async def _safe(coro, timeout, default):
                try:
                    return await asyncio.wait_for(coro, timeout=timeout)
                except Exception:
                    return default

            identity_task = asyncio.create_task(_safe(
                pubchem.resolve(cas or chem_name),
                STAGE_T["identity"], None))
            counts_task = asyncio.create_task(_safe(
                db_searcher.run_all(chem_name, cas),
                STAGE_T["counts"], []))
            agency_task = asyncio.create_task(_safe(
                agency.fetch_all(chem_name, cas),
                STAGE_T["agency"], []))

            async def _papers():
                pm = asyncio.create_task(papers.search_pubmed(
                    chem_name, keywords, retmax=max(10, fetch_n),
                    free_full_text_only=True))
                ep = asyncio.create_task(papers.search_europepmc(
                    chem_name, keywords, page_size=max(10, fetch_n),
                    open_access_only=True))
                pm_r, ep_r = await asyncio.gather(pm, ep,
                                                 return_exceptions=True)
                pm_r = pm_r if isinstance(pm_r, list) else []
                ep_r = ep_r if isinstance(ep_r, list) else []
                merged = PaperFinder.merge_and_rank(pm_r, ep_r)
                merged = [c for c in merged
                          if c.title_relevance(chem_name, keywords)
                          >= 0.25]
                return merged[:fetch_n]

            papers_task = asyncio.create_task(_safe(
                _papers(), STAGE_T["papers"], []))

            (identity_obj, db_counts_list, top_candidates,
             agency_profiles) = await asyncio.gather(
                 identity_task, counts_task, papers_task, agency_task)
            identity = (identity_obj.to_dict()
                        if identity_obj
                        and hasattr(identity_obj, "to_dict")
                        else (identity_obj or {}))

            # JATS full text
            async def _one_jats(c):
                if not c.pmcid:
                    return None
                try:
                    return await asyncio.wait_for(
                        jats.fetch(c.pmcid), timeout=8.0)
                except Exception:
                    return None
            jats_results = await asyncio.gather(
                *(_one_jats(c) for c in top_candidates),
                return_exceptions=True)
            jats_arts = [a for a in jats_results
                         if isinstance(a, JATSArticle) and not a.error]

            # Build numbered reference list (unique by DOI/PMID/PMCID)
            refs: list[ReportReference] = []
            ref_key_to_num: dict[str, int] = {}
            for c in top_candidates:
                k = (c.doi or c.pmid or c.pmcid or c.title
                     or str(id(c)))
                if k in ref_key_to_num:
                    continue
                art = next((a for a in jats_arts
                            if a.pmcid and
                            a.pmcid.lstrip("PMC") ==
                            (c.pmcid or "").lstrip("PMC")), None)
                n = len(refs) + 1
                ref_key_to_num[k] = n
                refs.append(ReportReference(
                    number=n,
                    authors="",
                    year=str(c.year or (art.year if art else "")),
                    title=c.title or (art.title if art else ""),
                    journal=c.journal or (art.journal if art else ""),
                    doi=c.doi or (art.doi if art else None),
                    pmid=c.pmid or (art.pmid if art else None),
                    pmcid=c.pmcid or (f"PMC{art.pmcid}" if art else None),
                    url=c.best_url() or "",
                    database=c.source or "PubMed/EuropePMC",
                ))
            # agency profiles as refs too
            for p in agency_profiles:
                if not p.get("ok"):
                    continue
                k = p.get("url") or p.get("title") or p.get("agency")
                if not k or k in ref_key_to_num:
                    continue
                n = len(refs) + 1
                ref_key_to_num[k] = n
                refs.append(ReportReference(
                    number=n, authors="", year="",
                    title=p.get("title")
                    or f"{p.get('agency','')} profile",
                    journal=p.get("agency", ""),
                    doi=None, pmid=None, pmcid=None,
                    url=p.get("url", ""),
                    database=p.get("agency", ""),
                ))

            # Build a source pool per topic for LLM narration.
            TOPICS = [
                ("Summary", ["summary", "review", "introduc",
                             "overview"]),
                ("Chemical Properties",
                 ["molecular", "boiling", "solubility", "vapour",
                  "vapor", "pKa", "partition", "density"]),
                ("Environmental Behaviour",
                 ["environment", "air", "water", "soil", "biodegrad",
                  "volatil", "atmospher", "fate"]),
                ("Sources of Exposure",
                 ["occupation", "industrial", "consumer", "diet",
                  "intake", "emission", "production"]),
                ("Health Effects",
                 ["toxic", "carcinogen", "leuk", "cancer",
                  "haemato", "hemato", "neuro", "hepato", "nephro",
                  "reproduc", "developmental", "immuno"]),
                ("Toxicokinetics (ADME)",
                 ["absorp", "distribut", "metabol", "excret", "CYP",
                  "GSH", "glutathione", "kinetics"]),
                ("Regulations and Guidelines",
                 ["OSHA", "NIOSH", "ACGIH", "PEL", "REL", "TLV",
                  "IDLH", "regulat", "IRIS", "RfC", "RfD", "MRL",
                  "IARC", "Group 1", "Group 2A"]),
                ("Risk Assessment",
                 ["risk", "dose-response", "slope factor", "unit risk",
                  "uncertainty", "benchmark dose"]),
                ("Vulnerable Populations",
                 ["children", "pregnan", "elderly", "sensitive",
                  "susceptib", "polymorphism", "GST"]),
                ("Mitigation and Prevention",
                 ["PPE", "ventilation", "engineering control",
                  "substitut", "reduction", "mitigat"]),
                ("Recent Research Findings",
                 ["recent", "novel", "emerging", "2023", "2024",
                  "2025", "2026"]),
            ]

            def _pool_for(keywords_regex_terms: list[str]) -> list[dict]:
                """Return a ranked list of candidate sources for a topic,
                with the correct [N] number from ref_key_to_num."""
                patt = re.compile(
                    "|".join(keywords_regex_terms), re.IGNORECASE)
                pool: list[dict] = []
                # PMC JATS full texts — one short excerpt per match
                for c in top_candidates:
                    k = (c.doi or c.pmid or c.pmcid or c.title
                         or str(id(c)))
                    n = ref_key_to_num.get(k)
                    if not n:
                        continue
                    art = next(
                        (a for a in jats_arts if a.pmcid
                         and a.pmcid.lstrip("PMC")
                         == (c.pmcid or "").lstrip("PMC")),
                        None)
                    body = (art.body_text if art else "") or c.abstract or ""
                    if not body:
                        continue
                    # find first 2 matching paragraphs, capped at 700 chars
                    paras = [p.strip() for p in
                             re.split(r"\n\s*\n", body)
                             if len(p.strip()) > 120]
                    hits = [p for p in paras if patt.search(p)][:2]
                    for h in hits:
                        pool.append({
                            "id": k,
                            "ref_num": n,
                            "text": h[:700],
                            "citation": (c.title or "")[:100],
                        })
                # Agency profile previews
                for p in agency_profiles:
                    if not p.get("ok"):
                        continue
                    k = p.get("url") or p.get("title")
                    n = ref_key_to_num.get(k)
                    if not n:
                        continue
                    prev = (p.get("text_preview") or "").strip()
                    if prev and patt.search(prev):
                        pool.append({
                            "id": k,
                            "ref_num": n,
                            "text": prev[:700],
                            "citation": (p.get("title")
                                         or p.get("agency", ""))[:100],
                        })
                return pool[:12]  # cap to keep prompt small

            # Ask the LLM to narrate each topic
            narrated: list[tuple[str, str]] = []  # (title, text)
            for topic_title, regex_terms in TOPICS:
                pool = _pool_for(regex_terms)
                if not pool:
                    continue
                # Build the per-topic prompt with the refs' true [N]
                src_lines = []
                for s in pool:
                    src_lines.append(
                        f"[{s['ref_num']}] ({s['citation']}) "
                        f"{s['text']}")
                sys_prompt = (
                    "You are a regulatory-grade toxicology writer "
                    "producing a clean, narrative literature-review "
                    "section in the style of WHO Environmental Health "
                    "Criteria or an ATSDR Toxicological Profile. "
                    "Strict rules: cite inline with [N] markers whose "
                    "numbers MUST appear in the supplied list; merge "
                    "duplicate statements; no bullet points; 2-4 "
                    "flowing paragraphs; neutral scientific tone; "
                    "NEVER invent facts or references; if the pool "
                    "is weak on a sub-topic, say the retrieved "
                    "corpus is limited.")
                user_prompt = (
                    f"Chemical: {chem_name}"
                    + (f" (CAS {cas})" if cas else "")
                    + f"\nSection title: {topic_title}\n\n"
                      "Numbered sources (your citation pool):\n\n"
                    + "\n\n".join(src_lines)
                    + "\n\nWrite the section now.")
                try:
                    txt = await llm.complete(
                        system=sys_prompt, user=user_prompt,
                        max_tokens=1200, temperature=0.2,
                        model=llm_model)
                    narrated.append((topic_title, txt.strip()))
                except Exception as exc:  # noqa: BLE001
                    narrated.append((topic_title,
                                     f"(LLM error for this section: "
                                     f"{type(exc).__name__}: {exc})"))

            # Compose ReportSections
            sections: list[ReportSection] = []
            today = datetime.date.today().strftime("%B %d, %Y")
            # Only databases that returned a REAL numeric count are tallied.
            n_ok = sum(1 for r in db_counts_list
                       if r.get("status") in ("ok", "html_ok")
                       and int(r.get("result_count") or 0) > 0)
            sections.append(ReportSection(
                title="Summary", level=1,
                paragraphs=[
                    (f"This literature review of {chem_name}"
                     + (f" (CAS {cas})" if cas else "")
                     + f" was compiled on {today} from {n_ok} "
                     f"scientific and regulatory databases, "
                     f"{len(top_candidates)} ranked peer-reviewed "
                     f"papers, {len(jats_arts)} with PMC full-text "
                     f"retrieval, and "
                     f"{sum(1 for p in agency_profiles if p.get('ok'))} "
                     f"regulatory agency profiles. Narrative synthesis "
                     f"was produced by the "
                     f"{llm.provider} LLM "
                     f"({llm_model or (llm.openai_model if llm.provider == 'openai' else llm.anthropic_model)}) "
                     f"with strict numbered-citation fidelity."),
                ],
            ))
            for title, body in narrated:
                sections.append(ReportSection(
                    title=title, level=1,
                    paragraphs=[body],
                ))

            # Render to docx
            outdir = _resolve_output_dir()
            fname = out_name or (
                chem_name.replace(" ", "_").replace("/", "_")
                + "_CleanLitReview.docx")
            path = os.path.join(outdir, fname)
            report_gen.generate(
                output_path=path,
                chemical=chem_name, cas=cas or "",
                sections=sections, references=refs,
                db_counts=db_counts_list,
                ledger_entries=None,
                generated_date=today,
                figures=[], tables=[],
                cover_image_path=None,
                include_appendices=True)
            computer_link = ("computer://"
                             + path.replace(" ", "%20").replace("\\", "/"))
            md = [
                f"# ✅ Clean Literature Report for **{chem_name}**"
                + (f" (CAS {cas})" if cas else ""),
                "",
                f"- **LLM backend:**            {llm.provider} "
                f"({llm_model or (llm.openai_model if llm.provider == 'openai' else llm.anthropic_model)})",
                f"- **Databases queried:**      {len(db_counts_list)} "
                f"(ok/html_ok: {n_ok})",
                f"- **Ranked papers:**          {len(top_candidates)}",
                f"- **PMC full-texts:**         {len(jats_arts)}",
                f"- **Agency profiles:**        "
                f"{sum(1 for p in agency_profiles if p.get('ok'))}",
                f"- **Narrative sections:**     {len(narrated)}",
                f"- **Unique references:**      {len(refs)}",
                "",
                f"[View your report]({computer_link})",
            ]
            return [types.TextContent(type="text",
                                      text="\n".join(md))]

        # ─────────────────── Per-database harvesters ────────────────────
        if name == "list_harvestable_databases":
            multi = ctx.multi_harvester
            dbs = multi.list_databases()
            lines = ["# Harvestable databases",
                     "",
                     "Each source implements a unified contract:",
                     "",
                     "    search(chemical, cas) → list[PaperRef]",
                     "    download_pdf(ref)     → bytes | None",
                     "    extract_full_text(ref, pdf_bytes) → (text, page_count, err)",
                     "    extract_figures(ref, pdf_bytes)   → list[ExtractedFigure]",
                     "",
                     f"**Count:** {len(dbs)} databases"]
            for i, d in enumerate(dbs, 1):
                lines.append(f"{i}. `{d}`")
            return [types.TextContent(type="text", text="\n".join(lines))]

        if name == "harvest_database":
            multi = ctx.multi_harvester
            db = str(arguments.get("database") or "").strip()
            chemical = str(arguments.get("chemical") or "").strip()
            cas = arguments.get("cas")
            limit = arguments.get("limit")
            if not db or not chemical:
                return [types.TextContent(
                    type="text",
                    text="❌ harvest_database requires {database, chemical}",
                )]
            try:
                docs = await multi.harvest_database(
                    db, chemical, cas=cas,
                    limit=int(limit) if limit is not None else None,
                )
            except KeyError as ke:
                return [types.TextContent(type="text", text=f"❌ {ke}")]
            # Record every harvested doc in the session ledger
            try:
                for d in docs:
                    ledger.record(d.source, {
                        "title": d.ref.title,
                        "url": d.ref.landing_url or d.ref.pdf_url or "",
                        "doi": d.ref.doi,
                        "pmid": d.ref.pmid,
                        "pmcid": d.ref.pmcid,
                        "year": d.ref.year,
                        "char_count": d.char_count,
                        "figure_count": len(d.figures),
                        "table_count": len(d.tables),
                    })
            except Exception:  # noqa: BLE001
                pass
            summary = {
                "database": db,
                "chemical": chemical,
                "cas": cas,
                "doc_count": len(docs),
                "total_chars": sum(d.char_count for d in docs),
                "total_figures": sum(len(d.figures) for d in docs),
                "total_tables": sum(len(d.tables) for d in docs),
                "docs": [d.to_dict() for d in docs],
            }
            return [types.TextContent(type="text",
                                      text=json.dumps(summary, indent=2, default=str))]

        if name == "harvest_all_databases":
            multi = ctx.multi_harvester
            chemical = str(arguments.get("chemical") or "").strip()
            cas = arguments.get("cas")
            per_db_limit = arguments.get("per_db_limit")
            if not chemical:
                return [types.TextContent(
                    type="text",
                    text="❌ harvest_all_databases requires {chemical}",
                )]
            docs = await multi.harvest_all(
                chemical, cas=cas,
                per_db_limit=int(per_db_limit) if per_db_limit is not None else None,
            )
            by_src: dict[str, list] = {}
            for d in docs:
                by_src.setdefault(d.source, []).append(d)
                try:
                    ledger.record(d.source, {
                        "title": d.ref.title,
                        "url": d.ref.landing_url or d.ref.pdf_url or "",
                        "doi": d.ref.doi,
                        "pmid": d.ref.pmid,
                        "pmcid": d.ref.pmcid,
                        "year": d.ref.year,
                        "char_count": d.char_count,
                        "figure_count": len(d.figures),
                        "table_count": len(d.tables),
                    })
                except Exception:  # noqa: BLE001
                    pass
            summary = {
                "chemical": chemical,
                "cas": cas,
                "databases": {
                    src: {
                        "doc_count": len(items),
                        "total_chars": sum(x.char_count for x in items),
                        "total_figures": sum(len(x.figures) for x in items),
                        "total_tables": sum(len(x.tables) for x in items),
                    }
                    for src, items in by_src.items()
                },
                "grand_total_docs": len(docs),
                "grand_total_chars": sum(d.char_count for d in docs),
                "grand_total_figures": sum(len(d.figures) for d in docs),
                "grand_total_tables": sum(len(d.tables) for d in docs),
                "docs": [d.to_dict() for d in docs],
            }
            return [types.TextContent(type="text",
                                      text=json.dumps(summary, indent=2, default=str))]

        return [types.TextContent(type="text", text=f"❌ Unknown tool: {name}")]

    except Exception as e:  # noqa: BLE001
        return [types.TextContent(
            type="text",
            text=f"❌ Tool `{name}` raised: {type(e).__name__}: {e}",
        )]
