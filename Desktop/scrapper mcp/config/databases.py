"""
config.databases — Curated toxicology database registry.

Source: mvp1_databases_final.xlsx (SciToxSynthesis).
ALL_DATABASES is the concatenation of HIGH + MED tier entries. When you add a
new data source, add it here — the rest of the pipeline picks it up
automatically via pipeline.urls.DatabaseURLBuilder.
"""
from __future__ import annotations


HIGH_VALUE_DATABASES: list[dict[str, str]] = [
    {"name": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
     "notes": "Full texts + free PDFs. Filter by Free Full Text.", "tier": "HIGH"},
    {"name": "PMC Full Text", "url": "https://www.ncbi.nlm.nih.gov/pmc/",
     "notes": "Full text XML for most papers. Best machine-readable source.", "tier": "HIGH"},
    {"name": "Semantic Scholar", "url": "https://www.semanticscholar.org/",
     "notes": "PDFs for many papers. Good for open-access versions.", "tier": "HIGH"},
    {"name": "NTP", "url": "https://ntp.niehs.nih.gov/",
     "notes": "Full texts + PDFs. Excellent for carcinogenicity, repro, dev tox.", "tier": "HIGH"},
    {"name": "WHO INCHEM", "url": "https://inchem.org/",
     "notes": "Covers IARC, EHC, ICSC, JECFA, PIMS. Deep regulatory tox.", "tier": "HIGH"},
    {"name": "WHO IPCS", "url": "https://www.who.int/teams/environment-climate-change-and-health/chemical-safety",
     "notes": "Comprehensive WHO chemical safety PDFs.", "tier": "HIGH"},
    {"name": "ECHA", "url": "https://echa.europa.eu/information-on-chemicals",
     "notes": "Registration Dossier — huge amount of study data.", "tier": "HIGH"},
]

MEDIUM_VALUE_DATABASES: list[dict[str, str]] = [
    {"name": "EuropePMC", "url": "https://europepmc.org/",
     "notes": "Only free full texts. Strong on preprints.", "tier": "MED"},
    {"name": "OpenAlex", "url": "https://openalex.org/works",
     "notes": "PDFs, citation counts, related papers. Queried via find_papers_openalex.", "tier": "MED"},
    {"name": "ATSDR Toxicological Profiles", "url": "https://www.atsdr.cdc.gov/toxicological-profiles/about/",
     "notes": "Per-chemical full toxicological review documents. Auto-fetched by fetch_agency_profile.", "tier": "MED"},
    {"name": "ATSDR MRLs", "url": "https://www.atsdr.cdc.gov/minimal-risk-levels/php/about/",
     "notes": "Acute/intermediate/chronic MRLs with supporting evidence.", "tier": "MED"},
    {"name": "CalEPA Prop 65 List", "url": "https://oehha.ca.gov/proposition-65/proposition-65-list",
     "notes": "Inhalation data, carcinogenicity, reproductive. Auto-fetched by fetch_agency_profile.", "tier": "MED"},
    {"name": "Canada DSL", "url": "https://pollution-waste.canada.ca/substances-search/",
     "notes": "Assessment documents by chemical name/CAS.", "tier": "MED"},
    {"name": "CONCAWE", "url": "https://www.concawe.eu/",
     "notes": "Petroleum/petrochemical exposure + environmental data.", "tier": "MED"},
    {"name": "NIOSH Pocket Guide", "url": "https://www.cdc.gov/niosh/npg/",
     "notes": "OELs, IDLH, physical/chemical properties, health hazards.", "tier": "MED"},
    {"name": "NIOSH IDLH", "url": "https://www.cdc.gov/niosh/idlh/default.html",
     "notes": "IDLH values with full supporting documentation.", "tier": "MED"},
    {"name": "OSHA Chemical Sampling", "url": "https://www.osha.gov/chemicaldata/sampling-analytical-methods",
     "notes": "Sampling methods, PELs, analytical methods.", "tier": "MED"},
    {"name": "Australia HCIS", "url": "https://hcis.safeworkaustralia.gov.au/",
     "notes": "Hazardous chemicals, TWA, SDS-level PDFs.", "tier": "MED"},
    {"name": "Australia Safe Work", "url": "https://www.safeworkaustralia.gov.au/",
     "notes": "WES (Workplace Exposure Standards).", "tier": "MED"},
    {"name": "Japan PRTR", "url": "https://www.env.go.jp/chemi/prtr/risk0.html",
     "notes": "Japanese Pollutant Release and Transfer Register — chemical hazard data.", "tier": "MED"},
    {"name": "ILO ICSC", "url": "https://www.ilo.org/dyn/icsc",
     "notes": "International Chemical Safety Cards — direct card search by CAS.", "tier": "MED"},
    {"name": "OECD eChemPortal", "url": "https://www.echemportal.org/echemportal/",
     "notes": "Aggregated OECD chemical assessments across jurisdictions.", "tier": "MED"},
    {"name": "Haz-Map", "url": "https://haz-map.com/",
     "notes": "Occupational health DB — chemical-to-disease links.", "tier": "MED"},
    {"name": "Unpaywall", "url": "https://unpaywall.org/",
     "notes": "OA PDF finder by DOI — surface free versions behind paywalls.", "tier": "MED"},
    {"name": "Zenodo MultiEndpointTox 2.1.0", "url": "https://zenodo.org/records/19536013",
     "notes": "Multi-endpoint toxicity dataset (QSAR-ready).", "tier": "MED"},
    {"name": "CPDB", "url": "https://files.toxplanet.com/cpdb/index.html",
     "notes": "Carcinogenic Potency Database — TD50 values.", "tier": "MED"},
    {"name": "Korea MOE", "url": "https://icis.me.go.kr/",
     "notes": "Korean Ministry of Environment chemical information.", "tier": "MED"},
    {"name": "Japan NITE (CHRIP)", "url": "https://www.nite.go.jp/en/index.html",
     "notes": "NITE Chemical Risk Information Platform.", "tier": "MED"},
    {"name": "Australia AICIS", "url": "https://www.industrialchemicals.gov.au/",
     "notes": "Australian Industrial Chemicals Introduction Scheme.", "tier": "MED"},
    {"name": "Silent Spring Institute", "url": "https://www.silentspring.org/",
     "notes": "Independent research on endocrine disruptors and consumer-product chemicals.", "tier": "MED"},
    {"name": "EPA IRIS", "url": "https://iris.epa.gov/",
     "notes": "EPA Integrated Risk Information System — RfD, RfC, cancer slope, unit risk. Auto-fetched by fetch_agency_profile.", "tier": "MED"},
]

ALL_DATABASES = HIGH_VALUE_DATABASES + MEDIUM_VALUE_DATABASES

CHEMICAL_IDENTITY_DATABASES: list[dict[str, str]] = [
    {"name": "PubChem", "url": "https://pubchem.ncbi.nlm.nih.gov/",
     "notes": "PRIMARY chemical identity source. CID, CAS, MW, SMILES, InChI, "
              "InChIKey, synonyms, molecular formula. Also contains ALL "
              "regulatory/toxicology data via headings. Resolves any chemical name.",
     "tier": "PRIMARY"},
]
