"""
NTP MCP Server — search NTP (National Toxicology Program) documents.

Run (stdio):  python backend/mcp/ntp/server.py
Run (HTTP):   python backend/mcp/ntp/server.py --http
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

mcp = FastMCP(
    name="NTP",
    instructions=(
        "Search the National Toxicology Program (NTP) for monographs, RoC "
        "profiles, and technical reports as direct PDF downloads."
    ),
)


@mcp.tool()
async def search_papers(
    query: Annotated[str, "Search query, e.g. 'Benzene'"],
    max_results: Annotated[int, "How many docs to return (max 50)"] = 20,
) -> str:
    """Search NTP documents (PDF format filter applied)."""
    result = await _search(query, max_results=min(max(1, max_results), 50))
    if result.get("status") == "error":
        return json.dumps({"status": "error", "query": query,
                           "error": result.get("error"), "papers": []})
    papers = result.get("papers", [])
    if not papers:
        return json.dumps({"status": "no_results", "query": query,
                           "total": 0, "papers": []})
    return json.dumps({
        "status": "ok", "query": query, "total": len(papers),
        "response_time_ms": result.get("response_time_ms"),
        "papers": [
            {
                "title":    p["title"],
                "type":     p["journal"],
                "url":      p["epmc_url"],
                "pdf_urls": p["pdf_urls"],
                "snippet":  p["abstract"][:300],
            }
            for p in papers
        ],
    }, indent=2)


@mcp.tool()
async def download_pdf(
    pdf_urls: Annotated[list[str], "NTP PDF URLs from search_papers"],
) -> str:
    """Download an NTP document PDF."""
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
