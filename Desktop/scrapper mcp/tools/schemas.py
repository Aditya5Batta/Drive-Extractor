"""
tools.schemas — The four-tool MCP surface.

After consolidation this server exposes exactly four semantic tools:

    1. resolve_chemical   — canonical identity for a name / CAS / SMILES / CID
    2. harvest_evidence   — one-shot evidence harvest across all 14 databases,
                            PubMed/EuropePMC, OpenAlex and 7 regulatory agencies,
                            keyword-routed into the 29 canonical review sections
    3. build_review       — compose the final Benzene-v3-style .docx from a harvest
    4. audit_run          — post-build hallucination / coverage / URL audit

This replaces the previous 32-tool surface. All low-level primitives (fetch_url,
fetch_pdf, extract_pdf_figures, find_papers, etc.) still exist inside the
pipeline but are no longer individually exposed — the four tools above compose
them internally so the MCP surface stays stable and reviewable.
"""
from __future__ import annotations
import mcp.types as types


ALL_TOOL_SCHEMAS: list[types.Tool] = [
    # ───────────────────────────────────────────────────────────────────
    #  1. resolve_chemical
    # ───────────────────────────────────────────────────────────────────
    types.Tool(
        name="resolve_chemical",
        description=(
            "Resolve a chemical query (name, CAS, SMILES, InChI, InChIKey, "
            "or PubChem CID) to a canonical identity dict: CID, CAS, IUPAC "
            "name, canonical/isomeric SMILES, InChI/InChIKey, molecular "
            "formula, molecular weight, synonyms, and PubChem URL. "
            "Must be called first; the returned identity is the input to "
            "harvest_evidence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description":
                        "Chemical name, CAS registry number, SMILES string, "
                        "InChI/InChIKey, or PubChem CID.",
                },
            },
            "required": ["query"],
        },
    ),

    # ───────────────────────────────────────────────────────────────────
    #  2. harvest_evidence
    # ───────────────────────────────────────────────────────────────────
    types.Tool(
        name="harvest_evidence",
        description=(
            "One-shot evidence harvest for a resolved chemical. Runs the "
            "14-database parallel harvester, searches PubMed/EuropePMC, "
            "counts OpenAlex matches, and fetches ATSDR/NTP/EPA IRIS/"
            "NIOSH/OEHHA/OSHA/ILO agency profiles. Every harvested "
            "document is scanned for the 570 section-anchor keywords and "
            "routed to its correct section in the 29-section canonical "
            "toxicology review schema. Writes every hit to the run's "
            "EvidenceLedger (JSONL) and emits structured trace events. "
            "Returns a HarvestResult with per_database counts, per_section "
            "evidence, agency profiles, and error diagnostics. Zero LLM "
            "calls — every excerpt is verbatim."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chemical": {
                    "type": "string",
                    "description":
                        "Display/search name (preferably the IUPAC or common "
                        "name returned by resolve_chemical).",
                },
                "cas": {
                    "type": "string",
                    "description":
                        "CAS registry number, if known. Used for CAS-specific "
                        "queries against databases that support it.",
                },
                "per_db_limit": {
                    "type": "integer",
                    "default": 15,
                    "description":
                        "Maximum documents to pull per database (keeps the "
                        "run bounded). 15 is sensible for production.",
                },
                "include_pubmed":   {"type": "boolean", "default": True},
                "include_openalex": {"type": "boolean", "default": True},
                "include_agencies": {"type": "boolean", "default": True},
            },
            "required": ["chemical"],
        },
    ),

    # ───────────────────────────────────────────────────────────────────
    #  3. build_review
    # ───────────────────────────────────────────────────────────────────
    types.Tool(
        name="build_review",
        description=(
            "Compose the final Benzene-v3-style .docx literature review for "
            "a chemical. Must be called AFTER resolve_chemical + "
            "harvest_evidence. The composer mirrors the reference layout: "
            "cover page, Summary, Scope/Methodology, Key Findings, "
            "Database Coverage, then the 9 canonical chapters "
            "(Chemical Properties, Environmental Behaviour, Toxicokinetics, "
            "Sources of Exposure, Health Effects, Regulations, Risk "
            "Assessment, Mitigation, Recent Research), followed by "
            "References and Appendices A/B. Every body paragraph is a "
            "verbatim excerpt from a harvested source with inline [n] "
            "citations. Zero LLM calls. Returns the output path plus a "
            "dict summarising section count, references, and any empty "
            "sections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chemical": {
                    "type": "string",
                    "description":
                        "Chemical display name (same value passed to harvest_evidence).",
                },
                "cas": {"type": "string"},
                "output_path": {
                    "type": "string",
                    "description":
                        "Absolute path where the .docx will be written. The "
                        "parent directory is created if missing.",
                },
                "per_db_limit": {"type": "integer", "default": 15},
                "cover_image_path": {
                    "type": "string",
                    "description":
                        "Optional path to a chemical-structure PNG for the cover page.",
                },
                "include_appendices": {"type": "boolean", "default": True},
            },
            "required": ["chemical", "output_path"],
        },
    ),

    # ───────────────────────────────────────────────────────────────────
    #  4. audit_run
    # ───────────────────────────────────────────────────────────────────
    types.Tool(
        name="audit_run",
        description=(
            "Post-build hallucination and coverage audit. Inspects the "
            "finished .docx, the evidence ledger JSONL written by "
            "harvest_evidence, and the tracer JSONL, then reports: "
            "(a) which of the 29 canonical sections got zero evidence, "
            "(b) any body paragraphs in the docx that do NOT appear as "
            "substrings of any ledger excerpt (potential hallucinations), "
            "(c) optional URL health check across every ledger URL, "
            "(d) database coverage breakdown, (e) step-level ok/warn/err "
            "counts from the tracer. Returns a single audit dict with an "
            "overall status of 'ok' / 'warn' / 'fail'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description":
                        "Run identifier (Tracer.run_id — returned by the "
                        "previous build_review response).",
                },
                "docx_path": {
                    "type": "string",
                    "description": "Absolute path to the finished .docx under audit.",
                },
                "ledger_path": {
                    "type": "string",
                    "description":
                        "Absolute path to the evidence ledger JSONL produced "
                        "during the same run.",
                },
                "trace_path": {
                    "type": "string",
                    "description":
                        "Optional absolute path to the tracer JSONL (for "
                        "step-level summary in the response).",
                },
                "chemical":   {"type": "string"},
                "check_urls": {
                    "type": "boolean", "default": False,
                    "description":
                        "If true, probe every ledger URL for HTTP status. "
                        "Slower but catches broken citations.",
                },
            },
            "required": ["run_id", "docx_path", "ledger_path"],
        },
    ),
]


__all__ = ["ALL_TOOL_SCHEMAS"]
