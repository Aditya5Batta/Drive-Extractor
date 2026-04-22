"""
Tox-Scraper MCP Server — thin entry point.

Heavy lifting lives in sub-packages:
  config/    — database registry, keyword bank, runtime settings
  core/      — http, html, pdf, keyword search, llm, formatter primitives
  sources/   — per-source integrations (PubChem, PubMed, OpenAlex, PMC,
               JATS, agencies, the 34-DB searcher)
  pipeline/  — linear chemical → report flow (urls, ledger, evidence,
               scraper, multimodal figures/tables, report builder,
               orchestrator)
  tools/     — MCP tool schemas + handler dispatch
  prompts    — MCP prompt templates

Run: register this file in Claude Desktop's claude_desktop_config.json.
"""
from __future__ import annotations
import asyncio
import sys

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from tools import TOOL_SCHEMAS, dispatch, build_context
from prompts import ALL_PROMPTS, get_prompt_messages

if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")


async def main() -> None:
    server = Server("tox-scraper")
    ctx = build_context()

    # ─── tools ────────────────────────────────────────────────────────────
    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        """Show Claude what tools are available."""
        return TOOL_SCHEMAS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        """Execute the named tool with its arguments."""
        return await dispatch(name, arguments, ctx)

    # ─── prompts ──────────────────────────────────────────────────────────
    @server.list_prompts()
    async def _list_prompts() -> list[types.Prompt]:
        """Show Claude what prompt templates are available."""
        return ALL_PROMPTS

    @server.get_prompt()
    async def _get_prompt(
        name: str, arguments: dict[str, str] | None = None,
    ) -> types.GetPromptResult:
        """Return the full prompt messages for the named template."""
        msgs = get_prompt_messages(name, arguments or {})
        return types.GetPromptResult(
            description=f"Prompt: {name}",
            messages=msgs,
        )

    # ─── stdio run ────────────────────────────────────────────────────────
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="tox-scraper",
                    server_version="3.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await ctx.fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
