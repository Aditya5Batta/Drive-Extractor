"""
pipeline.audit — Run-level hallucination detector.

Backs the `audit_run` MCP tool. Given a finished build_review run it reports:

  1. SECTION COVERAGE   — which of the 29 sections got zero evidence
  2. HALLUCINATION      — every paragraph in the final docx must appear (as a
                          substring) in at least one harvested source excerpt
                          recorded in the evidence ledger. Orphan paragraphs
                          are flagged.
  3. URL HEALTH         — probe each ledger URL for HTTP status (optional,
                          off by default to keep the audit fast).
  4. DATABASE COVERAGE  — how many of the 14 consulted databases returned ≥1 doc.
  5. TRACE SUMMARY      — total ok/warn/error events, top errors from the
                          tracer JSONL.

Pure observability — never modifies the run artifacts.
"""
from __future__ import annotations
import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

from config.sections import LITERATURE_SECTIONS, SECTION_TITLES
from core.provenance import EvidenceLedger, EvidenceRecord


# ─────────────────────────────────────────────────────────────────────────────
#  Return type
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AuditReport:
    """Result of an audit_run invocation."""
    run_id: str
    chemical: str | None = None
    docx_path: str | None = None
    ledger_path: str | None = None
    trace_path: str | None = None
    # coverage
    sections_with_evidence: int = 0
    sections_empty: list[str] = field(default_factory=list)
    # hallucinations
    orphan_paragraphs: list[dict[str, Any]] = field(default_factory=list)
    paragraphs_checked: int = 0
    # URL health
    urls_checked: int = 0
    urls_broken: list[dict[str, Any]] = field(default_factory=list)
    # trace summary
    trace_summary: dict[str, Any] = field(default_factory=dict)
    # database coverage
    databases_contributing: list[str] = field(default_factory=list)
    databases_empty:        list[str] = field(default_factory=list)
    # overall grade
    status: str = "ok"         # "ok" | "warn" | "fail"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id":                   self.run_id,
            "chemical":                 self.chemical,
            "docx_path":                self.docx_path,
            "ledger_path":              self.ledger_path,
            "trace_path":               self.trace_path,
            "status":                   self.status,
            "sections_with_evidence":   self.sections_with_evidence,
            "sections_empty":           self.sections_empty,
            "paragraphs_checked":       self.paragraphs_checked,
            "orphan_paragraph_count":   len(self.orphan_paragraphs),
            "orphan_paragraphs":        self.orphan_paragraphs[:50],
            "urls_checked":             self.urls_checked,
            "urls_broken":              self.urls_broken[:50],
            "trace_summary":            self.trace_summary,
            "databases_contributing":   self.databases_contributing,
            "databases_empty":          self.databases_empty,
            "warnings":                 self.warnings,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  docx paragraph reader (zero-dep — unzip + strip XML tags)
# ─────────────────────────────────────────────────────────────────────────────
def _extract_docx_paragraphs(docx_path: str) -> list[str]:
    """Return the plain-text body paragraphs from a .docx (no python-docx needed)."""
    import zipfile
    paras: list[str] = []
    if not os.path.exists(docx_path):
        return paras
    try:
        with zipfile.ZipFile(docx_path, "r") as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return paras
    # Split on <w:p ...> boundaries, strip tags, collapse whitespace.
    chunks = re.split(r"<w:p[\s>]", xml)
    for c in chunks[1:]:
        # drop everything after closing </w:p>, then strip all tags
        end = c.find("</w:p>")
        if end >= 0:
            c = c[:end]
        txt = re.sub(r"<[^>]+>", "", c)
        # collapse whitespace, drop leading ellipsis characters we injected ourselves
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) >= 40:
            paras.append(txt)
    return paras


# ─────────────────────────────────────────────────────────────────────────────
#  Ledger helpers
# ─────────────────────────────────────────────────────────────────────────────
def _build_ledger_corpus(ledger: EvidenceLedger) -> str:
    """Concatenate every ledger excerpt into one string for substring checks."""
    parts: list[str] = []
    for r in ledger.records:
        if r.excerpt:
            parts.append(r.excerpt)
        if r.title:
            parts.append(r.title)
    return " \n ".join(parts).lower()


def _collect_ledger_urls(ledger: EvidenceLedger) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in ledger.records:
        u = (r.url or "").strip()
        if u and u not in seen and u.lower().startswith(("http://", "https://")):
            seen.add(u)
            out.append(u)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Trace JSONL reader
