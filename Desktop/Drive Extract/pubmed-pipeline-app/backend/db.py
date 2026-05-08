"""
PostgreSQL models — 3-table design for EuropePMC pipeline.

Tables:
  runs          — one row per pipeline execution (summary)
  papers        — ONLY successful PDF downloads (clean, enriched data)
  activity_logs — complete trace: search query, every paper scanned,
                  every URL attempted (success or failure)
"""
from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:12345@localhost:5432/europepmc_pipeline",
)

engine            = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── TABLE 1: runs ─────────────────────────────────────────────────────────────
class Run(Base):
    """
    One full pipeline execution.
    Summary counters for quick dashboard display.
    """
    __tablename__ = "runs"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    run_id             = Column(String(32),  unique=True, nullable=False, index=True)
    chemical           = Column(String(256), nullable=False)
    started_at         = Column(DateTime,    default=datetime.utcnow)
    finished_at        = Column(DateTime,    nullable=True)
    duration_s         = Column(Float,       nullable=True)

    # Search summary
    papers_found       = Column(Integer, default=0)   # total papers EuropePMC returned
    papers_with_pdf    = Column(Integer, default=0)   # papers that had at least one PDF URL

    # Download summary
    pdfs_success       = Column(Integer, default=0)   # successfully downloaded
    pdfs_failed        = Column(Integer, default=0)   # tried but all URLs failed
    pdfs_skipped       = Column(Integer, default=0)   # beyond max_pdfs limit

    # URL-level summary
    total_urls_tried   = Column(Integer, default=0)
    total_urls_success = Column(Integer, default=0)

    status             = Column(String(32), default="started")  # started/success/no_pdfs/no_results/failed
    error              = Column(Text, nullable=True)


# ── TABLE 2: papers ───────────────────────────────────────────────────────────
class Paper(Base):
    """
    ONLY successfully downloaded papers — clean reference table.
    Every row means: we have a PDF for this paper.
    """
    __tablename__ = "papers"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(32), ForeignKey("runs.run_id"), nullable=False, index=True)
    chemical        = Column(String(256), nullable=False)   # chemical searched

    # Paper identity
    pmcid           = Column(String(32),  nullable=True, index=True)
    pmid            = Column(String(32),  nullable=True, index=True)
    doi             = Column(String(128), nullable=True)
    title           = Column(Text,        nullable=True)
    journal         = Column(String(256), nullable=True)
    year            = Column(String(8),   nullable=True)
    epmc_url        = Column(Text,        nullable=True)    # link to EuropePMC page

    # Where in the search results this paper appeared (1 = first result)
    result_position = Column(Integer,     nullable=True)

    # PDF download details
    pdf_url         = Column(Text,        nullable=True)    # URL that worked
    pdf_path        = Column(Text,        nullable=True)    # local file path
    file_size_kb    = Column(Integer,     nullable=True)

    downloaded_at   = Column(DateTime,    default=datetime.utcnow)


