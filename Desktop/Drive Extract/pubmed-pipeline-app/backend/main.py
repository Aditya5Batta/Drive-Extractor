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
    ActivityLog,      Paper,      Run,
    PmcActivityLog,   PmcPaper,   PmcRun,
    SsActivityLog,    SsPaper,    SsRun,
    EchaActivityLog,  EchaPaper,  EchaRun,
    NtpActivityLog,   NtpPaper,   NtpRun,
    WhoActivityLog,   WhoPaper,   WhoRun,
    OehhaActivityLog,  OehhaPaper,  OehhaRun,
    AtsdrActivityLog,  AtsdrPaper,  AtsdrRun,
    ZenodoActivityLog, ZenodoPaper, ZenodoRun,
    CanadaActivityLog, CanadaPaper, CanadaRun,
    ConcaweActivityLog, ConcawePaper, ConcaweRun,
    SafeWorkActivityLog, SafeWorkPaper, SafeWorkRun,
    OpenAlexActivityLog, OpenAlexPaper, OpenAlexRun,
    EfsaActivityLog, EfsaPaper, EfsaRun,
    NiteActivityLog, NitePaper, NiteRun,
    OecdActivityLog, OecdPaper, OecdRun,
    PubMedActivityLog, PubMedPaper, PubMedRun,
    NioshActivityLog, NioshPaper, NioshRun,
    get_db, init_db,
)
from europepmc       import search as _epmc_search,   download_pdf as _epmc_download
from pmc             import search as _pmc_search,    download_pdf as _pmc_download
from semanticscholar import search as _ss_search,     download_pdf as _ss_download
from echa            import search as _echa_search,   download_pdf as _echa_download
from ntp             import search as _ntp_search,    download_pdf as _ntp_download
from who_ipcs        import search as _who_search,    download_pdf as _who_download
from OEHHA           import search as _oehha_search,  download_pdf as _oehha_download
from ATSDR           import search as _atsdr_search,  download_pdf as _atsdr_download
from Zenodo          import search as _zenodo_search, download_pdf as _zenodo_download
from Canada_ca       import search as _canada_search, download_pdf as _canada_download
from Concawe         import search as _concawe_search, download_pdf as _concawe_download
from SafeWork_AU     import search as _safework_search, download_pdf as _safework_download
from OpenAlex        import search as _openalex_search, download_pdf as _openalex_download
from EFSA            import search as _efsa_search, download_pdf as _efsa_download
from NITE            import search as _nite_search, download_pdf as _nite_download
from OECD            import search as _oecd_search, download_pdf as _oecd_download
from PubMed          import search as _pubmed_search, download_pdf as _pubmed_download
from NIOSH           import search as _niosh_search,  download_pdf as _niosh_download

# ── paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
PDF_DIR      = PROJECT_ROOT / "pdfs"
PDF_DIR_EPMC = PDF_DIR / "europepmc"
PDF_DIR_PMC  = PDF_DIR / "pmc"
PDF_DIR_SS   = PDF_DIR / "semanticscholar"
PDF_DIR_ECHA  = PDF_DIR / "echa"
PDF_DIR_NTP   = PDF_DIR / "ntp"
PDF_DIR_WHO   = PDF_DIR / "who_ipcs"
PDF_DIR_OEHHA  = PDF_DIR / "OEHHA"
PDF_DIR_ATSDR  = PDF_DIR / "ATSDR"
PDF_DIR_ZENODO   = PDF_DIR / "Zenodo"
PDF_DIR_CANADA   = PDF_DIR / "Canada_ca"
PDF_DIR_CONCAWE  = PDF_DIR / "Concawe"
PDF_DIR_SAFEWORK = PDF_DIR / "SafeWork_AU"
PDF_DIR_OPENALEX = PDF_DIR / "OpenAlex"
PDF_DIR_EFSA     = PDF_DIR / "EFSA"
PDF_DIR_NITE     = PDF_DIR / "NITE"
PDF_DIR_OECD     = PDF_DIR / "OECD"
PDF_DIR_PUBMED   = PDF_DIR / "PubMed"
PDF_DIR_NIOSH    = PDF_DIR / "NIOSH"
FRONTEND         = PROJECT_ROOT / "frontend"

