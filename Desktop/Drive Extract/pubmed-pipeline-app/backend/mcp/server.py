"""
EuropePMC MCP Server
====================
Two tools exposed to any MCP client (Claude Desktop, etc.):

  1. search_papers   — search EuropePMC for open-access papers
  2. download_pdf    — download a paper's PDF from EuropePMC

Run (stdio — for Claude Desktop):
    python backend/mcp/server.py

Run (HTTP — for testing):
    python backend/mcp/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

# ── sys.path: add this folder so europepmc.py is importable ──────────────────
THIS_DIR = Path(__file__).parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

# ── load .env from project root ───────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
import europepmc

PDF_DIR = Path(os.getenv("PDF_DIR", str(THIS_DIR.parent.parent / "pdfs")))

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="EuropePMC",
    instructions=(
        "Search EuropePMC for open-access scientific papers and download "
        "their PDFs directly from EuropePMC. No other sources are used."
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
    Search EuropePMC for open-access papers matching the query.
    Returns paper metadata including title, authors, abstract, journal,
    year, DOI, PMCID, EuropePMC URL, and available PDF URLs.
    """
    papers = await europepmc.search(query, max_results=min(max(1, max_results), 50))

    if not papers:
        return json.dumps({
            "status":  "no_results",
            "query":   query,
            "total":   0,
            "papers":  [],
        })

    return json.dumps({
        "status": "ok",
        "query":  query,
        "total":  len(papers),
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
                "abstract": p["abstract"][:400] + "..." if len(p["abstract"]) > 400 else p["abstract"],
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
        "EuropePMC PDF URLs to try (use pdf_urls from search_papers result)"
    ],
    pmid: Annotated[
        str,
        "PubMed ID (optional, used for the saved filename)"
    ] = "",
) -> str:
    """
    Download the PDF for a paper using EuropePMC URLs.
    Pass the pdf_urls list returned by search_papers.
    Returns the local file path on success.
    """
    if not pmcid:
        return json.dumps({"status": "error", "message": "pmcid is required"})
    if not pdf_urls:
        return json.dumps({"status": "error", "message": "pdf_urls list is empty"})

    result = await europepmc.download_pdf(
        pmcid    = pmcid,
        pmid     = pmid or "unknown",
        pdf_urls = pdf_urls,
        pdf_dir  = PDF_DIR,
    )

    return json.dumps({
        "status":     result["status"],
        "pmcid":      pmcid,
        "pdf_path":   result["pdf_path"],
        "pdf_source": result["pdf_source"],
        "errors":     result["errors"],
    }, indent=2)


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = int(os.getenv("MCP_PORT", "8001"))
        print(f"[EuropePMC MCP] HTTP server -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        port = int(os.getenv("MCP_PORT", "8001"))
        print(f"[EuropePMC MCP] SSE server -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        # stdio mode: silent by design — MCP client connects via stdin/stdout
        # To test interactively, run:  mcp dev backend/mcp/server.py
        mcp.run(transport="stdio")