# ── TABLE 3: activity_logs ────────────────────────────────────────────────────
class ActivityLog(Base):
    """
    Complete step-by-step trace of everything that happened in a run.
    log_type values:
      search        — the EuropePMC query that was fired
      paper_found   — each paper returned by search (has PDF URL or not)
      url_attempt   — each URL tried for each paper (success or failure)
    """
    __tablename__ = "activity_logs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(32), ForeignKey("runs.run_id"), nullable=False, index=True)
    chemical     = Column(String(256), nullable=True)
    log_type     = Column(String(32),  nullable=False, index=True)  # search / paper_found / url_attempt
    logged_at    = Column(DateTime,    default=datetime.utcnow)
    # sort_order encodes display sequence within a run:
    #   search        →  0
    #   paper_found N →  N * 1000          (e.g. pos 1 = 1000, pos 2 = 2000)
    #   url_attempt   →  N * 1000 + attempt_no  (e.g. paper 1, try 2 = 1002)
    sort_order   = Column(Integer,     nullable=True, index=True)

    # ── search fields (log_type = "search") ──────────────────────────────────
    query_sent       = Column(Text,    nullable=True)   # full query string sent
    api_url          = Column(Text,    nullable=True)   # full EuropePMC URL
    response_time_ms = Column(Integer, nullable=True)
    papers_returned  = Column(Integer, nullable=True)   # total in response
    papers_with_pmcid= Column(Integer, nullable=True)   # after PMCID filter

    # ── paper_found fields (log_type = "paper_found") ─────────────────────────
    pmcid           = Column(String(32), nullable=True, index=True)
    pmid            = Column(String(32), nullable=True)
    doi             = Column(String(128),nullable=True)
    title           = Column(Text,       nullable=True)
    abstract        = Column(Text,       nullable=True)
    authors         = Column(Text,       nullable=True)  # comma-separated full names
    journal         = Column(String(256),nullable=True)
    year            = Column(String(8),  nullable=True)
    epmc_url        = Column(Text,       nullable=True)  # link to EuropePMC article page
    result_position = Column(Integer,    nullable=True)  # rank in search results (1-based)
    has_pdf_urls    = Column(Boolean,    nullable=True)  # did paper have any PDF URLs?
    pdf_url_count   = Column(Integer,    nullable=True)  # how many PDF URLs found
    pdf_urls_list   = Column(Text,       nullable=True)  # all PDF URLs, newline-separated

    # ── url_attempt fields (log_type = "url_attempt") ─────────────────────────
    url          = Column(Text,        nullable=True)
    attempt_no   = Column(Integer,     nullable=True)   # 1st try, 2nd try, …
    http_status  = Column(Integer,     nullable=True)   # 200, 403, 404, …
    content_type = Column(String(128), nullable=True)
    is_pdf       = Column(Boolean,     nullable=True)
    success      = Column(Boolean,     nullable=True)   # True = PDF saved
    file_size_kb = Column(Integer,     nullable=True)
    error        = Column(Text,        nullable=True)   # exception if any


# ══════════════════════════════════════════════════════════════════════════════
# PMC (PubMed Central) tables — mirror of the EuropePMC 3-table design
# Completely separate: pmc_runs / pmc_papers / pmc_activity_logs
# ══════════════════════════════════════════════════════════════════════════════

class PmcRun(Base):
    """One full PMC pipeline execution — summary counters."""
    __tablename__ = "pmc_runs"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    run_id             = Column(String(32),  unique=True, nullable=False, index=True)
    chemical           = Column(String(256), nullable=False)
    started_at         = Column(DateTime,    default=datetime.utcnow)
    finished_at        = Column(DateTime,    nullable=True)
    duration_s         = Column(Float,       nullable=True)

    papers_found       = Column(Integer, default=0)
    papers_with_pdf    = Column(Integer, default=0)

    pdfs_success       = Column(Integer, default=0)
    pdfs_failed        = Column(Integer, default=0)
    pdfs_skipped       = Column(Integer, default=0)

    total_urls_tried   = Column(Integer, default=0)
    total_urls_success = Column(Integer, default=0)

    status             = Column(String(32), default="started")
    error              = Column(Text, nullable=True)


class PmcPaper(Base):
    """ONLY successfully downloaded PMC papers — clean reference table."""
    __tablename__ = "pmc_papers"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(32), ForeignKey("pmc_runs.run_id"), nullable=False, index=True)
    chemical        = Column(String(256), nullable=False)

    pmcid           = Column(String(32),  nullable=True, index=True)
    pmid            = Column(String(32),  nullable=True, index=True)
    doi             = Column(String(128), nullable=True)
    title           = Column(Text,        nullable=True)
    journal         = Column(String(256), nullable=True)
    year            = Column(String(8),   nullable=True)
    epmc_url        = Column(Text,        nullable=True)

    result_position = Column(Integer,     nullable=True)

    pdf_url         = Column(Text,        nullable=True)
    pdf_path        = Column(Text,        nullable=True)
    file_size_kb    = Column(Integer,     nullable=True)

    downloaded_at   = Column(DateTime,    default=datetime.utcnow)


