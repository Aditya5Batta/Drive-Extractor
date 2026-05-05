"""
EuropePMC Pipeline — FastAPI Backend
  POST /api/run          — run pipeline for a chemical name
  GET  /api/runs         — list all past runs
  GET  /api/runs/{id}    — single run details
  GET  /api/runs/{id}/papers — papers for a run
  GET  /health           — health check
"""
from __future__ import annotations
import asyncio, json, uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import sys, os

# Make mcp/ importable
sys.path.insert(0, str(Path(__file__).parent / "mcp"))

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db import Paper, Run, get_db, init_db
from europepmc import download_pdf as _download_pdf
from europepmc import search as _search

PDF_DIR  = Path(__file__).parent.parent / "pdfs"   # always absolute, inside project
FRONTEND = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(_):
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="EuropePMC Pipeline", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Serve PDFs and frontend
if PDF_DIR.exists():
    app.mount("/pdfs", StaticFiles(directory=str(PDF_DIR)), name="pdfs")
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(FRONTEND / "index.html"))


# ── request model ─────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    chemical:     str
    max_results:  int = 25
    max_pdfs:     int = 10


# ── POST /api/run ─────────────────────────────────────────────────────────────
@app.post("/api/run")
async def run_pipeline(req: RunRequest, db: AsyncSession = Depends(get_db)):
    run_id    = f"RUN_{uuid.uuid4().hex[:8].upper()}"
    started   = datetime.utcnow()

    run = Run(
        run_id   = run_id,
        chemical = req.chemical.strip(),
        status   = "started",
    )
    db.add(run)
    await db.flush()

    papers_out = []
    pdfs_ok = pdfs_fail = pdfs_skip = 0

    try:
        # ── 1. Search EuropePMC ───────────────────────────────────────────────
        papers = await _search(req.chemical, max_results=req.max_results)
        run.papers_found = len(papers)
        await db.flush()

        # ── 2. Download PDFs (limited to max_pdfs) ────────────────────────────
        to_dl   = papers[: req.max_pdfs]
        to_skip = papers[req.max_pdfs :]

        sem = asyncio.Semaphore(2)

        async def dl(p: dict) -> dict:
            async with sem:
                return await _download_pdf(
                    pmcid    = p["pmcid"],
                    pmid     = p.get("pmid") or "unknown",
                    pdf_urls = p["pdf_urls"],
                    pdf_dir  = PDF_DIR,
                )

        dl_results = list(await asyncio.gather(*[dl(p) for p in to_dl]))

        # ── 3. Persist papers ─────────────────────────────────────────────────
        for paper, result in zip(to_dl, dl_results):
            status = result["status"]
            if status == "success":
                pdfs_ok += 1
            else:
                pdfs_fail += 1

            row = Paper(
                run_id     = run_id,
                pmcid      = paper["pmcid"],
                pmid       = paper.get("pmid"),
                title      = paper.get("title"),
                journal    = paper.get("journal"),
                year       = paper.get("year"),
                doi        = paper.get("doi"),
                epmc_url   = paper.get("epmc_url"),
                pdf_status = status,
                pdf_path   = result.get("pdf_path"),
                pdf_source = result.get("pdf_source"),
                error_log  = json.dumps(result.get("errors", [])) if result.get("errors") else None,
            )
            db.add(row)
            papers_out.append(_paper_out(paper, result))

        for paper in to_skip:
            pdfs_skip += 1
            row = Paper(
                run_id     = run_id,
                pmcid      = paper["pmcid"],
                pmid       = paper.get("pmid"),
                title      = paper.get("title"),
                journal    = paper.get("journal"),
                year       = paper.get("year"),
                doi        = paper.get("doi"),
                epmc_url   = paper.get("epmc_url"),
                pdf_status = "skipped",
            )
            db.add(row)
            papers_out.append(_paper_out(paper, {"status": "skipped"}))

        finished = datetime.utcnow()
        run.finished_at  = finished
        run.duration_s   = (finished - started).total_seconds()
        run.pdfs_success = pdfs_ok
        run.pdfs_failed  = pdfs_fail
        run.pdfs_skipped = pdfs_skip
        run.status       = (
            "success" if pdfs_ok > 0
            else "no_pdfs" if len(papers) > 0
            else "no_results"
        )

    except Exception as e:
        finished = datetime.utcnow()
        run.finished_at = finished
        run.duration_s  = (finished - started).total_seconds()
        run.status = "failed"
        run.error  = str(e)

    await db.commit()

    return {
        "run_id":   run_id,
        "chemical": req.chemical,
        "status":   run.status,
        "summary": {
            "papers_found":   run.papers_found or 0,
            "pdfs_success":   pdfs_ok,
            "pdfs_failed":    pdfs_fail,
            "pdfs_skipped":   pdfs_skip,
            "duration_s":     run.duration_s,
            "started_at":     str(started),
            "finished_at":    str(run.finished_at),
        },
        "papers": papers_out,
    }


# ── GET /api/runs ─────────────────────────────────────────────────────────────
@app.get("/api/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Run).order_by(desc(Run.started_at)).limit(100)
    )).scalars().all()
    return [_run_dict(r) for r in rows]


# ── GET /api/runs/{run_id} ────────────────────────────────────────────────────
@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(Run).where(Run.run_id == run_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Run not found")
    return _run_dict(row)


# ── GET /api/runs/{run_id}/papers ─────────────────────────────────────────────
@app.get("/api/runs/{run_id}/papers")
async def get_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Paper).where(Paper.run_id == run_id).order_by(Paper.id)
    )).scalars().all()
    return [_paper_dict(r) for r in rows]


# ── GET /health ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        db_ok = "connected"
    except Exception as e:
        db_ok = f"error: {e}"
    return {"status": "ok", "db": db_ok}


# ── serialisers ───────────────────────────────────────────────────────────────
def _run_dict(r: Run) -> dict:
    return {
        "run_id":       r.run_id,
        "chemical":     r.chemical,
        "status":       r.status,
        "started_at":   str(r.started_at),
        "finished_at":  str(r.finished_at),
        "duration_s":   r.duration_s,
        "papers_found": r.papers_found,
        "pdfs_success": r.pdfs_success,
        "pdfs_failed":  r.pdfs_failed,
        "pdfs_skipped": r.pdfs_skipped,
        "error":        r.error,
    }

def _paper_dict(p: Paper) -> dict:
    return {
        "pmcid":      p.pmcid,
        "pmid":       p.pmid,
        "title":      p.title,
        "journal":    p.journal,
        "year":       p.year,
        "doi":        p.doi,
        "epmc_url":   p.epmc_url,
        "pdf_status": p.pdf_status,
        "pdf_path":   p.pdf_path,
        "pdf_source": p.pdf_source,
        "error_log":  p.error_log,
        "fetched_at": str(p.fetched_at),
    }

def _paper_out(paper: dict, result: dict) -> dict:
    return {
        "pmcid":      paper["pmcid"],
        "pmid":       paper.get("pmid"),
        "title":      paper.get("title"),
        "journal":    paper.get("journal"),
        "year":       paper.get("year"),
        "doi":        paper.get("doi"),
        "epmc_url":   paper.get("epmc_url"),
        "pdf_status": result.get("status"),
        "pdf_path":   result.get("pdf_path"),
        "pdf_source": result.get("pdf_source"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True,
                app_dir=str(Path(__file__).parent))
