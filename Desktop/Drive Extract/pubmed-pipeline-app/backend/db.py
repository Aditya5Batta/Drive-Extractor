"""
PostgreSQL models — full traceability for EuropePMC pipeline runs.
Tables: runs, papers
"""
from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:12345@localhost:5432/europepmc_pipeline",
)

engine  = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── runs ──────────────────────────────────────────────────────────────────────
class Run(Base):
    """One pipeline execution triggered by the user."""
    __tablename__ = "runs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(32),  unique=True, nullable=False, index=True)
    chemical     = Column(String(256), nullable=False)   # user input
    started_at   = Column(DateTime,   default=func.now())
    finished_at  = Column(DateTime,   nullable=True)
    duration_s   = Column(Float,      nullable=True)     # seconds
    papers_found = Column(Integer,    default=0)
    pdfs_success = Column(Integer,    default=0)
    pdfs_failed  = Column(Integer,    default=0)
    pdfs_skipped = Column(Integer,    default=0)
    status       = Column(String(32), default="started") # started/success/partial/failed
    error        = Column(Text,       nullable=True)


# ── papers ────────────────────────────────────────────────────────────────────
class Paper(Base):
    """One paper found and processed during a run."""
    __tablename__ = "papers"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    run_id      = Column(String(32), ForeignKey("runs.run_id"), nullable=False, index=True)
    pmcid       = Column(String(32),  nullable=True)
    pmid        = Column(String(32),  nullable=True)
    title       = Column(Text,        nullable=True)
    journal     = Column(String(256), nullable=True)
    year        = Column(String(8),   nullable=True)
    doi         = Column(String(128), nullable=True)
    epmc_url    = Column(Text,        nullable=True)
    pdf_status  = Column(String(32),  default="pending")  # success/failed/no_url
    pdf_path    = Column(Text,        nullable=True)       # local file path
    pdf_source  = Column(Text,        nullable=True)       # URL that worked
    error_log   = Column(Text,        nullable=True)       # failure reasons
    fetched_at  = Column(DateTime,    default=func.now())


# ── helpers ───────────────────────────────────────────────────────────────────
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
