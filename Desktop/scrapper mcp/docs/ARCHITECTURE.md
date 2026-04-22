# Tox-Scraper Architecture

One-page map of how the package is organised. Read top to bottom.

## Folder layout

```
scrapper mcp/
├── server.py                ← MCP entry point (~180 lines)
├── prompts.py               ← MCP prompt templates
├── README.md
├── requirements.txt
│
├── config/                  ← Static configuration
│   ├── settings.py          ← ScraperConfig (timeouts, caps, user agent)
│   ├── databases.py         ← 31 curated tox DBs (HIGH + MED tier)
│   └── keywords.py          ← Tox keyword bank (cancer, neuro, PK, etc.)
│
├── core/                    ← Reusable primitives
│   ├── http.py              ← HTTPFetcher — the one http client
│   ├── html.py              ← HTMLExtractor — trafilatura + BeautifulSoup
│   ├── pdf.py               ← PDFExtractor (pdfminer + pypdf)
│   ├── pdf_multimodal.py    ← PDFTableExtractor, PDFFigureExtractor
│   ├── keywords.py          ← KeywordSearcher (KWIC matcher)
│   ├── llm.py               ← LLMAdapter (OpenAI + Anthropic)
│   └── formatter.py         ← Markdown formatters for MCP responses
│
├── sources/                 ← Per-source data integrations
│   ├── _models.py           ← ChemicalIdentity, PaperCandidate (shared)
│   ├── pubchem.py           ← PubChem PUG-REST resolver
│   ├── pubmed.py            ← PubMed + EuropePMC paper finder
│   ├── openalex.py          ← OpenAlex works API
│   ├── pmc.py               ← PMC full-text harvester
│   ├── jats.py              ← PMC JATS XML harvester (structured)
│   ├── agencies.py          ← NTP / ATSDR / IRIS / OEHHA / NIOSH
│   └── db_search.py         ← 34-database runner (DatabaseSearcher)
│
├── pipeline/                ← Linear chemical → report flow
│   ├── urls.py              ← DatabaseURLBuilder (31 search URLs)
│   ├── ledger.py            ← ExtractionLedger (per-session counts)
│   ├── evidence.py          ← EvidenceBuilder (verbatim quotes)
│   ├── scraper.py           ← Scraper + BatchProcessor
│   ├── report.py            ← ReportGenerator (.docx writer)
│   └── orchestrator.py      ← Linear run(chemical) with progress log
│
├── tools/                   ← MCP tool layer
│   ├── schemas.py           ← All 28 tool schemas (pure data)
│   ├── handlers.py          ← dispatch(name, args, ctx)
│   ├── context.py           ← ToolContext + build_context()
│   └── __init__.py          ← Public facade
│
└── docs/
    └── ARCHITECTURE.md      ← This file
```

## The linear flow — chemical name to report

When you say "make a review for benzene" Claude runs these MCP tools in
order. Each step logs its progress so you can see what's happening.

```
┌─────────────────────────────────────────────────────────────────┐
│ Step 1  resolve_chemical        PubChem → CID, CAS, SMILES, MW  │
│ Step 2  build_database_urls     31 search URLs (one per DB)     │
│ Step 3  count_papers_by_database   Per-DB paper counts          │
│ Step 4  find_papers             Top N ranked PubMed + EuropePMC │
│ Step 5  fetch_pmc_jats          Full text + figures + tables    │
│ Step 6  extract_pdf_figures     PDF fallback for missing figs   │
│ Step 7  build_evidence_record   KWIC quotes vs keyword bank     │
│ Step 8  fetch_agency_profile    NTP, ATSDR, IRIS, OEHHA, NIOSH  │
│ Step 9  generate_chemical_report  Compose final .docx           │
└─────────────────────────────────────────────────────────────────┘
```

The orchestrator in `pipeline/orchestrator.py` implements this exact
sequence and prints step-by-step progress to stderr (visible in Claude
Desktop logs).

## How the MCP layer fits together

```
  Claude Desktop
        │
        │ stdio JSON-RPC
        ▼
  server.py ────┬─ @list_tools  → TOOL_SCHEMAS (tools/schemas.py)
                │
                ├─ @call_tool   → dispatch(name, args, ctx) (tools/handlers.py)
                │                         │
                │                         ▼
                │                    ctx.fetcher, ctx.scraper, ctx.pubchem,
                │                    ctx.papers, ctx.openalex, ctx.pmc,
                │                    ctx.agency, ctx.db_searcher, ctx.llm, ...
                │
                ├─ @list_prompts → ALL_PROMPTS (prompts.py)
                │
                └─ @get_prompt   → get_prompt_messages(name, args)
```

`ToolContext` (tools/context.py) is the single point where every shared
client is instantiated. The MCP server creates one per process.

## MCP server decorators used

| Decorator               | Purpose                                       |
|-------------------------|-----------------------------------------------|
| `@server.list_tools()`  | Show Claude which tools exist                 |
| `@server.call_tool()`   | Execute a tool by name                        |
| `@server.list_prompts()`| Show Claude which prompt templates exist      |
| `@server.get_prompt()`  | Return the full message body for a prompt     |

## Determinism guarantees

- LLM calls: `temperature=0` everywhere.
- Database URL builder: pure function of (chemical, cas, cid).
- Keyword searcher: regex matching is stable.
- Same chemical name → same report, unless an upstream DB adds new records.

## Zero-hallucination rules

1. Every paragraph the LLM produces is checked against a whitelist of
   real (author, year) tuples harvested from the source pool.
2. Extractive quotes are preferred over paraphrase.
3. Figures and tables only appear if they were really extracted from a
   harvested PDF or JATS XML.
4. DBs that return zero papers are reported honestly — never invented.

## Adding a new data source

1. Drop a new file under `sources/` (`sources/my_db.py`).
2. Add the DB entry to `config/databases.py`.
3. Wire the searcher method into `sources/db_search.py`.
4. That's it — `pipeline.urls.DatabaseURLBuilder` picks it up on its own
   and every downstream tool (`count_papers_by_database`,
   `generate_chemical_report`) automatically includes it.

## Adding a new MCP tool

1. Append the schema to `tools/schemas.py`.
2. Add `if name == "my_tool":` branch in `tools/handlers.py`.
3. If it needs a new shared client, add it to `tools/context.py`.
4. Restart Claude Desktop.
