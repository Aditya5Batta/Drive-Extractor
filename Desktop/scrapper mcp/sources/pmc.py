"""
sources.pmc — PMC full-text harvester.

Returns PMC article full text + figure URLs + cited references.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Any

from bs4 import BeautifulSoup

from core.http import HTTPFetcher


@dataclass
class PMCArticle:
    pmcid: str
    pmid: str | None = None
    doi: str | None = None
    title: str = ""
    journal: str = ""
    year: str = ""
    abstract: str = ""
    full_text: str = ""
    figure_urls: list[str] = field(default_factory=list)
    table_urls: list[str] = field(default_factory=list)
    pdf_url: str | None = None
    reference_dois: list[str] = field(default_factory=list)
    reference_pmids: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PMCHarvester:
    """Harvest PMC articles: full text + figures + references.

    PMC is the gold standard for OA biomedical full text. It has:
      - Clean structured HTML (easy to extract)
      - Figure images at predictable URLs
      - Reference lists with DOI + PMID links (enables corpus expansion)

    Use this after find_papers returns PMC-indexed candidates, OR when
    harvesting a specific PMC article whose ID you already know.
    """

    def __init__(self, fetcher: HTTPFetcher, scraper: Scraper) -> None:
        self.fetcher = fetcher
        self.scraper = scraper

    @staticmethod
    def _normalize_pmcid(pmcid: str) -> str:
        pmcid = pmcid.strip()
        if not pmcid.upper().startswith("PMC"):
            pmcid = "PMC" + pmcid
        return pmcid

    async def fetch(self, pmcid_or_url: str) -> PMCArticle:
        # Accept bare PMCID or any PMC URL
        if pmcid_or_url.startswith("http"):
            m = re.search(r"PMC(\d+)", pmcid_or_url, re.IGNORECASE)
            if not m:
                return PMCArticle(pmcid="", error="Could not parse PMCID from URL")
            pmcid = "PMC" + m.group(1)
        else:
            pmcid = self._normalize_pmcid(pmcid_or_url)

        art = PMCArticle(pmcid=pmcid)
        # NIH PMC renders cleanly; EuropePMC requires JS
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        data, _ct, status, err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            art.error = f"fetch failed: {err or status}"
            return art

        # Clean text via trafilatura
        try:
            art.full_text = HTMLExtractor.extract_text(data, url)
            art.title = HTMLExtractor.extract_title(data)
        except Exception as e:  # noqa: BLE001
            art.error = f"text extraction: {e}"

        # Parse HTML for structured bits
        try:
            soup = BeautifulSoup(data, "lxml")
        except Exception:
            soup = BeautifulSoup(data, "html.parser")

        # Abstract (first <section> with class abstract, or first div.tsec)
        abs_tag = soup.find(attrs={"class": re.compile(r"abstract", re.I)})
        if abs_tag:
            art.abstract = abs_tag.get_text(separator=" ", strip=True)[:5000]

        # DOI and PMID meta
        for meta in soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = meta.get("content")
            if not content:
                continue
            if "citation_doi" in name and not art.doi:
                art.doi = content
            elif name == "citation_pmid" and not art.pmid:
                art.pmid = content
            elif name == "citation_journal_title" and not art.journal:
                art.journal = content
            elif name == "citation_year" and not art.year:
                art.year = content

        # Figure image URLs — PMC inserts <img src=...> inside figure containers,
        # AND provides figure landing URLs at /articles/{PMCID}/figure/F{n}/
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(rf"/articles/{pmcid}/figure/F\d+/", href, re.I):
                art.figure_urls.append(urljoin(url, href))
            elif re.search(rf"/articles/{pmcid}/table/T\d+/", href, re.I):
                art.table_urls.append(urljoin(url, href))
        # Dedupe preserving order
        art.figure_urls = list(dict.fromkeys(art.figure_urls))
        art.table_urls = list(dict.fromkeys(art.table_urls))

        # PDF link
        for a in soup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf") and pmcid.lower() in a["href"].lower():
                art.pdf_url = urljoin(url, a["href"])
                break
        if not art.pdf_url:
            # Fallback to EuropePMC PDF endpoint
            art.pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"

        # Reference extraction — look for DOI / PMID links in the reference section
        ref_section = soup.find(attrs={"id": re.compile(r"ref-list|bibliography|references", re.I)})
        search_in = ref_section if ref_section else soup
        seen_dois: set[str] = set()
        seen_pmids: set[str] = set()
        for a in search_in.find_all("a", href=True):
            href = a["href"]
            # DOI pattern
            m = re.search(r"doi\.org/(10\.[^\s\?#]+)", href)
            if m:
                d = m.group(1).rstrip("/")
                if d not in seen_dois:
                    seen_dois.add(d)
                    art.reference_dois.append(d)
                continue
            # PubMed PMID
            m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", href)
            if m:
                p = m.group(1)
                if p not in seen_pmids:
                    seen_pmids.add(p)
                    art.reference_pmids.append(p)

        # Cap reference lists to avoid bloat (top 100 usually)
        art.reference_dois = art.reference_dois[:100]
        art.reference_pmids = art.reference_pmids[:100]

        return art


# ══════════════════════════════════════════════════════════════════════════
#  AGENCY PROFILE FETCHER  — NTP, ATSDR, EPA IRIS, CalEPA, NIOSH IDLH
# ══════════════════════════════════════════════════════════════════════════
