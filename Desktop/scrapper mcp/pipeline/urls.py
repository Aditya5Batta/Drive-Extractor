"""
pipeline.urls — Per-database search URL builder.

Given a chemical identifier (name / CAS / CID), returns the exact URL to
hit on each database's search page. Deterministic: same input → same URLs.
"""
from __future__ import annotations
from urllib.parse import quote
from typing import Any

from config.databases import ALL_DATABASES


class DatabaseURLBuilder:
    """Given chemical name + CAS, build search URLs for every curated DB.

    For databases with an API (PubMed, OpenAlex, EuropePMC, PubChem), this
    returns the API endpoint as a reference but the dedicated tools should
    be used for those. For the rest, this returns search-form URLs or known
    per-chemical patterns. None of these URLs perform authentication or
    bypass anti-bot controls — they are public search URLs.
    """

    @staticmethod
    def build_all(chemical: str, cas: str | None = None) -> list[dict[str, str]]:
        name_q = quote(chemical)
        cas_q = quote(cas) if cas else ""
        cas_plain = (cas or "").strip()
        # Slug for URL paths: lowercase, ASCII hyphens
        slug = re.sub(r"[^a-z0-9]+", "-", chemical.lower()).strip("-") or "chemical"
        first = (chemical.strip()[:1] or "a").lower()
        out: list[dict[str, str]] = []

        def add(db: str, url: str, note: str = "") -> None:
            out.append({"database": db, "url": url, "notes": note})

        # ── Literature / bibliographic ──
        add("PubMed_search",
            f"https://pubmed.ncbi.nlm.nih.gov/?term={name_q}",
            "PubMed search results page (use find_papers for API access).")
        add("PMC_search",
            f"https://www.ncbi.nlm.nih.gov/pmc/?term={name_q}",
            "PMC full-text search.")
        add("EuropePMC_search",
            f"https://europepmc.org/search?query={name_q}",
            "EuropePMC search.")
        add("OpenAlex_search",
            f"https://api.openalex.org/works?search={name_q}&per-page=25",
            "OpenAlex Works API (use find_papers_openalex).")
        add("SemanticScholar_search",
            f"https://www.semanticscholar.org/search?q={name_q}",
            "Semantic Scholar search (has PDFs for many OA papers).")
        add("Unpaywall_DOI_lookup",
            "https://api.unpaywall.org/v2/",
            "Append DOI: /<doi>?email=you@example.com to look up OA location.")
        add("Zenodo_search",
            f"https://zenodo.org/search?q={name_q}",
            "Zenodo — datasets, preprints, tox data files.")

        # ── National / regulatory agencies ──
        add("ATSDR_ToxProfile_AZ",
            "https://www.atsdr.cdc.gov/toxprofiles/index.html",
            "ATSDR tox profiles A–Z; find the entry by name.")
        add("ATSDR_ToxFAQs_search",
            f"https://search.cdc.gov/search/?query={name_q}&siteLimit=atsdr",
            "ATSDR site search.")
        add("ATSDR_MRL_list",
            "https://www.atsdr.cdc.gov/minimal-risk-levels/index.html",
            "ATSDR MRL master list (PDF).")
        add("NTP_search",
            f"https://ntpsearch.niehs.nih.gov/home?q={name_q}",
            "NTP site search.")
        add("NTP_ROC",
            "https://ntp.niehs.nih.gov/go/roc15",
            "Report on Carcinogens (15th ed.) — search the PDF manually or via batch_scrape.")
        add("EPA_IRIS_AZ",
            "https://iris.epa.gov/AtoZ/?list_type=alpha",
            "EPA IRIS A–Z alphabetical index.")
        add("EPA_CompTox",
            f"https://comptox.epa.gov/dashboard/chemical/details/search?search={name_q}",
            "EPA CompTox Chemicals Dashboard (includes hazard data).")
        add("CalEPA_OEHHA_Prop65_list",
            "https://oehha.ca.gov/proposition-65/proposition-65-list",
            "Prop 65 list — find entry by name/CAS (HTML table).")
        add("CalEPA_OEHHA_chem_page",
            f"https://oehha.ca.gov/chemicals/{slug}",
            "CalEPA OEHHA per-chemical page (may 404 if slug differs).")
        add("NIOSH_NPG_search",
            f"https://www.cdc.gov/niosh/npg/npgsyn-{first}.html",
            f"NIOSH Pocket Guide alphabetical index (letter '{first}').")
        add("NIOSH_IDLH_list",
            "https://www.cdc.gov/niosh/idlh/intridl4.html",
            "NIOSH IDLH index.")
        add("OSHA_chemicaldata_search",
            f"https://www.osha.gov/chemicaldata/search?search={name_q}",
            "OSHA chemical data search.")
        add("ILO_ICSC_search",
            (f"https://www.ilo.org/dyn/icsc/showcard.listCards3?p_lang=en&p_search_text={cas_q or name_q}"),
            "ILO ICSC card lookup (CAS preferred).")
        add("Canada_DSL_search",
            f"https://pollution-waste.canada.ca/substances-search/?query={name_q}",
            "Canada DSL / substances search.")
        add("ECHA_infocard_search",
            f"https://echa.europa.eu/search-for-chemicals?q={name_q}",
            "ECHA search for chemicals (InfoCard).")
        add("ECHA_brief_profile",
            f"https://echa.europa.eu/brief-profile/-/briefprofile/{name_q}",
            "ECHA brief profile (requires ECHA's internal ID; the search URL is the reliable entry).")
        add("CONCAWE_search",
            f"https://www.concawe.eu/?s={name_q}",
            "CONCAWE site search (petroleum tox reports).")
        add("Silent_Spring_search",
            f"https://www.silentspring.org/?s={name_q}",
            "Silent Spring Institute site search.")

        # ── International / aggregators ──
        add("WHO_INCHEM_search",
            f"https://www.google.com/search?q=site%3Ainchem.org+{name_q}",
            "WHO INCHEM has no internal search; Google site: query is canonical.")
        add("WHO_IPCS_search",
            f"https://www.google.com/search?q=site%3Awho.int+IPCS+{name_q}",
            "WHO IPCS site: search.")
        add("eChemPortal_search",
            f"https://www.echemportal.org/echemportal/substance-search?searchValue={name_q}",
            "OECD eChemPortal substance search.")
        add("OECD_QSAR_Toolbox",
            "https://qsartoolbox.org/",
            "OECD QSAR Toolbox (desktop app; landing page only).")

        # ── Australia / Asia ──
        add("Australia_HCIS_search",
            f"https://hcis.safeworkaustralia.gov.au/HazardousChemical/Search?query={name_q}",
            "Australia HCIS hazardous chemicals search.")
        add("Australia_SafeWork_search",
            f"https://www.safeworkaustralia.gov.au/search?search={name_q}",
            "Safe Work Australia site search.")
        add("AICIS_search",
            f"https://www.industrialchemicals.gov.au/search?keywords={name_q}",
            "Australia AICIS industrial chemicals search.")
        add("Japan_NITE_search",
            f"https://www.nite.go.jp/en/chem/chrip/chrip_search/systemTop",
            "Japan NITE CHRIP (search form; submit manually).")
        add("Japan_PRTR",
            "https://www.env.go.jp/chemi/prtr/risk0.html",
            "Japan PRTR landing (navigate by chemical class).")
        add("Korea_MOE_CHEMP",
            "https://icis.me.go.kr/",
            "Korea MOE ICIS portal (Korean UI; search by CAS).")
        add("Haz_Map_search",
            f"https://haz-map.com/Agents?name={name_q}",
            "Haz-Map occupational agent search.")
        add("CPDB_index",
            "https://files.toxplanet.com/cpdb/index.html",
            "Carcinogenic Potency Database index (HTML tables by chemical).")

        # ── Chemical identity ──
        add("PubChem_search",
            f"https://pubchem.ncbi.nlm.nih.gov/#query={name_q}",
            "PubChem search (use resolve_chemical for structured identity).")
        if cas_plain:
            add("PubChem_by_CAS",
                f"https://pubchem.ncbi.nlm.nih.gov/#query={cas_q}",
                "PubChem search by CAS.")
        return out


# ══════════════════════════════════════════════════════════════════════════
#  EXTRACTION LEDGER  — per-session traceability for regulatory-grade reports
# ══════════════════════════════════════════════════════════════════════════
