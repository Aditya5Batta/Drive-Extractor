"""
core.pdf — PDF text extraction (pdfminer + pypdf fallback).

PDFExtractor.extract() returns (text, page_count, error). The is_pdf_content
helper is used by HTTPFetcher to auto-detect binary PDF responses served
with incorrect content-type headers.
"""
from __future__ import annotations
import io
import re
from typing import Any

from pdfminer.high_level import extract_text as pdfminer_extract
from pypdf import PdfReader


class PDFExtractor:
    """Extract full text from PDF bytes. Tries pdfminer first (better layout),
    falls back to pypdf (more resilient on weird PDFs)."""

    @staticmethod
    def extract(pdf_bytes: bytes) -> tuple[str, int, str | None]:
        """Returns (text, page_count, error_msg)."""
        if not pdf_bytes:
            return "", 0, "empty PDF"

        text = ""
        pages = 0
        err: str | None = None

        # Primary: pdfminer
        try:
            text = pdfminer_extract(io.BytesIO(pdf_bytes)) or ""
        except Exception as e:  # noqa: BLE001
            err = f"pdfminer: {e}"
            text = ""

        # Always try pypdf for page count, and as fallback if pdfminer empty
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages = len(reader.pages)
            if not text.strip():
                parts: list[str] = []
                for p in reader.pages:
                    try:
                        parts.append(p.extract_text() or "")
                    except Exception:
                        continue
                text = "\n\n".join(parts)
                if text.strip():
                    err = None  # fallback succeeded
        except Exception as e:  # noqa: BLE001
            if err:
                err = f"{err}; pypdf: {e}"
            else:
                err = f"pypdf: {e}"

        # Normalize
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if not text:
            return "", pages, err or "no extractable text (likely scanned — needs OCR)"
        return text, pages, None


# ════════════════════════════════════════════════════════════════════════════
#  CONTENT TYPE DETECTION
# ════════════════════════════════════════════════════════════════════════════

def is_pdf_content(content_type: str, data: bytes, url: str) -> bool:
    if "pdf" in content_type:
        return True
    if url.lower().endswith(".pdf"):
        return True
    # PDF magic number
    if data and data[:5] == b"%PDF-":
        return True
    return False


# ════════════════════════════════════════════════════════════════════════════
#  KEYWORD SEARCHER  — KWIC with word-boundary matching + scoring
# ════════════════════════════════════════════════════════════════════════════
