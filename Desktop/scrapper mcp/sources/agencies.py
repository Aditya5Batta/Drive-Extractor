"""
sources.agencies — Direct agency profile fetcher.

Covers NTP, ATSDR, EPA IRIS, OEHHA, and NIOSH.
"""
from __future__ import annotations
import asyncio
from typing import Any

from config.settings import CONFIG
from pipeline.scraper import Scraper


class AgencyProfileFetcher:
    """Direct-fetch authoritative agency chemical profiles.

    Each agency has a predictable URL pattern per chemical. This tool constructs
    all known URLs and fetches them in parallel. No keyword search needed — just
    give it the chemical name + CAS and it pulls the canonical regulatory docs.
    """

    def __init__(self, scraper: Scraper) -> None:
        self.scraper = scraper

    @staticmethod
    def build_urls(chemical: str, cas: str | None) -> list[dict[str, str]]:
        """Return list of {agency, url} to try. Includes both guess patterns
        and curated stable URLs for benzene-class common chemicals."""
        chem_slug = chemical.lower().replace(" ", "-")
        cas_clean = (cas or "").replace("-", "") if cas else ""
        urls: list[dict[str, str]] = []

        # ATSDR ToxFAQ — CAS-free slug URL
        urls.append({
            "agency": "ATSDR_ToxFAQs",
            "url": f"https://www.atsdr.cdc.gov/toxfaqs/tfacts{chem_slug}.pdf",
            "notes": "Brief toxicology fact sheet (PDF).",
        })
        # ATSDR landing (search portal)
        if cas:
            urls.append({
                "agency": "ATSDR_ToxProfile_Search",
                "url": f"https://wwwn.cdc.gov/TSP/substances/ToxSubstance.aspx?toxid={cas}",
                "notes": "ATSDR Tox Substance portal search.",
            })
        # EPA IRIS chemical landing — search-driven; we go via IRIS A-Z
        urls.append({
            "agency": "EPA_IRIS_Search",
            "url": f"https://iris.epa.gov/AtoZ/?list_type=alpha",
            "notes": "EPA IRIS A–Z index (manual lookup).",
        })
        # NTP Report on Carcinogens
        urls.append({
            "agency": "NTP_ROC",
            "url": f"https://ntp.niehs.nih.gov/go/roc15",
            "notes": "NTP Report on Carcinogens (15th ed) landing.",
        })
        # NIOSH Pocket Guide — numeric ID, unpredictable; fall back to search
        urls.append({
            "agency": "NIOSH_NPG_Search",
            "url": f"https://www.cdc.gov/niosh/npg/npgsyn-{chem_slug[0]}.html",
            "notes": "NIOSH Pocket Guide alphabetical index.",
        })
        # ICSC by CAS — stable URL pattern exists
        if cas:
            urls.append({
                "agency": "ILO_ICSC_Search",
                "url": f"https://www.ilo.org/dyn/icsc/showcard.listCards3?p_lang=en&p_search_text={cas}",
                "notes": "ILO ICSC search by CAS.",
            })
        # CalEPA OEHHA Prop 65 factsheets — search portal
        urls.append({
            "agency": "CalEPA_OEHHA_Prop65",
            "url": f"https://oehha.ca.gov/proposition-65/chemicals/{chem_slug}",
            "notes": "CalEPA OEHHA Prop 65 chemical factsheet.",
        })
        # OSHA chemical page
        urls.append({
            "agency": "OSHA_Chemical",
            "url": f"https://www.osha.gov/chemicaldata/{chem_slug}",
            "notes": "OSHA chemical data card (may 404 — OSHA IDs are numeric).",
        })
        # Haz-Map
        urls.append({
            "agency": "Haz_Map",
            "url": f"https://haz-map.com/Agents/{chem_slug}",
            "notes": "Haz-Map agent page.",
        })
        # Unpaywall — requires DOI, skip here (used via find_papers)
        # CalEPA OEHHA chemical search fallback
        urls.append({
            "agency": "OEHHA_Chemicals",
            "url": f"https://oehha.ca.gov/chemicals/{chem_slug}",
            "notes": "CalEPA OEHHA chemical portal.",
        })
        return urls

    async def fetch_all(self, chemical: str, cas: str | None) -> list[dict[str, Any]]:
        targets = self.build_urls(chemical, cas)
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(CONFIG.max_concurrent)

        async def one(t: dict[str, str]) -> dict[str, Any]:
            async with sem:
                page = await self.scraper.scrape(t["url"])
                return {
                    "agency": t["agency"],
                    "url": t["url"],
                    "notes": t["notes"],
                    "ok": page.ok,
                    "kind": page.kind,
                    "status": page.status,
                    "title": page.title,
                    "char_count": page.char_count,
                    "text_preview": page.text[:2000] if page.text else "",
                    "error": page.error,
                }

        results = await asyncio.gather(*(one(t) for t in targets))
        return results


# ══════════════════════════════════════════════════════════════════════════
#  EVIDENCE RECORD  — verbatim quotes + full source metadata
# ══════════════════════════════════════════════════════════════════════════