for d in (PDF_DIR_EPMC, PDF_DIR_PMC, PDF_DIR_SS,
          PDF_DIR_ECHA, PDF_DIR_NTP, PDF_DIR_WHO, PDF_DIR_OEHHA,
          PDF_DIR_ATSDR, PDF_DIR_ZENODO, PDF_DIR_CANADA, PDF_DIR_CONCAWE,
          PDF_DIR_SAFEWORK, PDF_DIR_OPENALEX,
          PDF_DIR_EFSA, PDF_DIR_NITE, PDF_DIR_OECD, PDF_DIR_PUBMED,
          PDF_DIR_NIOSH):
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
app.mount("/pdfs/echa",            StaticFiles(directory=str(PDF_DIR_ECHA)),  name="pdfs_echa")
app.mount("/pdfs/ntp",             StaticFiles(directory=str(PDF_DIR_NTP)),   name="pdfs_ntp")
app.mount("/pdfs/who_ipcs",        StaticFiles(directory=str(PDF_DIR_WHO)),   name="pdfs_who")
app.mount("/pdfs/OEHHA",           StaticFiles(directory=str(PDF_DIR_OEHHA)),  name="pdfs_oehha")
app.mount("/pdfs/ATSDR",           StaticFiles(directory=str(PDF_DIR_ATSDR)),  name="pdfs_atsdr")
app.mount("/pdfs/Zenodo",          StaticFiles(directory=str(PDF_DIR_ZENODO)), name="pdfs_zenodo")
app.mount("/pdfs/Canada_ca",       StaticFiles(directory=str(PDF_DIR_CANADA)),  name="pdfs_canada")
app.mount("/pdfs/Concawe",         StaticFiles(directory=str(PDF_DIR_CONCAWE)), name="pdfs_concawe")
app.mount("/pdfs/SafeWork_AU",     StaticFiles(directory=str(PDF_DIR_SAFEWORK)), name="pdfs_safework")
app.mount("/pdfs/OpenAlex",        StaticFiles(directory=str(PDF_DIR_OPENALEX)), name="pdfs_openalex")
app.mount("/pdfs/EFSA",            StaticFiles(directory=str(PDF_DIR_EFSA)),     name="pdfs_efsa")
app.mount("/pdfs/NITE",            StaticFiles(directory=str(PDF_DIR_NITE)),     name="pdfs_nite")
app.mount("/pdfs/OECD",            StaticFiles(directory=str(PDF_DIR_OECD)),     name="pdfs_oecd")
app.mount("/pdfs/PubMed",          StaticFiles(directory=str(PDF_DIR_PUBMED)),   name="pdfs_pubmed")
app.mount("/pdfs/NIOSH",           StaticFiles(directory=str(PDF_DIR_NIOSH)),    name="pdfs_niosh")
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


def _epmc_extras(p: dict) -> dict:
    """EuropePMC + PMC: just the EuropePMC article page link."""
    return {"epmc_url": p.get("epmc_url")}

def _ss_extras(p: dict) -> dict:
    """Semantic Scholar: ss_url plus SS-only metadata."""
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

# Document-database downloaders share the (pmcid, pmid, pdf_urls, pdf_dir)
# signature. We pass safe placeholders for pmcid/pmid (the docs don't have them).
async def _echa_dl(p: dict, d: Path) -> dict:
    return await _echa_download("doc", "noid", p["pdf_urls"], d)

async def _ntp_dl(p: dict, d: Path) -> dict:
    return await _ntp_download("doc", "noid", p["pdf_urls"], d)

async def _who_dl(p: dict, d: Path) -> dict:
    return await _who_download("doc", "noid", p["pdf_urls"], d)

async def _oehha_dl(p: dict, d: Path) -> dict:
    return await _oehha_download("doc", "noid", p["pdf_urls"], d)

async def _atsdr_dl(p: dict, d: Path) -> dict:
    return await _atsdr_download("doc", "noid", p["pdf_urls"], d)

async def _zenodo_dl(p: dict, d: Path) -> dict:
    return await _zenodo_download("doc", "noid", p["pdf_urls"], d)

async def _canada_dl(p: dict, d: Path) -> dict:
    return await _canada_download("doc", "noid", p["pdf_urls"], d)

async def _concawe_dl(p: dict, d: Path) -> dict:
    return await _concawe_download("doc", "noid", p["pdf_urls"], d)

async def _safework_dl(p: dict, d: Path) -> dict:
    return await _safework_download("doc", "noid", p["pdf_urls"], d)

