"""
ATSDR MCP Server
==================
Mirrors the experience of atsdr.cdc.gov — type a chemical name, click into
its Toxicological Profile, download the Complete Profile PDF (or any chapter).

Run (stdio — for Claude Desktop):
    python backend/mcp/ATSDR/server.py
Run (HTTP — for testing):
    python backend/mcp/ATSDR/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

THIS_DIR = Path(__file__).parent              # backend/mcp/ATSDR/
MCP_DIR  = THIS_DIR.parent                    # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
from ATSDR import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_ATSDR",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "ATSDR"),
))


# ─────────────────────────────────────────────────────────────────────────────
# Server-level instructions
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="ATSDR",
    instructions="""\
ATSDR — US Agency for Toxic Substances and Disease Registry
==============================================================
CDC's chemical-exposure & public-health agency.  Authoritative source for
US Toxicological Profiles — comprehensive, peer-reviewed monographs that
each cover one chemical's:
  • Health Effects (acute, chronic, carcinogenicity)
  • Toxicokinetics, Susceptible Populations, Biomarkers, Chemical Interactions
  • Chemical & Physical Information
  • Potential for Human Exposure
  • Environmental Fate, Transport, Levels
  • Adequacy of the Database
  • Regulations & Guidelines, References

When to use this server (vs PMC/EuropePMC/ECHA/NTP/OEHHA)
----------------------------------------------------------
• You need a comprehensive US public-health risk profile for a chemical.
• Researching exposure pathways, MRLs (Minimal Risk Levels), or biomarker
  data for a specific substance.
• Comparison: ECHA = EU regulatory, NTP = US carcinogenicity bioassays,
  OEHHA = California state regulatory, **ATSDR = US public-health profiles**.

How searching works (matches the website's Toxicological Profile flow)
----------------------------------------------------------------------
The website flow is:
  Step 1.  Visit https://www.atsdr.cdc.gov/toxprofiles/index.html
           (the master index — every chemical with a profile, alphabetical).
  Step 2.  Click the chemical name → opens a profile page on
           wwwn.cdc.gov/TSP/ToxProfiles/ToxProfiles.aspx?id=<id>&tid=<tid>
  Step 3.  On that page, click "Complete Profile pdf icon[PDF - X.X MB]"
           OR any individual chapter ("Health Effects", "Public Health", …).

This MCP server replicates exactly that:
  • Fetches the master index once.
  • Filters anchors whose visible text contains the search query
    (case-insensitive substring match).  Multiple matches are common
    (e.g. "benzene" matches Benzene, Ethylbenzene, Chlorobenzene, …).
  • For each matching profile (up to 4) fetches the profile page in
    parallel, extracts every PDF link in *website order*:
        Complete Profile → Preface → Public Health Statement → Chapter 1..8
  • Returns combined list (~10 PDFs per chemical, in website order).

Result ordering
---------------
For a single-chemical match like "Benzene", the very first paper in the
results is always the **Complete Profile** PDF (`tp3.pdf` for Benzene) —
matching the user's website expectation.  Subsequent papers are the per-
chapter PDFs in website order.

How downloading works
---------------------
Each PDF URL in the result is a direct link on `www.atsdr.cdc.gov/
ToxProfiles/`.  No fallbacks — these URLs always serve real PDFs.
Saved as <PDF_DIR_ATSDR>/atsdr_<original-stem>.pdf
(default pdfs/ATSDR/, override via PDF_DIR_ATSDR env var).

Typical workflow
----------------
  1. `search_papers("benzene", max_results=10)`
  2. Pick a chapter (or the first result = Complete Profile).
  3. `download_pdf(pdf_urls=<that paper's pdf_urls>)`
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Search
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def search_papers(
    query: Annotated[
        str,
        "Chemical name — e.g. 'Benzene', 'Lead', 'Asbestos'.  Case-insensitive "
        "substring match against ATSDR's master Toxicological Profiles index.  "
        "Multi-chemical matches are common (e.g. 'benzene' also matches "
        "Ethylbenzene, Chlorobenzene)."
    ],
    max_results: Annotated[
        int,
        "How many PDFs to return (1-50).  A single chemical's Toxicological "
        "Profile yields ~10 PDFs (Complete Profile + 8-9 chapter PDFs)."
    ] = 20,
) -> str:
    """
    Search ATSDR Toxicological Profiles → return Complete Profile + chapter PDFs.

    Returns a JSON object:
      status            "ok" | "no_results" | "error"
      query             the chemical query as sent
      total             number of PDFs returned
      response_time_ms  end-to-end (index fetch + parallel profile fetches)
      papers[]          per PDF, in website order:
        title       "<Chemical> — <Chapter title>"
                    (e.g. "Benzene — Complete Profile",
                          "Benzene — Health Effects")
        type        always "ATSDR"
        url         the profile landing page (wwwn.cdc.gov/TSP/ToxProfiles/...)
        pdf_urls    one-element list with the direct PDF URL on
                    www.atsdr.cdc.gov/ToxProfiles/
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
        "ATSDR PDF URL list from `search_papers`.  Always one element — pass "
        "through unchanged.  All URLs live under www.atsdr.cdc.gov/ToxProfiles/ "
        "and serve real PDF bytes directly."
    ],
) -> str:
    """
    Download a single ATSDR Toxicological Profile chapter PDF.

    No multi-URL fallback (each chapter has exactly one canonical PDF).
    Saved as <PDF_DIR_ATSDR>/atsdr_<original-stem>.pdf  (default pdfs/ATSDR/).

    Returns a JSON object:
      status        "success" | "failed" | "no_url" | "error"
      pdf_path      absolute path on disk (None if failed)
      pdf_source    the URL that was tried
      file_size_kb  size of saved file
      attempts[]    single-element trace { url, http_status, is_pdf,
                    success, error } — diagnostics if the download fails.
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
    port = int(os.getenv("MCP_ATSDR_PORT", "8008"))
    if "--http" in args:
        print(f"[ATSDR MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[ATSDR MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
