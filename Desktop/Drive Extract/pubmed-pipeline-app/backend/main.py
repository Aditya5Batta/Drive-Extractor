"""
Multi-database PDF pipeline — FastAPI Backend

Three databases, identical 3-table schema each (runs / papers / activity_logs):
  • EuropePMC      — POST /api/run        (prefix RUN_)
  • PubMed Central — POST /api/pmc/run    (prefix PMC_)
  • Semantic Scholar — POST /api/ss/run   (prefix SS_)

All three pipelines share ONE generic implementation (`_run_pipeline`); they
differ only in their `_DbDriver` config (search fn, download fn, ORM models,
PDF dir, optional extra fields).
"""
from __future__ import annotations
import asyncio, time, uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).parent / "mcp"))

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db import (
    ActivityLog,    Paper,    Run,
    PmcActivityLog, PmcPaper, PmcRun,
    SsActivityLog,  SsPaper,  SsRun,
    get_db, init_db,
)
from europepmc      import search as _epmc_search,  download_pdf as _epmc_download
from pmc            import search as _pmc_search,   download_pdf as _pmc_download
from semanticscholar import search as _ss_search,   download_pdf as _ss_download

# ── paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR      = PROJECT_ROOT / "pdfs"
PDF_DIR_EPMC = PDF_DIR / "europepmc"
PDF_DIR_PMC  = PDF_DIR / "pmc"
PDF_DIR_SS   = PDF_DIR / "semanticscholar"
FRONTEND     = PROJECT_ROOT / "frontend"

for d in (PDF_DIR_EPMC, PDF_DIR_PMC, PDF_DIR_SS):
    d.mkdir(parents=True, exist_ok=True)


# ── app ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_):
    await init_db()
    yield


app = FastAPI(title="Pubmed Pipeline", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/pdfs/europepmc",       StaticFiles(directory=str(PDF_DIR_EPMC)), name="pdfs_epmc")
app.mount("/pdfs/pmc",             StaticFiles(directory=str(PDF_DIR_PMC)),  name="pdfs_pmc")
app.mount("/pdfs/semanticscholar", StaticFiles(directory=str(PDF_DIR_SS)),   name="pdfs_ss")
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(FRONTEND / "index.html"))


class RunRequest(BaseModel):
    chemical: str
    max_pdfs: int = 10


# ══════════════════════════════════════════════════════════════════════════════
# Per-database driver — bundles everything that varies across EPMC / PMC / SS
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class _DbDriver:
    prefix:        str       # "RUN_" / "PMC_" / "SS_"
    pdf_dir:       Path
    search:        Callable[[str, int], Awaitable[dict]]
    download:      Callable[[dict, Path], Awaitable[dict]]
    Run:           Any        # SQLAlchemy model class
    Paper:         Any
    Log:           Any
    extra_fields:  Callable[[dict], dict]  # paper → kwargs (for SS-only fields)


def _no_extras(_paper: dict) -> dict: return {}

def _ss_extras(p: dict) -> dict:
    return {
        "paper_id":  p.get("paper_id"),
        "arxiv":     p.get("arxiv"),
        "ss_url":    p.get("ss_url"),
        "oa_status": p.get("oa_status"),
    }


# Wrap each download function in a uniform `(paper, pdf_dir) -> result` shape
async def _epmc_dl(p: dict, d: Path) -> dict:
    return await _epmc_download(
        pmcid=p["pmcid"] or "unknown", pmid=p.get("pmid") or "unknown",
        pdf_urls=p["pdf_urls"], pdf_dir=d)

async def _pmc_dl(p: dict, d: Path) -> dict:
    return await _pmc_download(
        pmcid=p["pmcid"] or "unknown", pmid=p.get("pmid") or "unknown",
        pdf_urls=p["pdf_urls"], pdf_dir=d)

async def _ss_dl(p: dict, d: Path) -> dict:
    return await _ss_download(
        paper_id=p.get("paper_id") or "unknown", pmid=p.get("pmid") or "unknown",
        pdf_urls=p["pdf_urls"], pdf_dir=d)


EPMC = _DbDriver("RUN_", PDF_DIR_EPMC, _epmc_search, _epmc_dl,
                 Run, Paper, ActivityLog, _no_extras)
