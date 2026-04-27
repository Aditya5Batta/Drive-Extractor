"""
core.tracing — Structured observability for the toxicology pipeline.

Every pipeline step emits a typed event to a JSONL trace file, so developers can
replay exactly what happened during a run: which database was queried, which
papers came back, which keywords hit, which PDFs downloaded, how long each
step took, and which errors were swallowed where.

Design goals
------------
* Zero-dependency (stdlib json + time + pathlib).
* Safe to call from async code (non-blocking file append, synchronous is fine
  since events are small and infrequent).
* Each event is a single JSON object on one line → grep-able, jq-able.
* Run-scoped: a single `Tracer` instance holds run_id + output path; passed
  through ToolContext so every adapter can call `ctx.tracer.event(...)`.
* Fail-safe: tracing errors are never raised to the caller.

Event schema (all fields optional except ts, run_id, step, event)
-----------------------------------------------------------------
  {
    "ts": "2026-04-22T12:48:03.214Z",
    "run_id": "run_formaldehyde_20260422T124803",
    "step": "harvest.ntp.search" | "harvest.ntp.pdf" | "compose.section.risk" | ...,
    "event": "start" | "ok" | "warn" | "error" | "metric",
    "chemical": "formaldehyde",
    "source": "ntp" | "atsdr" | "pubmed" | ...,
    "url": "https://...",
    "duration_ms": 1320,
    "bytes": 408123,
    "papers_found": 14,
    "papers_kept": 5,
    "keyword_hits": {"carcinogen": 17, "leukemia": 4, ...},
    "error": "timeout after 30s",
    "extra": {...}
  }

Summary report
--------------
Tracer.summary() reads its own JSONL back and produces a human-readable
summary: DB x status matrix, total bytes, total papers, keyword histogram,
top errors.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


@dataclass
class Tracer:
    """Append-only JSONL event log for a single pipeline run."""

    run_id: str
    output_dir: Path
    chemical: str | None = None
    # In-memory cache so summary() doesn't need a disk re-read if we prefer
    _events_cache: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, chemical: str, base_dir: str | Path = "/tmp/tox_traces") -> "Tracer":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        slug = (chemical or "unknown").replace(" ", "_").replace("/", "_")
        run_id = f"run_{slug}_{stamp}"
        out = Path(base_dir)
        out.mkdir(parents=True, exist_ok=True)
        tracer = cls(run_id=run_id, output_dir=out, chemical=chemical)
        tracer.event("pipeline", "start", chemical=chemical)
        return tracer

    @property
    def trace_path(self) -> Path:
        return self.output_dir / f"{self.run_id}.jsonl"

    def event(
        self,
        step: str,
        event: str = "ok",
        *,
        source: str | None = None,
        url: str | None = None,
        duration_ms: float | None = None,
        bytes: int | None = None,
        papers_found: int | None = None,
        papers_kept: int | None = None,
        keyword_hits: dict[str, int] | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> None:
        """Append one trace event. Never raises."""
        try:
            record: dict[str, Any] = {
                "ts": _utc_now_iso(),
                "run_id": self.run_id,
                "step": step,
                "event": event,
            }
            if self.chemical:
                record["chemical"] = self.chemical
            for k, v in (
                ("source", source), ("url", url), ("duration_ms", duration_ms),
                ("bytes", bytes), ("papers_found", papers_found),
                ("papers_kept", papers_kept), ("keyword_hits", keyword_hits),
                ("error", error),
            ):
                if v is not None:
                    record[k] = v
            if extra:
                record["extra"] = extra

            self._events_cache.append(record)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Tracing must never break the pipeline.
            pass

    @contextmanager
    def step(self, name: str, **tags: Any) -> Iterator[dict[str, Any]]:
        """Context manager: auto-time a step, emit start/ok/error events.

        Usage:
            with tracer.step("harvest.ntp.search", source="ntp") as s:
                papers = await ntp.search(...)
                s["papers_found"] = len(papers)
        """
        start = time.perf_counter()
        bag: dict[str, Any] = {}
        self.event(name, "start", **tags)
        try:
            yield bag
        except Exception as e:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self.event(name, "error", duration_ms=duration_ms,
                       error=f"{type(e).__name__}: {e}", **tags, **bag)
            raise
        else:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self.event(name, "ok", duration_ms=duration_ms, **tags, **bag)

    def summary(self) -> dict[str, Any]:
        """Produce a run summary from the event log for display to developers."""
        events = list(self._events_cache)
        # If the cache is empty but the file exists, re-read.
        if not events and self.trace_path.exists():
            try:
                with self.trace_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass

        by_source: dict[str, dict[str, int]] = {}
        keyword_totals: dict[str, int] = {}
        total_bytes = 0
        total_papers_found = 0
        total_papers_kept = 0
        errors: list[dict[str, str]] = []
        steps_ok = steps_err = steps_warn = 0

        for e in events:
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
                    "error": e.get("error", "?")[:160],
                })
            elif evt == "warn":
                steps_warn += 1

            for k, v in (e.get("keyword_hits") or {}).items():
                keyword_totals[k] = keyword_totals.get(k, 0) + int(v or 0)
            if e.get("bytes"):
                total_bytes += int(e["bytes"])
            if e.get("papers_found"):
                total_papers_found += int(e["papers_found"])
            if e.get("papers_kept"):
                total_papers_kept += int(e["papers_kept"])

        top_keywords = sorted(keyword_totals.items(), key=lambda kv: -kv[1])[:25]

        return {
            "run_id": self.run_id,
            "chemical": self.chemical,
            "trace_path": str(self.trace_path),
            "event_count": len(events),
            "steps_ok": steps_ok,
            "steps_warn": steps_warn,
            "steps_err": steps_err,
            "total_bytes_downloaded": total_bytes,
            "total_papers_found": total_papers_found,
            "total_papers_kept": total_papers_kept,
            "by_source": by_source,
            "top_keywords": top_keywords,
            "errors": errors[:25],
        }

    def close(self) -> dict[str, Any]:
        self.event("pipeline", "ok")
        return self.summary()


__all__ = ["Tracer"]
