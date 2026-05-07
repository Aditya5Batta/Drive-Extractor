"""
Zenodo MCP Server
===================
Mirrors the experience of zenodo.org with the Format → PDF facet applied —
type a chemical name, get every PDF-bearing record in website-relevance order.

Run (stdio — for Claude Desktop):
    python backend/mcp/Zenodo/server.py
Run (HTTP — for testing):
    python backend/mcp/Zenodo/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

THIS_DIR = Path(__file__).parent              # backend/mcp/Zenodo/
MCP_DIR  = THIS_DIR.parent                    # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
from Zenodo import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_ZENODO",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "Zenodo"),
))


# ─────────────────────────────────────────────────────────────────────────────
# Server-level instructions
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="Zenodo",
    instructions="""\
Zenodo — CERN-hosted general-purpose open research repository
================================================================
Operated by CERN under the European Commission's OpenAIRE project.
Stores 5M+ records: journal articles, preprints, datasets, software,
posters, presentations, theses, and reports across every discipline.
Every record gets a citable DOI.

When to use this server (vs PMC/EuropePMC/SS/ECHA/NTP/OEHHA/ATSDR)
-------------------------------------------------------------------
• You need preprints, theses, conference posters, software docs, or
  datasets that aren't on PMC/PubMed.
• Multidisciplinary topics — engineering, physics, materials, ML datasets,
  climate, social sciences.
• A specific Zenodo DOI was cited and you want the source PDF.

How searching works (matches the website with PDF format filter)
-----------------------------------------------------------------
The website URL with the user's filter is:
    https://zenodo.org/search?q=<chem>&f=file_type:pdf
We hit the equivalent JSON API directly:
    https://zenodo.org/api/records?q=<chem>&file_type=pdf&size=N
    &sort=bestmatch
   - `file_type=pdf`  → URL-level equivalent of the website's
     "Format → PDF" facet (drops dataset/software-only records).
   - `sort=bestmatch` → website's default relevance ranking.

Results come back ranked by Zenodo's relevance score, exactly matching
website order.  We skip any record whose `files[]` doesn't contain a `.pdf`
(very rare — the file_type filter pre-filters server-side).

Optional API token for higher rate limits
-----------------------------------------
Anonymous tier: 60 req/min (shared per IP).  Hitting the limit returns
HTTP 429 — the scraper retries with backoff [0, 2, 6] seconds.
For higher limits, get a free token at
    https://zenodo.org/account/settings/applications/tokens/new/
and set `ZENODO_API_TOKEN=…` in `.env`.  It's sent as `Authorization:
Bearer <token>`.

How downloading works
---------------------
Each result has exactly one PDF URL — the bitstream content URL on
zenodo.org.  No fallbacks.  Saved as
    <PDF_DIR_ZENODO>/zenodo_<original-stem>.pdf
(default pdfs/Zenodo/).

Typical workflow
----------------
  1. `search_papers("benzene", max_results=10)`
  2. Pick a record.
  3. `download_pdf(pdf_urls=<that record's pdf_urls>)`
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Search
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def search_papers(
    query: Annotated[
        str,
        "Free-text query — e.g. 'benzene', 'aspirin cardiovascular', "
        "'graph neural networks'.  Zenodo's search uses ElasticSearch under "
        "the hood; phrases, AND/OR, and field syntax (`title:benzene`) all "
        "work."
    ],
    max_results: Annotated[
        int,
        "How many records to return (1-50).  All results have at least one "
        "PDF (file_type=pdf filter applied at API level)."
    ] = 20,
) -> str:
    """
    Search Zenodo records that contain a PDF, in website-relevance order.

    Returns a JSON object:
      status            "ok" | "no_results" | "error"
      query             the query as sent
      total             number of records returned
      response_time_ms  Zenodo round-trip time
      papers[]          per record:
        title       record title
        type        Zenodo resource type (Publication, Dataset, Software, …)
        year        publication_date year
        authors     first 5 creator names
        doi         the Zenodo DOI
        url         zenodo.org/records/<id> landing page
        pdf_urls    one-element list with the direct PDF download URL
        abstract    up to 400 chars, HTML stripped from description
    """
    result = await _search(query, max_results=min(max(1, max_results), 50))
    if result.get("status") == "error":
        return json.dumps({"status": "error", "query": query,
                           "error": result.get("error"), "papers": []})
    papers = result.get("papers", [])
    if not papers:
        return json.dumps({"status": "no_results", "query": query,
                           "total": 0, "papers": []})
    return json.dumps({
        "status":           "ok",
        "query":            query,
        "total":            len(papers),
        "response_time_ms": result.get("response_time_ms"),
        "papers": [
            {
                "title":    p["title"],
                "type":     p["journal"],
                "year":     p["year"],
                "authors":  p["authors"][:5],
                "doi":      p["doi"],
                "url":      p["epmc_url"],
                "pdf_urls": p["pdf_urls"],
                "abstract": p["abstract"][:400],
            }
            for p in papers
        ],
    }, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — Download PDF
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def download_pdf(
    pdf_urls: Annotated[
        list[str],
        "Zenodo PDF URL list from `search_papers`.  Always one element — pass "
        "through unchanged.  All URLs live under zenodo.org/api/records/.../"
        "files/.../content and serve real PDF bytes directly."
    ],
) -> str:
    """
    Download a single Zenodo record PDF.

    No multi-URL fallback (each record has one canonical PDF).  Saved as
    <PDF_DIR_ZENODO>/zenodo_<original-stem>.pdf  (default pdfs/Zenodo/).

    Returns a JSON object:
      status        "success" | "failed" | "no_url" | "error"
      pdf_path      absolute path on disk (None if failed)
      pdf_source    the URL that was tried
      file_size_kb  size of saved file
      attempts[]    single-element trace { url, http_status, is_pdf,
                    success, error } — diagnostics if download fails.
    """
    if not pdf_urls:
        return json.dumps({"status": "error", "message": "pdf_urls list is empty"})
    result = await _download_pdf(pmcid="doc", pmid="noid",
                                 pdf_urls=pdf_urls, pdf_dir=PDF_DIR)
    return json.dumps({
        "status":       result["status"],
        "pdf_path":     result["pdf_path"],
        "pdf_source":   result["pdf_source"],
        "file_size_kb": result["file_size_kb"],
        "attempts":     [{k: v for k, v in a.items() if k != "attempted_at"}
                         for a in result.get("attempts", [])],
    }, indent=2)


if __name__ == "__main__":
    args = sys.argv[1:]
    port = int(os.getenv("MCP_ZENODO_PORT", "8009"))
    if "--http" in args:
        print(f"[Zenodo MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[Zenodo MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
