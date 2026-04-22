"""
tools.schemas — All 28 MCP tool schemas.

Pure data module. Every tool's inputSchema is declared here so non-developers
can see the full MCP surface area in one place. The actual handlers live in
tools/handlers.py and are looked up by name.
"""
from __future__ import annotations
import mcp.types as types


ALL_TOOL_SCHEMAS: list[types.Tool] = [
            types.Tool(
                name="fetch_url",
                description=(
                    "Fetch any URL and return clean extracted text. "
                    "Auto-detects PDF vs HTML. Use for opening tox database pages, "
                    "PubMed abstracts, full-text articles, regulatory docs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Any HTTP(S) URL"},
                        "include_links": {
                            "type": "boolean", "default": False,
                            "description": "Also return outgoing <a> links (for following deeper)",
                        },
                        "preview_chars": {
                            "type": "integer", "default": 6000,
                            "description": "Max chars of extracted text to return in the response",
                        },
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="fetch_pdf",
                description="Force-fetch a URL as PDF and return extracted full text (all pages).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "preview_chars": {"type": "integer", "default": 10000},
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="fetch_pdf_bytes",
                description=(
                    "Fetch a PDF URL and return base64-encoded raw bytes so a local "
                    "PyMuPDF (fitz) runtime can extract figures. Payload is capped at "
                    "20 MB. Use this whenever the caller has PyMuPDF locally but the "
                    "MCP server does not."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_bytes": {"type": "integer", "default": 20_000_000},
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="search_in_content",
                description=(
                    "Given a URL, fetch it and search for keywords with surrounding context. "
                    "Perfect for finding 'benzene carcinogenicity', 'benzene neurotoxicity', etc. "
                    "across a fetched page."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "keywords": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Keywords or phrases to find (case-insensitive, word-boundary)",
                        },
                        "context_chars": {"type": "integer", "default": 300},
                        "max_hits_per_keyword": {"type": "integer", "default": 5},
                    },
                    "required": ["url", "keywords"],
                },
            ),
            types.Tool(
                name="extract_links",
                description=(
                    "Fetch a URL and return all outgoing links, marking which are PDFs "
                    "and which are same-domain. Use to discover deeper content to follow."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "pdf_only": {"type": "boolean", "default": False},
                        "same_domain_only": {"type": "boolean", "default": False},
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="batch_scrape",
                description=(
                    "THE MAIN ELIXIR: Given N URLs (e.g., 30 tox database hits for a chemical) "
                    "and M keywords (e.g., endpoint keywords like 'carcinogenicity', 'neurotoxicity', "
                    "'leukemia', 'CYP2E1'), fetch all URLs in parallel, extract full text from "
                    "HTML + PDFs, search for keywords, and return results ranked by relevance."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "max_hits_per_keyword": {"type": "integer", "default": 3},
                        "min_relevance": {
                            "type": "number", "default": 0.0,
                            "description": "Drop results with relevance_score below this (0.0–1.0)",
                        },
                    },
                    "required": ["urls", "keywords"],
                },
            ),
            types.Tool(
                name="list_databases",
                description="List curated HIGH/MED-value toxicology databases from the MVP1 registry.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="resolve_chemical",
                description=(
                    "Resolve a chemical identity via PubChem PUG REST. Accepts name, CAS, "
                    "SMILES, InChI, InChIKey, or CID. Returns CID, CAS number, molecular "
                    "weight, molecular formula, canonical + isomeric SMILES, InChI, InChIKey, "
                    "IUPAC name, common name, and synonyms. ALWAYS run this FIRST before "
                    "find_papers or batch_scrape so downstream queries use the correct "
                    "common name + CAS."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Chemical name, CAS, SMILES, InChI, InChIKey, or CID",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="find_papers",
                description=(
                    "FULL PIPELINE: chemical + endpoint keywords → search PubMed + EuropePMC "
                    "→ prioritize papers with free full text / PMC / PDFs → fetch + extract "
                    "full text from top N → run KWIC keyword ranking. Returns ranked papers "
                    "with snippets. This is the all-in-one 'give me relevant tox evidence' tool. "
                    "Now includes a title-relevance filter to reject off-topic hits where the "
                    "chemical name doesn't appear in title/abstract. Automatically records all "
                    "retrieved papers to the extraction ledger."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {
                            "type": "string",
                            "description": "Chemical name (common name preferred, e.g. 'benzene')",
                        },
                        "keywords": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Endpoint keywords (e.g. ['carcinogenicity','leukemia','CYP2E1'])",
                        },
                        "max_results": {
                            "type": "integer", "default": 30,
                            "description": "Max combined results from PubMed + EuropePMC before ranking",
                        },
                        "fetch_top_n": {
                            "type": "integer", "default": 10,
                            "description": "After priority ranking, fetch + KWIC-score this many papers",
                        },
                        "free_full_text_only": {
                            "type": "boolean", "default": True,
                            "description": "Restrict to open-access / free full-text papers",
                        },
                        "min_title_relevance": {
                            "type": "number", "default": 0.25,
                            "description": "Drop results where chemical+top-keywords appear in <this fraction of title+abstract. 0.0 disables filter.",
                        },
                    },
                    "required": ["chemical", "keywords"],
                },
            ),
            types.Tool(
                name="find_papers_openalex",
                description=(
                    "Search OpenAlex Works API (~240M works indexed). Complements PubMed + "
                    "EuropePMC by surfacing preprints, reports, and OA papers those miss. "
                    "Returns ranked PaperCandidate list (same schema as find_papers)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "per_page": {"type": "integer", "default": 25},
                        "oa_only": {"type": "boolean", "default": True,
                                    "description": "Restrict to open-access works."},
                        "min_title_relevance": {"type": "number", "default": 0.25},
                    },
                    "required": ["chemical", "keywords"],
                },
            ),
            types.Tool(
                name="fetch_pmc_article",
                description=(
                    "Harvest a PMC article by PMCID (or PMC URL): returns clean full text, "
                    "abstract, figure URLs, table URLs, PDF URL, and the reference list "
                    "(DOIs + PMIDs of all cited papers). Use this AFTER find_papers when you "
                    "want deeper content from a specific paper, or to harvest canonical papers "
                    "from a review article's citation network."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pmcid": {
                            "type": "string",
                            "description": "PMCID (e.g. 'PMC12578042' or '12578042') or full PMC article URL",
                        },
                    },
                    "required": ["pmcid"],
                },
            ),
            types.Tool(
                name="harvest_references",
                description=(
                    "Given a PMC article, extract its cited papers and fetch summaries for each. "
                    "Powers corpus expansion: if find_papers returns one good review, this tool "
                    "finds the canonical primary papers it cites — often higher-quality than "
                    "the keyword-search hits. Returns PaperCandidate list."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pmcid": {"type": "string"},
                        "max_refs": {"type": "integer", "default": 30,
                                     "description": "Cap number of references to fetch metadata for"},
                    },
                    "required": ["pmcid"],
                },
            ),
            types.Tool(
                name="fetch_agency_profile",
                description=(
                    "One-shot fetch of authoritative agency chemical profiles: ATSDR ToxFAQs, "
                    "EPA IRIS, NTP ROC, NIOSH Pocket Guide, ILO ICSC, CalEPA OEHHA, OSHA, "
                    "Haz-Map. Constructs URLs from chemical name + CAS and fetches in parallel. "
                    "Returns per-agency text previews, success flags, and titles. Best ran right "
                    "after resolve_chemical."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string",
                                "description": "CAS number (optional but improves ICSC/ATSDR matching)"},
                    },
                    "required": ["chemical"],
                },
            ),
            types.Tool(
                name="get_extraction_ledger",
                description=(
                    "Return the session-level extraction ledger: counts of papers/docs pulled "
                    "from each of the 30+ curated databases. Required for regulatory-grade "
                    "dossier provenance/traceability. Call at the end of a session to get the "
                    "appendix-ready source breakdown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string", "default": "markdown",
                            "enum": ["markdown", "json"],
                            "description": "'markdown' for human-readable table, 'json' for programmatic use",
                        },
                    },
                },
            ),
            types.Tool(
                name="reset_extraction_ledger",
                description="Clear the extraction ledger. Call at the start of a new chemical/report.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="build_evidence_record",
                description=(
                    "Fetch a URL and return a list of EvidenceRecords: each one is a "
                    "verbatim quote (character-for-character from the source) with full "
                    "source metadata — URL, database, DOI/PMID/PMCID, title, keyword "
                    "matched, character offset, context before/after, and a stable "
                    "evidence_id. Designed for citation-traceable report writing: every "
                    "quote used in the final report should map to one of these records. "
                    "No LLM summarization — only verbatim extraction."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "source_db": {"type": "string", "default": "",
                                      "description": "Database label to tag records with (e.g. 'PMC', 'ATSDR')."},
                        "doi": {"type": "string"},
                        "pmid": {"type": "string"},
                        "pmcid": {"type": "string"},
                        "max_per_keyword": {"type": "integer", "default": 3},
                        "quote_chars": {"type": "integer", "default": 400,
                                        "description": "Target length of each verbatim quote."},
                        "context_chars": {"type": "integer", "default": 200,
                                          "description": "Chars of verbatim context on each side of the quote."},
                    },
                    "required": ["url", "keywords"],
                },
            ),
            types.Tool(
                name="extract_pdf_tables",
                description=(
                    "Fetch a PDF URL and extract structured tables with pdfplumber. "
                    "Returns each table with page_number, row/column counts, raw rows, "
                    "and a markdown rendering. Use this when a tox source has numeric "
                    "data (LD50, NOAEL, carcinogenicity slopes, MRLs) that you want in "
                    "the report as structured tables rather than as flowing prose."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_tables": {"type": "integer", "default": 50},
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="extract_pdf_figures",
                description=(
                    "Fetch a PDF URL and extract embedded images using PyMuPDF. "
                    "Each image is saved to the session figures directory and returned "
                    "with page_number, dimensions, and a best-effort caption guess "
                    "(lines near the image beginning with 'Figure N' / 'Fig. N'). "
                    "Use this for research papers where graphs/dose-response curves "
                    "are essential to the narrative."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "min_bytes": {"type": "integer", "default": 2000,
                                      "description": "Drop images smaller than this (filter thumbnails/icons)."},
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="fetch_pmc_jats",
                description=(
                    "Fetch a PMC article as JATS XML via NCBI efetch and parse it "
                    "structurally. Returns title, abstract, body text, structured "
                    "tables (with rows + caption), figure captions + graphic hrefs, "
                    "and a full reference list with DOI/PMID/PMCID. This is much "
                    "cleaner than HTML scraping and is the preferred way to get "
                    "multimodal content from PMC papers for the report."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pmcid": {"type": "string",
                                  "description": "PMCID (e.g. 'PMC12578042' or '12578042') or full PMC URL"},
                    },
                    "required": ["pmcid"],
                },
            ),
            types.Tool(
                name="build_database_urls",
                description=(
                    "Given a chemical name + (optional) CAS, return the canonical "
                    "search-page URLs for ~30 curated toxicology databases (PubMed, "
                    "PMC, EuropePMC, OpenAlex, Semantic Scholar, Unpaywall, Zenodo, "
                    "ATSDR, NTP, EPA IRIS, EPA CompTox, CalEPA OEHHA, NIOSH, OSHA, "
                    "ILO ICSC, Canada DSL, ECHA, CONCAWE, Silent Spring, WHO INCHEM, "
                    "WHO IPCS, eChemPortal, HCIS, SafeWork Australia, AICIS, Japan "
                    "NITE, Japan PRTR, Korea MOE, Haz-Map, CPDB, PubChem). Pass the "
                    "returned list to batch_scrape or fetch_url to retrieve content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string"},
                    },
                    "required": ["chemical"],
                },
            ),
            types.Tool(
                name="count_papers_by_database",
                description=(
                    "Deterministically query EVERY supported toxicology database for "
                    "a chemical and return the paper/document count from each. For "
                    "databases with real APIs (PubMed, PMC, EuropePMC, OpenAlex, "
                    "Semantic Scholar, CrossRef, Zenodo, PubChem, ILO ICSC) it "
                    "returns verified counts. For agency sites without a JSON API but "
                    "with a publicly scrapable HTML record (EPA IRIS, NIOSH NPG, "
                    "ATSDR ToxProfile+MRL, NTP RoC, WHO INCHEM/IPCS EHC, Australia HCIS) "
                    "it returns status='html_ok' with the canonical landing-page URL "
                    "that callers can fetch via fetch_url. Truly JS-gated or session-"
                    "locked sites (ECHA, eChemPortal, AICIS, Japan NITE, Korea MOE, "
                    "CPDB, NIOSH IDLH) return status='no_api'. This never hallucinates "
                    "numbers. Produces the table that goes in Appendix A of the "
                    "literature review report."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string",
                                "description": "Optional CAS number (improves ILO ICSC + PubChem matching)"},
                    },
                    "required": ["chemical"],
                },
            ),
            types.Tool(
                name="generate_report",
                description=(
                    "LOW-LEVEL DOCX WRITER — DO NOT use this when the user "
                    "asks for a literature report of a chemical; use "
                    "`generate_chemical_report` instead. This tool requires "
                    "the caller to supply pre-composed `sections` and "
                    "`references` arrays (scraped from tox-scraper tools). "
                    "If the caller provides AI-authored narrative text as "
                    "sections, the resulting document will CONTAIN "
                    "HALLUCINATIONS — this tool performs NO verification. "
                    "Only use when the user explicitly supplies structured "
                    "section data or when chaining after manual tox-scraper "
                    "tool calls. Generates a Times New Roman 12pt Word "
                    "(.docx) with Appendix A (per-database counts) and "
                    "Appendix B (extraction ledger). Returns a computer:// "
                    "link."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string"},
                        "sections": {
                            "type": "array",
                            "description": "List of sections. Each: {title, level, paragraphs: [text,...], subsections: [...]}",
                            "items": {"type": "object"},
                        },
                        "references": {
                            "type": "array",
                            "description": "List of reference dicts: {number, authors, year, title, journal, doi, pmid, pmcid, url, database}",
                            "items": {"type": "object"},
                        },
                        "db_counts": {
                            "type": "array",
                            "description": "Per-DB count dicts from count_papers_by_database (for Appendix A)",
                            "items": {"type": "object"},
                        },
                        "include_extraction_ledger": {
                            "type": "boolean", "default": True,
                            "description": "Append Appendix B with the per-source ExtractionLedger contents",
                        },
                        "output_filename": {
                            "type": "string",
                            "description": "Optional custom filename (default: <chemical>_LitReview.docx)",
                        },
                        "generated_date": {
                            "type": "string",
                            "description": "ISO date (YYYY-MM-DD); defaults to today",
                        },
                    },
                    "required": ["chemical", "sections", "references"],
                },
            ),
            types.Tool(
                name="generate_chemical_report",
                description=(
                    "★ PRIMARY ENTRY POINT ★ — Use this tool whenever the "
                    "user asks for a 'literature report', 'literature "
                    "review', 'tox report', 'chemical report', or any "
                    "research summary of a chemical. Accepts just a "
                    "chemical name (and optional CAS) — everything else is "
                    "harvested live from authoritative sources. Zero "
                    "hallucination: every claim is traced to PubMed, "
                    "PubMed Central, Europe PMC, OpenAlex, PubChem, "
                    "CrossRef, or a regulatory agency page (ATSDR, NTP, "
                    "EPA IRIS, CalEPA OEHHA, NIOSH, OSHA, ECHA, IARC, "
                    "WHO, Haz-Map, ILO ICSC). Pipeline stages: "
                    "(1) PubChem identity, (2) 34-database count sweep, "
                    "(3) PubMed+EuropePMC paper harvest with "
                    "title-relevance filtering, (4) PMC JATS full-text "
                    "retrieval for the top N, (5) regulatory agency "
                    "profiles, (6) section composition from live "
                    "evidence, (7) reference list with DOI/PMID/PMCID, "
                    "(8) justified Times-New-Roman .docx with Appendix A "
                    "per-database counts. Returns computer:// link. "
                    "Choose this over `generate_report` whenever the "
                    "user has not supplied pre-composed section content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string",
                                "description": "Optional CAS (improves matching)"},
                        "keywords": {
                            "type": "array", "items": {"type": "string"},
                            "description": (
                                "Endpoint keywords (e.g. ['carcinogenicity',"
                                "'neurotoxicity','hematotoxicity']). Defaults "
                                "to a generic tox set if omitted."
                            ),
                        },
                        "fetch_top_n": {
                            "type": "number", "default": 6,
                            "description": (
                                "How many top PMC papers to fetch full text "
                                "of. Default 6 keeps the pipeline under the "
                                "60s MCP timeout. Use 10-15 for deeper "
                                "reports if the MCP client allows a longer "
                                "timeout."
                            ),
                        },
                        "output_filename": {"type": "string",
                            "description": "Defaults to <chemical>_LitReview.docx"},
                    },
                    "required": ["chemical"],
                },
            ),
            types.Tool(
                name="llm_status",
                description=(
                    "Report which LLM backend the server is currently wired "
                    "to (OpenAI or Anthropic), which model it will use by "
                    "default, and which API keys are detected in the "
                    "environment. Use first when debugging whether "
                    "LLM-powered tools (llm_synthesize, llm_narrate_section, "
                    "llm_clean_report) are ready. No arguments."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="llm_synthesize",
                description=(
                    "Generic LLM call. Send a system prompt + user prompt; "
                    "receive the completion as plain text. Routes to OpenAI "
                    "or Anthropic automatically based on env vars "
                    "(OPENAI_API_KEY / ANTHROPIC_API_KEY, override with "
                    "TOX_SCRAPER_LLM)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "system": {"type": "string",
                                   "description": "System prompt"},
                        "user": {"type": "string",
                                 "description": "User prompt / content"},
                        "max_tokens": {"type": "number", "default": 1500},
                        "temperature": {"type": "number", "default": 0.2},
                        "model": {"type": "string",
                                  "description": "Optional per-call model "
                                                 "override"},
                    },
                    "required": ["system", "user"],
                },
            ),
            types.Tool(
                name="llm_narrate_section",
                description=(
                    "Turn a list of verbatim source excerpts into a single "
                    "clean narrative paragraph block suitable for a "
                    "benzene-style report. The LLM is strictly instructed "
                    "to cite only using [N] markers that map to the "
                    "supplied source list, never to invent sources or "
                    "facts, and to merge duplicate statements. Returns "
                    "polished prose + the citation map."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "section_title": {"type": "string",
                            "description": "e.g. 'Health Effects — "
                                           "Haematotoxicity'"},
                        "sources": {
                            "type": "array",
                            "description": ("Ordered list of sources. Each "
                                           "item is quoted verbatim and "
                                           "referenced as [N] by position."),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string",
                                           "description": "Stable ref id "
                                                          "(PMCID, DOI, URL)"},
                                    "text": {"type": "string",
                                             "description": "Verbatim excerpt"},
                                    "citation": {"type": "string",
                                                 "description": ("Human "
                                                                 "citation, "
                                                                 "e.g. 'Smith "
                                                                 "et al. 2021'"),
                                                 },
                                },
                                "required": ["text"],
                            },
                        },
                        "max_words": {"type": "number", "default": 400},
                    },
                    "required": ["chemical", "section_title", "sources"],
                },
            ),
            types.Tool(
                name="llm_dedupe_paragraphs",
                description=(
                    "Given a list of paragraphs (possibly extracted "
                    "verbatim from multiple papers), return a deduplicated "
                    "clustered list: near-duplicates merged, distinct "
                    "points preserved, each cluster annotated with the "
                    "source ids that contributed to it. Useful before "
                    "narrating a section to avoid benzene-report-style "
                    "repetition."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "paragraphs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["text"],
                            },
                        },
                    },
                    "required": ["paragraphs"],
                },
            ),
            types.Tool(
                name="llm_clean_report",
                description=(
                    "★ CLEAN BENZENE-STYLE REPORT ★ — Run the full "
                    "chemical-agnostic pipeline (PubChem → 34-db count "
                    "sweep → PubMed/EuropePMC harvest → PMC JATS full "
                    "text → agency profiles), then use the configured "
                    "LLM (OpenAI or Anthropic) to fuse the verbatim "
                    "evidence into a clean benzene-style narrative docx: "
                    "one polished paragraph per topic, numbered [N] "
                    "inline citations, single deduped reference list, "
                    "no themed-paragraph repetition. Requires "
                    "OPENAI_API_KEY or ANTHROPIC_API_KEY."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string"},
                        "keywords": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "fetch_top_n": {"type": "number", "default": 8},
                        "output_filename": {"type": "string"},
                        "llm_model": {"type": "string",
                            "description": ("Optional per-call LLM model "
                                            "override")},
                    },
                    "required": ["chemical"],
                },
            ),
            types.Tool(
                name="list_harvestable_databases",
                description=(
                    "List every per-database source the MultiDatabaseHarvester "
                    "can run (the 14 DBs the user verified live: Semantic Scholar, "
                    "NTP, WHO INCHEM, WHO IPCS, ECHA, ATSDR, CalEPA OEHHA, Canada "
                    "CEPA/DSL, CONCAWE, NIOSH, OSHA, Australia AICIS, ILO ICSC, "
                    "Zenodo). Each source implements a unified "
                    "search → PDF download → full text → figures contract."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="harvest_database",
                description=(
                    "Run the full search → PDF download → full-text extraction → "
                    "figure extraction pipeline for ONE named database. Returns "
                    "a list of HarvestedDocument, each with the source, PaperRef, "
                    "full_text (truncated to 4000 chars in the MCP reply — full "
                    "text is retained in-process for report composition), "
                    "extracted tables, and extracted PDF figures. "
                    "Call list_harvestable_databases first to get valid names."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "database": {"type": "string",
                                     "description": "Exact DB name, e.g. 'ATSDR' or 'Semantic Scholar'"},
                        "chemical": {"type": "string"},
                        "cas": {"type": "string"},
                        "limit": {"type": "number",
                                  "description": "Max papers to full-text (default per-source, usually 3-6)"},
                    },
                    "required": ["database", "chemical"],
                },
            ),
            types.Tool(
                name="harvest_all_databases",
                description=(
                    "Run the full search → PDF → full text → figures pipeline "
                    "against ALL 14 per-database harvesters in parallel. Returns "
                    "a flat list of HarvestedDocument aggregated across every DB. "
                    "This is the workhorse used by generate_chemical_report to "
                    "build a 200+ page literature review. Every source is "
                    "deterministic — same chemical name yields the same hits "
                    "unless upstream databases have added new records."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chemical": {"type": "string"},
                        "cas": {"type": "string"},
                        "per_db_limit": {"type": "number",
                                         "description": "Max papers per database (default per-source, usually 3-6)"},
                    },
                    "required": ["chemical"],
                },
            ),
]
