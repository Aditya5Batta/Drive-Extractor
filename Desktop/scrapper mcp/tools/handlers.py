"""
tools.handlers — MCP tool dispatch for the four-tool surface.

Every MCP call enters through `dispatch(name, arguments, ctx)`. The ctx
parameter is a ToolContext (tools/context.py) carrying every shared client.
The four branches below delegate to the pipeline modules — all heavy lifting
lives in pipeline.resolve / pipeline.harvest / pipeline.review / pipeline.audit.
"""
from __future__ import annotations
import json
import os

import mcp.types as types

from pipeline.audit    import audit_run      as _audit_run
from pipeline.harvest  import harvest_evidence as _harvest_evidence
from pipeline.resolve  import resolve_chemical as _resolve_chemical
from pipeline.review   import build_review   as _build_review
from sources._models   import ChemicalIdentity


def _ok(payload: dict | list | str) -> list[types.TextContent]:
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = str(payload)
    return [types.TextContent(type="text", text=text)]


def _err(name: str, exc: Exception) -> list[types.TextContent]:
    return _ok({"ok": False, "tool": name,
                "error": f"{type(exc).__name__}: {exc}"})


async def dispatch(name: str, arguments: dict, ctx) -> list[types.TextContent]:
    """Look up the tool by name and run its handler with the shared context."""
    try:
        # ───────────────────────────────────────────────────────────
        #  1. resolve_chemical
        # ───────────────────────────────────────────────────────────
        if name == "resolve_chemical":
            query = arguments.get("query", "")
            result = await _resolve_chemical(ctx.pubchem, query)
            return _ok(result)

        # ───────────────────────────────────────────────────────────
        #  2. harvest_evidence
        # ───────────────────────────────────────────────────────────
        if name == "harvest_evidence":
            chemical = arguments["chemical"]
            cas      = arguments.get("cas")
            # Attach a tracer + ledger for this run if one isn't already bound.
            if ctx.tracer is None or ctx.evidence_ledger is None:
                ctx.bind_run(chemical)

            result = await _harvest_evidence(
                chemical=chemical,
                cas=cas,
                multi_harvester=ctx.multi_harvester,
                papers=ctx.papers,
                openalex=ctx.openalex,
                pmc=ctx.pmc,
                agency=ctx.agency,
                tracer=ctx.tracer,
                ledger=ctx.evidence_ledger,
                per_db_limit=arguments.get("per_db_limit", 15),
                include_pubmed=arguments.get("include_pubmed", True),
                include_openalex=arguments.get("include_openalex", True),
                include_agencies=arguments.get("include_agencies", True),
            )
            payload = result.to_dict()
            payload["run_id"]      = ctx.tracer.run_id if ctx.tracer else None
            payload["trace_path"]  = str(ctx.tracer.trace_path) if ctx.tracer else None
            payload["ledger_path"] = str(ctx.evidence_ledger.path) if ctx.evidence_ledger else None
            payload["ok"] = True
            return _ok(payload)

        # ───────────────────────────────────────────────────────────
        #  3. build_review
        # ───────────────────────────────────────────────────────────
        if name == "build_review":
            chemical    = arguments["chemical"]
            output_path = arguments["output_path"]
            cas         = arguments.get("cas")
            cover_image = arguments.get("cover_image_path")
            include_app = arguments.get("include_appendices", True)
            per_db      = arguments.get("per_db_limit", 15)

            # Bind run (if not already) so we get a ledger + tracer
            if ctx.tracer is None or ctx.evidence_ledger is None:
                ctx.bind_run(chemical)

            # 1) Resolve identity (the composer needs formula/MW/IUPAC name)
            identity_dict = await _resolve_chemical(ctx.pubchem, chemical)
            identity = ChemicalIdentity(
                query=chemical,
                cid=identity_dict.get("cid"),
                cas=identity_dict.get("cas") or cas,
                iupac_name=identity_dict.get("iupac_name"),
                canonical_smiles=identity_dict.get("canonical_smiles"),
                isomeric_smiles=identity_dict.get("isomeric_smiles"),
                inchi=identity_dict.get("inchi"),
                inchikey=identity_dict.get("inchikey"),
                molecular_formula=identity_dict.get("molecular_formula"),
                molecular_weight=identity_dict.get("molecular_weight"),
                synonyms=identity_dict.get("synonyms") or [],
                common_name=identity_dict.get("common_name"),
                pubchem_url=identity_dict.get("pubchem_url"),
                error=identity_dict.get("error"),
            )

            # 2) Harvest evidence
            harvest = await _harvest_evidence(
                chemical=chemical,
                cas=identity.cas or cas,
                multi_harvester=ctx.multi_harvester,
                papers=ctx.papers,
                openalex=ctx.openalex,
                pmc=ctx.pmc,
                agency=ctx.agency,
                tracer=ctx.tracer,
                ledger=ctx.evidence_ledger,
                per_db_limit=per_db,
            )

            # 3) Compose
            with (ctx.tracer.step("compose.review") if ctx.tracer
                  else _noop_ctx()):
                review = _build_review(
                    chemical=chemical,
                    identity=identity,
                    harvest=harvest,
                    output_path=output_path,
                    report_gen=ctx.report_gen,
                    cover_image_path=cover_image,
                    include_appendices=include_app,
                )

            payload = review.to_dict()
            payload["ok"]          = True
            payload["run_id"]      = ctx.tracer.run_id if ctx.tracer else None
            payload["trace_path"]  = str(ctx.tracer.trace_path) if ctx.tracer else None
            payload["ledger_path"] = str(ctx.evidence_ledger.path) if ctx.evidence_ledger else None
            payload["per_database"]        = harvest.per_database
            payload["per_database_errors"] = harvest.per_database_errors
            payload["total_documents"]     = harvest.total_documents
            payload["total_records"]       = harvest.total_records
            return _ok(payload)

        # ───────────────────────────────────────────────────────────
        #  4. audit_run
        # ───────────────────────────────────────────────────────────
        if name == "audit_run":
            result = await _audit_run(
                run_id=arguments["run_id"],
                docx_path=arguments["docx_path"],
                ledger_path=arguments["ledger_path"],
                trace_path=arguments.get("trace_path"),
                chemical=arguments.get("chemical"),
                check_urls=arguments.get("check_urls", False),
                fetcher=ctx.fetcher if arguments.get("check_urls") else None,
            )
            return _ok(result.to_dict())

        # ───────────────────────────────────────────────────────────
        #  Unknown tool
        # ───────────────────────────────────────────────────────────
        return _ok({"ok": False, "error": f"unknown tool: {name}",
                     "available": ["resolve_chemical", "harvest_evidence",
                                   "build_review", "audit_run"]})

    except Exception as exc:
        return _err(name, exc)


class _noop_ctx:
    """Fallback context manager when tracer is unbound (mirrors tracer.step API)."""
    def __enter__(self):  return self
    def __exit__(self, *_):  return False
