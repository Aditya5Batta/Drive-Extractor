from __future__ import annotations
import asyncio, json, uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import ApiCall, Download, Paper, Run, get_db, init_db
from services import (
    build_query, download_pdf, fetch_metadata, fetch_pmc_ids,
    search_pubmed, verify_chemical, PDF_DIR
)

FRONTEND = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(_):
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="PubMed PDF Pipeline", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if PDF_DIR.exists():
    app.mount("/pdfs", StaticFiles(directory=str(PDF_DIR)), name="pdfs")
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
    @app.get("/", include_in_schema=False)
    async def index(): return FileResponse(str(FRONTEND / "index.html"))


class PipelineReq(BaseModel):
    chemical: str
    keywords: list[str] = [
        "pharmacokinetic", "toxicokinetic", "AUC", "Cmax", "clearance",
        "metabolism", "absorption", "plasma", "blood plasma concentration",
        "serum", "blood",
    ]
    max_results: int = 20
    max_pdfs:    int = 5


@app.post("/api/run")
async def run_pipeline(req: PipelineReq, db: AsyncSession = Depends(get_db)):
    run_id = f"RUN_{uuid.uuid4().hex[:8].upper()}"

    run = Run(run_id=run_id, chemical_input=req.chemical,
              keywords=json.dumps(req.keywords), status="started")
    db.add(run)
    await db.flush()

    # Step 1 — PubChem (single call, safe to flush here)
    chem = await verify_chemical(req.chemical)
    db.add(ApiCall(run_id=run_id, call_type="pubchem",
                   status="success" if chem["success"] else "failed",
                   message=chem.get("error") or "verified"))
    if chem["success"]:
        run.preferred_name = chem["preferred_name"]
        run.casrn          = chem["CASRN"]
        run.pubchem_cid    = chem["PubChem_CID"]
        run.synonyms       = json.dumps(chem["synonyms"])
    await db.flush()

    # Step 2 — PubMed searches in parallel (NO DB writes inside gather)
    sem = asyncio.Semaphore(4)

    async def search_kw(kw: str) -> tuple[str, list[dict], int]:
        async with sem:
            q = build_query(chem, kw)
            pmids, hits = await search_pubmed(q, req.max_results)
            if not pmids:
                return kw, [], hits
            metas   = await fetch_metadata(pmids)
            pmc_map = await fetch_pmc_ids(pmids)
            papers  = [
                {**meta, "pmid": pmid, "rank": rank, "keyword": kw, "search_query": q,
                 "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                 "pmc_id": pmc_map.get(pmid)}
                for rank, (pmid, meta) in enumerate(zip(pmids, metas), 1)
            ]
            return kw, papers, hits

    all_results = await asyncio.gather(*[search_kw(kw) for kw in req.keywords])

    # Write search logs to DB sequentially (safe now)
    total_hits = 0
    for kw, papers_kw, hits in all_results:
        total_hits += hits
        db.add(ApiCall(run_id=run_id, call_type="pubmed_search", keyword=kw,
                       query=build_query(chem, kw), hit_count=hits,
                       status="success", message=f"{hits} hits"))
    await db.flush()

    # De-duplicate by PMID
    seen: set[str] = set()
    papers: list[dict] = []
    for _, group, _ in all_results:
        for p in group:
            pmid = p.get("pmid", "")
            if pmid and pmid in seen:
                continue
            if pmid:
                seen.add(pmid)
            papers.append(p)

    run.kw_searched  = len(req.keywords)
    run.total_hits   = total_hits
    run.papers_found = len(papers)

    # Step 3 — PDF downloads in parallel (NO DB writes inside gather)
    candidates = papers[: len(req.keywords) * req.max_pdfs]
    dl_sem = asyncio.Semaphore(4)

    async def dl_one(p: dict) -> dict:
        async with dl_sem:
            return await download_pdf(p)

    downloaded = list(await asyncio.gather(*[dl_one(p) for p in candidates]))
    rest = [{**p, "pdf_status": "skipped", "pdf_url": None,
             "pdf_path": None, "pdf_source": None} for p in papers[len(candidates):]]
    final = downloaded + rest

    # Step 4 — Persist papers sequentially
    pdfs_ok = pdfs_fail = 0
    out_papers = []

    for p in final:
        db_paper = Paper(
            run_id=run_id, pmid=p.get("pmid"), title=p.get("title"),
            abstract=p.get("abstract"), authors=p.get("authors"),
            journal=p.get("journal"), year=p.get("year"), doi=p.get("doi"),
            pubmed_url=p.get("pubmed_url"), pmc_id=p.get("pmc_id"),
            keyword=p.get("keyword"), search_query=p.get("search_query"),
            rank=p.get("rank"), pdf_url=p.get("pdf_url"),
            pdf_status=p.get("pdf_status", "pending"),
            pdf_path=p.get("pdf_path"), pdf_source=p.get("pdf_source"),
        )
        db.add(db_paper)

        if p.get("pdf_status") == "success":
            pdfs_ok += 1
        elif p.get("pdf_status") in ("failed", "no_pdf_found"):
            pdfs_fail += 1

        out_papers.append({
            "keyword": p.get("keyword"), "search_query": p.get("search_query"),
            "rank": p.get("rank"), "pmid": p.get("pmid"), "title": p.get("title"),
            "doi": p.get("doi"), "journal": p.get("journal"), "year": p.get("year"),
            "pubmed_url": p.get("pubmed_url"), "pdf_url": p.get("pdf_url"),
            "pdf_status": p.get("pdf_status"), "pdf_path": p.get("pdf_path"),
            "pdf_source": p.get("pdf_source"),
        })

    # Flush papers first so we have IDs for downloads
    await db.flush()

    # Add download records
    for p, db_paper in zip(final, (await db.execute(
        select(Paper).where(Paper.run_id == run_id).order_by(Paper.id)
    )).scalars().all()):
        if p.get("pdf_status") == "success":
            db.add(Download(
                paper_id=db_paper.id, pmid=p.get("pmid"),
                source=p.get("pdf_source"), pdf_url=p.get("pdf_url"),
                status="success", file_path=p.get("pdf_path"),
            ))

    run.pdfs_ok   = pdfs_ok
    run.pdfs_fail = pdfs_fail
    run.status    = "success" if pdfs_ok else ("partial_success" if papers else "no_results")
    await db.commit()

    return {
        "run_id": run_id,
        "chemical": {
            "input": req.chemical, "preferred_name": chem.get("preferred_name"),
            "casrn": chem.get("CASRN"), "pubchem_cid": chem.get("PubChem_CID"),
            "synonyms": chem.get("synonyms", []),
        },
        "summary": {
            "keywords_searched": run.kw_searched, "total_pubmed_hits": run.total_hits,
            "papers_found": run.papers_found, "pdfs_downloaded": pdfs_ok, "pdfs_failed": pdfs_fail,
        },
        "papers": out_papers,
        "status": run.status,
    }


# ─── History endpoints ────────────────────────────────────────────────────────

@app.get("/api/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Run).order_by(desc(Run.created_at)).limit(100))).scalars().all()
    return [_run_dict(r) for r in rows]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Run).where(Run.run_id == run_id))).scalar_one_or_none()
    if not row: raise HTTPException(404, "Run not found")
    return _run_dict(row)


