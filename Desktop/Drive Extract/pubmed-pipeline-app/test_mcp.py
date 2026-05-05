import asyncio, sys
sys.path.insert(0, "backend/mcp")
from europepmc import search, download_pdf
from pathlib import Path

async def test():
    print("Searching EuropePMC for: Benzene blood plasma\n")
    papers = await search("Benzene blood plasma", max_results=5)
    print(f"Found {len(papers)} papers\n")
    for i, p in enumerate(papers, 1):
        pmcid   = p["pmcid"]
        title   = p["title"][:80]
        journal = p["journal"]
        year    = p["year"]
        nurls   = len(p["pdf_urls"])
        print(f"{i}. [{pmcid}] {title}")
        print(f"   {journal} ({year}) | {nurls} PDF URL(s)")
        print()

    # Try downloading first paper
    if papers:
        p = papers[0]
        print(f"Downloading PDF for: {p['pmcid']}...")
        result = await download_pdf(p["pmcid"], p["pmid"] or "unknown", p["pdf_urls"], Path("pdfs"))
        print(f"Status : {result['status']}")
        print(f"Saved  : {result['pdf_path']}")
        if result["errors"]:
            print(f"Errors : {result['errors']}")

asyncio.run(test())