async def _openalex_dl(p: dict, d: Path) -> dict:
    return await _openalex_download("doc", "noid", p["pdf_urls"], d)

async def _efsa_dl(p: dict, d: Path) -> dict:
    return await _efsa_download("doc", "noid", p["pdf_urls"], d)

async def _nite_dl(p: dict, d: Path) -> dict:
    return await _nite_download("doc", "noid", p["pdf_urls"], d)

async def _oecd_dl(p: dict, d: Path) -> dict:
    return await _oecd_download("doc", "noid", p["pdf_urls"], d)

async def _pubmed_dl(p: dict, d: Path) -> dict:
    return await _pubmed_download(
        pmcid=p.get("pmcid") or "unknown",
        pmid=p.get("pmid") or "noid",
        pdf_urls=p["pdf_urls"], pdf_dir=d)

async def _niosh_dl(p: dict, d: Path) -> dict:
    return await _niosh_download("doc", "noid", p["pdf_urls"], d)


EPMC   = _DbDriver("RUN_",    PDF_DIR_EPMC,   _epmc_search,   _epmc_dl,
                   Run, Paper, ActivityLog, _epmc_extras)
PMC    = _DbDriver("PMC_",    PDF_DIR_PMC,    _pmc_search,    _pmc_dl,
                   PmcRun, PmcPaper, PmcActivityLog, _epmc_extras)
SS     = _DbDriver("SS_",     PDF_DIR_SS,     _ss_search,     _ss_dl,
                   SsRun, SsPaper, SsActivityLog, _ss_extras)
ECHA   = _DbDriver("ECHA_",   PDF_DIR_ECHA,   _echa_search,   _echa_dl,
                   EchaRun, EchaPaper, EchaActivityLog, _epmc_extras)
NTP    = _DbDriver("NTP_",    PDF_DIR_NTP,    _ntp_search,    _ntp_dl,
                   NtpRun, NtpPaper, NtpActivityLog, _epmc_extras)
WHO    = _DbDriver("WHO_",    PDF_DIR_WHO,    _who_search,    _who_dl,
                   WhoRun, WhoPaper, WhoActivityLog, _epmc_extras)
OEHHA  = _DbDriver("OEHHA_",  PDF_DIR_OEHHA,  _oehha_search,  _oehha_dl,
                   OehhaRun, OehhaPaper, OehhaActivityLog, _epmc_extras)
ATSDR  = _DbDriver("ATSDR_",  PDF_DIR_ATSDR,  _atsdr_search,  _atsdr_dl,
                   AtsdrRun, AtsdrPaper, AtsdrActivityLog, _epmc_extras)
ZENODO = _DbDriver("ZEN_",    PDF_DIR_ZENODO, _zenodo_search, _zenodo_dl,
                   ZenodoRun, ZenodoPaper, ZenodoActivityLog, _epmc_extras)
CANADA  = _DbDriver("CAN_",     PDF_DIR_CANADA,  _canada_search,  _canada_dl,
                    CanadaRun, CanadaPaper, CanadaActivityLog, _epmc_extras)
CONCAWE = _DbDriver("CONC_",   PDF_DIR_CONCAWE, _concawe_search, _concawe_dl,
                    ConcaweRun, ConcawePaper, ConcaweActivityLog, _epmc_extras)
SAFEWORK = _DbDriver("SWAU_", PDF_DIR_SAFEWORK, _safework_search, _safework_dl,
                     SafeWorkRun, SafeWorkPaper, SafeWorkActivityLog, _epmc_extras)
OPENALEX = _DbDriver("OAX_",  PDF_DIR_OPENALEX, _openalex_search, _openalex_dl,
                     OpenAlexRun, OpenAlexPaper, OpenAlexActivityLog, _epmc_extras)
EFSA = _DbDriver("EFSA_", PDF_DIR_EFSA, _efsa_search, _efsa_dl,
                 EfsaRun, EfsaPaper, EfsaActivityLog, _epmc_extras)
NITE = _DbDriver("NITE_", PDF_DIR_NITE, _nite_search, _nite_dl,
                 NiteRun, NitePaper, NiteActivityLog, _epmc_extras)
OECD = _DbDriver("OECD_", PDF_DIR_OECD, _oecd_search, _oecd_dl,
                 OecdRun, OecdPaper, OecdActivityLog, _epmc_extras)
