"""
Concawe MCP Server
================
Mirrors the experience of ntp.niehs.nih.gov search with the
"Formats → PDF" facet applied — search NTP's toxicology document library
and download PDFs.

Run (stdio — for Claude Desktop):
    python backend/mcp/ntp/server.py
Run (HTTP — for testing):
    python backend/mcp/ntp/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

THIS_DIR = Path(__file__).parent
MCP_DIR  = THIS_DIR.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
from ntp import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_NTP",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "ntp"),
))


# ─────────────────────────────────────────────────────────────────────────────
# Server-level instructions
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="NTP",
    instructions="""\
Concawe — US National Toxicology Program document library
========================================================
NIEHS / NIH agency that runs long-term toxicology and carcinogenicity
testing for the US government.  Authoritative source for:
  • Technical Reports (TR)        — 2-year carcinogenesis bioassays
  • Genetically Modified Models   (GMM) reports
  • Toxicology Studies            (TOX)
  • Report on Carcinogens         (RoC) profiles & background documents
  • Monographs and Concept docs

When to use this server (vs PMC/EuropePMC/ECHA)
-----------------------------------------------
• You need a US regulatory toxicology report on a substance.
• You're researching carcinogen classification, OEL/exposure data, NIH
  cancer-bioassay results.
• You want NTP's authoritative profile for a chemical (RoC profiles are
  the US-government carcinogen list).

How searching works (matches the website's "Format: PDF" facet)
---------------------------------------------------------------
NTP's search is powered by a separate Funnelback host
(`ntpsearch.niehs.nih.gov`).  We hit it directly with:
  • `Extension=PDF`  → URL-level equivalent of the website's
    "Formats → PDF (Adobe Portable Document Format)" facet (drops the
    result count from ~685 to ~639 for "Benzene", matching the website's
    PDF-filtered count exactly).
  • `num_ranks` controls page size.

We then parse the main `<ol id="results">` block (the actual search
result list — NOT the small "Search suggestions" box at the top, which
only contains 2-3 hand-picked links).  Results come back in NTP's
relevance order, exactly as the website shows them.

URLs returned include two patterns:
  • Direct PDF      e.g. /sites/default/files/ntp/roc/content/profiles/<chem>.pdf
  • /go/<id> redirect e.g. ntp.niehs.nih.gov/go/tr289 → resolves to the
    actual PDF on /sites/default/files/...  (NTP shorthand URL — we follow
    redirects automatically).
We filter to only entries that look like PDFs (or `/go/` redirects)
and skip abstract / metadata-only landing pages.

Query syntax
------------
Free-text full-text search powered by Funnelback:
  • Free text — "benzene", "aspirin"
  • Phrase    — "Report on Carcinogens"
  • Result count varies a lot:  Benzene → 639 PDFs, Aspirin → ~3,
    obscure chemicals → 0.

How downloading works
---------------------
Each result has exactly one PDF URL (no fallbacks).  /go/<id> redirects
are followed automatically to the actual PDF on NTP's filesystem.  Saved
filename uses the FINAL URL stem (after redirects), so /go/tr289 saves
as `ntp_tr289.pdf`.

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
        "Search query — usually a chemical name, "
        "e.g. 'benzene', 'aspirin', 'formaldehyde', 'asbestos'.  Funnelback "
        "matches across document body and metadata."
    ],
    max_results: Annotated[
        int,
        "How many documents to return (1-50).  Tool over-fetches 2× internally "
        "and filters non-PDF entries client-side, so the final count may be "
        "less than max_results when results are mixed types."
    ] = 20,
) -> str:
    """
    Search NTP filtering to PDF-only results — every hit is downloadable.

    `Extension=PDF` is appended to the URL automatically (the website's
    "Formats → PDF" facet).  The main `<ol id="results">` block is parsed,
    skipping the small "Search suggestions" sidebar.

    Returns a JSON object:
      status            "ok" | "no_results" | "error"
      query             the query as sent
      total             number of docs returned (after PDF filter)
      response_time_ms  Funnelback round-trip time
      papers[]          per document:
        title       e.g. "TR-289: Benzene (CASRN 71-43-2) in F344/N Rats..."
        type        always "NTP"
        url         canonical NTP URL (often a /go/<id> shorthand)
        pdf_urls    one-element list with the same URL
        snippet     first 400 chars of search snippet
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
        "NTP PDF URL list from `search_papers`.  Always one element — pass "
        "through unchanged.  /go/<id> shorthand URLs are accepted; HTTP "
        "redirects are followed to the actual PDF automatically."
    ],
) -> str:
    """
    Download a single NTP document PDF (follows /go/ redirects).

    No multi-URL fallback (NTP hosts each doc once).  Saved to
    <PDF_DIR_NTP>/ntp_<final-url-stem>.pdf  (default pdfs/ntp/).
    The "final-url-stem" is computed AFTER following redirects, so a
    /go/tr289 URL saves as `ntp_tr289.pdf`.

    Returns a JSON object:
      status        "success" | "failed" | "no_url" | "error"
      pdf_path      absolute path on disk (None if failed)
      pdf_source    the URL that was originally tried
      file_size_kb  size of saved file
      attempts[]    single-element trace { url, http_status, is_pdf,
                    success, error }.
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
    port = int(os.getenv("MCP_NTP_PORT", "8005"))
    if "--http" in args:
        print(f"[NTP MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[NTP MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