PMC  = _DbDriver("PMC_", PDF_DIR_PMC,  _pmc_search,  _pmc_dl,
                 PmcRun, PmcPaper, PmcActivityLog, _no_extras)
SS   = _DbDriver("SS_",  PDF_DIR_SS,   _ss_search,   _ss_dl,
                 SsRun, SsPaper, SsActivityLog, _ss_extras)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline helpers
# ══════════════════════════════════════════════════════════════════════════════
def _build_candidates(papers: list[dict]) -> list[tuple[int, dict]]:
    """
    Walk papers in relevance order, keep only those with pdf_urls,
    deduplicate by DOI / PMID / normalised title.
    """
    seen_doi:   set[str] = set()
    seen_pmid:  set[str] = set()
    seen_title: set[str] = set()
    out: list[tuple[int, dict]] = []
    for pos, p in enumerate(papers, 1):
        if not p.get("pdf_urls"): continue
        doi   = (p.get("doi")   or "").lower().strip()
        pmid  = (p.get("pmid")  or "").strip()
        title = " ".join((p.get("title") or "").lower().split())
        if doi   and doi   in seen_doi:   continue
        if pmid  and pmid  in seen_pmid:  continue
        if title and title in seen_title: continue
        out.append((pos, p))
        if doi:   seen_doi.add(doi)
        if pmid:  seen_pmid.add(pmid)
        if title: seen_title.add(title)
    return out


async def _download_until_target(
    candidates: list[tuple[int, dict]],
    dl_func: Callable[[int, dict], Awaitable[dict]],
    target: int,
    batch_size: int = 5,
) -> dict[int, dict]:
    """Batched download in order; stop at first `target` successes."""
    dl_map: dict[int, dict] = {}
    successes = 0
    i = 0
    while successes < target and i < len(candidates):
        batch = candidates[i : i + batch_size]
        i += len(batch)
        results = await asyncio.gather(*[dl_func(pos, p) for pos, p in batch])
        for (pos, _), r in zip(batch, results):
            dl_map[pos] = r
            if r.get("status") == "success":
                successes += 1
                if successes >= target: break
    return dl_map