PUBMED = _DbDriver("PM_",    PDF_DIR_PUBMED, _pubmed_search, _pubmed_dl,
                   PubMedRun, PubMedPaper, PubMedActivityLog, _epmc_extras)
NIOSH  = _DbDriver("NIOSH_", PDF_DIR_NIOSH,  _niosh_search,  _niosh_dl,
                   NioshRun, NioshPaper, NioshActivityLog, _epmc_extras)


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline helpers
# ══════════════════════════════════════════════════════════════════════════════
def _build_candidates(papers: list[dict]) -> list[tuple[int, dict]]:
    """
    Walk papers in relevance order, keep only those with pdf_urls.
    Papers with multiple pdf_urls are expanded — one candidate per URL — so
    every chapter/file gets a download slot (e.g. ATSDR BTEX has 8 chapters).
    Deduplicates by pdf_url, DOI, and PMID.
    """
    seen_doi:  set[str] = set()
    seen_pmid: set[str] = set()
    seen_url:  set[str] = set()
    out: list[tuple[int, dict]] = []
    for pos, p in enumerate(papers, 1):
        urls = p.get("pdf_urls") or []
        if not urls:
            continue
        doi  = (p.get("doi")  or "").lower().strip()
        pmid = (p.get("pmid") or "").strip()
        if doi  and doi  in seen_doi:  continue
        if pmid and pmid in seen_pmid: continue
        for url in urls:
            if url in seen_url:
                continue
            seen_url.add(url)
            # Create a shallow copy with exactly one pdf_url so the download
            # function fetches this specific file, not the whole list.
            entry = dict(p, pdf_urls=[url])
            out.append((pos, entry))
        if doi:  seen_doi.add(doi)
        if pmid: seen_pmid.add(pmid)
    return out


async def _download_until_target(
    candidates: list[tuple[int, dict]],
    dl_func: Callable[[int, dict], Awaitable[dict]],
    target: int,
    batch_size: int = 5,
) -> dict[int, list[dict]]:
    """
    Batched download in order; stop after `target` total successes.
    Returns {pos: [result, ...]} — a list per pos because one paper (pos)
    may expand to multiple PDF files (e.g. ATSDR multi-chapter reports).
    """
    dl_map: dict[int, list[dict]] = {}
    successes = 0
    i = 0
    while successes < target and i < len(candidates):
        batch = candidates[i : i + batch_size]
        i += len(batch)
        results = await asyncio.gather(*[dl_func(pos, p) for pos, p in batch])
        for (pos, _), r in zip(batch, results):
            dl_map.setdefault(pos, []).append(r)
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
            # Distinguish a real "no_results" from an upstream API failure
            # (e.g. SS rate-limited 429) so the user knows to retry.
            if sr.get("status") == "error":
                run.status = "failed"
                run.error  = sr.get("error") or "search returned no data"
            else:
                run.status = "no_results"
            run.finished_at = datetime.utcnow()
            run.duration_s  = (run.finished_at - started).total_seconds()
            await db.commit()
            return _build_response(run_id, chemical, run, [], 0, 0, 0,
                                   max_results=req.max_pdfs)

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
                result_position=pos,
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
        # dl_map[pos] is now a LIST of results (one per expanded PDF URL)
        for pos, p in enumerate(papers, 1):
            if not p.get("pdf_urls"):
                no_url += 1
                papers_out.append(_paper_out(p, {"status": "no_url", "attempts": []}, pos))
                continue

            results_for_pos = dl_map.get(pos, [])
            if not results_for_pos:
                skip += 1
                papers_out.append(_paper_out(p, {"status": "skipped"}, pos))
                continue

            # Each result in the list corresponds to one expanded PDF URL
            first_out = True
            for sub_idx, result in enumerate(results_for_pos):
                attempts = result.get("attempts", [])
                urls_tried += len(attempts)
                urls_ok    += sum(1 for a in attempts if a["success"])
                if result["status"] == "success":
                    ok += 1
                    db.add(d.Paper(
                        run_id=run_id, chemical=chemical,
                        pmcid=p.get("pmcid"), pmid=p.get("pmid"), doi=p.get("doi"),
                        title=p.get("title"), journal=p.get("journal"),
                        year=p.get("year"),
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
                        sort_order=pos * 1000 + sub_idx * 100 + att["attempt_no"],
                        pmcid=p.get("pmcid"), pmid=p.get("pmid"),
                        title=p.get("title"), result_position=pos,
                        url=att["url"], attempt_no=att["attempt_no"],
                        http_status=att["http_status"],
                        content_type=att["content_type"],
                        is_pdf=att["is_pdf"], success=att["success"],
                        file_size_kb=att["file_size_kb"], error=att["error"],
                        **d.extra_fields(p),
                    ))
                # Add one papers_out entry per downloaded file so the UI shows each PDF
                if first_out:
                    papers_out.append(_paper_out(p, result, pos))
                    first_out = False
                else:
                    # Extra PDFs from the same page: show as separate rows in UI
                    p_copy = dict(p, pdf_urls=[result.get("pdf_source") or ""])
                    papers_out.append(_paper_out(p_copy, result, pos))

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
    return _build_response(run_id, chemical, run, papers_out, ok, fail, skip, no_url,
                           max_results=req.max_pdfs)


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

