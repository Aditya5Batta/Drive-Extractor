"""
prompts — MCP prompt templates.

Exposed through @server.list_prompts() and @server.get_prompt() so Claude
Desktop users can pick a pre-wired prompt from the slash-command menu.

Each prompt is a short, deterministic instruction that steers Claude
through the correct sequence of tool calls to build a literature review.
"""
from __future__ import annotations
from typing import Any

import mcp.types as types


# ─────────────────────────────────────────────────────────────────────────────
# Prompt: full chemical literature review
# ─────────────────────────────────────────────────────────────────────────────
FULL_REVIEW_PROMPT = types.Prompt(
    name="chemical_tox_review",
    description=(
        "Produce a full toxicology literature review for a given chemical. "
        "Walks through every tool in order: resolve_chemical → find_papers → "
        "fetch_pmc_jats → extract_pdf_figures → count_papers_by_database → "
        "generate_chemical_report."
    ),
    arguments=[
        types.PromptArgument(
            name="chemical",
            description="Chemical name (e.g. 'benzene', 'L-cysteine', 'acrylamide').",
            required=True,
        ),
        types.PromptArgument(
            name="cas",
            description="CAS number if known (e.g. '71-43-2'). Optional — PubChem resolves by name.",
            required=False,
        ),
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt: quick DB check
# ─────────────────────────────────────────────────────────────────────────────
QUICK_DB_CHECK_PROMPT = types.Prompt(
    name="quick_db_check",
    description=(
        "Quick count of how many papers each of the 34 databases returns for a "
        "given chemical. No PDF download, no report. Useful as a first-pass "
        "feasibility check before running the full review."
    ),
    arguments=[
        types.PromptArgument(name="chemical", description="Chemical name or CAS.", required=True),
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt: PMC full-text deep dive
# ─────────────────────────────────────────────────────────────────────────────
PMC_DEEP_DIVE_PROMPT = types.Prompt(
    name="pmc_deep_dive",
    description=(
        "Harvest the top N PMC open-access papers for a chemical, pull the full "
        "JATS XML, extract every figure and table, and return verbatim evidence "
        "quotes that match the tox keyword bank."
    ),
    arguments=[
        types.PromptArgument(name="chemical", description="Chemical name or CAS.", required=True),
        types.PromptArgument(name="top", description="Top N papers (default 8).", required=False),
    ],
)


ALL_PROMPTS: list[types.Prompt] = [
    FULL_REVIEW_PROMPT,
    QUICK_DB_CHECK_PROMPT,
    PMC_DEEP_DIVE_PROMPT,
]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt body rendering — deterministic text returned to Claude.
# ─────────────────────────────────────────────────────────────────────────────
def render_full_review(chemical: str, cas: str | None = None) -> str:
    """Plan for Claude to execute a full literature review end to end."""
    cas_line = f"\nCAS: {cas}" if cas else ""
    return (
        f"Produce a zero-hallucination toxicology literature review for:\n"
        f"Chemical: {chemical}{cas_line}\n\n"
        "Run these MCP tools in order (pass outputs forward as inputs):\n"
        "  1. resolve_chemical(chemical)          → get CID, CAS, SMILES, MW\n"
        "  2. build_database_urls(chemical, cas)  → 34 search URLs\n"
        "  3. count_papers_by_database(...)        → per-DB paper counts\n"
        "  4. find_papers(chemical, top=8)         → ranked PMC candidates\n"
        "  5. fetch_pmc_jats(pmcid) for each       → full text + tables + figs\n"
        "  6. extract_pdf_figures(url) when JATS is missing figures\n"
        "  7. build_evidence_record(url, keywords) → verbatim quotes\n"
        "  8. fetch_agency_profile(chemical) for each of NTP/ATSDR/IRIS/OEHHA/NIOSH\n"
        "  9. generate_chemical_report(chemical, cas) → final .docx\n"
        "\nRules:\n"
        "  • Only cite papers that appear in the harvested pool.\n"
        "  • Quote verbatim when possible; paraphrase only with [N] citations.\n"
        "  • Every figure or table must come from a real harvested source.\n"
        "  • If a DB returns zero hits, report that honestly — do not invent.\n"
    )


def render_quick_check(chemical: str) -> str:
    return (
        f"For the chemical `{chemical}`, call count_papers_by_database and "
        f"return a one-table summary of counts per DB (PubMed, PMC, EuropePMC, "
        f"OpenAlex, ECHA, NTP, ATSDR, IRIS, OEHHA, NIOSH, ... all 34). Do not "
        f"fetch PDFs. Do not synthesise narrative. Just the counts."
    )


def render_pmc_deep_dive(chemical: str, top: str | int = 8) -> str:
    return (
        f"For `{chemical}`, call find_papers(top={top}) then for each PMCID "
        f"call fetch_pmc_jats. Extract every figure and table. Return a "
        f"structured list: paper title → PMCID → figure captions → table "
        f"headers → first 500 chars of each section."
    )


PROMPT_RENDERERS = {
    "chemical_tox_review": lambda args: render_full_review(
        args.get("chemical", ""), args.get("cas")),
    "quick_db_check": lambda args: render_quick_check(args.get("chemical", "")),
    "pmc_deep_dive": lambda args: render_pmc_deep_dive(
        args.get("chemical", ""), args.get("top", 8)),
}


def get_prompt_messages(name: str, arguments: dict[str, Any]) -> list[types.PromptMessage]:
    """Return the message list for a named prompt."""
    renderer = PROMPT_RENDERERS.get(name)
    if not renderer:
        body = f"Unknown prompt: {name}"
    else:
        body = renderer(arguments or {})
    return [types.PromptMessage(
        role="user",
        content=types.TextContent(type="text", text=body),
    )]
