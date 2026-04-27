"""
core.provenance — Evidence ledger: every fact in the final docx is traceable.

This is the anti-hallucination mechanism. Every paragraph the report generator
emits must be anchored to at least one `EvidenceRecord` — a row describing:

  * which database the source came from
  * the source URL / DOI / PMID / PMCID
  * the exact text span that supports the claim
  * which configured keywords matched the span (and with what counts)
  * a confidence score (0..1) derived from extraction quality + keyword density

If a section of the docx has no matching ledger row, the composer must either
skip the section or emit a clearly-flagged "Insufficient evidence — not
generated for this chemical" placeholder. No free-form prose is allowed.

The ledger is persisted as JSONL so developers can audit it independently of
the docx. A companion HTML audit view can be produced from the same file.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _stable_id(source: str, url: str, idx: int) -> str:
    h = hashlib.sha1(f"{source}|{url}|{idx}".encode("utf-8")).hexdigest()[:10]
    return f"ev_{source}_{h}"


@dataclass
class EvidenceRecord:
    """One row of the evidence ledger."""

    evidence_id: str              # stable, e.g. "ev_ntp_a3f9c2"
    source: str                   # "pubmed" | "pmc" | "atsdr" | ...
    database_label: str           # human label e.g. "ATSDR Toxicological Profile"
    chemical: str
    url: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    # The evidence excerpt itself (the snippet that supports downstream claims)
    excerpt: str = ""
    excerpt_page: int | None = None  # page number in PDF, if applicable
    # Keyword hits inside the excerpt
    matched_keywords: list[str] = field(default_factory=list)
    keyword_counts: dict[str, int] = field(default_factory=dict)
    # Which report section this excerpt supports, e.g. "carcinogenicity" | "exposure"
    section: str | None = None
    # Extraction metadata
    extraction_method: str = "text"  # "text" | "pdf" | "jats" | "html"
    char_count: int = 0
    confidence: float = 0.0          # 0..1
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EvidenceLedger:
    """Append-only ledger. Records can be queried by source, keyword, section."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[EvidenceRecord] = []

    # ── Writing ──────────────────────────────────────────────────────────────

    def add(self, rec: EvidenceRecord) -> None:
        self.records.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")

    def add_from_text(
        self,
        *,
        source: str,
        database_label: str,
        chemical: str,
        text: str,
        keywords: list[str],
        section: str | None = None,
        url: str | None = None,
        doi: str | None = None,
        pmid: str | None = None,
        pmcid: str | None = None,
        title: str | None = None,
        authors: str | None = None,
        year: str | None = None,
        extraction_method: str = "text",
        max_excerpts: int = 5,
        window: int = 280,
    ) -> list[EvidenceRecord]:
        """Scan `text` for keyword hits, slice ±window chars around each hit,
        build up to `max_excerpts` EvidenceRecords with per-keyword counts,
        and append them to the ledger."""
        if not text or not keywords:
            return []

        records: list[EvidenceRecord] = []
        # Compile keyword regexes once
        patterns: list[tuple[str, re.Pattern[str]]] = []
        for kw in keywords:
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            if " " in kw_clean:
                pat = re.compile(re.escape(kw_clean).replace(r"\ ", r"\s+"), re.I)
            else:
                pat = re.compile(rf"\b{re.escape(kw_clean)}\b", re.I)
            patterns.append((kw_clean.lower(), pat))

        # Sweep text, grab up to max_excerpts distinct windows containing hits
        used_spans: list[tuple[int, int]] = []
        idx = 0
        for kw_label, pat in patterns:
            for m in pat.finditer(text):
                start = max(0, m.start() - window)
                end = min(len(text), m.end() + window)
                # Skip if this window heavily overlaps an already-used one
                if any(not (end < s or start > e) for s, e in used_spans):
                    continue
                used_spans.append((start, end))
                excerpt = text[start:end].strip()

                # Tally ALL matched keywords inside this excerpt (not just the trigger)
                counts: dict[str, int] = {}
                for kl, kpat in patterns:
                    hits = len(list(kpat.finditer(excerpt)))
                    if hits:
                        counts[kl] = hits
                matched = list(counts.keys())

                # Confidence: based on keyword density and excerpt length
                kw_density = sum(counts.values()) / max(1, len(excerpt) // 100)
                conf = min(1.0, 0.3 + 0.15 * len(matched) + 0.05 * min(5, kw_density))

                eid = _stable_id(source, url or "", idx)
                rec = EvidenceRecord(
                    evidence_id=eid,
                    source=source,
                    database_label=database_label,
                    chemical=chemical,
                    url=url, doi=doi, pmid=pmid, pmcid=pmcid,
                    title=title, authors=authors, year=year,
                    excerpt=excerpt,
                    matched_keywords=matched,
                    keyword_counts=counts,
                    section=section,
                    extraction_method=extraction_method,
                    char_count=len(excerpt),
                    confidence=round(conf, 3),
                )
                self.add(rec)
                records.append(rec)
                idx += 1
                if len(records) >= max_excerpts:
                    break
            if len(records) >= max_excerpts:
                break
        return records

    # ── Querying ─────────────────────────────────────────────────────────────

    def by_source(self, source: str) -> list[EvidenceRecord]:
        return [r for r in self.records if r.source == source]

    def by_keyword(self, keyword: str) -> list[EvidenceRecord]:
        kl = keyword.lower()
        return [r for r in self.records if kl in r.matched_keywords]

    def by_section(self, section: str) -> list[EvidenceRecord]:
        return [r for r in self.records if r.section == section]

    def coverage_report(self) -> dict:
        """Produce per-section, per-source, per-keyword counts for display."""
        by_src: dict[str, int] = {}
        by_section: dict[str, int] = {}
        by_kw: dict[str, int] = {}
        for r in self.records:
            by_src[r.source] = by_src.get(r.source, 0) + 1
            if r.section:
                by_section[r.section] = by_section.get(r.section, 0) + 1
            for k in r.matched_keywords:
                by_kw[k] = by_kw.get(k, 0) + 1
        return {
            "total_records": len(self.records),
            "by_source": by_src,
            "by_section": by_section,
            "by_keyword": dict(sorted(by_kw.items(), key=lambda kv: -kv[1])[:50]),
            "mean_confidence": (
                round(sum(r.confidence for r in self.records) / len(self.records), 3)
                if self.records else 0.0
            ),
        }

    # ── Loading from disk (for audit tooling) ────────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceLedger":
        p = Path(path)
        led = cls(p)
        if not p.exists():
            return led
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    # EvidenceRecord tolerates missing optional fields
                    led.records.append(EvidenceRecord(**d))
                except Exception:
                    continue
        return led


__all__ = ["EvidenceRecord", "EvidenceLedger"]