@app.post("/api/echa/run")
async def run_echa(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(ECHA, req, db)

@app.post("/api/ntp/run")
async def run_ntp(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(NTP, req, db)

@app.post("/api/who/run")
async def run_who(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(WHO, req, db)

@app.post("/api/oehha/run")
async def run_oehha(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(OEHHA, req, db)

@app.post("/api/atsdr/run")
async def run_atsdr(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(ATSDR, req, db)

@app.post("/api/zenodo/run")
async def run_zenodo(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(ZENODO, req, db)

@app.post("/api/canada/run")
async def run_canada(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(CANADA, req, db)

@app.post("/api/concawe/run")
async def run_concawe(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(CONCAWE, req, db)

@app.post("/api/safework/run")
async def run_safework(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(SAFEWORK, req, db)

@app.post("/api/openalex/run")
async def run_openalex(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(OPENALEX, req, db)

@app.post("/api/efsa/run")
async def run_efsa(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(EFSA, req, db)

@app.post("/api/nite/run")
async def run_nite(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(NITE, req, db)

@app.post("/api/oecd/run")
async def run_oecd(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(OECD, req, db)

@app.post("/api/pubmed/run")
async def run_pubmed(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(PUBMED, req, db)

@app.post("/api/niosh/run")
async def run_niosh(req: RunRequest, db: AsyncSession = Depends(get_db)):
    return await _run_pipeline(NIOSH, req, db)


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

@app.get("/api/echa/runs")
async def list_runs_echa(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, EchaRun)

@app.get("/api/ntp/runs")
async def list_runs_ntp(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, NtpRun)

@app.get("/api/who/runs")
async def list_runs_who(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, WhoRun)

@app.get("/api/oehha/runs")
async def list_runs_oehha(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, OehhaRun)

@app.get("/api/atsdr/runs")
async def list_runs_atsdr(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, AtsdrRun)

@app.get("/api/zenodo/runs")
async def list_runs_zenodo(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, ZenodoRun)

@app.get("/api/canada/runs")
async def list_runs_canada(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, CanadaRun)

@app.get("/api/concawe/runs")
async def list_runs_concawe(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, ConcaweRun)

@app.get("/api/safework/runs")
async def list_runs_safework(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, SafeWorkRun)

@app.get("/api/openalex/runs")
async def list_runs_openalex(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, OpenAlexRun)

@app.get("/api/efsa/runs")
async def list_runs_efsa(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, EfsaRun)

@app.get("/api/nite/runs")
async def list_runs_nite(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, NiteRun)

@app.get("/api/oecd/runs")
async def list_runs_oecd(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, OecdRun)

@app.get("/api/pubmed/runs")
async def list_runs_pubmed(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, PubMedRun)

@app.get("/api/niosh/runs")
async def list_runs_niosh(db: AsyncSession = Depends(get_db)):
    return await _list_runs(db, NioshRun)


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
def _build_response(run_id, chemical, run, papers_out, ok, fail, skip, no_url=0,
                    max_results: int | None = None):
    # Return papers in original search-result order (result_position ascending).
    # Failed/no_url papers are kept so the UI shows the source's exact ranking;
    # the frontend renders missing PDFs gracefully (title + View/DOI links).
    # Cap to max_results so the response matches what the user requested,
    # not the over-fetched candidate pool.
    ordered = sorted(papers_out, key=lambda p: p.get("result_position", 0))
    if max_results:
        ordered = ordered[:max_results]
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
        "papers": ordered,
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
