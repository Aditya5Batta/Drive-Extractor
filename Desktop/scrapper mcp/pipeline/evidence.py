"""
pipeline.evidence — Verbatim evidence bundle builder.

Given a URL + list of keywords, fetches the page, scans for keyword hits,
and returns a structured EvidenceRecord with exact quoted passages.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class EvidenceRecord:
    """A single piece of extracted evidence, suitable for citation in a report.

    Every field is either verbatim from the source or a deterministic identifier.
    There is no LLM-summarized content here — this is designed so the report writer
    can quote directly and claim provenance without risk of hallucination.
    """
    evidence_id: str          # stable short ID: sha1(url + position)[:10]
    source_db: str            # e.g. "PubMed", "PMC", "ATSDR", "ECHA"
    source_url: str           # canonical URL of the document this came from
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str = ""
    journal: str | None = None
    year: str | None = None
    keyword: str = ""         # the query keyword that matched
    verbatim_quote: str = ""  # EXACT text from source (no paraphrase, no edits)
    char_offset: int = 0      # offset in extracted text where quote starts
    page_number: int | None = None  # PDF page if available
    paragraph_index: int | None = None  # paragraph number within page if known
    context_before: str = ""  # 200 chars of verbatim context preceding the quote
    context_after: str = ""   # 200 chars of verbatim context following the quote
    retrieved_at: str = ""    # ISO-8601 timestamp when fetched

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def citation(self) -> str:
        """Generate a human-readable inline citation string."""
        bits: list[str] = []
        if self.title:
            bits.append(self.title[:100])
        if self.journal:
            bits.append(self.journal)
        if self.year:
            bits.append(str(self.year))
        ids: list[str] = []
        if self.pmid: ids.append(f"PMID:{self.pmid}")
        if self.pmcid: ids.append(self.pmcid)
        if self.doi: ids.append(f"DOI:{self.doi}")
        if ids:
            bits.append(" ".join(ids))
        bits.append(f"[{self.evidence_id}]")
        return " · ".join(b for b in bits if b)


class EvidenceBuilder:
    """Build EvidenceRecord lists from fetched pages + keyword hits.

    Critical property: the `verbatim_quote` field is pulled character-for-character
    from the extracted text. No summarization, no paraphrase. The report-writer
    LLM can then quote these with confidence that the quote actually exists in
    the source.
    """

    def __init__(self, scraper: "Scraper") -> None:
        self.scraper = scraper

    @staticmethod
    def _evidence_id(url: str, position: int) -> str:
        h = hashlib.sha1(f"{url}|{position}".encode("utf-8")).hexdigest()
        return h[:10]

    @staticmethod
    def _now_iso() -> str:
        import datetime
        return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    async def build_from_url(
        self,
        url: str,
        keywords: list[str],
        source_db: str = "",
        doi: str | None = None,
        pmid: str | None = None,
        pmcid: str | None = None,
        max_per_keyword: int = 3,
        quote_chars: int = 400,
        context_chars: int = 200,
    ) -> list[EvidenceRecord]:
        """Fetch the URL, search for keywords, and return EvidenceRecords with
        verbatim quotes and full provenance.
        """
        page = await self.scraper.scrape(url)
        if not page.ok or not page.text:
            return []

        title = page.title or ""
        text = page.text
        records: list[EvidenceRecord] = []
        now = self._now_iso()

        for kw in keywords:
            if not kw.strip():
                continue
            pat = KeywordSearcher._compile(kw)
            matches = list(pat.finditer(text))
            for m in matches[:max_per_keyword]:
                # Quote = the sentence-ish window around the match, verbatim
                q_start = max(0, m.start() - quote_chars // 2)
                q_end = min(len(text), m.end() + quote_chars // 2)
                # Snap to whitespace boundaries to avoid mid-word cuts
                while q_start > 0 and not text[q_start].isspace():
                    q_start -= 1
                while q_end < len(text) and not text[q_end].isspace():
                    q_end += 1
                verbatim = text[q_start:q_end].strip()

                # Context before/after the quote
                c_before_start = max(0, q_start - context_chars)
                c_after_end = min(len(text), q_end + context_chars)
                ctx_before = text[c_before_start:q_start].strip()
                ctx_after = text[q_end:c_after_end].strip()

                eid = self._evidence_id(url, m.start())
                records.append(EvidenceRecord(
                    evidence_id=eid,
                    source_db=source_db,
                    source_url=url,
                    doi=doi,
                    pmid=pmid,
                    pmcid=pmcid,
                    title=title,
                    keyword=kw,
                    verbatim_quote=verbatim,
                    char_offset=m.start(),
                    page_number=None,   # caller can fill in for PDFs
                    paragraph_index=None,
                    context_before=ctx_before,
                    context_after=ctx_after,
                    retrieved_at=now,
                ))
        return records


# ══════════════════════════════════════════════════════════════════════════
#  PDF TABLE EXTRACTOR  — pdfplumber-based, structured rows with provenance
# ══════════════════════════════════════════════════════════════════════════
