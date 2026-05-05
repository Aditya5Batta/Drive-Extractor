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


# ── helpers ───────────────────────────────────────────────────────────────────
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
