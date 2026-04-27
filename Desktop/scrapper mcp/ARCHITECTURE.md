# Toxicology Literature Review Pipeline — Architecture

*One-line summary: you type a chemical name, Claude calls one MCP tool, and that tool runs a 14-database parallel harvest, logs every URL and keyword hit, attaches a confidence score to every excerpt, and renders a 200-page Word document whose every paragraph is traceable back to a source URL.*

---

## 1. End-to-end workflow

```
┌────────────┐  chat message  ┌─────────────────┐  MCP call   ┌─────────────────────────┐
│  You       │───────────────▶│  Claude (LLM)   │────────────▶│  tox-scraper MCP server │
│  (chat)    │◀───────────────│  in Cowork      │◀────────────│  (Python, FastMCP)      │
└────────────┘   final docx   └─────────────────┘   JSON+file └───────────┬─────────────┘
                                                                           │
                                    ┌──────────────────────────────────────┤
                                    │  ToolContext (built once per process)│
                                    └──────────────────────────────────────┘
                                               │
       ┌──────────────┬──────────────┬─────────┴──────────┬────────────────┬───────────────┐
       ▼              ▼              ▼                    ▼                ▼               ▼
  HTTPFetcher    PubChem        MultiDB harvester    EvidenceBuilder   ReportGen      LLMAdapter
  (httpx)        resolver       (14 DB adapters)     (JATS parser)    (python-docx)  (OpenAI/Claude)
       │              │              │                    │                │               │
       ▼              ▼              ▼                    ▼                ▼               ▼
   Retry +      pubchem.py      SemanticScholar,       JATS XML         Times New      Only wrapped
   cache         NCBI API        NTP, WHO INCHEM,      → paragraphs     Roman, 12pt,   under llm_*
   (_cache{})                   WHO IPCS, ECHA,                         justified,     tools — NOT
                                ATSDR, CalEPA,                          Appendix A/B   called from
                                Canada CEPA, …                                         build_chem
                                (see §4)                                                review.
```

The architecture has three hard boundaries: **Claude**, **MCP server (Python process)**, **external services**. Claude never touches a URL directly during a toxicology run — every fetch is delegated to the MCP server so the full-text, PDF extraction, and provenance ledger happen server-side and come back to Claude as summarised JSON.

---

## 2. The MCP tool surface (32 tools, registered in `tools/schemas.py`)

The server exposes 32 tools. Claude picks one at a time; your pipeline entry point is **`build_chemical_review`**, which orchestrates the rest internally.

| Group | Tools |
|---|---|
| **Primary pipeline** (1) | `build_chemical_review` *(one-shot: chem name → trace + ledger + docx)* |
| **Composed reports** (2) | `generate_report`, `generate_chemical_report` |
| **Identity** (1) | `resolve_chemical` |
| **Paper search** (3) | `find_papers`, `find_papers_openalex`, `count_papers_by_database` |
| **Full text / JATS** (2) | `fetch_pmc_article`, `fetch_pmc_jats` |
| **Multi-DB harvest** (3) | `list_harvestable_databases`, `harvest_database`, `harvest_all_databases` |
| **Agency profiles** (1) | `fetch_agency_profile` |
| **PDF / figure / table** (4) | `fetch_pdf`, `fetch_pdf_bytes`, `extract_pdf_figures`, `extract_pdf_tables` |
| **Scrape helpers** (4) | `fetch_url`, `batch_scrape`, `search_in_content`, `extract_links` |
| **Reference & ledger** (4) | `build_database_urls`, `harvest_references`, `build_evidence_record`, `get_extraction_ledger`, `reset_extraction_ledger` |
| **LLM wrappers** (5) | `llm_status`, `llm_synthesize`, `llm_narrate_section`, `llm_dedupe_paragraphs`, `llm_clean_report` |
| **Listing** (1) | `list_databases` |

Every tool receives the same `ToolContext` object, which is instantiated exactly once at server start (`build_context()` in `tools/context.py`). That context holds 15 singleton clients: `fetcher`, `scraper`, `batch`, `pubchem`, `papers`, `openalex`, `pmc`, `agency`, `evidence_builder`, `jats`, `ledger`, `db_searcher`, `multi_harvester`, `report_gen`, `llm`, plus the optional per-run `tracer` and `evidence_ledger` bound by `bind_run()`.

---

## 3. The one-shot pipeline: `build_chemical_review`

Four stages, all on the server, each wrapped in a traced `step()` block:

