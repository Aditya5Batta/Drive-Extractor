"""
core.pdf_multimodal — Figure + table extractors for PDFs.

PDFTableExtractor uses pdfplumber; PDFFigureExtractor uses PyMuPDF (fitz).
Both feature-gate cleanly — if the library is missing, the extractor
returns empty results instead of crashing.
"""
from __future__ import annotations
import hashlib
import io
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import pdfplumber  # type: ignore
    HAS_PDFPLUMBER = True
except Exception:
    HAS_PDFPLUMBER = False

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except Exception:
    HAS_PYMUPDF = False


@dataclass
class ExtractedTable:
    source_url: str
    page_number: int        # 1-indexed
    table_index: int        # 0-indexed within the page
    rows: list[list[str]]   # rows[i][j] — strings, verbatim from source
    caption: str = ""
    char_count: int = 0
    row_count: int = 0
    col_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        if not self.rows:
            return ""
        header = self.rows[0]
        body = self.rows[1:] if len(self.rows) > 1 else []
        lines: list[str] = []
        if self.caption:
            lines.append(f"**{self.caption}**")
        lines.append("| " + " | ".join((c or "").replace("\n", " ").strip() for c in header) + " |")
        lines.append("|" + "|".join(" --- " for _ in header) + "|")
        for row in body:
            # Pad/truncate row to header length
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            else:
                row = row[:len(header)]
            lines.append("| " + " | ".join((c or "").replace("\n", " ").strip() for c in row) + " |")
        return "\n".join(lines)


class PDFTableExtractor:
    """Extract tables from a PDF using pdfplumber. Returns structured rows
    with page-number provenance so the report writer can cite them precisely.
    """

    @staticmethod
    def extract(pdf_bytes: bytes, source_url: str) -> tuple[list[ExtractedTable], str | None]:
        if not HAS_PDFPLUMBER:
            return [], "pdfplumber not installed (pip install pdfplumber)"
        if not pdf_bytes:
            return [], "empty PDF"
        tables_out: list[ExtractedTable] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page_idx, page in enumerate(pdf.pages, start=1):
                    try:
                        page_tables = page.extract_tables() or []
                    except Exception:  # noqa: BLE001
                        page_tables = []
                    for t_idx, raw in enumerate(page_tables):
                        # raw: list[list[str|None]]
                        rows = [[("" if c is None else str(c)) for c in row] for row in raw if row]
                        if not rows:
                            continue
                        col_count = max(len(r) for r in rows)
                        char_count = sum(len(c) for r in rows for c in r)
                        # Skip trivially small "tables" (often layout artifacts)
                        if len(rows) < 2 or col_count < 2 or char_count < 20:
                            continue
                        tables_out.append(ExtractedTable(
                            source_url=source_url,
                            page_number=page_idx,
                            table_index=t_idx,
                            rows=rows,
                            caption="",  # pdfplumber doesn't give captions directly
                            char_count=char_count,
                            row_count=len(rows),
                            col_count=col_count,
                        ))
        except Exception as e:  # noqa: BLE001
            return tables_out, f"pdfplumber error: {type(e).__name__}: {e}"
        return tables_out, None


# ══════════════════════════════════════════════════════════════════════════
#  PDF FIGURE EXTRACTOR  — PyMuPDF, saves embedded images + captions
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ExtractedFigure:
    source_url: str
    page_number: int        # 1-indexed
    image_index: int        # 0-indexed within page
    image_path: str         # absolute local path to saved image file
    image_format: str       # "png", "jpg", etc.
    width: int = 0
    height: int = 0
    caption_guess: str = "" # best-effort text near the image on the page

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PDFFigureExtractor:
    """Extract embedded images from a PDF using PyMuPDF. Saves them to a
    per-session output dir and returns page-numbered references.
    """

    @staticmethod
    def extract(
        pdf_bytes: bytes,
        source_url: str,
        output_dir: str,
        min_bytes: int = 2_000,
    ) -> tuple[list[ExtractedFigure], str | None]:
        if not HAS_PYMUPDF:
            return [], "PyMuPDF not installed (pip install pymupdf)"
        if not pdf_bytes:
            return [], "empty PDF"
        os.makedirs(output_dir, exist_ok=True)
        # Hash the URL to create a stable prefix
        prefix = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:10]
        figs: list[ExtractedFigure] = []
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:  # noqa: BLE001
            return [], f"PyMuPDF open: {type(e).__name__}: {e}"

        try:
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                page_text = ""
                try:
                    page_text = page.get_text("text") or ""
                except Exception:
                    pass
                try:
                    images = page.get_images(full=True)
                except Exception:
                    images = []
                for img_idx, img in enumerate(images):
                    xref = img[0]
                    try:
                        pix = fitz.Pixmap(doc, xref)
                    except Exception:
                        continue
                    try:
                        # Flatten CMYK / alpha to RGB
                        if pix.n - pix.alpha > 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        data = pix.tobytes("png")
                    except Exception:
                        try:
                            data = pix.tobytes()
                        except Exception:
                            continue
                    if not data or len(data) < min_bytes:
                        continue
                    fname = f"{prefix}_p{page_idx+1:03d}_i{img_idx:02d}.png"
                    fpath = os.path.join(output_dir, fname)
                    try:
                        with open(fpath, "wb") as fh:
                            fh.write(data)
                    except Exception:
                        continue
                    # Best-effort caption: look for lines starting with "Figure N" or "Fig. N"
                    cap = ""
                    for line in page_text.splitlines():
                        s = line.strip()
                        if re.match(r"^(Figure|Fig\.?)\s*\d+", s, re.IGNORECASE):
                            cap = s[:300]
                            break
                    figs.append(ExtractedFigure(
                        source_url=source_url,
                        page_number=page_idx + 1,
                        image_index=img_idx,
                        image_path=fpath,
                        image_format="png",
                        width=pix.width,
                        height=pix.height,
                        caption_guess=cap,
                    ))
        finally:
            try:
                doc.close()
            except Exception:
                pass
        return figs, None


# ══════════════════════════════════════════════════════════════════════════
#  JATS XML HARVESTER  — PMC full text as structured XML (not scraped HTML)
# ══════════════════════════════════════════════════════════════════════════
