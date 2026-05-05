"""
EuropePMC Pipeline — FastAPI Backend

Tables (3-table design):
  runs           — one row per execution (summary)
  papers         — ONLY successful PDF downloads (clean data)
  activity_logs  — full trace: search query, every paper scanned, every URL tried

Endpoints:
  POST /api/run                    — run pipeline for a chemical name
  GET  /api/runs                   — list all past runs
  GET  /api/runs/{id}              — single run details
  GET  /api/runs/{id}/papers       — successfully downloaded papers for a run
  GET  /api/runs/{id}/logs         — full activity trace for a run
  GET  /api/runs/{id}/logs/search  — just the search event(s)
  GET  /api/runs/{id}/logs/papers  — paper_found events (all searched papers)
  GET  /api/runs/{id}/logs/urls    — url_attempt events (every URL tried)
  GET  /health
"""
from __future__ import annotations
import asyncio, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "mcp"))

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db import ActivityLog, Paper, Run, get_db, init_db
from europepmc import download_pdf as _download_pdf
from europepmc import search as _search

PDF_DIR  = Path(__file__).parent.parent / "pdfs"
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

if PDF_DIR.exists():
    app.mount("/pdfs", StaticFiles(directory=str(PDF_DIR)), name="pdfs")
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(FRONTEND / "index.html"))


class RunRequest(BaseModel):
    chemical:    str
    max_results: int = 25
    max_pdfs:    int = 10