| Stage | What runs | Writes to |
|---|---|---|
| **1. Resolve** | `pubchem.resolve(chemical)` → CID, CAS, synonyms, InChI, SMILES | `trace: resolve.pubchem` |
| **2. Harvest** | `MultiDatabaseHarvester.harvest_all(chemical, cas)` — fans out to 14 adapters in parallel (`asyncio.create_task`), awaits each, captures per-DB errors | `trace: harvest.multi_db` + `harvest.{db_name}` for each failure |
| **3. Ledger** | For every harvested doc with ≥200 chars of real text: scan the full text for all 169 keywords with ±280-char windows, up to 8 excerpts per doc. Each hit becomes one `EvidenceRecord` written to the ledger JSONL | `trace: ledger.{db_name}` + `ledger.jsonl` |
| **4. Compose** | Delegate to `generate_chemical_report` with `TOX_SCRAPER_OUT_DIR` pinned to the run directory and `output_filename=f"{run_id}.docx"` — that tool renders the .docx and writes it to the pinned path | `trace: compose.docx` + `{run_id}.docx` |

All four stages emit a `start` event and then either an `ok` event with `duration_ms` or an `error` event with the exception string. The trace file is append-only JSONL at `/tmp/tox_runs/{run_id}.jsonl`.

---

## 4. How many databases, how many HTTP calls, how many workers

**14 database adapters**, all registered in `sources/harvester.py:REGISTRY`:

| # | Adapter class | File | Custodian |
|---|---|---|---|
| 1 | `SemanticScholarSource` | `sources/semantic_scholar.py` | Allen Institute |
| 2 | `NTPSource` | `sources/ntp.py` | NIEHS / NTP |
| 3 | `WHOInchemSource` | `sources/who_inchem.py` | WHO INCHEM |
| 4 | `WHOIpcsSource` | `sources/who_ipcs.py` | WHO IPCS |
| 5 | `EChaSource` | `sources/echa.py` | European Chemicals Agency |
| 6 | `ATSDRSource` | `sources/atsdr.py` | CDC / ATSDR |
| 7 | `CalEPASource` | `sources/calepa.py` | California EPA OEHHA |
| 8 | `CanadaDSLSource` | `sources/canada_dsl.py` | Health Canada CEPA/DSL |
| 9 | `ConcaweSource` | `sources/concawe.py` | CONCAWE |
| 10 | `NIOSHSource` | `sources/niosh.py` | CDC / NIOSH |
| 11 | `OSHASource` | `sources/osha.py` | US OSHA |
| 12 | `AICISSource` | `sources/aicis.py` | Australia AICIS |
| 13 | `ILOSource` | `sources/ilo.py` | ILO ICSC |
| 14 | `ZenodoSource` | `sources/zenodo.py` | Zenodo (open data) |

Plus PubChem, PubMed, EuropePMC, OpenAlex, PMC JATS — these aren't part of the 14-parallel fan-out; they're called separately from the composer.

**Concurrency and timeout settings** — these are fixed in `config/settings.py`:

| Setting | Value | Meaning |
|---|---|---|
| `timeout` | 8.0 s | Per-URL HTTP timeout |
| `max_concurrent` | 8 | Semaphore width inside `BatchProcessor.run` |
| `httpx.Limits(max_connections)` | 16 | `max_concurrent × 2` — connection pool cap |
| `max_retries` | 1 | One retry, 1.5 s backoff. Worst-case per URL = 16 s |
| `max_content_chars` | 200,000 | Per-page text cap |
| `max_pdf_bytes` | 50 MB | Per-PDF size cap |
| `context_chars` | 300 | KWIC window on each side of a keyword hit (KeywordSearcher) |
| Provenance window | 280 | ±280 chars around each keyword hit in the ledger |
| Ledger excerpts per doc | 8 max | `add_from_text(..., max_excerpts=8)` |
| `BaseDatabaseSource.search_limit` | 5 | Default papers per DB unless overridden |

**Per-run HTTP call volume (14-DB harvest, `per_db_limit=5`):**

```
Stage           Calls per DB           Total calls
search          1-3 (varies by API)    14 - 42
download_pdf    up to 5                up to 70
html fallback   up to 5                up to 70  (only if PDF absent)
                                      ──────────
                                      up to ~200 calls per chemical
```

All calls go through the single `HTTPFetcher` instance which caches by URL, so duplicates are free. Fan-out is `asyncio.create_task` (one task per DB) gated by the 16-connection httpx pool, so the effective parallelism during a harvest is min(number of active tasks, 16 connections).

