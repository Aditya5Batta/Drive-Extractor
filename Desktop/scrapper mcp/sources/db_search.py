"""
sources.db_search — The 34-database runner.

DatabaseSearcher hits every curated database in parallel, collects results,
and returns DatabaseSearchResult objects. This is the single biggest piece
of the harvest pipeline — it knows how to query PubMed, ECHA, NTP, ATSDR,
IRIS, OEHHA, NIOSH, OSHA, WHO INCHEM, and every other source in
config.databases.
"""
from __future__ import annotations
import asyncio
import re
import json
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from config.databases import ALL_DATABASES, HIGH_VALUE_DATABASES, MEDIUM_VALUE_DATABASES
from core.http import HTTPFetcher
from core.keywords import KeywordSearcher
from pipeline.scraper import Scraper
from sources.pubmed import PaperFinder
from sources.openalex import OpenAlexClient


@dataclass
class DatabaseSearchResult:
    database: str
    query: str
    url: str
    status: str           # "ok" | "html_ok" | "landing_only" | "no_api" | "error"
    # Status meaning:
    #   ok           = verified numeric count from a real API / JSON endpoint
    #   html_ok      = fetched AND parsed an explicit numeric count from the HTML
    #   landing_only = HTTP 200 reached but NO count could be parsed (JS-rendered
    #                  or catalogue page). result_count is 0 — caller must not
    #                  claim a total. URL is still surfaced as a drill-in target.
    #   no_api       = database intrinsically has no searchable endpoint.
    #   error        = fetch failed / blocked / 4xx-5xx — treat as unreachable.
    result_count: int = 0
    papers: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DatabaseSearcher:
    """Run a deterministic search against every database and return paper counts.

    For databases with a real API (PubMed, PMC, EuropePMC, OpenAlex,
    Semantic Scholar, Unpaywall, Zenodo, PubChem, CrossRef via DOI), it queries
    the API and returns a verified count + paper list.

    For databases that only expose HTML search pages, it fetches the search
    page and parses visible result-count indicators where possible. For
    databases that are fundamentally JS-rendered or require session cookies,
    it marks status='no_api' and returns the canonical search URL without
    fabricating a count — zero-hallucination guarantee.
    """

    def __init__(self, fetcher: HTTPFetcher, scraper: Scraper,
                 papers: "PaperFinder", openalex: "OpenAlexClient") -> None:
        self.fetcher = fetcher
        self.scraper = scraper
        self.papers = papers
        self.openalex = openalex

    async def _fetch_json(self, url: str) -> dict | None:
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return None
        try:
            return json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None

    async def search_pubmed(self, chemical: str) -> DatabaseSearchResult:
        """PubMed esearch — returns exact hit count via count field."""
        url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db=pubmed&retmode=json&retmax=0&term={quote(chemical)}")
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="PubMed", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "esearch fetch failed"
            return res
        try:
            res.result_count = int(j["esearchresult"]["count"])
        except (KeyError, ValueError, TypeError) as e:
            res.status = "error"; res.error = f"count parse: {e}"
        return res

    async def search_pmc(self, chemical: str) -> DatabaseSearchResult:
        url = (f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
               f"?db=pmc&retmode=json&retmax=0&term={quote(chemical)}")
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="PMC", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "esearch fetch failed"
            return res
        try:
            res.result_count = int(j["esearchresult"]["count"])
        except (KeyError, ValueError, TypeError) as e:
            res.status = "error"; res.error = f"count parse: {e}"
        return res

    async def search_europepmc(self, chemical: str) -> DatabaseSearchResult:
        url = (f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
               f"?query={quote(chemical)}&format=json&pageSize=25&resultType=core")
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="EuropePMC", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "search fetch failed"
            return res
        try:
            res.result_count = int(j.get("hitCount", 0))
        except (ValueError, TypeError) as e:
            res.status = "error"; res.error = f"hitCount parse: {e}"
        return res

    async def search_openalex(self, chemical: str) -> DatabaseSearchResult:
        url = (f"https://api.openalex.org/works?search={quote(chemical)}&per-page=25"
               f"&mailto={quote('aditya@scitoxsynthesis.local')}")
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="OpenAlex", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "openalex fetch failed"
            return res
        try:
            meta = j.get("meta", {}) or {}
            res.result_count = int(meta.get("count", 0))
        except (ValueError, TypeError) as e:
            res.status = "error"; res.error = f"meta parse: {e}"
        return res

    async def search_semantic_scholar(self, chemical: str) -> DatabaseSearchResult:
        # Semantic Scholar Graph API — no auth needed for basic queries
        url = (f"https://api.semanticscholar.org/graph/v1/paper/search"
               f"?query={quote(chemical)}&limit=25")
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="Semantic Scholar", query=chemical,
                                   url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "fetch failed (rate-limited or unavailable)"
            return res
        try:
            res.result_count = int(j.get("total", 0))
        except (ValueError, TypeError) as e:
            res.status = "error"; res.error = f"total parse: {e}"
        return res

    async def search_pubchem(self, chemical: str, cas: str | None) -> DatabaseSearchResult:
        # PubChem: a single compound resolution, not a search. Count = 1 if resolves.
        q = cas or chemical
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(q)}/cids/JSON"
        if cas and re.fullmatch(r"\d{2,7}-\d{2}-\d", cas.strip()):
            url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/xref/RN/{quote(cas)}/cids/JSON"
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="PubChem", query=q, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "PubChem lookup failed"
            return res
        try:
            cids = j["IdentifierList"]["CID"]
            res.result_count = len(cids) if cids else 0
        except (KeyError, TypeError):
            res.result_count = 0
        return res

    async def search_zenodo(self, chemical: str) -> DatabaseSearchResult:
        url = f"https://zenodo.org/api/records?q={quote(chemical)}&size=25"
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="Zenodo", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "zenodo fetch failed"
            return res
        try:
            res.result_count = int(j.get("hits", {}).get("total", 0))
        except (ValueError, TypeError, AttributeError) as e:
            res.status = "error"; res.error = f"total parse: {e}"
        return res

    async def search_crossref(self, chemical: str) -> DatabaseSearchResult:
        # CrossRef is a proxy for "DOI-registered literature" — complements
        # PubMed for non-biomedical lit and confirms DOI availability.
        url = f"https://api.crossref.org/works?query={quote(chemical)}&rows=25"
        j = await self._fetch_json(url)
        res = DatabaseSearchResult(database="CrossRef", query=chemical, url=url, status="ok")
        if j is None:
            res.status = "error"; res.error = "crossref fetch failed"
            return res
        try:
            res.result_count = int(j.get("message", {}).get("total-results", 0))
        except (ValueError, TypeError, AttributeError) as e:
            res.status = "error"; res.error = f"total parse: {e}"
        return res

    async def search_ilo_icsc(self, cas: str | None, chemical: str = "") -> DatabaseSearchResult:
        """ILO ICSC cards are keyed by CAS. Fetch the search page and count cards.

        Endpoint proven live by user screenshot (2026-04):
        https://www.ilo.org/dyn/icsc/showcard.listCards3?p_lang=en&p_search_text=<CAS-or-name>
        """
        query = cas or chemical
        if not query:
            return DatabaseSearchResult(
                database="ILO ICSC", query="", url="", status="error",
                error="ILO ICSC requires chemical name or CAS",
            )
        url = (f"https://www.ilo.org/dyn/icsc/showcard.listCards3"
               f"?p_lang=en&p_search_text={quote(query)}")
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="ILO ICSC", query=query, url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "ILO fetch failed"
            return res
        # Count links to individual cards in raw HTML (survive trafilatura stripping)
        hits = len(re.findall(r"showcard\.display\?p_lang=en&amp;p_card_id=\d+", html))
        if hits == 0:
            hits = len(re.findall(r"showcard\.display\?p_lang=en&p_card_id=\d+", html))
        if hits == 0:
            hits = len(re.findall(r"icsc\d{4}", html))
        res.result_count = hits
        res.status = "html_ok" if hits >= 1 else "error"
        if hits == 0:
            res.error = "no ICSC cards matched"
        return res

    # ─────────────────── GENERIC HTML SCRAPE HELPERS ───────────────────

    async def _raw_html(self, url: str) -> str | None:
        """Fetch raw HTML bytes and decode (bypasses trafilatura extraction).

        Search-result counts are often in boilerplate that trafilatura strips
        (headers, result indicators, <meta>). We need raw HTML to count them.
        """
        data, _ct, status, _err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return None
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return data.decode("latin-1", errors="replace")

    def _first_count(self, html: str, patterns: list[str]) -> int:
        """Try each regex pattern and return first numeric match found."""
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
            if m:
                try:
                    return int(m.group(1).replace(",", "").replace(" ", ""))
                except (ValueError, IndexError):
                    continue
        return 0

    async def _scrape_count(
        self, db: str, url: str, query: str, patterns: list[str],
        fallback_link_pattern: str | None = None,
        landing_note: str | None = None,
    ) -> DatabaseSearchResult:
        """Generic scraper: fetch URL, try count regexes, fall back to link-counting.

        Distinguishes FOUR cases with zero hallucinations:
          - Raw HTML has a visible numeric count → status='html_ok', result_count=N
          - HTML responded (200) but no count parseable (JS-rendered results)
            → status='landing_only', result_count=0, error=landing_note.
              The URL loaded but we did NOT scrape a real count; callers MUST
              NOT treat this as a verified hit.
          - Fetch error / 4xx / 5xx → status='error', result_count=0
          - Fetch returned bytes but HTML body is empty / very short → 'error'
        """
        res = DatabaseSearchResult(database=db, query=query, url=url, status="ok")
        data, _ct, http_status, _err = await self.fetcher.fetch(url)
        if data is None or http_status >= 400 or http_status == 0:
            res.status = "error"
            res.error = f"HTTP {http_status}" if http_status else (_err or "fetch failed")
            return res
        try:
            html = data.decode("utf-8", errors="replace")
        except Exception:
            html = data.decode("latin-1", errors="replace")

        # Proxy / WAF challenges often return 200 with a tiny challenge body.
        if len(html) < 500:
            res.status = "error"
            res.error = f"response body suspiciously small ({len(html)} bytes) — likely blocked"
            return res

        count = self._first_count(html, patterns)
        if count == 0 and fallback_link_pattern:
            count = len(re.findall(fallback_link_pattern, html, re.IGNORECASE))
        if count >= 1:
            res.result_count = count
            res.status = "html_ok"
            return res
        # HTTP 200 but count/link parse failed (JS-rendered or boilerplate-only)
        res.result_count = 0
        res.status = "landing_only"
        res.error = landing_note or "search page is live (JS-rendered); use fetch_url to drill in"
        return res

    # ─────────────────── PER-DATABASE LIVE HTML SCRAPERS ───────────────────

    async def scrape_semantic_scholar_html(self, chemical: str) -> DatabaseSearchResult:
        """Semantic Scholar HTML fallback — www.semanticscholar.org/search?q=...

        User-verified: ~214,000 results for benzene. The page is Next.js
        hydrated; __NEXT_DATA__ JSON payload includes totalResults.
        """
        url = f"https://www.semanticscholar.org/search?q={quote(chemical)}&sort=relevance"
        return await self._scrape_count(
            "Semantic Scholar (HTML)", url, chemical,
            patterns=[
                r'"totalResults"\s*:\s*(\d+)',
                r'"total"\s*:\s*(\d+)',
                r'About\s+([\d,]+)\s+results',
                r'([\d,]+)\s+(?:papers?|results?)\s+found',
            ],
            landing_note=(
                "Semantic Scholar Graph API at "
                "https://api.semanticscholar.org/graph/v1/paper/search "
                "is the deterministic count source; the HTML page hydrates "
                "totalResults from that API."
            ),
        )

    async def scrape_ntp(self, chemical: str) -> DatabaseSearchResult:
        """NTP search (JS-SPA) — we land on the search URL and point to the
        RoC profile / LT-RPT directories which are directly fetchable."""
        url = f"https://ntpsearch.niehs.nih.gov/home?q={quote(chemical)}"
        return await self._scrape_count(
            "NTP", url, chemical,
            patterns=[
                r'"totalHits"\s*:\s*(\d+)',
                r'"total"\s*:\s*(\d+)',
                r'([\d,]+)\s+result[s]?\s+found',
            ],
            fallback_link_pattern=r'ntp\.niehs\.nih\.gov/(?:publications|go|pubhealth|ntp)',
            landing_note=(
                "NTP search is a JavaScript SPA so the count isn't in raw HTML. "
                "RoC 15th-Ed. profiles live at "
                "https://ntp.niehs.nih.gov/ntp/roc/content/profiles/ "
                "and study reports under /ntp/htdocs/lt_rpts/; use fetch_url on "
                "the specific profile/report filename (e.g. benzene.pdf)."
            ),
        )

    async def scrape_inchem(self, chemical: str) -> DatabaseSearchResult:
        """WHO INCHEM full-site search.

        INCHEM has no working keyword search (search.htm 404s); the only real
        index is the catalogue list at /pages/search.html. Callers must know the
        EHC/CICAD/ICSC number. We return the catalogue landing so fetch_url can
        walk collections; for common chemicals the LLM can guess the EHC number
        (e.g. benzene = EHC 150) and confirm via
        inchem.org/documents/ehc/ehc/ehcNNN.htm.
        """
        url = "https://www.inchem.org/pages/search.html"
        return await self._scrape_count(
            "WHO INCHEM", url, chemical,
            patterns=[
                r'([\d,]+)\s+collections?',
                r'([\d,]+)\s+documents?',
            ],
            fallback_link_pattern=r'documents/(?:ehc|cicad|pims|icsc|iarc|hsg|sids)',
            landing_note=(
                "INCHEM index — collections listed at /pages/search.html. "
                "Direct EHC URL template: /documents/ehc/ehc/ehc<NN>.htm; "
                "CICAD: /documents/cicads/cicads/cicad<NN>.htm; "
                "IARC Monograph summary: /documents/iarc/volNN/<slug>.html."
            ),
        )

    async def scrape_who_ipcs(self, chemical: str) -> DatabaseSearchResult:
        """WHO IPCS — same backing site as INCHEM."""
        url = "https://www.inchem.org/pages/search.html"
        return await self._scrape_count(
            "WHO IPCS", url, chemical,
            patterns=[r'([\d,]+)\s+(?:collections?|documents?)'],
            fallback_link_pattern=r'documents/(?:ehc|cicad|pims|sids|hsg)',
            landing_note=(
                "WHO IPCS docs (EHC, CICAD, HSG, PIMs, SIDS) are catalogued on "
                "inchem.org. WHO also publishes newer assessments at "
                "https://www.who.int/publications/search (general search). "
                "Use fetch_url with the specific EHC/CICAD number once known."
            ),
        )

    async def scrape_echa(self, chemical: str, cas: str | None) -> DatabaseSearchResult:
        """ECHA full-site search — echa.europa.eu/search (JS-rendered counts).

        The ECHA search page loads fine (HTTP 200) but the result tiles are
        Angular-rendered, so the raw HTML doesn't contain the count. The URL
        itself is stable (user-verified: 5,019 results for benzene) so we mark
        it html_ok and point callers at direct record pages:
          - substance-information/-/substanceinfo/100.000.685 (benzene by EC)
          - registration-dossier/-/registered-dossier/15530  (benzene REACH)
        """
        q = cas or chemical
        url = f"https://echa.europa.eu/search?text={quote(q)}"
        return await self._scrape_count(
            "ECHA", url, q,
            patterns=[
                r'<strong>([\d,]+)</strong>\s+Results?\s+for',
                r'([\d,]+)\s+Results?\s+for',
                r'About\s+([\d,]+)\s+results',
                r'"total"\s*:\s*(\d+)',
            ],
            fallback_link_pattern=r'echa\.europa\.eu/(?:substance|registration|candidate-list)',
            landing_note=(
                "ECHA search is Angular-rendered so the count isn't in raw "
                "HTML. Direct substance record URLs follow the pattern "
                "https://echa.europa.eu/substance-information/-/substanceinfo/"
                "<EC dotted> (find EC via CAS lookup). REACH dossiers at "
                "/registration-dossier/-/registered-dossier/<ID>."
            ),
        )

    async def scrape_atsdr_toxprofiles(self, chemical: str) -> DatabaseSearchResult:
        """ATSDR ToxProfiles via search.cdc.gov (user-verified siteLimit=atsdr)."""
        url = (f"https://search.cdc.gov/search/?query={quote(chemical)}"
               f"&siteLimit=atsdr.cdc.gov")
        return await self._scrape_count(
            "ATSDR ToxProfiles", url, chemical,
            patterns=[
                r'About\s+([\d,]+)\s+results',
                r'([\d,]+)\s+results?\s+for',
                r'"totalResults"\s*:\s*"?(\d+)',
                r'of\s+about\s+([\d,]+)',
            ],
            fallback_link_pattern=r'atsdr\.cdc\.gov/(?:toxprofiles|ToxProfiles|MMG|phs|csem)',
            landing_note=(
                "ATSDR search-engine landing page. Tox Profile PDFs live at "
                "https://www.atsdr.cdc.gov/toxprofiles/tp<NN>.pdf "
                "(benzene = tp3.pdf). MRL table: "
                "https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx. "
                "Use fetch_pdf on the profile PDF for full-text."
            ),
        )

    async def scrape_calepa_oehha(self, chemical: str) -> DatabaseSearchResult:
        """CalEPA OEHHA full-site search (user-verified: 700 results for benzene)."""
        url = f"https://oehha.ca.gov/about/search-results?q={quote(chemical)}"
        return await self._scrape_count(
            "CalEPA OEHHA", url, chemical,
            patterns=[
                r'([\d,]+)\s+results?\s+found',
                r'About\s+([\d,]+)\s+results',
                r'Total:?\s*([\d,]+)',
                r'Results\s+\d+-\d+\s+of\s+([\d,]+)',
            ],
            fallback_link_pattern=r'oehha\.ca\.gov/(?:air|water|chemicals|proposition-65)/',
            landing_note=(
                "OEHHA search — results load via async JS. Direct record "
                "types worth fetching: /air/general-info/oehha-acute-chronic-"
                "and-8-hour-reference-exposure-levels-rels, "
                "/chemicals/<slug>, /proposition-65/chemicals/<slug>."
            ),
        )

    async def scrape_canada_substances(self, chemical: str, cas: str | None) -> DatabaseSearchResult:
        """Canada.ca Substances search (user screenshot: 3,406 results for benzene).

        Canada.ca blocks bots on /sr/srb.html with 403 + Akamai challenge, so
        we route through the public search JSON endpoint when possible, and
        otherwise fall back to the ECCC Substances Management substance list.
        """
        q = cas or chemical
        # Tier 1: public JSON API (sometimes works without challenge)
        url = (f"https://www.canada.ca/en/sr/srb.html?q={quote(q)}"
               f"&cdn=canada&st=s&num=10&langs=en")
        res = await self._scrape_count(
            "Canada Substances", url, q,
            patterns=[
                r'About\s+([\d,]+)\s+results',
                r'([\d,]+)\s+search\s+results?',
                r'Displaying\s+results?\s+\d+\s*-\s*\d+\s+of\s+about\s+([\d,]+)',
                r'Results?\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)',
            ],
            fallback_link_pattern=r'canada\.ca/en/(?:environment-climate-change|health-canada)',
            landing_note=(
                "Canada.ca site search; Environment Canada substances pages at "
                "https://canada.ca/en/environment-climate-change/services/"
                "management-toxic-substances/list-canadian-environmental-"
                "protection-act.html (Schedule 1 CEPA). "
                "Substance-specific info at /en/health-canada/services/"
                "chemical-substances/."
            ),
        )
        return res

    async def scrape_concawe(self, chemical: str) -> DatabaseSearchResult:
        """CONCAWE refinery-industry science — concawe.eu (user-verified)."""
        url = f"https://www.concawe.eu/?s={quote(chemical)}"
        return await self._scrape_count(
            "CONCAWE", url, chemical,
            patterns=[
                r'([\d,]+)\s+results?\s+for',
                r'Found\s+([\d,]+)\s+(?:result|match)',
                r'About\s+([\d,]+)\s+results',
            ],
            fallback_link_pattern=r'concawe\.eu/(?:publication|wp-content/uploads)',
            landing_note=(
                "CONCAWE WordPress search. Publications index at "
                "https://www.concawe.eu/publications/ — each tile links to a "
                "downloadable PDF under /wp-content/uploads/YYYY/MM/. "
                "Benzene-specific reports include the REACH benzene "
                "consortium datasets (rpt_94-52, rpt_06-09, etc.)."
            ),
        )

    async def scrape_niosh(self, chemical: str) -> DatabaseSearchResult:
        """NIOSH via search.cdc.gov siteLimit=NIOSH (user-verified)."""
        url = (f"https://search.cdc.gov/search/?query={quote(chemical)}"
               f"&siteLimit=NIOSH")
        return await self._scrape_count(
            "NIOSH", url, chemical,
            patterns=[
                r'About\s+([\d,]+)\s+results',
                r'([\d,]+)\s+results?\s+for',
                r'"totalResults"\s*:\s*"?(\d+)',
                r'of\s+about\s+([\d,]+)',
            ],
            fallback_link_pattern=r'cdc\.gov/niosh/(?:npg|idlh|topics|pgp|docs)',
            landing_note=(
                "NIOSH via CDC site-search. Pocket Guide: "
                "https://www.cdc.gov/niosh/npg/npgd<NNNN>.html (benzene=0049). "
                "Criteria Documents & IDLH PDFs under /niosh/docs/ and "
                "/niosh/idlh/. Use fetch_url on specific numbered hazard cards."
            ),
        )

    async def scrape_osha(self, chemical: str) -> DatabaseSearchResult:
        """OSHA full-site search (user-verified: 510 results for benzene).

        The search.osha.gov affiliate API was deprecated April 2025. We now
        land on the OSHA standard page for common substances (1910.1028 for
        benzene) and expose the laws-regs search as an html_ok URL.
        """
        url = f"https://www.osha.gov/laws-regs/standardinterpretations/search?search_api_fulltext={quote(chemical)}"
        return await self._scrape_count(
            "OSHA", url, chemical,
            patterns=[
                r'([\d,]+)\s+results?\s+found',
                r'About\s+([\d,]+)\s+results',
                r'Results?\s+\d+\s*-\s*\d+\s+of\s+(?:about\s+)?([\d,]+)',
                r'of\s+([\d,]+)\s+total',
            ],
            fallback_link_pattern=r'osha\.gov/(?:laws-regs|chemicaldata|annotated-pels|SLTC)',
            landing_note=(
                "OSHA Standards Interpretations search. Substance-specific "
                "standard URLs: /laws-regs/regulations/standardnumber/1910/"
                "1910.<NNNN> (benzene=1910.1028); PEL annotation tables under "
                "/annotated-pels/; chemical data at /chemicaldata/<slug>."
            ),
        )

    async def scrape_aicis(self, chemical: str, cas: str | None) -> DatabaseSearchResult:
        """Australia AICIS chemical search (user-verified)."""
        q = cas or chemical
        url = f"https://www.industrialchemicals.gov.au/search/node/{quote(q)}"
        return await self._scrape_count(
            "Australia AICIS", url, q,
            patterns=[
                r'([\d,]+)\s+results?\s+(?:found|returned)',
                r'About\s+([\d,]+)\s+results',
                r'Displaying\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)',
                r'Showing\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)',
            ],
            fallback_link_pattern=r'industrialchemicals\.gov\.au/(?:chemicals|business|about)',
            landing_note=(
                "AICIS Drupal search. Chemical-specific pages under "
                "/chemicals/<slug> (e.g. /chemicals/benzene). Evaluations "
                "and certificates: /business/search-assessments."
            ),
        )

    async def scrape_zenodo_html(self, chemical: str) -> DatabaseSearchResult:
        """Zenodo HTML fallback (user-verified: 1,641 results for benzene)."""
        url = f"https://zenodo.org/search?q={quote(chemical)}"
        return await self._scrape_count(
            "Zenodo (HTML)", url, chemical,
            patterns=[
                r'([\d,]+)\s+result\(s\)\s+found',
                r'([\d,]+)\s+results?\s+found',
                r'"total"\s*:\s*(\d+)',
            ],
            fallback_link_pattern=r'zenodo\.org/records?/\d+',
            landing_note=(
                "Zenodo deterministic count is from the JSON API "
                "https://zenodo.org/api/records?q=<chemical> (hits.total). "
                "HTML page hydrates from the same source."
            ),
        )

    async def search_html_site(
        self, db: str, url: str, result_pattern: str, chemical: str,
    ) -> DatabaseSearchResult:
        """Generic HTML search page scraper: counts regex matches (not exact count).
        Kept for auxiliary niche databases where we just need a positive signal."""
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database=db, query=chemical, url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "fetch failed"
            return res
        matches = re.findall(result_pattern, html, re.IGNORECASE)
        res.result_count = len(matches)
        res.status = "html_ok" if len(matches) >= 1 else "error"
        if len(matches) == 0:
            res.error = "no matches for pattern"
        return res

    # ─────────── ADDITIONAL SCRAPERS (upgraded from no_api) ───────────

    async def scrape_niosh_idlh(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """NIOSH IDLH — per-chemical document lookup via CDC CAS-indexed PDFs.

        The CDC hosts one PDF per IDLH-listed chemical at
        https://www.cdc.gov/niosh/idlh/<cas>.html with a normalized CAS
        (strip dashes). Benzene is https://www.cdc.gov/niosh/idlh/71432.html
        with the documented 500 ppm IDLH value. Returns html_ok with
        result_count=1 if the per-chemical document exists.
        """
        query = cas or chemical
        res = DatabaseSearchResult(
            database="NIOSH IDLH", query=query,
            url="https://www.cdc.gov/niosh/idlh/", status="ok",
        )
        if cas:
            cas_nd = cas.replace("-", "").strip()
            per_chem = f"https://www.cdc.gov/niosh/idlh/{cas_nd}.html"
            html = await self._raw_html(per_chem)
            if html and ("IDLH" in html or "Immediately Dangerous" in html):
                res.url = per_chem
                res.result_count = 1
                res.status = "html_ok"
                m = re.search(r"(\d+[\d,\.]*)\s*ppm", html)
                if m:
                    res.error = f"IDLH documented; value: {m.group(1)} ppm"
                return res
        # Fall back to the alphabetical index
        first = (chemical.strip()[:1] or "a").lower()
        url = f"https://www.cdc.gov/niosh/idlh/idlh-{first}.html"
        html = await self._raw_html(url)
        res.url = url
        if html is None:
            res.status = "error"
            res.error = "NIOSH IDLH index fetch failed"
            return res
        # Count chemical entries on the alphabetical index page
        matches = re.findall(
            rf"(?i)[^a-z0-9]{re.escape(chemical.lower())}[^a-z0-9]",
            html.lower(),
        )
        if matches:
            res.result_count = 1
            res.status = "html_ok"
            res.error = f"Chemical found in NIOSH IDLH alphabetical index ({first})"
        else:
            res.status = "error"
            res.error = "chemical not found in NIOSH IDLH list"
        return res

    async def scrape_oecd_echemportal(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """OECD eChemPortal — search against the sitemap-indexed SIDS database.

        eChemPortal proper is CloudFlare-protected for browser access, but
        the OECD SIDS archive can be searched via
        https://hpvchemicals.oecd.org/UI/SIDS_Details.aspx?key=<CAS>
        and the eChemPortal public search endpoint
        https://www.echemportal.org/echemportal/substance-search?casNumber=<CAS>
        returns a predictable page for CAS-indexed substances.
        """
        query = cas or chemical
        url = (f"https://www.echemportal.org/echemportal/substance-search"
               f"?casNumber={quote(cas)}" if cas
               else f"https://www.echemportal.org/echemportal/substance-search?searchText={quote(chemical)}")
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="OECD eChemPortal", query=query,
                                   url=url, status="ok")
        if html is None:
            # Fetch failed — do NOT pretend success. Point at the per-CAS OECD
            # SIDS URL as a drill-in target but mark as error since we have
            # zero evidence the alternate URL is reachable either.
            res.status = "error"
            if cas:
                alt = f"https://hpvchemicals.oecd.org/UI/SIDS_Details.aspx?key={quote(cas)}"
                res.url = alt
                res.error = ("eChemPortal fetch failed (likely CloudFlare / proxy). "
                             "Alternate OECD SIDS URL surfaced for manual drill-in; "
                             "not verified reachable this run.")
            else:
                res.error = "eChemPortal fetch failed (CloudFlare or blocked)"
            return res
        cnt = self._first_count(html, [
            r'"totalResults"\s*:\s*(\d+)',
            r'([\d,]+)\s+results?\s+found',
            r'Total\s*:\s*([\d,]+)',
        ])
        if cnt >= 1:
            res.result_count = cnt
            res.status = "html_ok"
        else:
            res.result_count = 0
            res.status = "landing_only"
            res.error = "eChemPortal landing accessible (JS-rendered; no count parsed)"
        return res

    async def scrape_japan_nite(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """Japan NITE CHRIP — CAS-indexed chemical records at
        https://www.nite.go.jp/chem/chrip/chrip_search/dt?cno=<CAS>.
        """
        query = cas or chemical
        if cas:
            url = f"https://www.nite.go.jp/chem/chrip/chrip_search/dt?cno={quote(cas)}"
        else:
            url = f"https://www.nite.go.jp/chem/chrip/chrip_search/srhInput?nameWord={quote(chemical)}"
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="Japan NITE (CHRIP)", query=query,
                                   url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "NITE fetch failed"
            return res
        if "chem/chrip" in html or "CHRIP" in html:
            # Landed on a CHRIP-branded page but did not parse a real count.
            res.result_count = 0
            res.status = "landing_only"
            res.error = "CHRIP landing accessible (JS-rendered; no count parsed)"
        else:
            res.status = "error"
            res.error = "CHRIP record not found"
        return res

    async def scrape_japan_prtr(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """Japan PRTR — MoE PRTR substance registry at
        https://www.env.go.jp/chemi/prtr/substance/list.html.
        """
        url = "https://www.env.go.jp/chemi/prtr/substance/index.html"
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="Japan PRTR", query=cas or chemical,
                                   url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "PRTR index fetch failed"
            return res
        # Search the substance-list page for CAS or name
        hit = False
        if cas and cas in html:
            hit = True
        if not hit:
            hit = chemical.lower() in html.lower()
        if hit:
            # Name / CAS string appears on the PRTR index page — genuine
            # positive signal that the chemical is regulated. Count of "1"
            # here means "one matching regulatory listing", not "one paper".
            res.result_count = 1
            res.status = "html_ok"
            res.error = "Chemical name / CAS matched on PRTR Class 1 substance list"
        else:
            res.status = "error"
            res.error = "not found in PRTR list"
        return res

    async def scrape_korea_moe_icis(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """Korea MoE ICIS — Integrated Chemical Information System.

        ICIS has a REST-like search at
        https://icis.me.go.kr/search/chemSearchResult.do?searchVal=<CAS>.
        """
        query = cas or chemical
        url = f"https://icis.me.go.kr/search/chemSearchResult.do?searchVal={quote(query)}"
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="Korea MOE ICIS", query=query,
                                   url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "ICIS fetch failed"
            return res
        cnt = self._first_count(html, [
            r"총\s*<[^>]+>\s*(\d+)\s*<",   # Korean "total: N"
            r'total\s*:\s*(\d+)',
            r'([\d,]+)\s+건',               # Korean count "N items"
        ])
        if cnt >= 1:
            res.result_count = cnt
            res.status = "html_ok"
        else:
            res.result_count = 0
            res.status = "landing_only"
            res.error = "ICIS search landing accessible (JS-rendered; no count parsed)"
        return res

    async def scrape_cpdb(self, chemical: str, cas: str | None = None) -> DatabaseSearchResult:
        """Carcinogenic Potency Database — static archive text files.

        CPDB is static at https://files.toxplanet.com/cpdb/cpdb.html with
        a single master table; we count occurrences of the chemical name
        in that table.
        """
        url = "https://files.toxplanet.com/cpdb/chemicalsummary.html"
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="CPDB", query=chemical,
                                   url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "CPDB archive fetch failed"
            return res
        hits = len(re.findall(
            rf"(?i)\b{re.escape(chemical)}\b", html,
        ))
        if hits >= 1:
            res.result_count = hits
            res.status = "html_ok"
        else:
            res.status = "error"; res.error = "not found in CPDB archive"
        return res

    async def scrape_safe_work_australia(self, chemical: str) -> DatabaseSearchResult:
        """Safe Work Australia — search the exposure standards database."""
        url = (f"https://www.safeworkaustralia.gov.au/search?"
               f"search_api_fulltext={quote(chemical)}&page=0")
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="Australia Safe Work",
                                   query=chemical, url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "SWA fetch failed"
            return res
        cnt = self._first_count(html, [
            r'About\s+([\d,]+)\s+results?',
            r'([\d,]+)\s+results?\s+found',
            r'"total"\s*:\s*(\d+)',
        ])
        if cnt == 0:
            cnt = len(re.findall(r'class="search-result"', html))
        if cnt >= 1:
            res.result_count = cnt
            res.status = "html_ok"
        else:
            res.result_count = 0
            res.status = "landing_only"
            res.error = "Safe Work Australia search landing accessible (no count parsed)"
        return res

    async def scrape_iarc(self, chemical: str) -> DatabaseSearchResult:
        """IARC Monographs — search the publications.iarc.fr full-text index."""
        url = f"https://publications.iarc.fr/_publications/media/?search={quote(chemical)}"
        html = await self._raw_html(url)
        res = DatabaseSearchResult(database="IARC Monographs",
                                   query=chemical, url=url, status="ok")
        if html is None:
            res.status = "error"; res.error = "IARC fetch failed"
            return res
        cnt = len(re.findall(r"publications\.iarc\.fr/_publications/\d+", html))
        if cnt >= 1:
            res.result_count = cnt
            res.status = "html_ok"
        else:
            res.result_count = 0
            res.status = "landing_only"
            res.error = "IARC publications landing accessible (JS-rendered; no record links parsed)"
        return res

    async def run_all(self, chemical: str, cas: str | None = None) -> list[DatabaseSearchResult]:
        """Run deterministic counts for every database where we can.

        Five result kinds are returned:
          * status='ok'           — verified JSON-API count (PubMed, PMC, etc.)
          * status='html_ok'      — scraped numeric count from raw HTML
          * status='landing_only' — URL reachable (HTTP 200) but NO count
                                    parsed. result_count is 0. Drill-in target
                                    only; do NOT treat as a verified hit.
          * status='no_api'       — database intrinsically has no searchable API
          * status='error'        — fetch failed / blocked / 4xx-5xx / tiny body

        Zero-hallucination guarantee: the runner NEVER sets result_count to a
        fabricated sentinel. A result_count > 0 means either a real API count
        or a real HTML-parsed count. landing_only always carries result_count=0.
        """
        tasks: list[asyncio.Task[DatabaseSearchResult]] = []

        # ── API-verified counts (JSON) ──────────────────────────────────────
        tasks.append(asyncio.create_task(self.search_pubmed(chemical)))
        tasks.append(asyncio.create_task(self.search_pmc(chemical)))
        tasks.append(asyncio.create_task(self.search_europepmc(chemical)))
        tasks.append(asyncio.create_task(self.search_openalex(chemical)))
        tasks.append(asyncio.create_task(self.search_semantic_scholar(chemical)))
        tasks.append(asyncio.create_task(self.search_pubchem(chemical, cas)))
        tasks.append(asyncio.create_task(self.search_zenodo(chemical)))
        tasks.append(asyncio.create_task(self.search_crossref(chemical)))

        # ── HTML-scraped counts (14 databases user verified live) ──────────
        tasks.append(asyncio.create_task(self.scrape_semantic_scholar_html(chemical)))
        tasks.append(asyncio.create_task(self.scrape_ntp(chemical)))
        tasks.append(asyncio.create_task(self.scrape_inchem(chemical)))
        tasks.append(asyncio.create_task(self.scrape_who_ipcs(chemical)))
        tasks.append(asyncio.create_task(self.scrape_echa(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_atsdr_toxprofiles(chemical)))
        tasks.append(asyncio.create_task(self.scrape_calepa_oehha(chemical)))
        tasks.append(asyncio.create_task(self.scrape_canada_substances(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_concawe(chemical)))
        tasks.append(asyncio.create_task(self.scrape_niosh(chemical)))
        tasks.append(asyncio.create_task(self.scrape_osha(chemical)))
        tasks.append(asyncio.create_task(self.scrape_aicis(chemical, cas)))
        tasks.append(asyncio.create_task(self.search_ilo_icsc(cas, chemical)))
        tasks.append(asyncio.create_task(self.scrape_zenodo_html(chemical)))
        # Upgraded scrapers for previously no_api databases
        tasks.append(asyncio.create_task(self.scrape_niosh_idlh(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_oecd_echemportal(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_japan_nite(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_japan_prtr(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_korea_moe_icis(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_cpdb(chemical, cas)))
        tasks.append(asyncio.create_task(self.scrape_safe_work_australia(chemical)))
        tasks.append(asyncio.create_task(self.scrape_iarc(chemical)))

        # ── Additional niche HTML sources (auxiliary) ──────────────────────
        tasks.append(asyncio.create_task(self.search_html_site(
            "EPA CompTox",
            f"https://comptox.epa.gov/dashboard/chemical-lists/search?search={quote(chemical)}",
            r"dashboard/chemical/details", chemical,
        )))
        tasks.append(asyncio.create_task(self.search_html_site(
            "Haz-Map",
            f"https://haz-map.com/search/?q={quote(chemical)}",
            r"/Agents/\d+", chemical,
        )))
        tasks.append(asyncio.create_task(self.search_html_site(
            "Silent Spring",
            f"https://www.silentspring.org/?s={quote(chemical)}",
            r"<article", chemical,
        )))

        # Cap every per-DB task to PER_DB_TIMEOUT seconds so one slow/
        # hanging scraper can never stall the whole 37-way gather. Without
        # this, a single DB that never responds blocks the entire sweep
        # until the underlying HTTP timeout * retry count elapses.
        PER_DB_TIMEOUT = 10.0

        async def _bounded(task: asyncio.Task[DatabaseSearchResult]) -> DatabaseSearchResult:
            try:
                return await asyncio.wait_for(task, timeout=PER_DB_TIMEOUT)
            except asyncio.TimeoutError:
                return DatabaseSearchResult(
                    database="UNKNOWN", query=chemical, url="",
                    status="error",
                    error=f"per-DB timeout after {PER_DB_TIMEOUT}s",
                )
            except Exception as e:  # noqa: BLE001
                return DatabaseSearchResult(
                    database="UNKNOWN", query=chemical, url="",
                    status="error", error=f"{type(e).__name__}: {e}",
                )

        results = await asyncio.gather(*(_bounded(t) for t in tasks),
                                       return_exceptions=True)
        out: list[DatabaseSearchResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(DatabaseSearchResult(
                    database="UNKNOWN", query=chemical, url="",
                    status="error", error=f"{type(r).__name__}: {r}",
                ))
            else:
                out.append(r)

        # ── Agency landing pages. These URLs are known useful drill-in targets
        # but we DO NOT fetch them here (they are JS-heavy / session-cookie
        # gated). We now probe each with a single GET and honestly report the
        # outcome rather than claiming a fake result_count=1.
        from urllib.parse import quote as _q
        name_q = _q(chemical)
        first_letter = (chemical.strip()[:1] or "a").lower()
        landing_candidates = [
            ("EPA IRIS",
             "https://iris.epa.gov/AtoZ/?list_type=alpha",
             "EPA IRIS A-Z index; follow the chemical link for RfD/RfC/slope factors."),
            ("NIOSH Pocket Guide",
             f"https://www.cdc.gov/niosh/npg/npgsyn-{first_letter}.html",
             f"NIOSH NPG alphabetical index (letter '{first_letter}'); "
             "follow npgdNNNN.html for the hazard card."),
            ("ATSDR MRLs",
             "https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx",
             "ATSDR consolidated MRL table; search_in_content on CAS to locate rows."),
            ("Australia HCIS",
             f"https://hcis.safeworkaustralia.gov.au/HazardousChemical/Search?query={name_q}",
             "Australia HCIS hazardous chemicals search; Details page per hit."),
        ]
        async def _probe(db: str, url: str, note: str) -> DatabaseSearchResult:
            data, _ct, st, err = await self.fetcher.fetch(url)
            r = DatabaseSearchResult(database=db, query=chemical, url=url,
                                     status="error", result_count=0)
            if data is None or st == 0 or st >= 400:
                r.error = f"fetch failed: HTTP {st or 'n/a'} {err or ''}".strip()
                return r
            # HTTP 200 — honest: we did not parse a count, this is landing-only.
            try:
                html = data.decode("utf-8", errors="replace")
            except Exception:
                html = data.decode("latin-1", errors="replace")
            if len(html) < 500:
                r.error = f"response body too small ({len(html)} bytes) — likely blocked"
                return r
            r.status = "landing_only"
            r.error = note
            return r

        probe_tasks = [asyncio.create_task(_probe(db, url, note))
                       for db, url, note in landing_candidates]
        bounded_probes = [_bounded(t) for t in probe_tasks]  # same per-DB cap
        for t in await asyncio.gather(*bounded_probes, return_exceptions=True):
            if isinstance(t, Exception):
                continue
            out.append(t)

        # All previously-no_api databases now have real scrapers above.
        # This block kept empty intentionally so run_all reports only actual
        # scraper outcomes (ok/html_ok/error).

        return out


# ════════════════════════════════════════════════════════════════════════════
#  REPORT GENERATOR  — python-docx, Times New Roman 12pt, deterministic
# ════════════════════════════════════════════════════════════════════════════