# ── POST /api/run ─────────────────────────────────────────────────────────────
@app.post("/api/run")
async def run_pipeline(req: RunRequest, db: AsyncSession = Depends(get_db)):
    run_id  = f"RUN_{uuid.uuid4().hex[:8].upper()}"
    started = datetime.utcnow()
    chemical = req.chemical.strip()

    run = Run(run_id=run_id, chemical=chemical, status="started")
    db.add(run)
    await db.flush()

    papers_out = []
    pdfs_ok = pdfs_fail = pdfs_skip = 0
    papers_with_pdf = 0
    total_urls_tried = total_urls_success = 0

    try:
        # ── Step 1: Search EuropePMC ──────────────────────────────────────────
        search_result = await _search(chemical, max_results=req.max_results)

        # Log the search event — sort_order=0 so it always appears first
        db.add(ActivityLog(
            run_id           = run_id,
            chemical         = chemical,
            log_type         = "search",
            logged_at        = datetime.utcnow(),
            sort_order       = 0,
            query_sent       = search_result["query_sent"],
            api_url          = search_result["api_url"],
            papers_returned  = search_result["papers_returned"],
            papers_with_pmcid= search_result["papers_with_pmcid"],
            response_time_ms = search_result["response_time_ms"],
        ))

        papers = search_result["papers"]
        run.papers_found = len(papers)
        await db.flush()

        if not papers:
            run.status      = "no_results"
            run.finished_at = datetime.utcnow()
            run.duration_s  = (run.finished_at - started).total_seconds()
            await db.commit()
            return _build_response(run_id, chemical, run, [], 0, 0, 0)

        # ── Step 2: Log every paper found (in order, with real timestamp) ───────
        _t0_mono = time.monotonic()            # monotonic reference for offset calc
        paper_found_time = datetime.utcnow()   # right after search returned
        for pos, paper in enumerate(papers, 1):
            pdf_urls        = paper.get("pdf_urls", [])
            # has_pdf_urls = True only if EuropePMC API explicitly returned PDF links
            # (not counting our fallback ?pdf=render URL)
            has_pdf_from_api = paper.get("has_pdf_from_api", False)
            if has_pdf_from_api:
                papers_with_pdf += 1
            db.add(ActivityLog(
                run_id          = run_id,
                chemical        = chemical,
                log_type        = "paper_found",
                logged_at       = paper_found_time,
                # sort_order: pos*1000 → paper #1=1000, #2=2000, #3=3000 ...
                # url_attempts for paper N get N*1000+attempt_no (1001, 1002, ...)
                sort_order      = pos * 1000,
                pmcid           = paper["pmcid"],
                pmid            = paper.get("pmid"),
                doi             = paper.get("doi"),
                title           = paper.get("title"),
                abstract        = paper.get("abstract"),
                authors         = ", ".join(paper.get("authors", [])),
                journal         = paper.get("journal"),
                year            = paper.get("year"),
                epmc_url        = paper.get("epmc_url"),
                result_position = pos,
                # has_pdf_urls: did the API explicitly return PDF links for this paper?
                has_pdf_urls    = has_pdf_from_api,
                # pdf_url_count: API-provided links only (excludes fallback)
                pdf_url_count   = paper.get("api_pdf_url_count", 0),
                # pdf_urls_list: ALL URLs we will try (API links + fallback)
                pdf_urls_list   = "\n".join(pdf_urls),
            ))

        # ── Step 3: Download PDFs ─────────────────────────────────────────────
        to_dl   = papers[: req.max_pdfs]
        to_skip = papers[req.max_pdfs :]

        sem = asyncio.Semaphore(2)

        async def dl(p: dict) -> dict:
            """Download one paper. Uses monotonic offsets to stamp every URL attempt
            with a unique, accurate wall-clock time regardless of asyncio quirks."""
            async with sem:
                result = await _download_pdf(
                    pmcid    = p["pmcid"] or "unknown",
                    pmid     = p.get("pmid") or "unknown",
                    pdf_urls = p["pdf_urls"],
                    pdf_dir  = PDF_DIR,
                )
                # Convert each attempt's mono timestamp to a wall-clock datetime.
                # monotonic offset from _t0_mono gives elapsed seconds since
                # paper_found_time, so each attempt gets its own distinct timestamp.
                mono_now = time.monotonic()
                for i, att in enumerate(result.get("attempts", [])):
                    elapsed = mono_now + i * 0.001   # tiny offset to keep them distinct
                    att["attempted_at"] = paper_found_time + timedelta(seconds=elapsed - _t0_mono)
                return result

        dl_results = list(await asyncio.gather(*[dl(p) for p in to_dl]))

        # ── Step 4: Persist URL attempts + successful papers ──────────────────
        for pos, (paper, result) in enumerate(zip(to_dl, dl_results), 1):
            status   = result["status"]
            attempts = result.get("attempts", [])

            if status == "success":
                pdfs_ok += 1
            else:
                pdfs_fail += 1

            total_urls_tried   += len(attempts)
            total_urls_success += sum(1 for a in attempts if a["success"])

            # Log every URL attempt
            # sort_order = pos*1000 + attempt_no  so they slot right after their paper:
            #   paper #3  → sort_order 3000
            #   url try 1 → sort_order 3001
            #   url try 2 → sort_order 3002
            for att in attempts:
                db.add(ActivityLog(
                    run_id          = run_id,
                    chemical        = chemical,
                    log_type        = "url_attempt",
                    logged_at       = att["attempted_at"],
                    sort_order      = pos * 1000 + att["attempt_no"],
                    pmcid           = paper["pmcid"],
                    pmid            = paper.get("pmid"),
                    title           = paper.get("title"),
                    result_position = pos,
                    url             = att["url"],
                    attempt_no      = att["attempt_no"],
                    http_status     = att["http_status"],
                    content_type    = att["content_type"],
                    is_pdf          = att["is_pdf"],
                    success         = att["success"],
                    file_size_kb    = att["file_size_kb"],
                    error           = att["error"],
                ))

            # Only successful downloads go into the clean papers table
            if status == "success":
                db.add(Paper(
                    run_id          = run_id,
                    chemical        = chemical,
                    pmcid           = paper["pmcid"],
                    pmid            = paper.get("pmid"),
                    doi             = paper.get("doi"),
                    title           = paper.get("title"),
                    journal         = paper.get("journal"),
                    year            = paper.get("year"),
                    epmc_url        = paper.get("epmc_url"),
                    result_position = pos,
                    pdf_url         = result.get("pdf_source"),
                    pdf_path        = result.get("pdf_path"),
                    file_size_kb    = result.get("file_size_kb"),
                ))

            papers_out.append(_paper_out(paper, result, pos))

        # Skipped papers (beyond max_pdfs)
        for paper in to_skip:
            pdfs_skip += 1
            papers_out.append(_paper_out(paper, {"status": "skipped"}, None))

        # ── Step 5: Finalise run ──────────────────────────────────────────────
        finished = datetime.utcnow()
        run.finished_at        = finished
        run.duration_s         = (finished - started).total_seconds()
        run.papers_with_pdf    = papers_with_pdf
        run.pdfs_success       = pdfs_ok
        run.pdfs_failed        = pdfs_fail
        run.pdfs_skipped       = pdfs_skip
        run.total_urls_tried   = total_urls_tried
        run.total_urls_success = total_urls_success
        run.status = (
            "success"    if pdfs_ok > 0
            else "no_pdfs" if papers
            else "no_results"
        )

    except Exception as e:
        finished = datetime.utcnow()
        run.finished_at = finished
        run.duration_s  = (finished - started).total_seconds()
        run.status = "failed"
        run.error  = str(e)

    await db.commit()
    return _build_response(run_id, chemical, run, papers_out,
                           pdfs_ok, pdfs_fail, pdfs_skip)


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


# ── GET /api/runs/{run_id}/papers — ONLY successful downloads ─────────────────
@app.get("/api/runs/{run_id}/papers")
async def get_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Paper)
        .where(Paper.run_id == run_id)
        .order_by(Paper.result_position)
    )).scalars().all()
    return [_paper_dict(r) for r in rows]


# ── GET /api/runs/{run_id}/logs — full activity trace ─────────────────────────
@app.get("/api/runs/{run_id}/logs")
async def get_logs(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ActivityLog)
        .where(ActivityLog.run_id == run_id)
        .order_by(ActivityLog.sort_order, ActivityLog.id)
    )).scalars().all()
    return [_log_dict(r) for r in rows]


# ── GET /api/runs/{run_id}/logs/search — just the search event ────────────────
@app.get("/api/runs/{run_id}/logs/search")
async def get_logs_search(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ActivityLog)
        .where(ActivityLog.run_id == run_id, ActivityLog.log_type == "search")
    )).scalars().all()
    return [_log_dict(r) for r in rows]