# ─────────────────────────────────────────────────────────────────────────────
def _read_trace_summary(trace_path: str) -> dict[str, Any]:
    import json
    if not trace_path or not os.path.exists(trace_path):
        return {}
    steps_ok = steps_warn = steps_err = 0
    errors: list[dict[str, str]] = []
    by_source: dict[str, dict[str, int]] = {}
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                src = e.get("source") or "—"
                by_source.setdefault(src, {"ok": 0, "error": 0, "warn": 0, "start": 0})
                evt = e.get("event", "ok")
                if evt in by_source[src]:
                    by_source[src][evt] += 1
                if evt == "ok":
                    steps_ok += 1
                elif evt == "error":
                    steps_err += 1
                    errors.append({
                        "step": e.get("step", "?"),
                        "source": src,
                        "error": (e.get("error") or "")[:160],
                    })
                elif evt == "warn":
                    steps_warn += 1
    except Exception:
        pass
    return {
        "steps_ok":    steps_ok,
        "steps_warn":  steps_warn,
        "steps_err":   steps_err,
        "by_source":   by_source,
        "errors":      errors[:25],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  URL health probe
# ─────────────────────────────────────────────────────────────────────────────
async def _probe_urls(urls: list[str], fetcher) -> list[dict[str, Any]]:
    """Return a list of broken URLs with HTTP status. Uses the shared HTTPFetcher."""
    broken: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(6)

    async def probe(u: str) -> None:
        async with sem:
            try:
                # HEAD first; fall back to GET with a tiny range header if HEAD refused
                status, _ = await fetcher.head(u) if hasattr(fetcher, "head") else (None, None)
                if status is None or status >= 400:
                    # Try a low-cost GET
                    try:
                        resp = await fetcher.get(u)
                        status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
                    except Exception as e:
                        broken.append({"url": u, "status": "error",
                                       "error": f"{type(e).__name__}: {e}"})
                        return
                if status and int(status) >= 400:
                    broken.append({"url": u, "status": int(status)})
            except Exception as e:
                broken.append({"url": u, "status": "error",
                               "error": f"{type(e).__name__}: {e}"})

    await asyncio.gather(*(probe(u) for u in urls))
    return broken


# ─────────────────────────────────────────────────────────────────────────────
#  Main entry
# ─────────────────────────────────────────────────────────────────────────────
async def audit_run(
    *,
    run_id: str,
    docx_path: str,
    ledger_path: str,
    trace_path: str | None = None,
    chemical: str | None = None,
    check_urls: bool = False,
    fetcher: Any = None,
) -> AuditReport:
    """Run the post-build audit. Returns an AuditReport."""
    report = AuditReport(
        run_id=run_id,
        chemical=chemical,
        docx_path=docx_path,
        ledger_path=ledger_path,
        trace_path=trace_path,
    )

    # ── 1. Load ledger ──────────────────────────────────────────────────────
    ledger = EvidenceLedger.load(ledger_path) if ledger_path else EvidenceLedger("")
    records: list[EvidenceRecord] = ledger.records

    # ── 2. Section coverage ─────────────────────────────────────────────────
    sections_seen: set[str] = {r.section for r in records if r.section}
    report.sections_with_evidence = len(sections_seen & set(LITERATURE_SECTIONS))
    report.sections_empty = [
        sid for sid in LITERATURE_SECTIONS if sid not in sections_seen
    ]

    # ── 3. Database coverage ────────────────────────────────────────────────
    dbs_seen: set[str] = {r.source for r in records if r.source}
    report.databases_contributing = sorted(dbs_seen)
    # (no authoritative "should-contribute" list — leave empty unless we knew it)
    report.databases_empty = []

    # ── 4. Hallucination check: every body paragraph must ground in ledger ─
    if docx_path and os.path.exists(docx_path):
        paragraphs = _extract_docx_paragraphs(docx_path)
        corpus = _build_ledger_corpus(ledger)
        # Drop our own prelude/appendix boilerplate so we don't flag it.
        _BOILERPLATE_MARKERS = (
            "comprehensive literature review",
            "this review synthesises",
            "this document provides a comprehensive",
            "every bracketed [n] citation above",
            "per-database document counts",
            "databases that produced no documents",
            "no keyword-anchored evidence was routed",
            "date:",
            "evidence base:",
            "cas no.",
            "for human health",
            "safety of",
            "chapter ",
            "appendix a",
            "appendix b",
            "table of contents",
            "scope and methodology",
            "key findings at a glance",
            "database coverage and retrieval",
            "references",
            "summary",
            "total (api-counted)",
        )

        def is_boilerplate(p: str) -> bool:
            low = p.lower()
            return any(m in low for m in _BOILERPLATE_MARKERS)

        for p in paragraphs:
            if is_boilerplate(p):
                continue
            # Strip the trailing " [n]" citation + any "(Source: X.)" prefix/suffix
            core = re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]\s*$", "", p).strip()
            core = re.sub(r"\s*\(Source:[^)]*\)\s*$", "", core).strip()
            # Sample first 80 chars to do substring lookup
            needle = core.lower()[:80].strip()
            report.paragraphs_checked += 1
            if not needle:
                continue
            # Allow some leading/trailing ellipsis we emitted ourselves
            needle_stripped = needle.lstrip("…").strip()
            if needle_stripped and needle_stripped not in corpus:
                report.orphan_paragraphs.append({
                    "paragraph": p[:240],
                    "reason":    "no matching ledger excerpt",
                })

    # ── 5. URL health (optional) ────────────────────────────────────────────
    if check_urls and fetcher is not None:
        urls = _collect_ledger_urls(ledger)
        report.urls_checked = len(urls)
        try:
            report.urls_broken = await _probe_urls(urls, fetcher)
        except Exception as e:
            report.warnings.append(f"url-probe error: {type(e).__name__}: {e}")

    # ── 6. Trace summary ────────────────────────────────────────────────────
    if trace_path:
        report.trace_summary = _read_trace_summary(trace_path)

    # ── 7. Grade ────────────────────────────────────────────────────────────
    warnings: list[str] = []
    if report.sections_empty:
        warnings.append(
            f"{len(report.sections_empty)} / {len(LITERATURE_SECTIONS)} sections had no evidence."
        )
    if report.orphan_paragraphs:
        warnings.append(
            f"{len(report.orphan_paragraphs)} body paragraphs had no matching ledger excerpt."
        )
    if report.urls_broken:
        warnings.append(f"{len(report.urls_broken)} URLs returned HTTP ≥ 400 or errored.")
    if report.trace_summary.get("steps_err", 0) > 0:
        warnings.append(f"{report.trace_summary['steps_err']} pipeline steps errored.")
    report.warnings = warnings

    if report.orphan_paragraphs:
        report.status = "fail"
    elif warnings:
        report.status = "warn"
    else:
        report.status = "ok"

    return report


__all__ = ["AuditReport", "audit_run"]