# ══════════════════════════════════════════════════════════════════════════════
# Generic pipeline — used by all 3 databases
# ══════════════════════════════════════════════════════════════════════════════
async def _run_pipeline(d: _DbDriver, req: RunRequest, db: AsyncSession) -> dict:
    run_id   = f"{d.prefix}{uuid.uuid4().hex[:8].upper()}"
    started  = datetime.utcnow()
    chemical = req.chemical.strip()

    run = d.Run(run_id=run_id, chemical=chemical, status="started")
    db.add(run); await db.flush()

    papers_out: list[dict] = []
    ok = fail = skip = no_url = with_pdf = 0
    urls_tried = urls_ok = 0

    try:
        # 1) Search — over-fetch 10× max_pdfs so retries can find replacements
        auto_max = min(req.max_pdfs * 10, 200)
        sr = await d.search(chemical, auto_max)

        db.add(d.Log(
            run_id=run_id, chemical=chemical, log_type="search",
            logged_at=datetime.utcnow(), sort_order=0,
            query_sent=sr["query_sent"], api_url=sr["api_url"],
            papers_returned=sr["papers_returned"],
            papers_with_pmcid=sr["papers_with_pmcid"],
            response_time_ms=sr["response_time_ms"],
        ))
        papers = sr["papers"]
        run.papers_found = len(papers)
        await db.flush()

        if not papers:
            run.status = "no_results"
            run.finished_at = datetime.utcnow()
            run.duration_s  = (run.finished_at - started).total_seconds()
            await db.commit()
            return _build_response(run_id, chemical, run, [], 0, 0, 0)

        # 2) Log every paper found, in relevance order
        t0 = time.monotonic()
        found_at = datetime.utcnow()
        for pos, p in enumerate(papers, 1):
            if p.get("has_pdf_from_api"): with_pdf += 1
            db.add(d.Log(
                run_id=run_id, chemical=chemical, log_type="paper_found",
                logged_at=found_at, sort_order=pos * 1000,
                pmcid=p.get("pmcid"), pmid=p.get("pmid"), doi=p.get("doi"),
                title=p.get("title"), abstract=p.get("abstract"),
                authors=", ".join(p.get("authors", [])),
                journal=p.get("journal"), year=p.get("year"),
                epmc_url=p.get("epmc_url"), result_position=pos,
                has_pdf_urls=p.get("has_pdf_from_api", False),
                pdf_url_count=p.get("api_pdf_url_count", 0),
                pdf_urls_list="\n".join(p.get("pdf_urls", [])),
                **d.extra_fields(p),
            ))

        # 3) Build candidate list & download until we have max_pdfs successes
        candidates = _build_candidates(papers)
        sem = asyncio.Semaphore(2)

        async def _do_dl(_pos: int, p: dict) -> dict:
            async with sem:
                result = await d.download(p, d.pdf_dir)
                # back-fill realistic timestamps for url_attempt logs
                mono = time.monotonic()
                for k, att in enumerate(result.get("attempts", [])):
                    att["attempted_at"] = found_at + timedelta(
                        seconds=(mono + k * 0.001) - t0)
                return result

        dl_map = await _download_until_target(candidates, _do_dl, req.max_pdfs)

        # 4) Persist results in original paper order
        for pos, p in enumerate(papers, 1):
            if not p.get("pdf_urls"):
                result, _bucket = {"status": "no_url", "attempts": []}, "no_url"
                no_url += 1
            elif pos in dl_map:
                result   = dl_map[pos]
                attempts = result.get("attempts", [])
                urls_tried += len(attempts)
                urls_ok    += sum(1 for a in attempts if a["success"])
                if result["status"] == "success":
                    ok += 1
                    db.add(d.Paper(
                        run_id=run_id, chemical=chemical,
                        pmcid=p.get("pmcid"), pmid=p.get("pmid"), doi=p.get("doi"),
                        title=p.get("title"), journal=p.get("journal"),
                        year=p.get("year"), epmc_url=p.get("epmc_url"),
                        result_position=pos,
                        pdf_url=result.get("pdf_source"),
                        pdf_path=result.get("pdf_path"),
                        file_size_kb=result.get("file_size_kb"),
                        **d.extra_fields(p),
                    ))
                else:
                    fail += 1
                for att in attempts:
                    db.add(d.Log(
                        run_id=run_id, chemical=chemical, log_type="url_attempt",
                        logged_at=att["attempted_at"],
                        sort_order=pos * 1000 + att["attempt_no"],
                        pmcid=p.get("pmcid"), pmid=p.get("pmid"),
                        title=p.get("title"), result_position=pos,
                        url=att["url"], attempt_no=att["attempt_no"],
                        http_status=att["http_status"],
                        content_type=att["content_type"],
                        is_pdf=att["is_pdf"], success=att["success"],
                        file_size_kb=att["file_size_kb"], error=att["error"],
                        **d.extra_fields(p),
                    ))
            else:
                result = {"status": "skipped"}
                skip += 1
            papers_out.append(_paper_out(p, result, pos))

        # 5) Finalise run row
        finished = datetime.utcnow()
        run.finished_at        = finished
        run.duration_s         = (finished - started).total_seconds()
        run.papers_with_pdf    = with_pdf
        run.pdfs_success       = ok
        run.pdfs_failed        = fail
        run.pdfs_skipped       = skip + no_url
        run.total_urls_tried   = urls_tried
        run.total_urls_success = urls_ok
        run.status = ("success" if ok > 0
                      else "no_pdfs" if papers else "no_results")

    except Exception as e:
        run.finished_at = datetime.utcnow()
        run.duration_s  = (run.finished_at - started).total_seconds()
        run.status = "failed"
        run.error  = str(e)

    await db.commit()
    return _build_response(run_id, chemical, run, papers_out, ok, fail, skip, no_url)


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints — thin wrappers around the generic pipeline
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/run")
async def run_epmc(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(EPMC, req, db)

@app.post("/api/pmc/run")
async def run_pmc(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(PMC, req, db)

@app.post("/api/ss/run")
async def run_ss(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(SS, req, db)


# ── runs listings (one shared dict shape) ─────────────────────────────────────
@app.get("/api/runs")
async def list_runs_epmc(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, Run)

@app.get("/api/pmc/runs")
async def list_runs_pmc(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, PmcRun)

@app.get("/api/ss/runs")
async def list_runs_ss(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, SsRun)


async def _list_runs(db: AsyncSession, model) -> list[dict]:
    rows = (await db.execute(
        select(model).order_by(desc(model.started_at)).limit(100)
    )).scalars().all()
    return [_run_dict(r) for r in rows]


# ── single-run endpoints (EuropePMC only — kept for backward compatibility) ──
@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Run).where(Run.run_id == run_id))).scalar_one_or_none()
    if not row: raise HTTPException(404, "Run not found")
    return _run_dict(row)

