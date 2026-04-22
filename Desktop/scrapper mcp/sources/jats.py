"""
sources.jats — PMC JATS XML harvester.

Returns structured article body, tables (TSV), and figures from PMC's JATS
XML feed — the most machine-readable source for tox literature.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    from lxml import etree as _lxml_etree  # type: ignore
    HAS_LXML = True
except Exception:
    HAS_LXML = False

from core.http import HTTPFetcher


@dataclass
class JATSTable:
    label: str
    caption: str
    html: str          # raw <table> HTML — rendered by any markdown viewer
    rows: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JATSFigure:
    label: str
    caption: str
    graphic_href: str  # relative href; resolve against the PMC OA endpoint

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JATSArticle:
    pmcid: str
    pmid: str | None = None
    doi: str | None = None
    title: str = ""
    journal: str = ""
    year: str = ""
    abstract: str = ""
    body_text: str = ""
    tables: list[JATSTable] = field(default_factory=list)
    figures: list[JATSFigure] = field(default_factory=list)
    references: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class JATSHarvester:
    """Fetch PMC article in JATS XML format and parse tables/figures/refs.

    Uses the NCBI E-utilities efetch endpoint with db=pmc&rettype=xml which
    returns clean JATS XML. This is the canonical machine-readable form of
    PMC articles and is much more reliable than scraping HTML.
    """

    EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def __init__(self, fetcher: HTTPFetcher) -> None:
        self.fetcher = fetcher

    @staticmethod
    def _norm_pmcid(pmcid: str) -> str:
        pmcid = pmcid.strip()
        if pmcid.upper().startswith("PMC"):
            return pmcid[3:]
        return pmcid

    async def fetch(self, pmcid_or_url: str) -> JATSArticle:
        if pmcid_or_url.startswith("http"):
            m = re.search(r"PMC(\d+)", pmcid_or_url, re.IGNORECASE)
            if not m:
                return JATSArticle(pmcid="", error="Could not parse PMCID from URL")
            pmcid_num = m.group(1)
        else:
            pmcid_num = self._norm_pmcid(pmcid_or_url)

        pmcid_full = f"PMC{pmcid_num}"
        url = f"{self.EFETCH}?db=pmc&id={pmcid_num}&rettype=xml&retmode=xml"

        data, _ct, status, err = await self.fetcher.fetch(url)
        if data is None or status >= 400:
            return JATSArticle(pmcid=pmcid_full, error=f"fetch failed: {err or status}")

        art = JATSArticle(pmcid=pmcid_full)

        if not HAS_LXML:
            # Fallback: use BeautifulSoup in xml mode
            try:
                soup = BeautifulSoup(data, "lxml-xml")
            except Exception:
                try:
                    soup = BeautifulSoup(data, "xml")
                except Exception as e:  # noqa: BLE001
                    art.error = f"xml parse (bs4): {e}"
                    return art
            return self._parse_with_bs4(soup, art)

        try:
            root = _lxml_etree.fromstring(data)
        except Exception as e:  # noqa: BLE001
            art.error = f"xml parse (lxml): {e}"
            return art
        return self._parse_with_lxml(root, art)

    @staticmethod
    def _text_of(node) -> str:
        """Serialize an XML element's text content, preserving order."""
        if node is None:
            return ""
        # Use itertext for lxml nodes
        try:
            return " ".join(t.strip() for t in node.itertext() if t.strip())
        except AttributeError:
            return node.get_text(" ", strip=True) if node else ""

    def _parse_with_lxml(self, root, art: JATSArticle) -> JATSArticle:
        # JATS has no default namespace typically; use local-name() fallbacks.
        def find_first(elem, xpath: str):
            try:
                res = elem.xpath(xpath)
                return res[0] if res else None
            except Exception:
                return None

        # Title
        t = find_first(root, ".//article-title")
        if t is not None:
            art.title = self._text_of(t)[:500]
        # Journal
        j = find_first(root, ".//journal-title")
        if j is not None:
            art.journal = self._text_of(j)[:200]
        # Year
        y = find_first(root, ".//pub-date/year")
        if y is not None:
            art.year = self._text_of(y)[:4]
        # IDs
        for id_node in root.xpath(".//article-id"):
            pub = (id_node.get("pub-id-type") or "").lower()
            val = (id_node.text or "").strip()
            if pub == "doi" and not art.doi:
                art.doi = val
            elif pub == "pmid" and not art.pmid:
                art.pmid = val
        # Abstract
        abs_node = find_first(root, ".//abstract")
        if abs_node is not None:
            art.abstract = self._text_of(abs_node)[:8000]
        # Body (prefer <body>)
        body = find_first(root, ".//body")
        if body is not None:
            art.body_text = self._text_of(body)

        # Tables
        for tw in root.xpath(".//table-wrap"):
            label = self._text_of(find_first(tw, ".//label"))
            caption = self._text_of(find_first(tw, ".//caption"))
            tbl = find_first(tw, ".//table")
            html = ""
            rows: list[list[str]] = []
            if tbl is not None:
                try:
                    html = _lxml_etree.tostring(tbl, encoding="unicode", method="html")
                except Exception:
                    html = ""
                # Also parse rows as plain strings
                for tr in tbl.xpath(".//tr"):
                    row = [self._text_of(c) for c in tr.xpath(".//th|.//td")]
                    if any(c.strip() for c in row):
                        rows.append(row)
            art.tables.append(JATSTable(
                label=label[:50], caption=caption[:1000],
                html=html[:20000], rows=rows,
            ))

        # Figures
        for fig in root.xpath(".//fig"):
            label = self._text_of(find_first(fig, ".//label"))
            caption = self._text_of(find_first(fig, ".//caption"))
            graphic = find_first(fig, ".//graphic")
            href = ""
            if graphic is not None:
                # xlink:href attribute
                for k, v in graphic.attrib.items():
                    if k.endswith("href"):
                        href = v
                        break
            art.figures.append(JATSFigure(
                label=label[:50], caption=caption[:1000], graphic_href=href,
            ))

        # References (simplified — title + authors + year + PMID/DOI if present)
        for ref in root.xpath(".//ref"):
            cite = {}
            pub_node = find_first(ref, ".//element-citation") or find_first(ref, ".//mixed-citation")
            if pub_node is None:
                continue
            cite["title"] = self._text_of(find_first(pub_node, ".//article-title"))[:300]
            cite["year"] = self._text_of(find_first(pub_node, ".//year"))[:4]
            cite["source"] = self._text_of(find_first(pub_node, ".//source"))[:200]
            for pid in pub_node.xpath(".//pub-id"):
                t = (pid.get("pub-id-type") or "").lower()
                v = (pid.text or "").strip()
                if t == "doi":
                    cite["doi"] = v
                elif t == "pmid":
                    cite["pmid"] = v
                elif t == "pmcid":
                    cite["pmcid"] = v
            if any(cite.values()):
                art.references.append(cite)

        return art

    def _parse_with_bs4(self, soup, art: JATSArticle) -> JATSArticle:
        # Lightweight fallback when lxml isn't available
        t = soup.find("article-title")
        if t: art.title = t.get_text(" ", strip=True)[:500]
        j = soup.find("journal-title")
        if j: art.journal = j.get_text(" ", strip=True)[:200]
        y = soup.find("year")
        if y: art.year = y.get_text(strip=True)[:4]
        for id_node in soup.find_all("article-id"):
            pub = (id_node.get("pub-id-type") or "").lower()
            val = id_node.get_text(strip=True)
            if pub == "doi" and not art.doi:
                art.doi = val
            elif pub == "pmid" and not art.pmid:
                art.pmid = val
        abs_node = soup.find("abstract")
        if abs_node:
            art.abstract = abs_node.get_text(" ", strip=True)[:8000]
        body = soup.find("body")
        if body:
            art.body_text = body.get_text(" ", strip=True)
        for tw in soup.find_all("table-wrap"):
            label_n = tw.find("label")
            caption_n = tw.find("caption")
            tbl = tw.find("table")
            rows: list[list[str]] = []
            html = ""
            if tbl:
                html = str(tbl)[:20000]
                for tr in tbl.find_all("tr"):
                    row = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                    if any(c.strip() for c in row):
                        rows.append(row)
            art.tables.append(JATSTable(
                label=(label_n.get_text(strip=True) if label_n else "")[:50],
                caption=(caption_n.get_text(" ", strip=True) if caption_n else "")[:1000],
                html=html, rows=rows,
            ))
        for fig in soup.find_all("fig"):
            label_n = fig.find("label")
            caption_n = fig.find("caption")
            graphic = fig.find("graphic")
            href = ""
            if graphic:
                for k, v in graphic.attrs.items():
                    if k.endswith("href"):
                        href = v
                        break
            art.figures.append(JATSFigure(
                label=(label_n.get_text(strip=True) if label_n else "")[:50],
                caption=(caption_n.get_text(" ", strip=True) if caption_n else "")[:1000],
                graphic_href=href,
            ))
        return art


# ══════════════════════════════════════════════════════════════════════════
#  DATABASE URL BUILDERS  — per-chemical search URL constructors for the
#  non-API databases. These just BUILD URLs; the caller passes them to
#  fetch_url / batch_scrape. This keeps the tool surface simple.
# ══════════════════════════════════════════════════════════════════════════
