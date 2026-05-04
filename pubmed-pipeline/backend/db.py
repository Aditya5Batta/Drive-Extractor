from __future__ import annotations
from datetime import datetime
from pathlib import Path
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import os
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:password@localhost:5432/pubmed_pipeline")

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with Session() as s:
        yield s


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id:               Mapped[int]      = mapped_column(Integer, primary_key=True)
    run_id:           Mapped[str]      = mapped_column(String(64), unique=True, index=True)
    chemical_input:   Mapped[str]      = mapped_column(String(512))
    preferred_name:   Mapped[str|None] = mapped_column(String(512))
    casrn:            Mapped[str|None] = mapped_column(String(64))
    pubchem_cid:      Mapped[str|None] = mapped_column(String(32))
    synonyms:         Mapped[str|None] = mapped_column(Text)          # JSON array
    keywords:         Mapped[str|None] = mapped_column(Text)          # JSON array
    status:           Mapped[str]      = mapped_column(String(32), default="started")
    kw_searched:      Mapped[int]      = mapped_column(Integer, default=0)
    total_hits:       Mapped[int]      = mapped_column(Integer, default=0)
    papers_found:     Mapped[int]      = mapped_column(Integer, default=0)
    pdfs_ok:          Mapped[int]      = mapped_column(Integer, default=0)
    pdfs_fail:        Mapped[int]      = mapped_column(Integer, default=0)
    error:            Mapped[str|None] = mapped_column(Text)
    created_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    papers:           Mapped[list["Paper"]]   = relationship("Paper",   back_populates="run")
    calls:            Mapped[list["ApiCall"]] = relationship("ApiCall", back_populates="run")


class Paper(Base):
    __tablename__ = "papers"
    id:              Mapped[int]      = mapped_column(Integer, primary_key=True)
    run_id:          Mapped[str]      = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    pmid:            Mapped[str|None] = mapped_column(String(32), index=True)
    title:           Mapped[str|None] = mapped_column(Text)
    abstract:        Mapped[str|None] = mapped_column(Text)
    authors:         Mapped[str|None] = mapped_column(Text)           # JSON array
    journal:         Mapped[str|None] = mapped_column(String(512))
    year:            Mapped[str|None] = mapped_column(String(8))
    doi:             Mapped[str|None] = mapped_column(String(256))
    pubmed_url:      Mapped[str|None] = mapped_column(Text)
    pmc_id:          Mapped[str|None] = mapped_column(String(32))
    keyword:         Mapped[str|None] = mapped_column(String(512))
    search_query:    Mapped[str|None] = mapped_column(Text)
    rank:            Mapped[int|None] = mapped_column(Integer)
    pdf_url:         Mapped[str|None] = mapped_column(Text)
    pdf_status:      Mapped[str]      = mapped_column(String(32), default="pending")
    pdf_path:        Mapped[str|None] = mapped_column(Text)
    pdf_source:      Mapped[str|None] = mapped_column(String(64))
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run:             Mapped["Run"]    = relationship("Run", back_populates="papers")
    downloads:       Mapped[list["Download"]] = relationship("Download", back_populates="paper")


class ApiCall(Base):
    __tablename__ = "api_calls"
    id:           Mapped[int]      = mapped_column(Integer, primary_key=True)
    run_id:       Mapped[str]      = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    call_type:    Mapped[str]      = mapped_column(String(64))   # pubchem | pubmed_search | pdf_download
    keyword:      Mapped[str|None] = mapped_column(String(512))
    query:        Mapped[str|None] = mapped_column(Text)
    hit_count:    Mapped[int|None] = mapped_column(Integer)
    status:       Mapped[str]      = mapped_column(String(32))
    duration_ms:  Mapped[float|None] = mapped_column(Float)
    message:      Mapped[str|None] = mapped_column(Text)
    error:        Mapped[str|None] = mapped_column(Text)
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run:          Mapped["Run"]    = relationship("Run", back_populates="calls")


class Download(Base):
    __tablename__ = "downloads"
    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    paper_id:    Mapped[int]      = mapped_column(Integer, ForeignKey("papers.id"), index=True)
    pmid:        Mapped[str|None] = mapped_column(String(32))
    source:      Mapped[str|None] = mapped_column(String(64))
    pdf_url:     Mapped[str|None] = mapped_column(Text)
    status:      Mapped[str]      = mapped_column(String(32))
    file_path:   Mapped[str|None] = mapped_column(Text)
    file_size:   Mapped[int|None] = mapped_column(Integer)
    duration_ms: Mapped[float|None] = mapped_column(Float)
    error:       Mapped[str|None] = mapped_column(Text)
    created_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paper:       Mapped["Paper"]  = relationship("Paper", back_populates="downloads")