class PmcActivityLog(Base):
    """
    Complete step-by-step trace for a PMC run.
    log_type: search / paper_found / url_attempt
    sort_order: search=0, paper N=N*1000, url attempt=N*1000+attempt_no
    """
    __tablename__ = "pmc_activity_logs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(32), ForeignKey("pmc_runs.run_id"), nullable=False, index=True)
    chemical     = Column(String(256), nullable=True)
    log_type     = Column(String(32),  nullable=False, index=True)
    logged_at    = Column(DateTime,    default=datetime.utcnow)
    sort_order   = Column(Integer,     nullable=True, index=True)

    # search fields
    query_sent        = Column(Text,    nullable=True)
    api_url           = Column(Text,    nullable=True)
    response_time_ms  = Column(Integer, nullable=True)
    papers_returned   = Column(Integer, nullable=True)
    papers_with_pmcid = Column(Integer, nullable=True)

    # paper_found fields
    pmcid           = Column(String(32),  nullable=True, index=True)
    pmid            = Column(String(32),  nullable=True)
    doi             = Column(String(128), nullable=True)
    title           = Column(Text,        nullable=True)
    abstract        = Column(Text,        nullable=True)
    authors         = Column(Text,        nullable=True)
    journal         = Column(String(256), nullable=True)
    year            = Column(String(8),   nullable=True)
    epmc_url        = Column(Text,        nullable=True)
    result_position = Column(Integer,     nullable=True)
    has_pdf_urls    = Column(Boolean,     nullable=True)
    pdf_url_count   = Column(Integer,     nullable=True)
    pdf_urls_list   = Column(Text,        nullable=True)

    # url_attempt fields
    url          = Column(Text,        nullable=True)
    attempt_no   = Column(Integer,     nullable=True)
    http_status  = Column(Integer,     nullable=True)
    content_type = Column(String(128), nullable=True)
    is_pdf       = Column(Boolean,     nullable=True)
    success      = Column(Boolean,     nullable=True)
    file_size_kb = Column(Integer,     nullable=True)
    error        = Column(Text,        nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Semantic Scholar tables — mirror of the 3-table design
# Completely separate: ss_runs / ss_papers / ss_activity_logs
# ══════════════════════════════════════════════════════════════════════════════

class SsRun(Base):
    """One full Semantic Scholar pipeline execution — summary counters."""
    __tablename__ = "ss_runs"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    run_id             = Column(String(32),  unique=True, nullable=False, index=True)
    chemical           = Column(String(256), nullable=False)
    started_at         = Column(DateTime,    default=datetime.utcnow)
    finished_at        = Column(DateTime,    nullable=True)
    duration_s         = Column(Float,       nullable=True)

    papers_found       = Column(Integer, default=0)
    papers_with_pdf    = Column(Integer, default=0)

    pdfs_success       = Column(Integer, default=0)
    pdfs_failed        = Column(Integer, default=0)
    pdfs_skipped       = Column(Integer, default=0)

    total_urls_tried   = Column(Integer, default=0)
    total_urls_success = Column(Integer, default=0)

    status             = Column(String(32), default="started")
    error              = Column(Text, nullable=True)


class SsPaper(Base):
    """ONLY successfully downloaded Semantic Scholar papers — clean reference table."""
    __tablename__ = "ss_papers"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(32), ForeignKey("ss_runs.run_id"), nullable=False, index=True)
    chemical        = Column(String(256), nullable=False)

    paper_id        = Column(String(64),  nullable=True, index=True)  # SS corpus ID
    pmcid           = Column(String(32),  nullable=True, index=True)
    pmid            = Column(String(32),  nullable=True, index=True)
    doi             = Column(String(128), nullable=True)
    arxiv           = Column(String(64),  nullable=True)
    title           = Column(Text,        nullable=True)
    journal         = Column(String(256), nullable=True)
    year            = Column(String(8),   nullable=True)
    ss_url          = Column(Text,        nullable=True)   # semanticscholar.org article page
    oa_status       = Column(String(32),  nullable=True)   # GREEN / BRONZE / HYBRID

    result_position = Column(Integer,     nullable=True)

    pdf_url         = Column(Text,        nullable=True)
    pdf_path        = Column(Text,        nullable=True)
    file_size_kb    = Column(Integer,     nullable=True)

    downloaded_at   = Column(DateTime,    default=datetime.utcnow)


