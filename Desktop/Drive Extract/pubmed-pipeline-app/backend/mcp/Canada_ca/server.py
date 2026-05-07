"""
Canada.ca Substances Search MCP Server
=======================================
Mirrors https://pollution-waste.canada.ca/substances-search/Substance?lang=en
as two MCP tools — search the index, then download a result's PDF.

Run (stdio — for Claude Desktop):
    python backend/mcp/Canada_ca/server.py
Run (HTTP — for testing):
    python backend/mcp/Canada_ca/server.py --http
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from typing import Annotated

THIS_DIR = Path(__file__).parent              # backend/mcp/Canada_ca/
MCP_DIR  = THIS_DIR.parent                    # backend/mcp/
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from dotenv import load_dotenv
load_dotenv(THIS_DIR.parent.parent.parent / ".env")    # project root .env

from mcp.server.fastmcp import FastMCP
from Canada_ca import search as _search, download_pdf as _download_pdf

PDF_DIR = Path(os.getenv(
    "PDF_DIR_CANADA",
    str(THIS_DIR.parent.parent.parent / "pdfs" / "Canada_ca"),
))


# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="Canada_ca",
    instructions="""\
Canada.ca Substances Search
============================
Mirrors https://pollution-waste.canada.ca/substances-search/Substance?lang=en

When to use this server
-----------------------
• Canadian government substance/chemical assessments and reports.
• Looking for Health Canada or Environment and Climate Change Canada documents.
• Searching for substance profiles, screening assessments, or risk management.

How searching works
-------------------
• Fetches the Canada.ca substances search page directly using the query.
• Returns results in the same order as the website (document order).
• For each result page, extracts any PDF links marked with [PDF - X KB] badges.
• Results include page title, snippet, and all PDF URLs found on that page.

How downloading works
---------------------
• `download_pdf` walks the `pdf_urls` list in order.
• Stops at the first URL that returns valid PDF bytes (HTTP 200 + PDF content).
• Saves as canada_<url-stem>.pdf in the configured PDF directory.

Typical workflow
----------------
  1. Call `search_papers("benzene", max_results=20)`
  2. Pick a result with pdf_urls.
  3. Call `download_pdf(pdf_urls=<that paper's pdf_urls>)`
""",
)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — Search
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def search_papers(
    query: Annotated[
        str,
        "Chemical name or substance to search for, e.g. 'benzene', 'lead', 'mercury'."
    ],
    max_results: Annotated[
        int,
        "How many results to return (1-50)."
    ] = 25,
) -> str:
    """
    Search Canada.ca substances and return results in website order with PDF URLs.

    Returns a JSON object:
      status           "ok" | "no_results" | "error"
      query            the query as sent
      total            number of results returned
      response_time_ms how long the search took
      papers[]         per result:
        title          page title
        abstract       short text snippet from the page
        journal        always "Canada.ca"
        epmc_url       Canada.ca landing page URL
        pdf_urls       list of PDF URLs found on that page (empty = no PDF)
        has_pdf_from_api  whether any PDFs were found
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
        "status": "ok", "query": query, "total": len(papers),
        "response_time_ms": result.get("response_time_ms"),
        "papers": [
            {
                "title":            p["title"],
                "abstract":         p["abstract"][:400] + ("..." if len(p["abstract"]) > 400 else ""),
                "journal":          p["journal"],
                "epmc_url":         p["epmc_url"],
                "pdf_urls":         p["pdf_urls"],
                "has_pdf_from_api": p["has_pdf_from_api"],
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
        "Ordered list of PDF URLs to attempt — pass the `pdf_urls` array "
        "from `search_papers` exactly as-is."
    ],
    pmcid: Annotated[
        str,
        "Optional identifier for the document (used for tracing)."
    ] = "",
    pmid: Annotated[
        str,
        "Optional secondary identifier."
    ] = "",
) -> str:
    """
    Download a PDF from Canada.ca.

    Walks `pdf_urls` in order, stops at the first successful response.
    Saves to <PDF_DIR_CANADA>/canada_<url-stem>.pdf.

    Returns a JSON object:
      status        "success" | "failed" | "no_url" | "error"
      pdf_path      absolute path of the saved PDF (None if failed)
      pdf_source    URL that delivered the PDF
      attempts[]    per-URL trace
    """
    if not pdf_urls:
        return json.dumps({"status": "error", "message": "pdf_urls list is empty"})

    result = await _download_pdf(
        pmcid=pmcid or "canada",
        pmid=pmid or "unknown",
        pdf_urls=pdf_urls,
        pdf_dir=PDF_DIR,
    )
    return json.dumps({
        "status":     result["status"],
        "pdf_path":   result["pdf_path"],
        "pdf_source": result["pdf_source"],
        "attempts":   [{k: v for k, v in a.items() if k != "attempted_at"}
                       for a in result.get("attempts", [])],
    }, indent=2)


if __name__ == "__main__":
    args = sys.argv[1:]
    port = int(os.getenv("MCP_PORT", "8010"))
    if "--http" in args:
        print(f"[Canada.ca MCP] HTTP -> http://localhost:{port}/mcp")
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    elif "--sse" in args:
        print(f"[Canada.ca MCP] SSE -> http://localhost:{port}/sse")
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
