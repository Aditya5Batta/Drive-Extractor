"""
sources.harvester — MultiDatabaseHarvester.

Instantiates every per-database source module and provides:

  harvest_database(name, chemical, cas, limit)
      Run the search → PDF → full text → figures pipeline for ONE named DB.

  harvest_all(chemical, cas, per_db_limit)
      Run every DB in parallel and return a flat list of HarvestedDocument.

The 14 DBs the user verified live are wired in here. Keeping the registry
in one module means the MCP tool layer doesn't need to know which Python
module corresponds to which DB name.
"""
from __future__ import annotations
import asyncio
import os
from typing import Any

from core.http import HTTPFetcher

from sources._base import BaseDatabaseSource, HarvestedDocument, PaperRef
from sources.semantic_scholar import SemanticScholarSource
from sources.ntp import NTPSource
from sources.who_inchem import WHOInchemSource
from sources.who_ipcs import WHOIpcsSource
from sources.echa import EChaSource
from sources.atsdr import ATSDRSource
from sources.calepa import CalEPASource
from sources.canada_dsl import CanadaDSLSource
from sources.concawe import ConcaweSource
from sources.niosh import NIOSHSource
from sources.osha import OSHASource
from sources.aicis import AICISSource
from sources.ilo import ILOSource
from sources.zenodo import ZenodoSource


# Order matters only for display — parallel harvest runs them all together
REGISTRY: list[type[BaseDatabaseSource]] = [
    SemanticScholarSource,
    NTPSource,
    WHOInchemSource,
    WHOIpcsSource,
    EChaSource,
    ATSDRSource,
    CalEPASource,
    CanadaDSLSource,
    ConcaweSource,
    NIOSHSource,
    OSHASource,
    AICISSource,
    ILOSource,
    ZenodoSource,
]


class MultiDatabaseHarvester:
    """Runs every registered per-DB source against a chemical and merges
    the HarvestedDocument outputs into one flat list."""

    def __init__(self, fetcher: HTTPFetcher, figures_dir: str | None = None) -> None:
        self.fetcher = fetcher
        # Portable default: tempdir/tox_scraper_figures (Windows / mac / Linux).
        if not figures_dir:
            import tempfile
            figures_dir = os.path.join(tempfile.gettempdir(), "tox_scraper_figures")
        self.figures_dir = figures_dir
        self.sources: dict[str, BaseDatabaseSource] = {
            cls.name: cls(fetcher=fetcher, figures_dir=figures_dir)
            for cls in REGISTRY
        }

    # ── single-database entry point ------------------------------------------

    def list_databases(self) -> list[str]:
        return list(self.sources.keys())

    async def harvest_database(
        self, name: str, chemical: str, cas: str | None = None,
        limit: int | None = None,
    ) -> list[HarvestedDocument]:
        if name not in self.sources:
            raise KeyError(
                f"unknown database '{name}'. Valid: {', '.join(self.sources.keys())}"
            )
        return await self.sources[name].harvest(chemical, cas=cas, limit=limit)

    # ── all-databases fan-out ------------------------------------------------

    async def harvest_all(
        self, chemical: str, cas: str | None = None,
        per_db_limit: int | None = None,
    ) -> list[HarvestedDocument]:
        # Keep (name → task) so we know which DB each result came from.
        # Previously we used as_completed which stripped the mapping, making
        # it impossible to report which DB failed vs succeeded.
        pairs: list[tuple[str, asyncio.Task[list[HarvestedDocument]]]] = [
            (src.name, asyncio.create_task(
                src.harvest(chemical, cas=cas, limit=per_db_limit),
                name=f"harvest:{src.name}",
            ))
            for src in self.sources.values()
        ]
        per_db: dict[str, list[HarvestedDocument]] = {}
        per_db_error: dict[str, str] = {}
        for name, task in pairs:
            try:
                per_db[name] = await task
            except Exception as e:  # noqa: BLE001
                per_db[name] = []
                per_db_error[name] = f"{type(e).__name__}: {e}"
                print(f"[harvester] {name}: {type(e).__name__}: {e}", flush=True)

        # Stash per-DB outcomes on the harvester so callers that want to
        # surface them in diagnostics can reach them via last_run_* attrs.
        self.last_run_per_db: dict[str, list[HarvestedDocument]] = per_db
        self.last_run_errors: dict[str, str] = per_db_error

        flat: list[HarvestedDocument] = []
        for docs in per_db.values():
            flat.extend(docs)
        return flat

    # ── search-only convenience (no PDF/text/figure extraction) --------------

    async def search_all(
        self, chemical: str, cas: str | None = None,
    ) -> dict[str, list[PaperRef]]:
        """Just run search() on each source — useful when the caller only
        wants a paper-list count without the heavy harvest pass."""
        tasks = [
            asyncio.create_task(src.search(chemical, cas=cas), name=f"search:{src.name}")
            for src in self.sources.values()
        ]
        out: dict[str, list[PaperRef]] = {}
        pairs = list(zip(self.sources.keys(), tasks))
        for name, task in pairs:
            try:
                out[name] = await task
            except Exception as e:  # noqa: BLE001
                out[name] = []
        return out
