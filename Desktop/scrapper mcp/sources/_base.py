"""
sources._base — Unified per-database harvester contract.

Every per-database source module (semantic_scholar.py, ntp.py, echa.py, …)
subclasses BaseDatabaseSource and implements the same four-step contract:

    search(chemical, cas)    → list[PaperRef]         # find candidate docs
    download_pdf(ref)        → bytes | None           # fetch the PDF
    extract_full_text(ref)   → (text, page_count)     # pdfminer/pypdf
    extract_figures(ref)     → list[ExtractedFigure]  # PyMuPDF

The harvest() convenience method runs all four in order for every paper
returned by search() and returns a HarvestedDocument per paper with
full_text, page_count, figures, tables, and any error strings.

Why a shared contract
---------------------
Before: DatabaseSearcher returned *counts only* for 14 DBs. The user
explicitly asked for per-DB source files that actually extract PDFs + full
text + multimodal figures ("I need all sources separately as a file where
you will extract pdf and full text data from them").

After: Every DB is its own file with the same shape, so the orchestrator
can walk them uniformly and the report generator can compose sections
without knowing which DB a given passage came from.
"""
from __future__ import annotations
import asyncio
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from core.http import HTTPFetcher
from core.pdf import PDFExtractor, is_pdf_content
from core.pdf_multimodal import (
    PDFFigureExtractor, PDFTableExtractor,
    ExtractedFigure, ExtractedTable,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PaperRef:
    """A lightweight reference to a document living on some database."""
    source: str                     # which DB — "Semantic Scholar", "NTP", …
    title: str = ""
    authors: str = ""               # free text; formatting varies by DB
    year: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    landing_url: str | None = None  # HTML page for the document
    pdf_url: str | None = None      # direct PDF URL if we know it
    abstract: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # DB-specific fields

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarvestedDocument:
    """Full harvest result for a single PaperRef — what the report writer gets."""
    source: str
    ref: PaperRef
    pdf_bytes_len: int = 0
    page_count: int = 0
    full_text: str = ""
    char_count: int = 0
    tables: list[ExtractedTable] = field(default_factory=list)
    figures: list[ExtractedFigure] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ref": self.ref.to_dict(),
            "pdf_bytes_len": self.pdf_bytes_len,
            "page_count": self.page_count,
            "full_text_chars": self.char_count,
            "full_text": self.full_text[:4000],      # truncate for MCP reply
            "full_text_truncated": len(self.full_text) > 4000,
            "tables": [t.to_dict() for t in self.tables],
            "figures": [f.to_dict() for f in self.figures],
            "errors": list(self.errors),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class BaseDatabaseSource:
    """Abstract contract for a database source.

    Concrete subclasses MUST override:
        name (class attr) — human-readable DB name
        search(chemical, cas) — async, returns list[PaperRef]

    They MAY override:
        download_pdf(ref) — default walks ref.pdf_url via HTTPFetcher
        extract_full_text(ref, pdf_bytes) — default uses PDFExtractor
        extract_figures(ref, pdf_bytes, figures_dir) — default uses PyMuPDF
        extract_tables(ref, pdf_bytes) — default uses pdfplumber

    search_limit limits how many papers harvest() will full-text; the rest
    come back as PaperRef only. Default 5 — enough to keep runs fast while
    still producing meaty report sections.
    """

    name: str = "BASE"
    search_limit: int = 5

    def __init__(self, fetcher: HTTPFetcher, figures_dir: str | None = None) -> None:
        self.fetcher = fetcher
        # Portable default: tempdir/tox_scraper_figures (Windows / mac / Linux).
        if not figures_dir:
            import os, tempfile
            figures_dir = os.path.join(tempfile.gettempdir(), "tox_scraper_figures")
        self.figures_dir = figures_dir

    # ── overridable -----------------------------------------------------------

    async def search(self, chemical: str, cas: str | None = None) -> list[PaperRef]:
        raise NotImplementedError(f"{self.name}.search not implemented")

    async def download_pdf(self, ref: PaperRef) -> tuple[bytes | None, str | None]:
        """Default: walk ref.pdf_url, auto-detect PDFs at landing_url as fallback."""
        url = ref.pdf_url or ref.landing_url
        if not url:
            return None, "no PDF url"
        data, ct, status, err = await self.fetcher.fetch(url)
        if data is None:
            return None, err or f"HTTP {status}"
        if not is_pdf_content(ct, data, url):
            return None, f"not a PDF (content-type={ct})"
        return data, None

    async def extract_full_text(
        self, ref: PaperRef, pdf_bytes: bytes,
    ) -> tuple[str, int, str | None]:
        """Default: pdfminer → pypdf fallback."""
        return PDFExtractor.extract(pdf_bytes)

    async def extract_figures(
        self, ref: PaperRef, pdf_bytes: bytes,
    ) -> tuple[list[ExtractedFigure], str | None]:
        """Default: PyMuPDF embedded-image extraction."""
        url = ref.pdf_url or ref.landing_url or ""
        return PDFFigureExtractor.extract(pdf_bytes, url, self.figures_dir)

    async def extract_tables(
        self, ref: PaperRef, pdf_bytes: bytes,
    ) -> tuple[list[ExtractedTable], str | None]:
        """Default: pdfplumber table extraction."""
        url = ref.pdf_url or ref.landing_url or ""
        return PDFTableExtractor.extract(pdf_bytes, url)

    # ── concrete harvest loop -------------------------------------------------

    async def harvest_one(self, ref: PaperRef) -> HarvestedDocument:
        """Run download → full text → tables → figures for one PaperRef."""
        doc = HarvestedDocument(source=self.name, ref=ref)
        pdf_bytes, err = await self.download_pdf(ref)
        if pdf_bytes is None or not pdf_bytes:
            if err:
                doc.errors.append(f"pdf: {err}")
            # Try the landing URL for HTML full text as a fallback so we still
            # get *something* when no PDF exists.
            if ref.landing_url:
                data, ct, status, ferr = await self.fetcher.fetch(ref.landing_url)
                if data and status < 400 and "text/html" in ct:
                    try:
                        from core.html import HTMLExtractor
                        html_text = HTMLExtractor.extract_text(data, ref.landing_url)
                        if html_text:
                            doc.full_text = html_text
                            doc.char_count = len(html_text)
                    except Exception as e:  # noqa: BLE001
                        doc.errors.append(f"html fallback: {e}")
            return doc

        doc.pdf_bytes_len = len(pdf_bytes)
        text, pages, terr = await self.extract_full_text(ref, pdf_bytes)
        if terr:
            doc.errors.append(f"fulltext: {terr}")
        doc.full_text = text
        doc.page_count = pages
        doc.char_count = len(text)

        tables, taberr = await self.extract_tables(ref, pdf_bytes)
        if taberr:
            doc.errors.append(f"tables: {taberr}")
        doc.tables = tables

        figs, ferr = await self.extract_figures(ref, pdf_bytes)
        if ferr:
            doc.errors.append(f"figures: {ferr}")
        doc.figures = figs

        return doc

    async def harvest(
        self, chemical: str, cas: str | None = None,
        limit: int | None = None,
    ) -> list[HarvestedDocument]:
        """End-to-end: search + harvest each hit up to `limit` papers."""
        refs = await self.search(chemical, cas=cas)
        n = limit if limit is not None else self.search_limit
        refs = refs[:n]
        if not refs:
            return []
        tasks = [asyncio.create_task(self.harvest_one(r)) for r in refs]
        docs: list[HarvestedDocument] = []
        for coro in asyncio.as_completed(tasks):
            try:
                docs.append(await coro)
            except Exception as e:  # noqa: BLE001
                docs.append(HarvestedDocument(
                    source=self.name, ref=PaperRef(source=self.name),
                    errors=[f"harvest_one: {type(e).__name__}: {e}"],
                ))
        return docs


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used across per-DB modules
# ─────────────────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Minimal tag strip for text mining when trafilatura over-strips."""
    return re.sub(r"<[^>]+>", " ", html)


def _collect_pdf_links(html: str, base: str) -> list[str]:
    """Pull every href that ends in .pdf (case-insensitive) from an HTML blob."""
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+?\.pdf[^"\']*)["\']', html, re.IGNORECASE):
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            # Reconstruct against base origin
            mm = re.match(r"(https?://[^/]+)", base)
            if mm:
                url = mm.group(1) + url
        elif not url.startswith("http"):
            url = base.rstrip("/") + "/" + url.lstrip("/")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
