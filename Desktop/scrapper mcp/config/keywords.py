"""
config.keywords — Toxicology keyword bank.

Every term the pipeline searches for when scanning harvested text. Grouped
by category so a non-developer can scan the list, add or remove terms, and
see exactly what the scraper is looking for.

Usage:
    from config.keywords import ALL_KEYWORDS, KEYWORDS_BY_CATEGORY

All keywords are lowercase and matched case-insensitively by
core.keywords.KeywordSearcher.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Health endpoints
# ─────────────────────────────────────────────────────────────────────────────
CANCER_KEYWORDS = [
    "carcinogenicity", "carcinogen", "cancer", "tumor", "tumour",
    "neoplasm", "malignancy", "leukemia", "leukaemia", "lymphoma",
    "IARC", "classification group 1", "group 2a", "group 2b",
    "mutagenic", "mutagenicity", "genotoxicity", "clastogenic",
]

REPRODUCTIVE_KEYWORDS = [
    "reproductive toxicity", "reproductive", "fertility", "teratogenic",
    "developmental toxicity", "embryotoxic", "fetotoxic", "fetus",
    "pregnancy", "gestational", "sperm", "ovary", "testis", "testicular",
]

NEURO_KEYWORDS = [
    "neurotoxicity", "neurotoxic", "neurological", "CNS depression",
    "cognitive", "memory impairment", "peripheral neuropathy",
    "encephalopathy", "parkinsonism",
]

HEPATIC_KEYWORDS = [
    "hepatotoxicity", "hepatic", "liver damage", "hepatocyte",
    "cirrhosis", "steatosis", "liver enzyme", "ALT elevation", "AST elevation",
]

RENAL_KEYWORDS = [
    "nephrotoxicity", "renal toxicity", "kidney damage", "tubular necrosis",
    "proteinuria", "glomerular",
]

RESPIRATORY_KEYWORDS = [
    "respiratory toxicity", "pulmonary", "lung", "pneumonitis",
    "asthma", "bronchitis", "emphysema", "pulmonary edema",
]

IMMUNO_KEYWORDS = [
    "immunotoxicity", "immunosuppression", "sensitization", "allergic",
    "hypersensitivity", "contact dermatitis",
]

HEMATOLOGIC_KEYWORDS = [
    "hematotoxicity", "haematotoxicity", "bone marrow", "aplastic anemia",
    "aplastic anaemia", "pancytopenia", "neutropenia", "thrombocytopenia",
    "myelodysplastic",
]

CARDIOVASCULAR_KEYWORDS = [
    "cardiotoxicity", "cardiac", "arrhythmia", "myocardial",
    "hypertension", "vasoconstriction",
]

ENDOCRINE_KEYWORDS = [
    "endocrine disruption", "thyroid", "estrogenic", "androgenic",
    "hormone", "anti-androgen", "adrenal",
]

# ─────────────────────────────────────────────────────────────────────────────
# Exposure routes
# ─────────────────────────────────────────────────────────────────────────────
ROUTE_KEYWORDS = [
    "inhalation", "oral", "dermal", "subcutaneous", "intravenous",
    "intraperitoneal", "ingestion", "dietary exposure", "drinking water",
    "occupational exposure",
]

# ─────────────────────────────────────────────────────────────────────────────
# Pharmacokinetics / metabolism
# ─────────────────────────────────────────────────────────────────────────────
PK_KEYWORDS = [
    "absorption", "distribution", "metabolism", "excretion",
    "ADME", "half-life", "bioavailability", "cytochrome P450",
    "CYP2E1", "CYP1A1", "CYP3A4", "glucuronidation", "sulfation",
    "conjugation", "phase I", "phase II",
]

# ─────────────────────────────────────────────────────────────────────────────
# Study design + regulatory terms
# ─────────────────────────────────────────────────────────────────────────────
STUDY_KEYWORDS = [
    "NOAEL", "LOAEL", "LD50", "LC50", "BMD", "BMDL", "TDI", "ADI",
    "RfD", "RfC", "MRL", "OEL", "PEL", "TLV", "IDLH", "TWA", "STEL",
    "unit risk", "cancer slope factor", "dose-response",
    "chronic", "subchronic", "acute",
]

SPECIES_KEYWORDS = [
    "human", "rat", "mouse", "rabbit", "dog", "monkey", "guinea pig",
    "zebrafish", "in vitro", "in vivo", "epidemiological",
]

# ─────────────────────────────────────────────────────────────────────────────
# Regulatory / agency mentions
# ─────────────────────────────────────────────────────────────────────────────
AGENCY_KEYWORDS = [
    "IARC", "NTP", "ATSDR", "EPA", "IRIS", "OEHHA", "NIOSH", "OSHA",
    "WHO", "IPCS", "ECHA", "REACH", "Proposition 65", "Prop 65",
    "JECFA", "EFSA", "FDA", "Health Canada",
]

# ─────────────────────────────────────────────────────────────────────────────
# Aggregated views
# ─────────────────────────────────────────────────────────────────────────────
KEYWORDS_BY_CATEGORY: dict[str, list[str]] = {
    "cancer": CANCER_KEYWORDS,
    "reproductive": REPRODUCTIVE_KEYWORDS,
    "neuro": NEURO_KEYWORDS,
    "hepatic": HEPATIC_KEYWORDS,
    "renal": RENAL_KEYWORDS,
    "respiratory": RESPIRATORY_KEYWORDS,
    "immuno": IMMUNO_KEYWORDS,
    "hematologic": HEMATOLOGIC_KEYWORDS,
    "cardiovascular": CARDIOVASCULAR_KEYWORDS,
    "endocrine": ENDOCRINE_KEYWORDS,
    "route": ROUTE_KEYWORDS,
    "pharmacokinetics": PK_KEYWORDS,
    "study_design": STUDY_KEYWORDS,
    "species": SPECIES_KEYWORDS,
    "agency": AGENCY_KEYWORDS,
}

# Flat list — every keyword, no duplicates, lowercase.
ALL_KEYWORDS: list[str] = sorted({
    kw.lower()
    for group in KEYWORDS_BY_CATEGORY.values()
    for kw in group
})


def keywords_for(category: str | None = None) -> list[str]:
    """Return the keyword list for a category, or ALL_KEYWORDS if None."""
    if category is None:
        return list(ALL_KEYWORDS)
    return list(KEYWORDS_BY_CATEGORY.get(category, []))
