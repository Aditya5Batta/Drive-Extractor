"""
Semantic Scholar MCP Server
============================
Two tools exposed to any MCP client (Claude Desktop, etc.):

  1. search_papers   — search Semantic Scholar Graph API for open-access papers
  2. download_pdf    — download a paper's PDF from the open-access URL

Run (stdio — for Claude Desktop):
    python backend/mcp/semanticscholar/server.py

Run (HTTP — for testing):
    python backend/mcp/semanticscholar/server.py --http

Run (SSE — for testing):
    python backend/mcp/semanticscholar/server.py --sse
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

# ── sys.path: add parent (mcp/) so semanticscholar package is importable ─────
THIS_DIR = Path(__file__).parent            # backend/mcp/semanticscholar/
MCP_DIR  = THIS_DIR.parent                 # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")

from mcp.server.fastmcp import FastMCP
from semanticscholar import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_SS",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "semanticscholar")
))

mcp = FastMCP(
    name="SemanticScholar",
    instructions=(
        "Search Semantic Scholar for open-access scientific papers and download "
        "their PDFs. Uses the Semantic Scholar Graph API — only papers with a "
        "free open-access PDF are returned."
    ),
)


@mcp.tool()
async def search_papers(
    query: Annotated[str, "Search query e.g. 'Benzene blood plasma toxicokinetics'"],
    max_results: Annotated[int, "How many papers to return (max 100)"] = 25,
) -> str:
    """
    Search Semantic Scholar for papers with open-access PDFs.
    Returns title, authors, journal, year, DOI, ArXiv ID, SS URL, and PDF URL.
    """
    result = await _search(query, max_results=min(max(1, max_results), 100))

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
                "paper_id": p["paper_id"],
                "pmcid":    p["pmcid"],
                "pmid":     p["pmid"],
                "doi":      p["doi"],
                "arxiv":    p["arxiv"],
                "title":    p["title"],
                "authors":  p["authors"][:5],
                "journal":  p["journal"],
                "year":     p["year"],
                "ss_url":   p["ss_url"],
                "pdf_urls": p["pdf_urls"],
                "oa_status": p["oa_status"],
                "abstract": p["abstract"][:400] + "..." if len(p["abstract"]) > 400 else p["abstract"],
            }
            for p in papers
        ],
    }, indent=2)


@mcp.tool()
async def download_pdf(
    paper_id: Annotated[str, "Semantic Scholar paper ID (from search_papers result)"],
    pdf_urls: Annotated[list[str], "Open-access PDF URLs to try (from search_papers result)"],
    pmid: Annotated[str, "PubMed ID (optional, used for the saved filename)"] = "",
) -> str:
    """
    Download a Semantic Scholar paper's PDF from its open-access URL.
    Pass the pdf_urls list returned by search_papers.
    Returns the local file path on success.
    """
    if not paper_id:
        return json.dumps({"status": "error", "message": "paper_id is required"})
    if not pdf_urls:
        return json.dumps({"status": "error", "message": "pdf_urls list is empty"})

    result = await _download_pdf(
        paper_id = paper_id,
        pmid     = pmid or "unknown",
        pdf_urls = pdf_urls,
        pdf_dir  = PDF_DIR,
    )

    return json.dumps({
        "status":       result["status"],
        "paper_id":     paper_id,
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


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = int(os.getenv("MCP_SS_PORT", "8003"))
        print(f"[SemanticScholar MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        port = int(os.getenv("MCP_SS_PORT", "8003"))
        print(f"[SemanticScholar MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