@app.get("/api/runs/{run_id}/papers")
async def get_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Paper).where(Paper.run_id == run_id).order_by(Paper.rank))).scalars().all()
    return [_paper_dict(r) for r in rows]


@app.get("/api/runs/{run_id}/calls")
async def get_calls(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ApiCall).where(ApiCall.run_id == run_id).order_by(ApiCall.created_at))).scalars().all()
    return [{"type": r.call_type, "keyword": r.keyword, "query": r.query,
             "hits": r.hit_count, "status": r.status, "message": r.message} for r in rows]


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    return {"status": "ok", "db": db_status}


def _run_dict(r: Run) -> dict:
    return {"run_id": r.run_id, "chemical": r.chemical_input, "preferred_name": r.preferred_name,
            "casrn": r.casrn, "pubchem_cid": r.pubchem_cid, "status": r.status,
            "kw_searched": r.kw_searched, "total_hits": r.total_hits, "papers_found": r.papers_found,
            "pdfs_ok": r.pdfs_ok, "pdfs_fail": r.pdfs_fail, "created_at": str(r.created_at)}

def _paper_dict(p: Paper) -> dict:
    return {"pmid": p.pmid, "title": p.title, "journal": p.journal, "year": p.year,
            "doi": p.doi, "pubmed_url": p.pubmed_url, "keyword": p.keyword,
            "rank": p.rank, "pdf_status": p.pdf_status, "pdf_path": p.pdf_path,
            "pdf_source": p.pdf_source, "pdf_url": p.pdf_url}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
