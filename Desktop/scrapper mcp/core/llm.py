"""
core.llm — Unified OpenAI / Anthropic completion client.

Deterministic text-in/text-out adapter. Used by all llm_* MCP tools.

Provider selection (first match wins):
  1. TOX_SCRAPER_LLM env var  ("openai" | "anthropic" | "none")
  2. OPENAI_API_KEY           → OpenAI backend
  3. ANTHROPIC_API_KEY        → Anthropic backend
  4. otherwise                → not configured (llm_* tools return clear error)
"""
from __future__ import annotations
import os
from typing import Any
import httpx


class LLMAdapter:
    """Unified OpenAI / Anthropic completion client.

    Deliberately minimal: no streaming, no function-calling — just text-in /
    text-out. All tools that need an LLM go through `complete()`; switching
    provider is a one-env-var change with no call-site edits.
    """

    def __init__(self, *, http: "HTTPFetcher | None" = None) -> None:
        override = (os.environ.get("TOX_SCRAPER_LLM") or "").lower().strip()
        openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

        if override == "none":
            self.provider: str | None = None
            self.api_key: str | None = None
        elif override == "openai" and openai_key:
            self.provider, self.api_key = "openai", openai_key
        elif override == "anthropic" and anthropic_key:
            self.provider, self.api_key = "anthropic", anthropic_key
        elif openai_key:
            self.provider, self.api_key = "openai", openai_key
        elif anthropic_key:
            self.provider, self.api_key = "anthropic", anthropic_key
        else:
            self.provider, self.api_key = None, None

        self.openai_model = (os.environ.get("OPENAI_MODEL")
                             or "gpt-4o-mini").strip()
        self.anthropic_model = (os.environ.get("ANTHROPIC_MODEL")
                                or "claude-sonnet-4-5").strip()
        self._http = http

    # ─────────────────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        return bool(self.provider and self.api_key)

    def describe(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "provider": self.provider,
            "model": (self.openai_model if self.provider == "openai"
                      else self.anthropic_model if self.provider == "anthropic"
                      else None),
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "override_env": os.environ.get("TOX_SCRAPER_LLM") or None,
            "note": ("LLM ready — narrative-synthesis tools enabled."
                     if self.ready else
                     "No LLM API key detected. Set OPENAI_API_KEY or "
                     "ANTHROPIC_API_KEY to enable synthesis tools. "
                     "Scraper tools (fetch_*, find_papers, generate_chemical_report) "
                     "work without an LLM."),
        }

    # ─────────────────────────────────────────────────────────────────
    async def complete(self, *, system: str, user: str,
                       max_tokens: int = 1500,
                       temperature: float = 0.2,
                       model: str | None = None) -> str:
        if not self.ready:
            raise RuntimeError(
                "LLM is not configured. Set OPENAI_API_KEY or "
                "ANTHROPIC_API_KEY in the environment.")
        if self.provider == "openai":
            return await self._openai(system, user, max_tokens,
                                      temperature, model)
        return await self._anthropic(system, user, max_tokens,
                                     temperature, model)

    async def _openai(self, system: str, user: str, max_tokens: int,
                      temperature: float, model: str | None) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": (model or self.openai_model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]

    async def _anthropic(self, system: str, user: str, max_tokens: int,
                         temperature: float, model: str | None) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": (model or self.anthropic_model),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data["content"][0]["text"]