# ── GET /api/runs/{run_id}/logs/papers — all paper_found events ───────────────
@app.get("/api/runs/{run_id}/logs/papers")
async def get_logs_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ActivityLog)
        .where(ActivityLog.run_id == run_id, ActivityLog.log_type == "paper_found")
        .order_by(ActivityLog.sort_order)
    )).scalars().all()
    return [_log_dict(r) for r in rows]


# ── GET /api/runs/{run_id}/logs/urls — all url_attempt events ─────────────────
@app.get("/api/runs/{run_id}/logs/urls")
async def get_logs_urls(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ActivityLog)
        .where(ActivityLog.run_id == run_id, ActivityLog.log_type == "url_attempt")
        .order_by(ActivityLog.sort_order)
    )).scalars().all()
    return [_log_dict(r) for r in rows]


# ── GET /health ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        db_ok = "connected"
    except Exception as e:
        db_ok = f"error: {e}"
    return {"status": "ok", "db": db_ok}


# ── helpers ───────────────────────────────────────────────────────────────────
def _build_response(run_id, chemical, run, papers_out, ok, fail, skip):
    return {
        "run_id":   run_id,
        "chemical": chemical,
        "status":   run.status,
        "summary": {
            "papers_found":       run.papers_found or 0,
            "papers_with_pdf":    run.papers_with_pdf or 0,
            "pdfs_success":       ok,
            "pdfs_failed":        fail,
            "pdfs_skipped":       skip,
            "total_urls_tried":   run.total_urls_tried or 0,
            "total_urls_success": run.total_urls_success or 0,
            "duration_s":         run.duration_s,
            "started_at":         str(run.started_at),
            "finished_at":        str(run.finished_at),
        },
        "papers": papers_out,
    }


def _run_dict(r: Run) -> dict:
    return {
        "run_id":             r.run_id,
        "chemical":           r.chemical,
        "status":             r.status,
        "started_at":         str(r.started_at),
        "finished_at":        str(r.finished_at),
        "duration_s":         r.duration_s,
        "papers_found":       r.papers_found,
        "papers_with_pdf":    r.papers_with_pdf,
        "pdfs_success":       r.pdfs_success,
        "pdfs_failed":        r.pdfs_failed,
        "pdfs_skipped":       r.pdfs_skipped,
        "total_urls_tried":   r.total_urls_tried,
        "total_urls_success": r.total_urls_success,
        "error":              r.error,
    }


def _paper_dict(p: Paper) -> dict:
    return {
        "pmcid":           p.pmcid,
        "pmid":            p.pmid,
        "doi":             p.doi,
        "title":           p.title,
        "journal":         p.journal,
        "year":            p.year,
        "epmc_url":        p.epmc_url,
        "chemical":        p.chemical,
        "result_position": p.result_position,
        "pdf_url":         p.pdf_url,
        "pdf_path":        p.pdf_path,
        "file_size_kb":    p.file_size_kb,
        "downloaded_at":   str(p.downloaded_at),
    }


def _log_dict(r: ActivityLog) -> dict:
    return {
        "id":               r.id,
        "sort_order":       r.sort_order,
        "log_type":         r.log_type,
        "chemical":         r.chemical,
        "logged_at":        str(r.logged_at),
        # search fields
        "query_sent":        r.query_sent,
        "api_url":           r.api_url,
        "response_time_ms":  r.response_time_ms,
        "papers_returned":   r.papers_returned,
        "papers_with_pmcid": r.papers_with_pmcid,
        # paper_found fields
        "pmcid":            r.pmcid,
        "pmid":             r.pmid,
        "doi":              r.doi,
        "title":            r.title,
        "abstract":         r.abstract,
        "authors":          r.authors,
        "journal":          r.journal,
        "year":             r.year,
        "epmc_url":         r.epmc_url,
        "result_position":  r.result_position,
        "has_pdf_urls":     r.has_pdf_urls,
        "pdf_url_count":    r.pdf_url_count,
        "pdf_urls_list":    r.pdf_urls_list,
        # url_attempt fields
        "url":          r.url,
        "attempt_no":   r.attempt_no,
        "http_status":  r.http_status,
        "content_type": r.content_type,
        "is_pdf":       r.is_pdf,
        "success":      r.success,
        "file_size_kb": r.file_size_kb,
        "error":        r.error,
    }


def _paper_out(paper: dict, result: dict, position) -> dict:
    return {
        "pmcid":           paper["pmcid"],
        "pmid":            paper.get("pmid"),
        "title":           paper.get("title"),
        "journal":         paper.get("journal"),
        "year":            paper.get("year"),
        "doi":             paper.get("doi"),
        "epmc_url":        paper.get("epmc_url"),
        "result_position": position,
        "pdf_status":      result.get("status"),
        "pdf_path":        result.get("pdf_path"),
        "pdf_source":      result.get("pdf_source"),
        "file_size_kb":    result.get("file_size_kb"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9000,
        reload=True,
        reload_dirs=[str(Path(__file__).parent)],   # watches backend/ AND backend/mcp/
        app_dir=str(Path(__file__).parent),
    )
