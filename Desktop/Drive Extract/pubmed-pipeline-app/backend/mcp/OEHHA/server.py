"""
OEHHA MCP Server
==================
Mirrors the experience of typing a chemical name into oehha.ca.gov's search
box and downloading its PDF documents (Reference Exposure Levels, Public
Health Goals, Hazard Identifications, Proposition 65 NSRLs/MADLs, technical
support documents, etc.).

Run (stdio — for Claude Desktop):
    python backend/mcp/OEHHA/server.py
Run (HTTP — for testing):
    python backend/mcp/OEHHA/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

THIS_DIR = Path(__file__).parent              # backend/mcp/OEHHA/
MCP_DIR  = THIS_DIR.parent                    # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
from OEHHA import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_OEHHA",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "OEHHA"),
))


# ─────────────────────────────────────────────────────────────────────────────
# Server-level instructions
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="OEHHA",
    instructions="""\
OEHHA — California Office of Environmental Health Hazard Assessment
=====================================================================
California's chemical risk-assessment authority within CalEPA.  Authoritative
source for:
  • Reference Exposure Levels (RELs)              — air toxics
  • Public Health Goals (PHGs)                    — drinking water
  • Hazard Identification documents (DART/CIC)
  • Proposition 65 listings (NSRLs / MADLs)
  • Air Toxics Hot Spots program docs
  • OEHHA Technical Support Documents

When to use this server (vs ECHA / NTP / WHO IPCS)
--------------------------------------------------
• You need California-specific chemical risk values (RELs, PHGs, NSRLs,
  MADLs) — these are the gold-standard numbers used in California
  regulatory practice and often cited internationally.
• Proposition 65 work — OEHHA maintains the official Prop 65 list.
• Drinking-water / air-quality regulatory limits research.
• Comparison: ECHA = EU regulatory, NTP = US federal toxicology,
  WHO IPCS = global, OEHHA = California state regulatory.

How searching works
-------------------
Why we don't hit oehha.ca.gov directly:
  The OEHHA website is behind Incapsula bot protection — every plain-HTTP
  request to an HTML page gets a 951-byte Incapsula challenge instead of
  the actual page (even with full Chrome-like headers and HTTP/2).

How we get around it:
  We use DuckDuckGo's HTML search endpoint — `html.duckduckgo.com/html/`
  is not bot-protected and accepts the operator chain
       site:oehha.ca.gov  <chemical>  filetype:pdf
  which returns up to 10 OEHHA-hosted PDFs per query, each with a title
  and snippet.  This is the same set of documents the user gets by
  typing the chemical into OEHHA's own search box (which itself is a
  Google CSE) — we just discover them via DuckDuckGo's index instead.

Why this works for downloading:
  OEHHA's *PDF files* (the URLs at /sites/default/files/... and
  /media/downloads/...) are NOT Incapsula-protected — only the HTML pages
  are.  So once we have the PDF URL from DuckDuckGo, the download is a
  plain GET that returns the actual PDF bytes.

Typical results for "Benzene":
  • Benzene Reference Exposure Levels Technical Support Document (2014)
  • Draft Hazard Identification of the Developmental & Reproductive
    Toxicity of Benzene
  • Public Health Goal for Benzene (June 2001)
  • Benzene NSRL (Prop 65)
  • Long-term Health Effects of Exposure to Ethylbenzene
  …

Query syntax
------------
Free-text chemical name — e.g. 'benzene', 'lead', '1,3-butadiene', 'BPA'.
Quotes around multi-word names work as expected ("vinyl chloride").

How downloading works
---------------------
Each result has exactly one direct PDF URL on oehha.ca.gov (no fallbacks).
Saved filename uses the original PDF stem, so for example
    https://oehha.ca.gov/sites/default/files/media/downloads/crnr/benzene.pdf
is saved as  oehha_benzene.pdf  in the configured PDF_DIR_OEHHA.

Typical workflow
----------------
  1. `search_papers("benzene", max_results=10)`
  2. Pick a doc.
  3. `download_pdf(pdf_urls=<that doc's pdf_urls>)`
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Search
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def search_papers(
    query: Annotated[
        str,
        "Chemical name — e.g. 'benzene', 'lead', '1,3-butadiene', "
        "'\"vinyl chloride\"'.  Multi-word chemicals can be quoted."
    ],
    max_results: Annotated[
        int,
        "How many documents to return (1-30).  DuckDuckGo returns ~10 "
        "results per page; this tool fetches one page."
    ] = 10,
) -> str:
    """
    Search OEHHA's PDF document library — every result is downloadable.

    Implementation: queries DuckDuckGo HTML with the `site:oehha.ca.gov
    <chemical> filetype:pdf` operator combo, which surfaces the same
    OEHHA documents the user would get from oehha.ca.gov's own search
    box but without hitting the site's Incapsula bot challenge.

    Returns a JSON object:
      status            "ok" | "no_results" | "error"
      query             the chemical query as sent
      total             number of OEHHA PDFs returned
      response_time_ms  DuckDuckGo round-trip time
      papers[]          per document:
        title       human-readable title (DDG's "PDF " prefix stripped)
        type        always "OEHHA"
        url         direct oehha.ca.gov PDF URL
        pdf_urls    one-element list with the same URL
        snippet     up to 400 chars of context — usually contains the
                    chemical name in the document body
    """
    result = await _search(query, max_results=min(max(1, max_results), 30))
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
                "url":      p["epmc_url"],
                "pdf_urls": p["pdf_urls"],
                "snippet":  p["abstract"][:400],
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
        "OEHHA PDF URL list from `search_papers`.  Always one element — pass "
        "through unchanged.  All URLs live under oehha.ca.gov/sites/default/"
        "files/ or oehha.ca.gov/media/downloads/ which are NOT bot-protected."
    ],
) -> str:
    """
    Download a single OEHHA document PDF.

    No multi-URL fallback (OEHHA hosts each doc once).  Saved to
    <PDF_DIR_OEHHA>/oehha_<original-filename-stem>.pdf  (default pdfs/OEHHA/).

    Returns a JSON object:
      status        "success" | "failed" | "no_url" | "error"
      pdf_path      absolute path on disk (None if failed)
      pdf_source    the URL that was tried
      file_size_kb  size of saved file
      attempts[]    single-element trace { url, http_status, is_pdf,
                    success, error } — useful for diagnostics.
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
    port = int(os.getenv("MCP_OEHHA_PORT", "8007"))
    if "--http" in args:
        print(f"[OEHHA MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[OEHHA MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