The **169 keywords** break down as: cancer 18, reproductive 14, neuro 9, hepatic 9, renal 6, respiratory 8, immuno 6, hematologic 9, cardiovascular 6, endocrine 7, exposure routes 10, pharmacokinetics 16, study design 23, species 11, agency 18. They're all lowercased, de-duplicated, and scanned case-insensitively (single words use `\b…\b` word boundaries; multi-word phrases use a whitespace-tolerant regex).

---

## 5. Confidence scoring — the actual formula

In `core/provenance.py`, every excerpt gets one confidence score between 0 and 1. The formula is one line:

```python
kw_density = sum(counts.values()) / max(1, len(excerpt) // 100)
conf       = min(1.0, 0.3 + 0.15 * len(matched) + 0.05 * min(5, kw_density))
```

What that means in plain English:

- **Base floor of 0.30** — every excerpt that makes it into the ledger starts at 0.3 because it was pulled from a known, logged source with a real URL.
- **+0.15 per distinct keyword matched** in the excerpt (capped by `min(1.0, …)` at 1.0). One keyword → 0.45. Three → 0.75. Five or more → 1.0.
- **Density bonus** up to +0.25 (capped because `min(5, kw_density)` clips at 5 → 5 × 0.05 = 0.25). Density = total keyword hits ÷ excerpt length in hundreds of characters.

So a 560-character excerpt that contains "carcinogen", "IARC Group 1", "leukemia", and "myelodysplastic" (4 distinct keywords, 4 total hits) scores: 0.3 + 0.15 × 4 + 0.05 × min(5, 4/5) = 0.3 + 0.60 + 0.04 = 0.94. An excerpt with only one keyword hit scores ≈0.45.

The ledger records are written as JSONL so you can re-compute or re-rank offline if you want a different scoring formula — nothing is baked into the docx.

---

## 6. Where LLMs are used (and where they are NOT)

**The answer: during `build_chemical_review`, the LLM is NOT called at all.** By design. The composer (`generate_chemical_report`) does the whole docx from scraped evidence only.

LLMs live in `core/llm.py` and are only invoked through five explicit wrapper tools:

| Tool | Purpose | Called from `build_chemical_review`? |
|---|---|---|
| `llm_status` | Report which provider is configured | No |
| `llm_synthesize` | Turn a passage into a paragraph | No (opt-in only) |
| `llm_narrate_section` | Rewrite one section from bullet evidence | No |
| `llm_dedupe_paragraphs` | Remove near-duplicates | No |
| `llm_clean_report` | Pass over the whole docx | No |

Provider selection is environment-driven (`TOX_SCRAPER_LLM`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) and the default model is `gpt-4o-mini` or `claude-sonnet-4-5`. Temperature 0.2, max tokens 1500 per call. **Until one of those `llm_*` tools is deliberately called, not a single token is generated by an LLM. That's the anti-hallucination guarantee.**

---

## 7. How the Word document is synthesized

The .docx rendering has two paths. `build_chemical_review` uses **Path A** (the Python docx path); the JS path is used separately when you want the benzene-exact layout.

**Path A — Python (`pipeline/report.py:ReportGenerator`), invoked from `generate_chemical_report`:**

1. **Collect evidence.** Runs (in parallel via `asyncio.gather`): stage 1 PubChem identity, stage 2 34-DB count sweep, stage 3 PubMed + EuropePMC search, stage 5 agency profiles (ATSDR, NTP, OEHHA, NIOSH, OSHA, Haz-Map), stage 9 14-DB multi-harvest. Stage 4 (PMC JATS full text for the top N candidates) waits on stage 3.
2. **Compose sections** in `ReportGenerator.SECTIONS` — Title, Executive Summary, Identification, Production & Use, Carcinogenicity, Genotoxicity, Neurotoxicity, Hematotoxicity, Reproductive/Developmental, PK/ADME, Exposure, Regulatory, Risk Assessment, References, Appendix A (per-DB counts), Appendix B (keyword hits).
3. **Render** via python-docx. Times New Roman 12 pt, justified prose, inline superscript citations `[1]…[N]` resolved against the references list built from DOI/PMID/PMCID collected during harvest. The paragraph alignment is JUSTIFY.
4. **Write** to `{output_dir}/{run_id}.docx`.

**Path B — Node.js (`tox_work/compose_report.js` + 43 section modules), manually invoked when you want the long benzene-v3 layout:**