@app.get("/api/runs/{run_id}/papers")
async def get_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Paper).where(Paper.run_id == run_id).order_by(Paper.result_position)
    )).scalars().all()
    return [_paper_dict(r) for r in rows]

@app.get("/api/runs/{run_id}/logs")
async def get_logs(run_id: str, db: AsyncSession = Depends(get_db)):
    return await _logs(db, run_id, log_type=None)

@app.get("/api/runs/{run_id}/logs/search")
async def get_logs_search(run_id: str, db: AsyncSession = Depends(get_db)):
    return await _logs(db, run_id, "search")

@app.get("/api/runs/{run_id}/logs/papers")
async def get_logs_papers(run_id: str, db: AsyncSession = Depends(get_db)):
    return await _logs(db, run_id, "paper_found")

@app.get("/api/runs/{run_id}/logs/urls")
async def get_logs_urls(run_id: str, db: AsyncSession = Depends(get_db)):
    return await _logs(db, run_id, "url_attempt")


async def _logs(db: AsyncSession, run_id: str, log_type: str | None) -> list[dict]:
    q = select(ActivityLog).where(ActivityLog.run_id == run_id)
    if log_type: q = q.where(ActivityLog.log_type == log_type)
    rows = (await db.execute(q.order_by(ActivityLog.sort_order, ActivityLog.id))).scalars().all()
    return [_log_dict(r) for r in rows]


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "ok", "db": f"error: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# Dict shapers (one per row type — used by all 3 DBs)
# ══════════════════════════════════════════════════════════════════════════════
def _build_response(run_id, chemical, run, papers_out, ok, fail, skip, no_url=0):
    return {
        "run_id":   run_id,
        "chemical": chemical,
        "status":   run.status,
        "summary": {
            "pdfs_success": ok,
            "pdfs_failed":  fail,
            "duration_s":   run.duration_s,
            "started_at":   str(run.started_at),
            "finished_at":  str(run.finished_at),
        },
        "papers": [p for p in papers_out if p.get("pdf_status") == "success"],
    }


def _run_dict(r) -> dict:
    return {
        "run_id":             r.run_id,
        "chemical":           r.chemical,
        "status":             r.status,
        "started_at":         str(r.started_at),
        "finished_at":        str(r.finished_at),
        "duration_s":         r.duration_s,
        "papers_found":       getattr(r, "papers_found", None),
        "papers_with_pdf":    getattr(r, "papers_with_pdf", None),
        "pdfs_success":       r.pdfs_success,
        "pdfs_failed":        r.pdfs_failed,
        "pdfs_skipped":       getattr(r, "pdfs_skipped", None),
        "total_urls_tried":   getattr(r, "total_urls_tried", None),
        "total_urls_success": getattr(r, "total_urls_success", None),
        "error":              r.error,
    }


def _paper_dict(p) -> dict:
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


def _log_dict(r) -> dict:
    return {
        "id":               r.id,
        "sort_order":       r.sort_order,
        "log_type":         r.log_type,
        "chemical":         r.chemical,
        "logged_at":        str(r.logged_at),
        "query_sent":        r.query_sent,
        "api_url":           r.api_url,
        "response_time_ms":  r.response_time_ms,
        "papers_returned":   r.papers_returned,
        "papers_with_pmcid": r.papers_with_pmcid,
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
        "pmcid":           paper.get("pmcid"),
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
        host="0.0.0.0", port=9000, reload=True,
        reload_dirs=[str(Path(__file__).parent)],
        app_dir=str(Path(__file__).parent),
    )
