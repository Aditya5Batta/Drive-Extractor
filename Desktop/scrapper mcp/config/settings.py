"""
config.settings — Runtime configuration for the scraper.

One ScraperConfig instance (CONFIG) is shared by every module. Change values
here to tune timeouts, content caps, KWIC window size, and the user agent.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ScraperConfig:
    # timeout: per-URL HTTP timeout. 8s is enough for any responsive
    # scientific API; slower sites will legitimately fail fast so the
    # 37-DB parallel sweep can return within the 60s MCP client timeout.
    timeout: float = 8.0
    max_concurrent: int = 8
    # max_retries=1 → one retry on transient errors, no exponential blowup.
    # With timeout=8s, worst-case per URL = 2 × 8s = 16s.
    max_retries: int = 1
    max_content_chars: int = 200_000         # per-page cap (text)
    max_pdf_bytes: int = 50 * 1024 * 1024    # 50 MB PDF cap
    context_chars: int = 300                 # KWIC window on each side
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


CONFIG = ScraperConfig()
