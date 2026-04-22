"""
core.keywords — KWIC (keyword-in-context) matcher.

Given a block of text and a list of keywords, returns every match with
surrounding context. Used by search_in_content, batch_scrape, and every
evidence-building path in the pipeline.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from config.settings import CONFIG


@dataclass
class KeywordHit:
    keyword: str
    snippet: str
    position: int

@dataclass
class KeywordSearchResult:
    total_hits: int
    per_keyword_counts: dict[str, int]
    hits: list[KeywordHit]
    relevance_score: float  # 0..1 — unique keywords hit / total keywords requested

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_hits": self.total_hits,
            "per_keyword_counts": self.per_keyword_counts,
            "hits": [asdict(h) for h in self.hits],
            "relevance_score": round(self.relevance_score, 3),
        }


class KeywordSearcher:
    """Keyword-in-context search. Word-boundary-aware, case-insensitive."""

    @staticmethod
    def _compile(keyword: str) -> re.Pattern[str]:
        # Multi-word keywords → allow flexible whitespace; single-word → word boundary
        parts = keyword.strip().split()
        if len(parts) == 1:
            pat = r"\b" + re.escape(parts[0]) + r"\b"
        else:
            pat = r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"
        return re.compile(pat, re.IGNORECASE)

    @classmethod
    def search(
        cls,
        text: str,
        keywords: list[str],
        context_chars: int = CONFIG.context_chars,
        max_hits_per_keyword: int = 5,
    ) -> KeywordSearchResult:
        hits: list[KeywordHit] = []
        per_keyword_counts: dict[str, int] = {}

        for kw in keywords:
            if not kw.strip():
                continue
            pat = cls._compile(kw)
            matches = list(pat.finditer(text))
            per_keyword_counts[kw] = len(matches)
            for m in matches[:max_hits_per_keyword]:
                start = max(0, m.start() - context_chars)
                end = min(len(text), m.end() + context_chars)
                snippet = text[start:end]
                # Highlight the match
                rel_start = m.start() - start
                rel_end = m.end() - start
                snippet = (
                    snippet[:rel_start]
                    + "**" + snippet[rel_start:rel_end] + "**"
                    + snippet[rel_end:]
                )
                # Clean whitespace
                snippet = re.sub(r"\s+", " ", snippet).strip()
                hits.append(KeywordHit(keyword=kw, snippet=snippet, position=m.start()))

        unique_hit_kws = sum(1 for v in per_keyword_counts.values() if v > 0)
        total_kws = max(1, len([k for k in keywords if k.strip()]))
        score = unique_hit_kws / total_kws

        return KeywordSearchResult(
            total_hits=sum(per_keyword_counts.values()),
            per_keyword_counts=per_keyword_counts,
            hits=hits,
            relevance_score=score,
        )


# ════════════════════════════════════════════════════════════════════════════
#  UNIFIED FETCH+EXTRACT  — the workhorse
# ════════════════════════════════════════════════════════════════════════════
