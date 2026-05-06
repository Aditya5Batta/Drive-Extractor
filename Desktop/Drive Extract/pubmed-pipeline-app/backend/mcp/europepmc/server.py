"""
EuropePMC MCP Server
====================
Two tools:
  1. search_papers   — search EuropePMC for open-access papers
  2. download_pdf    — download a paper's PDF from EuropePMC

Run (stdio — for Claude Desktop):
    python backend/mcp/europepmc/server.py
Run (HTTP — for testing):
    python backend/mcp/europepmc/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

# sys.path: add parent (mcp/) so `europepmc` package is importable
THIS_DIR = Path(__file__).parent              # backend/mcp/europepmc/
MCP_DIR  = THIS_DIR.parent                    # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")    # project root .env

from mcp.server.fastmcp import FastMCP
from europepmc import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_EPMC",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "europepmc"),
))

mcp = FastMCP(
    name="EuropePMC",
    instructions=(
        "Search EuropePMC for open-access scientific papers and download "
        "their PDFs directly from EuropePMC."
    ),
)


@mcp.tool()
async def search_papers(
    query: Annotated[str, "Search query e.g. 'Benzene blood plasma toxicokinetics'"],
    max_results: Annotated[int, "How many papers to return (max 50)"] = 25,
) -> str:
    """Search EuropePMC for open-access papers."""
    result = await _search(query, max_results=min(max(1, max_results), 50))
    papers = result.get("papers", [])
    if not papers:
        return json.dumps({"status": "no_results", "query": query,
                           "total": 0, "papers": []})

    return json.dumps({
        "status": "ok", "query": query, "total": len(papers),
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
                "abstract": p["abstract"][:400] + ("..." if len(p["abstract"]) > 400 else ""),
            }
            for p in papers
        ],
    }, indent=2)


@mcp.tool()
async def download_pdf(
    pmcid: Annotated[str, "PMC ID of the paper e.g. 'PMC1234567'"],
    pdf_urls: Annotated[list[str], "EuropePMC PDF URLs to try (from search_papers)"],
    pmid: Annotated[str, "PubMed ID (optional, used for the saved filename)"] = "",
) -> str:
    """Download the PDF for a paper from EuropePMC."""
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
        "status":     result["status"],
        "pmcid":      pmcid,
        "pdf_path":   result["pdf_path"],
        "pdf_source": result["pdf_source"],
        "attempts":   result.get("attempts", []),
    }, indent=2)


if __name__ == "__main__":
    args = sys.argv[1:]
    port = int(os.getenv("MCP_PORT", "8001"))
    if "--http" in args:
        print(f"[EuropePMC MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[EuropePMC MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
