"""
PubMed Central (PMC) MCP Server
================================
Two tools exposed to any MCP client (Claude Desktop, etc.):

  1. search_papers   — search PubMed Central (NCBI E-utilities) for papers
  2. download_pdf    — download a paper's PDF (tries NCBI then EuropePMC fallback)

Run (stdio — for Claude Desktop):
    python backend/mcp/pmc/server.py

Run (HTTP — for testing):
    python backend/mcp/pmc/server.py --http

Run (SSE — for testing):
    python backend/mcp/pmc/server.py --sse
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

# ── sys.path: add parent (mcp/) so pmc package is importable ─────────────────
THIS_DIR = Path(__file__).parent            # backend/mcp/pmc/
MCP_DIR  = THIS_DIR.parent                 # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# ── load .env from project root ───────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")   # project root

from mcp.server.fastmcp import FastMCP

# Use aliases so tool functions can use clean names without collision
from pmc import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv("PDF_DIR_PMC", str(THIS_DIR.parent.parent.parent / "pdfs" / "pmc")))

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="PubMedCentral",
    instructions=(
        "Search PubMed Central (NCBI) for scientific papers and download "
        "their PDFs. Uses NCBI E-utilities for search and tries NCBI native "
        "PDF first, then EuropePMC render URL as fallback for better coverage."
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Search
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_papers(
    query: Annotated[
        str,
        "Search query e.g. 'Benzene blood plasma toxicokinetics'"
    ],
    max_results: Annotated[
        int,
        "How many papers to return (max 50)"
    ] = 25,
) -> str:
    """
    Search PubMed Central for papers matching the query via NCBI E-utilities.
    Returns paper metadata including title, authors, journal, year, DOI,
    PMCID, PMID, article URL, and PDF URLs to try (NCBI + EuropePMC fallback).
    """
    result = await _search(query, max_results=min(max(1, max_results), 50))

    if result.get("status") == "error":
        return json.dumps({
            "status": "error",
            "query":  query,
            "error":  result.get("error"),
            "papers": [],
        })

    papers = result.get("papers", [])
    if not papers:
        return json.dumps({
            "status": "no_results",
            "query":  query,
            "total":  0,
            "papers": [],
        })

    return json.dumps({
        "status":           "ok",
        "query":            query,
        "total":            len(papers),
        "response_time_ms": result.get("response_time_ms"),
        "papers": [
            {
                "pmcid":    p["pmcid"],
                "pmid":     p["pmid"],
                "title":    p["title"],
                "authors":  p["authors"][:5],
                "journal":  p["journal"],
                "year":     p["year"],
                "doi":      p["doi"],
                "epmc_url": p["epmc_url"],
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
    pmcid: Annotated[
        str,
        "PMC ID of the paper e.g. 'PMC1234567' (from search_papers result)"
    ],
    pdf_urls: Annotated[
        list[str],
        "PDF URLs to try in order (use pdf_urls from search_papers result)"
    ],
    pmid: Annotated[
        str,
        "PubMed ID (optional, used for the saved filename)"
    ] = "",
) -> str:
    """
    Download the PDF for a PMC paper.
    Tries NCBI's native PDF URL first, then EuropePMC render URL as fallback.
    Pass the pdf_urls list returned by search_papers.
    Returns the local file path on success.
    """
    if not pmcid:
        return json.dumps({"status": "error", "message": "pmcid is required"})
    if not pdf_urls:
        return json.dumps({"status": "error", "message": "pdf_urls list is empty"})

    result = await _download_pdf(
        pmcid    = pmcid,
        pmid     = pmid or "unknown",
        pdf_urls = pdf_urls,
        pdf_dir  = PDF_DIR,
    )

    return json.dumps({
        "status":       result["status"],
        "pmcid":        pmcid,
        "pdf_path":     result["pdf_path"],
        "pdf_source":   result["pdf_source"],
        "file_size_kb": result["file_size_kb"],
        "attempts": [
            {
                "url":         a["url"],
                "attempt_no":  a["attempt_no"],
                "http_status": a["http_status"],
                "is_pdf":      a["is_pdf"],
                "success":     a["success"],
                "error":       a["error"],
            }
            for a in result.get("attempts", [])
        ],
    }, indent=2)


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = int(os.getenv("MCP_PMC_PORT", "8002"))
        print(f"[PMC MCP] HTTP server -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        port = int(os.getenv("MCP_PMC_PORT", "8002"))
        print(f"[PMC MCP] SSE server -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        # stdio mode — for Claude Desktop
        mcp.run(transport="stdio")
