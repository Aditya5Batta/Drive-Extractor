"""
core.formatter — Markdown formatters for MCP tool responses.

Every MCP tool ultimately returns a string; Formatter.* methods render
structured data (pages, evidence, tables) into readable markdown.
"""
from __future__ import annotations
from typing import Any


class Formatter:
    @staticmethod
    def page_md(page: PageResult, preview_chars: int = 4000) -> str:
        lines = [f"# {page.title or page.url}", ""]
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| URL | {page.url} |")
        lines.append(f"| Kind | {page.kind} |")
        lines.append(f"| Status | {page.status} |")
        lines.append(f"| Content-Type | {page.content_type or '-'} |")
        lines.append(f"| Chars extracted | {page.char_count:,} |")
        if page.page_count:
            lines.append(f"| Pages | {page.page_count} |")
        if page.error:
            lines.append(f"| ⚠ Error | {page.error} |")
        lines.append("")

        if page.text:
            preview = page.text[:preview_chars]
            lines.append(f"## Extracted text ({len(page.text):,} chars total)")
            lines.append("```")
            lines.append(preview)
            if len(page.text) > preview_chars:
                lines.append(f"\n...[truncated — {len(page.text) - preview_chars:,} more chars]")
            lines.append("```")

        if page.links:
            lines.append(f"\n## Outgoing links ({len(page.links)})")
            for ln in page.links[:50]:
                marker = "📄" if ln["is_pdf"] else ("🏠" if ln["same_domain"] else "🔗")
                lines.append(f"- {marker} [{ln['text'][:80]}]({ln['url']})")
            if len(page.links) > 50:
                lines.append(f"\n...[{len(page.links) - 50} more links omitted]")

        return "\n".join(lines)

    @staticmethod
    def keyword_result_md(url: str, ksr: KeywordSearchResult) -> str:
        lines = [f"# Keyword hits for: {url}", ""]
        lines.append(f"- Total hits: **{ksr.total_hits}**")
        lines.append(f"- Relevance score: **{ksr.relevance_score:.2f}** "
                     f"(unique keywords matched / requested)")
        lines.append("")
        lines.append("## Per-keyword counts")
        lines.append("| Keyword | Hits |")
        lines.append("|---------|------|")
        for kw, n in sorted(ksr.per_keyword_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {kw} | {n} |")
        lines.append("")
        if ksr.hits:
            lines.append("## Snippets (keyword in context)")
            for h in ksr.hits:
                lines.append(f"\n**[{h.keyword}]** _(pos {h.position})_")
                lines.append(f"> {h.snippet}")
        return "\n".join(lines)

    @staticmethod
    def batch_md(result: dict[str, Any]) -> str:
        lines = ["# Batch scrape summary", ""]
        lines.append(f"- Requested URLs: **{result['requested']}**")
        lines.append(f"- Succeeded: **{result['succeeded']}**")
        lines.append(f"- Failed: **{result['failed']}**")
        lines.append(f"- Keywords searched: **{len(result['keywords'])}** "
                     f"— {', '.join(result['keywords'][:10])}"
                     + ("..." if len(result['keywords']) > 10 else ""))
        lines.append("")

        if result["ranked_results"]:
            lines.append("## Ranked results (by relevance × hit count)")
            lines.append("")
            lines.append("| Rank | URL | Kind | Relevance | Hits | Chars | Pages |")
            lines.append("|------|-----|------|-----------|------|-------|-------|")
            for i, r in enumerate(result["ranked_results"], 1):
                lines.append(
                    f"| {i} | {r['url'][:80]} | {r['kind']} | "
                    f"{r['relevance_score']:.2f} | {r['total_hits']} | "
                    f"{r.get('char_count', 0):,} | {r.get('page_count', 0) or '-'} |"
                )
            lines.append("")
            lines.append("## Top-5 detailed snippets")
            for i, r in enumerate(result["ranked_results"][:5], 1):
                lines.append(f"\n### {i}. {r.get('title') or r['url']}")
                lines.append(f"**URL:** {r['url']}  \n"
                             f"**Kind:** {r['kind']}  |  **Relevance:** {r['relevance_score']:.2f}  |  "
                             f"**Hits:** {r['total_hits']}")
                hits = r.get("hits", [])[:5]
                for h in hits:
                    lines.append(f"\n> **[{h['keyword']}]** {h['snippet']}")

        if result["failures"]:
            lines.append("\n## Failures")
            for f in result["failures"][:20]:
                lines.append(f"- `{f['url']}` — {f.get('error', 'unknown')} "
                             f"(status {f.get('status', 0)})")

        return "\n".join(lines)

    @staticmethod
    def databases_md() -> str:
        lines = ["# Curated toxicology databases (MVP1)", ""]
        lines.append("## ★★★ HIGH VALUE — include in pipeline")
        lines.append("| # | Database | URL | Notes |")
        lines.append("|---|----------|-----|-------|")
        for i, d in enumerate(HIGH_VALUE_DATABASES, 1):
            lines.append(f"| {i} | {d['name']} | {d['url']} | {d['notes']} |")
        lines.append("\n## ★★ MED VALUE")
        lines.append("| # | Database | URL | Notes |")
        lines.append("|---|----------|-----|-------|")
        for i, d in enumerate(MEDIUM_VALUE_DATABASES, 1):
            lines.append(f"| {i} | {d['name']} | {d['url']} | {d['notes']} |")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#  DATABASE SEARCHER  — deterministic per-DB search with paper counts
# ════════════════════════════════════════════════════════════════════════════