class SsActivityLog(Base):
    """
    Complete step-by-step trace for a Semantic Scholar run.
    log_type: search / paper_found / url_attempt
    sort_order: search=0, paper N=N*1000, url attempt=N*1000+attempt_no
    """
    __tablename__ = "ss_activity_logs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(32), ForeignKey("ss_runs.run_id"), nullable=False, index=True)
    chemical     = Column(String(256), nullable=True)
    log_type     = Column(String(32),  nullable=False, index=True)
    logged_at    = Column(DateTime,    default=datetime.utcnow)
    sort_order   = Column(Integer,     nullable=True, index=True)

    # search fields
    query_sent        = Column(Text,    nullable=True)
    api_url           = Column(Text,    nullable=True)
    response_time_ms  = Column(Integer, nullable=True)
    papers_returned   = Column(Integer, nullable=True)
    papers_with_pmcid = Column(Integer, nullable=True)  # = papers with open PDF

    # paper_found fields
    paper_id        = Column(String(64),  nullable=True, index=True)
    pmcid           = Column(String(32),  nullable=True, index=True)
    pmid            = Column(String(32),  nullable=True)
    doi             = Column(String(128), nullable=True)
    arxiv           = Column(String(64),  nullable=True)
    title           = Column(Text,        nullable=True)
    abstract        = Column(Text,        nullable=True)
    authors         = Column(Text,        nullable=True)
    journal         = Column(String(256), nullable=True)
    year            = Column(String(8),   nullable=True)
    ss_url          = Column(Text,        nullable=True)
    oa_status       = Column(String(32),  nullable=True)
    result_position = Column(Integer,     nullable=True)
    has_pdf_urls    = Column(Boolean,     nullable=True)
    pdf_url_count   = Column(Integer,     nullable=True)
    pdf_urls_list   = Column(Text,        nullable=True)

    # url_attempt fields
    url          = Column(Text,        nullable=True)
    attempt_no   = Column(Integer,     nullable=True)
    http_status  = Column(Integer,     nullable=True)
    content_type = Column(String(128), nullable=True)
    is_pdf       = Column(Boolean,     nullable=True)
    success      = Column(Boolean,     nullable=True)
    file_size_kb = Column(Integer,     nullable=True)
    error        = Column(Text,        nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
# Document databases (ECHA / NTP / WHO IPCS)
# Same 3-table shape — pmcid/pmid/doi columns stay null because these sources
# index regulatory documents, not journal articles.  `epmc_url` holds the
# document's source URL; `journal` is reused as the doc-type label.
# ══════════════════════════════════════════════════════════════════════════════
def _make_run_class(name: str, table: str):
    return type(name, (Base,), {
        "__tablename__":      table,
        "id":                 Column(Integer, primary_key=True, autoincrement=True),
        "run_id":             Column(String(32),  unique=True, nullable=False, index=True),
        "chemical":           Column(String(256), nullable=False),
        "started_at":         Column(DateTime,    default=datetime.utcnow),
        "finished_at":        Column(DateTime,    nullable=True),
        "duration_s":         Column(Float,       nullable=True),
        "papers_found":       Column(Integer, default=0),
        "papers_with_pdf":    Column(Integer, default=0),
        "pdfs_success":       Column(Integer, default=0),
        "pdfs_failed":        Column(Integer, default=0),
        "pdfs_skipped":       Column(Integer, default=0),
        "total_urls_tried":   Column(Integer, default=0),
        "total_urls_success": Column(Integer, default=0),
        "status":             Column(String(32), default="started"),
        "error":              Column(Text, nullable=True),
    })

def _make_paper_class(name: str, table: str, runs_table: str):
    return type(name, (Base,), {
        "__tablename__":   table,
        "id":              Column(Integer, primary_key=True, autoincrement=True),
        "run_id":          Column(String(32), ForeignKey(f"{runs_table}.run_id"),
                                  nullable=False, index=True),
        "chemical":        Column(String(256), nullable=False),
        "pmcid":           Column(String(32),  nullable=True),
        "pmid":            Column(String(32),  nullable=True),
        "doi":             Column(String(128), nullable=True),
        "title":           Column(Text,        nullable=True),
        "journal":         Column(String(256), nullable=True),
        "year":            Column(String(8),   nullable=True),
        "epmc_url":        Column(Text,        nullable=True),
        "result_position": Column(Integer,     nullable=True),
        "pdf_url":         Column(Text,        nullable=True),
        "pdf_path":        Column(Text,        nullable=True),
        "file_size_kb":    Column(Integer,     nullable=True),
        "downloaded_at":   Column(DateTime,    default=datetime.utcnow),
    })

def _make_log_class(name: str, table: str, runs_table: str):
    return type(name, (Base,), {
        "__tablename__":   table,
        "id":              Column(Integer, primary_key=True, autoincrement=True),
        "run_id":          Column(String(32), ForeignKey(f"{runs_table}.run_id"),
                                  nullable=False, index=True),
        "chemical":        Column(String(256), nullable=True),
        "log_type":        Column(String(32), nullable=False, index=True),
        "logged_at":       Column(DateTime, default=datetime.utcnow),
        "sort_order":      Column(Integer, nullable=True, index=True),
        # search
        "query_sent":        Column(Text,    nullable=True),
        "api_url":           Column(Text,    nullable=True),
        "response_time_ms":  Column(Integer, nullable=True),
        "papers_returned":   Column(Integer, nullable=True),
        "papers_with_pmcid": Column(Integer, nullable=True),
        # paper_found
        "pmcid":           Column(String(32),  nullable=True),
        "pmid":            Column(String(32),  nullable=True),
        "doi":             Column(String(128), nullable=True),
        "title":           Column(Text,        nullable=True),
        "abstract":        Column(Text,        nullable=True),
        "authors":         Column(Text,        nullable=True),
        "journal":         Column(String(256), nullable=True),
        "year":            Column(String(8),   nullable=True),
        "epmc_url":        Column(Text,        nullable=True),
        "result_position": Column(Integer,     nullable=True),
        "has_pdf_urls":    Column(Boolean,     nullable=True),
        "pdf_url_count":   Column(Integer,     nullable=True),
        "pdf_urls_list":   Column(Text,        nullable=True),
        # url_attempt
        "url":          Column(Text,        nullable=True),
        "attempt_no":   Column(Integer,     nullable=True),
        "http_status":  Column(Integer,     nullable=True),
        "content_type": Column(String(128), nullable=True),
        "is_pdf":       Column(Boolean,     nullable=True),
        "success":      Column(Boolean,     nullable=True),
        "file_size_kb": Column(Integer,     nullable=True),
        "error":        Column(Text,        nullable=True),
    })


EchaRun         = _make_run_class  ("EchaRun",         "echa_runs")
EchaPaper       = _make_paper_class("EchaPaper",       "echa_papers",       "echa_runs")
EchaActivityLog = _make_log_class  ("EchaActivityLog", "echa_activity_logs", "echa_runs")

NtpRun          = _make_run_class  ("NtpRun",          "ntp_runs")
NtpPaper        = _make_paper_class("NtpPaper",        "ntp_papers",        "ntp_runs")
NtpActivityLog  = _make_log_class  ("NtpActivityLog",  "ntp_activity_logs", "ntp_runs")

WhoRun          = _make_run_class  ("WhoRun",          "who_runs")
WhoPaper        = _make_paper_class("WhoPaper",        "who_papers",        "who_runs")
WhoActivityLog  = _make_log_class  ("WhoActivityLog",  "who_activity_logs", "who_runs")

OehhaRun         = _make_run_class  ("OehhaRun",         "oehha_runs")
OehhaPaper       = _make_paper_class("OehhaPaper",       "oehha_papers",       "oehha_runs")
OehhaActivityLog = _make_log_class  ("OehhaActivityLog", "oehha_activity_logs", "oehha_runs")

AtsdrRun         = _make_run_class  ("AtsdrRun",         "atsdr_runs")
AtsdrPaper       = _make_paper_class("AtsdrPaper",       "atsdr_papers",       "atsdr_runs")
AtsdrActivityLog = _make_log_class  ("AtsdrActivityLog", "atsdr_activity_logs", "atsdr_runs")

ZenodoRun         = _make_run_class  ("ZenodoRun",         "zenodo_runs")
ZenodoPaper       = _make_paper_class("ZenodoPaper",       "zenodo_papers",       "zenodo_runs")
ZenodoActivityLog = _make_log_class  ("ZenodoActivityLog", "zenodo_activity_logs", "zenodo_runs")

CanadaRun         = _make_run_class  ("CanadaRun",         "canada_runs")
CanadaPaper       = _make_paper_class("CanadaPaper",       "canada_papers",       "canada_runs")
CanadaActivityLog = _make_log_class  ("CanadaActivityLog", "canada_activity_logs", "canada_runs")

ConcaweRun         = _make_run_class  ("ConcaweRun",         "concawe_runs")
ConcawePaper       = _make_paper_class("ConcawePaper",       "concawe_papers",       "concawe_runs")
ConcaweActivityLog = _make_log_class  ("ConcaweActivityLog", "concawe_activity_logs", "concawe_runs")

SafeWorkRun         = _make_run_class  ("SafeWorkRun",         "safework_runs")
SafeWorkPaper       = _make_paper_class("SafeWorkPaper",       "safework_papers",       "safework_runs")
SafeWorkActivityLog = _make_log_class  ("SafeWorkActivityLog", "safework_activity_logs", "safework_runs")

OpenAlexRun         = _make_run_class  ("OpenAlexRun",         "openalex_runs")
OpenAlexPaper       = _make_paper_class("OpenAlexPaper",       "openalex_papers",       "openalex_runs")
OpenAlexActivityLog = _make_log_class  ("OpenAlexActivityLog", "openalex_activity_logs", "openalex_runs")

EfsaRun         = _make_run_class  ("EfsaRun",         "efsa_runs")
EfsaPaper       = _make_paper_class("EfsaPaper",       "efsa_papers",       "efsa_runs")
EfsaActivityLog = _make_log_class  ("EfsaActivityLog", "efsa_activity_logs", "efsa_runs")

NiteRun         = _make_run_class  ("NiteRun",         "nite_runs")
NitePaper       = _make_paper_class("NitePaper",       "nite_papers",       "nite_runs")
NiteActivityLog = _make_log_class  ("NiteActivityLog", "nite_activity_logs", "nite_runs")

OecdRun         = _make_run_class  ("OecdRun",         "oecd_runs")
OecdPaper       = _make_paper_class("OecdPaper",       "oecd_papers",       "oecd_runs")
OecdActivityLog = _make_log_class  ("OecdActivityLog", "oecd_activity_logs", "oecd_runs")

PubMedRun         = _make_run_class  ("PubMedRun",         "pubmed_runs")
PubMedPaper       = _make_paper_class("PubMedPaper",       "pubmed_papers",       "pubmed_runs")
PubMedActivityLog = _make_log_class  ("PubMedActivityLog", "pubmed_activity_logs", "pubmed_runs")

NioshRun         = _make_run_class  ("NioshRun",         "niosh_runs")
NioshPaper       = _make_paper_class("NioshPaper",       "niosh_papers",       "niosh_runs")
NioshActivityLog = _make_log_class  ("NioshActivityLog", "niosh_activity_logs", "niosh_runs")

IloRun         = _make_run_class  ("IloRun",         "ilo_runs")
IloPaper       = _make_paper_class("IloPaper",       "ilo_papers",       "ilo_runs")
IloActivityLog = _make_log_class  ("IloActivityLog", "ilo_activity_logs", "ilo_runs")

IarcRun         = _make_run_class  ("IarcRun",         "iarc_runs")
IarcPaper       = _make_paper_class("IarcPaper",       "iarc_papers",       "iarc_runs")
IarcActivityLog = _make_log_class  ("IarcActivityLog", "iarc_activity_logs", "iarc_runs")

CpdbRun         = _make_run_class  ("CpdbRun",         "cpdb_runs")
CpdbPaper       = _make_paper_class("CpdbPaper",       "cpdb_papers",       "cpdb_runs")
CpdbActivityLog = _make_log_class  ("CpdbActivityLog", "cpdb_activity_logs", "cpdb_runs")


# ── helpers ───────────────────────────────────────────────────────────────────
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
