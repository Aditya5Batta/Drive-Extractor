# Tox Scraper MCP

MCP server that fetches toxicology database URLs (HTML + PDF), extracts full text, and finds keyword evidence — designed to complement the SciToxSynthesis pipeline.

## Setup (Windows)

```powershell
cd "C:\Users\ADITYA BATTA\Desktop\scrapper mcp"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Register with Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tox-scraper": {
      "command": "C:\\Users\\ADITYA BATTA\\Desktop\\scrapper mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\ADITYA BATTA\\Desktop\\scrapper mcp\\server.py"]
    }
  }
}
```

Restart Claude Desktop.

## LLM backend (OpenAI or Anthropic)

Four of the 28 tools synthesise clean narrative prose — these require an LLM.
The server auto-selects a backend based on environment variables:

```powershell
# Option A — OpenAI (preferred for local dev + production)
setx OPENAI_API_KEY "sk-..."
# (optional)
setx OPENAI_MODEL  "gpt-4o-mini"

# Option B — Anthropic
setx ANTHROPIC_API_KEY "sk-ant-..."
setx ANTHROPIC_MODEL   "claude-sonnet-4-5"

# Option C — explicit override when both keys exist
setx TOX_SCRAPER_LLM   "openai"      # or "anthropic" or "none"
```

Selection priority: `TOX_SCRAPER_LLM` → `OPENAI_API_KEY` → `ANTHROPIC_API_KEY` → none.
Call the `llm_status` MCP tool any time to verify which backend is active.

Scraper tools (`fetch_*`, `find_papers`, `generate_chemical_report`) work
without any LLM key — only the `llm_*` tools and `llm_clean_report` require it.

> **Note:** `server.py` is an MCP stdio server. It must be launched by an MCP
> client (Claude Desktop, Cursor, or another MCP host). There is no standalone
> CLI mode — run it by registering it in Claude Desktop as shown above.

## Tools exposed

Scraping + harvest (no LLM — 23 tools):

| Tool | Purpose |
|------|---------|
| `fetch_url` | HTML → clean main text (auto-detects PDFs) |
| `fetch_pdf` | PDF → full text (all pages) |
| `fetch_pdf_bytes` | PDF → raw bytes for local extraction |
| `search_in_content` | KWIC keyword hits in text/URL |
| `extract_links` | Outgoing links from a page |
| `batch_scrape` | N URLs × M keywords → ranked evidence |
| `list_databases` | Curated HIGH/MED tox databases |
| `resolve_chemical` | PubChem: name/CAS → CID, SMILES, InChI, formula |
| `find_papers` | PubMed + EuropePMC → ranked + KWIC |
| `find_papers_openalex` | OpenAlex works API |
| `fetch_pmc_article` | PMC full text + figures + refs |
| `fetch_pmc_jats` | PMC JATS XML → structured article |
| `harvest_references` | Extract cited papers (DOIs, PMIDs) |
| `fetch_agency_profile` | NTP / ATSDR / IRIS / OEHHA / NIOSH |
| `get_extraction_ledger` | Per-session papers-per-DB ledger |
| `reset_extraction_ledger` | Clear the ledger |
| `build_evidence_record` | Verbatim evidence bundle for a URL |
| `extract_pdf_tables` | pdfplumber-backed tables |
| `extract_pdf_figures` | PyMuPDF-backed figures |
| `build_database_urls` | Deterministic search URLs per DB |
| `count_papers_by_database` | Per-database result counts |
| `generate_report` | Prepared bundle → docx |
| `generate_chemical_report` | 8-stage pipeline → verbatim docx |

LLM-powered (requires OPENAI_API_KEY or ANTHROPIC_API_KEY — 5 tools):

| Tool | Purpose |
|------|---------|
| `llm_status` | Show which backend is wired up |
| `llm_synthesize` | Generic system/user completion |
| `llm_narrate_section` | Verbatim sources → clean `[N]`-cited paragraph |
| `llm_dedupe_paragraphs` | Cluster near-duplicate paragraphs |
| `llm_clean_report` | Full pipeline + LLM → clean benzene-style docx |

## Typical flow (inside Claude Desktop)

Give Claude the 30 URLs for a chemical + endpoint keywords and call `batch_scrape`:

```
urls: [30 PubMed/PMC/ECHA/ATSDR/etc. URLs for benzene]
keywords: ["carcinogenicity", "leukemia", "aplastic anemia", "CYP2E1",
           "neurotoxicity", "bone marrow", "muconic acid", ...]
```

Returns results ranked by `relevance_score` (unique keywords matched / requested) and `total_hits`, with top-5 snippets inlined.

## Notes

- Extractors: `trafilatura` for HTML main content (boilerplate-free), `pdfminer.six` + `pypdf` fallback for PDFs, BeautifulSoup for link extraction.
- Scanned PDFs return an error — this server does not OCR. Add `pytesseract` if needed later.
- Per-page cap: 200K chars. Per-PDF cap: 50 MB. Adjust in `ScraperConfig` at the top of `server.py`.
- Parallelism: 6 concurrent fetches (tune `CONFIG.max_concurrent`).
- Cache: in-memory per-session by URL — same URL in one session won't re-fetch.