- Driver `compose_report.js` wires 43 chapter modules (`sections/`): cover, history, chemistry, exposure, health, toxicokinetics, carcinogenicity, cancer_sites, cohorts_deep, mechanism, reproductive_developmental, respiratory_deep, dermal_ocular, epidemiology_methods, biomonitoring, risk, regulatory, pbpk_modeling, in_vitro, new_approach_methods, mixture_toxicology, food_water, indoor_air_settings, atmospheric, industry_sectors, occupational_surveillance, consumer_products, vulnerable, ecotox, analytical, clinical_management, case_studies, emerging_research, economic_burden, international_harmonization, systematic_review, environment, mitigation, ntp_iris_comparison, summary, multi_db_evidence, references, appendix.
- Each module takes the docx body plus helpers `{h, para, headerTable, image, imageCaption, pageBreak}` and appends its section.
- `docx-js` renders Times New Roman 12 pt, US Letter 12240×15840 DXA, justified, Appendix B with per-database retrieval counts.

---

## 8. How I verify the report is correct — three independent checks

**Check 1 — JSONL trace.** Every pipeline step emits a start/ok/error event to `/tmp/tox_runs/{run_id}.jsonl`. Each event carries `ts`, `run_id`, `step`, `event`, `source`, `url`, `duration_ms`, `bytes`, `papers_found`, `papers_kept`, `keyword_hits`. So after a run you can grep one file and see: which of the 14 DBs returned zero, which returned 403, which PDF extraction failed, and how long each step took. `tracer.summary()` rolls these up into a by-source matrix, keyword histogram, total bytes, and first 25 errors.

**Check 2 — Evidence ledger.** `core/provenance.py` stores every excerpt with `evidence_id`, `source`, `url`, `doi/pmid/pmcid`, `excerpt`, `matched_keywords`, `keyword_counts`, `confidence`, `extraction_method`, `char_count`. This is the anti-hallucination layer: if a section of the docx has zero ledger rows for it, the composer must skip the section or emit a flagged "insufficient evidence" placeholder. The `coverage_report()` returns `total_records`, `by_source`, `by_section`, `by_keyword`, `mean_confidence`.

**Check 3 — docx structural validation.** After generation, the file is opened with `python-docx` and we read paragraph count, table count, and the first headings to confirm structure. For the JS path, docx-js validation runs via `scripts/office/validate.py`. A "generated" file that doesn't open gets rejected.

**Per-DB paper counts** — these are in two places: the trace's `harvest.multi_db` event contains a `per_db_counts` map (14 entries, one per DB), and the ledger's `by_source` report contains an `evidence records per source` count. When you see Appendix B in the docx showing "Semantic Scholar: 12 papers, NTP: 3 PDFs" etc., those numbers come directly from the trace file and are guaranteed to match the trace.

---

## 9. Quick reference — where to look when something goes wrong

| Symptom | File to open | What to look for |
|---|---|---|
| Zero papers from a DB | `/tmp/tox_runs/{run_id}.jsonl` | Events where `step="harvest.{db}"`, `event="error"` — look at `error` field |
| Low confidence across the board | `/tmp/tox_runs/{run_id}.ledger.jsonl` | `matched_keywords` field mostly empty → text extraction is broken, not keywords |
| Docx has empty sections | Ledger `coverage_report().by_section` | Sections with 0 records → no evidence harvested for that topic |
| Wrong CAS or identity | Trace event `resolve.pubchem`, extras | Check PubChem response time + returned `cid`/`cas` |
| Slow run | Trace `duration_ms` column | Sort events by duration — usually it's one slow DB holding up `harvest.multi_db` |
| MCP timeout (60 s) | `tools/handlers.py:STAGE_TIMEOUTS` | Per-stage hard caps: identity 5 s, counts 12 s, papers 10 s, jats 10 s, agency 10 s, multi 14 s |

---

## 10. The guarantee, spelled out

1. Every URL fetched goes through one `HTTPFetcher` with `max_connections=16`, `timeout=8s`, retry=1. No ad-hoc `requests.get()` anywhere — so every network call is logged.
2. Every keyword match is recorded in the evidence ledger with the exact ±280-character excerpt, the URL, and a confidence score.
3. The docx composer emits zero LLM-generated prose during `build_chemical_review`. LLMs are opt-in only.
4. Every section that ends up in the docx is backed by at least one ledger row; sections without evidence are flagged, not invented.
5. The JSONL trace is append-only — you can rerun `tracer.summary()` offline from the file and reconstruct exactly what happened, millisecond by millisecond.
