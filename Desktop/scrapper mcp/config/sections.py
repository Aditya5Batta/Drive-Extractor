"""
config.sections — 29-section toxicology report schema.

Single source of truth for:
  • LITERATURE_QUERY_TEMPLATES — exact queries used against PubMed / EuropePMC /
    OpenAlex / Semantic Scholar. Each template is formatted with .format(name=..., cas=...).
  • LITERATURE_SECTIONS        — ordered list of the 29 sections every report contains.
  • SECTION_KEYWORDS           — per-section keyword bank. Drives BOTH evidence
    routing (which section a hit belongs to) and section-specific ranking.

All keywords are lowercase. Matching is case-insensitive substring.

Consumers:
  - pipeline.harvest  → runs the query templates, routes hits to sections
  - pipeline.review   → renders sections in LITERATURE_SECTIONS order
  - pipeline.audit    → checks coverage (did every section get ≥1 record?)
"""
from __future__ import annotations


# ═════════════════════════════════════════════════════════════════════════════
#  LITERATURE_QUERY_TEMPLATES — formatted with .format(name=..., cas=...)
# ═════════════════════════════════════════════════════════════════════════════
LITERATURE_QUERY_TEMPLATES = [
    # ── General toxicology & safety ──
    '"{name}" safety human health',
    '"{name}" health effects review',
    '"{name}" toxicology review',
    '"{name}" hazard assessment',
    '"{name}" risk assessment human',
    '"{cas}" toxicity',
    '"{cas}" health effects',
    '"{name}" toxicological profile',
    '"{name}" safety data sheet toxicity',
    # ── Chemical properties ──
    '"{name}" chemical properties',
    '"{name}" solubility vapor pressure boiling point',
    '"{name}" molecular weight density physical properties',
    # ── Environmental fate & behavior ──
    '"{name}" environmental fate',
    '"{name}" environmental behavior degradation',
    '"{name}" bioaccumulation',
    '"{name}" photodegradation atmospheric lifetime',
    '"{name}" soil groundwater contamination persistence',
    # ── Toxicokinetics / ADME ──
    '"{name}" toxicokinetics',
    '"{name}" absorption distribution metabolism excretion',
    '"{name}" ADME pharmacokinetics',
    '"{name}" metabolic pathway biotransformation',
    '"{name}" CYP2E1 metabolic pathway',
    '"{name}" metabolite urinary biomarker',
    '"{name}" muconic acid phenylmercapturic acid',
    '"{name}" biomonitoring urinary metabolite',
    # ── Exposure sources ──
    '"{name}" exposure sources',
    '"{name}" occupational exposure',
    '"{name}" consumer product exposure',
    '"{name}" environmental exposure',
    '"{name}" water contamination',
    '"{name}" indoor air exposure',
    '"{name}" paint glue solvent household',
    '"{name}" petroleum refinery worker',
    '"{name}" gasoline service station attendant',
    '"{name}" rubber manufacturing industry',
    '"{name}" tobacco smoke secondhand',
    '"{name}" vehicle exhaust emission',
    '"{name}" personal air monitoring badge',
    # ── Acute toxicity endpoints ──
    '"{name}" acute health effects',
    '"{name}" acute toxicity symptoms',
    '"{name}" acute oral toxicity LD50',
    '"{name}" acute inhalation toxicity LC50',
    '"{name}" acute dermal toxicity',
    '"{name}" skin irritation corrosion',
    '"{name}" eye irritation serious damage',
    '"{name}" skin sensitization allergy',
    # ── Chronic / cancer / blood effects ──
    '"{name}" chronic health effects',
    '"{name}" carcinogen',
    '"{name}" carcinogenicity mechanism',
    '"{name}" leukemia risk',
    '"{name}" aplastic anemia',
    '"{name}" bone marrow toxicity',
    '"{name}" lymphoma myeloma',
    '"{name}" hematotoxicity blood disorder',
    '"{name}" myelodysplastic syndrome',
    '"{name}" pancytopenia thrombocytopenia',
    # ── Genotoxicity / mutagenicity ──
    '"{name}" genotoxicity mutagenicity',
    '"{name}" genotoxicity in vitro',
    '"{name}" genotoxicity in vivo',
    '"{name}" mutagenicity Ames test',
    '"{name}" DNA damage chromosome aberration',
    '"{name}" micronucleus assay clastogenicity',
    '"{name}" oxidative stress metabolism',
    # ── Neurotoxicity ──
    '"{name}" neurotoxicity',
    '"{name}" neurotoxic effects nervous system',
    '"{name}" central nervous system depression',
    '"{name}" peripheral neuropathy',
    '"{name}" cognitive impairment neurological',
    # ── Immunotoxicity ──
    '"{name}" immunotoxicity',
    '"{name}" immune system effects',
    '"{name}" immunosuppression lymphocyte',
    '"{name}" immune function antibody',
    # ── Reproductive / developmental toxicity ──
    '"{name}" reproductive developmental toxicity',
    '"{name}" reproductive toxicity fertility',
    '"{name}" developmental toxicity teratogenicity',
    '"{name}" pregnancy fetal development',
    '"{name}" children pediatric exposure',
    '"{name}" endocrine disruption hormonal',
    # ── Organ-specific toxicity ──
    '"{name}" hepatotoxicity liver damage',
    '"{name}" nephrotoxicity kidney',
    '"{name}" cardiotoxicity cardiovascular',
    '"{name}" respiratory toxicity pulmonary',
    '"{name}" inhalation exposure lung',
    '"{name}" dermal absorption skin',
    '"{name}" oral ingestion gastrointestinal',
    # ── Dose-response / thresholds ──
    '"{name}" dose response low dose',
    '"{name}" NOAEL LOAEL benchmark dose',
    '"{name}" cancer slope factor unit risk',
    '"{name}" reference dose concentration RfD RfC',
    '"{name}" uncertainty factor safety margin',
    # ── Regulations ──
    '"{name}" OSHA permissible exposure limit',
    '"{name}" EPA regulation guideline',
    '"{name}" WHO guideline',
    '"{name}" IARC classification',
    '"{name}" regulatory standard',
    '"{name}" EPA risk management program',
    '"{name}" ACGIH TLV threshold limit',
    '"{name}" NIOSH REL recommended exposure',
    '"{name}" drinking water standard MCL',
    '"{name}" air quality standard ambient',
    # ── Risk assessment ──
    '"{name}" risk assessment dose response',
    '"{name}" vulnerable populations children',
    '"{name}" genetic susceptibility',
    '"{name}" TSCA risk evaluation',
    '"{name}" exposure assessment modeling',
    # ── Mitigation ──
    '"{name}" exposure mitigation prevention',
    '"{name}" workplace safety measures',
    '"{name}" alternative solvents substitution',
    '"{name}" engineering control ventilation PPE',
    '"{name}" regulatory change standard update',
    '"{name}" environmental remediation cleanup',
    '"{name}" soil contamination groundwater',
    '"{name}" environmental justice community',
    '"{name}" emission inventory source apportionment',
    # ── Epidemiology & study types ──
    '"{name}" recent research findings',
    '"{name}" epidemiology study',
    '"{name}" biomarker exposure',
    '"{name}" cohort study mortality',
    '"{name}" case control study cancer',
    '"{name}" meta-analysis systematic review',
    '"{name}" animal study rat mouse',
    '"{name}" in vitro genotoxicity assay',
    # ── Year-targeted (force recent papers) ──
    '{name} toxicity 2024',
    '{name} toxicity 2025',
    '{name} toxicity 2023',
    '{name} health effects 2024',
    '{name} health effects 2025',
    '{name} exposure assessment 2024',
    '{name} carcinogenicity 2024',
    '{name} occupational health 2024',
    '{name} environmental contamination 2024',
    '{name} regulatory update 2024 2025',
]


# ═════════════════════════════════════════════════════════════════════════════
#  LITERATURE_SECTIONS — 29 canonical section IDs, in report order
# ═════════════════════════════════════════════════════════════════════════════
LITERATURE_SECTIONS = [
    "chemical_properties",
    "environmental_behavior",
    "toxicokinetics",
    "exposure_consumer",
    "exposure_occupational",
    "exposure_environmental",
    "exposure_water_soil",
    "health_effects_acute",
    "health_effects_chronic",
    "genotoxicity_mutagenicity",
    "neurotoxicity",
    "immunotoxicity",
    "reproductive_developmental",
    "organ_specific_toxicity",
    "health_effects_occupational",
    "regulations_international",
    "regulations_osha",
    "regulations_epa",
    "regulations_community",
    "regulations_legal",
    "risk_assessment_occupational",
    "risk_assessment_vulnerable",
    "risk_assessment_genetic",
    "risk_assessment_regulatory",
    "mitigation_strategies",
    "mitigation_workplace",
    "mitigation_regulatory",
    "mitigation_community",
    "recent_research",
]


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION_TITLES — display titles for the composer. Keys match LITERATURE_SECTIONS.
# ═════════════════════════════════════════════════════════════════════════════
SECTION_TITLES: dict[str, str] = {
    "chemical_properties":          "Chemical Properties",
    "environmental_behavior":       "Environmental Behaviour",
    "toxicokinetics":               "Toxicokinetics (ADME)",
    "exposure_consumer":            "Consumer-Product Exposure",
    "exposure_occupational":        "Occupational Exposure",
    "exposure_environmental":       "Environmental Exposure",
    "exposure_water_soil":          "Water and Soil Contamination",
    "health_effects_acute":         "Acute Health Effects",
    "health_effects_chronic":       "Chronic Health Effects",
    "genotoxicity_mutagenicity":    "Genotoxicity and Mutagenicity",
    "neurotoxicity":                "Neurotoxicity",
    "immunotoxicity":               "Immunotoxicity",
    "reproductive_developmental":   "Reproductive and Developmental Toxicity",
    "organ_specific_toxicity":      "Organ-Specific Toxicity",
    "health_effects_occupational":  "Occupational Health Effects",
    "regulations_international":    "International Guidelines",
    "regulations_osha":             "Occupational Exposure Limits",
    "regulations_epa":              "U.S. EPA Assessments",
    "regulations_community":        "Community and Public-Health Regulation",
    "regulations_legal":            "Legal and Compliance Framework",
    "risk_assessment_occupational": "Occupational Risk Assessment",
    "risk_assessment_vulnerable":   "Risk Assessment — Vulnerable Populations",
    "risk_assessment_genetic":      "Genetic Susceptibility and Risk",
    "risk_assessment_regulatory":   "Regulatory Risk Evaluation",
    "mitigation_strategies":        "Exposure-Reduction Strategies",
    "mitigation_workplace":         "Workplace Safety Measures",
    "mitigation_regulatory":        "Regulatory Change and Policy Mitigation",
    "mitigation_community":         "Community-Level Interventions",
    "recent_research":              "Recent Research Findings",
}


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION_GROUPS — H1 "chapters" that group related sections for the composer.
#  Order is the order the final docx renders.
# ═════════════════════════════════════════════════════════════════════════════
SECTION_GROUPS: list[tuple[str, list[str]]] = [
    ("Chemical Properties",          ["chemical_properties"]),
    ("Environmental Behaviour",      ["environmental_behavior"]),
    ("Toxicokinetics",               ["toxicokinetics"]),
    ("Sources of Exposure", [
        "exposure_consumer",
        "exposure_occupational",
        "exposure_environmental",
        "exposure_water_soil",
    ]),
    ("Health Effects", [
        "health_effects_acute",
        "health_effects_chronic",
        "genotoxicity_mutagenicity",
        "neurotoxicity",
        "immunotoxicity",
        "reproductive_developmental",
        "organ_specific_toxicity",
        "health_effects_occupational",
    ]),
    ("Regulations and Guidelines", [
        "regulations_international",
        "regulations_osha",
        "regulations_epa",
        "regulations_community",
        "regulations_legal",
    ]),
    ("Risk Assessment", [
        "risk_assessment_occupational",
        "risk_assessment_vulnerable",
        "risk_assessment_genetic",
        "risk_assessment_regulatory",
    ]),
    ("Mitigation and Prevention", [
        "mitigation_strategies",
        "mitigation_workplace",
        "mitigation_regulatory",
        "mitigation_community",
    ]),
    ("Recent Research Findings",     ["recent_research"]),
]


# ═════════════════════════════════════════════════════════════════════════════
#  SECTION_KEYWORDS — per-section keyword bank (lowercase, substring match)
# ═════════════════════════════════════════════════════════════════════════════
SECTION_KEYWORDS: dict[str, list[str]] = {
    "chemical_properties": [
        "chemical properties", "physical properties", "molecular weight", "boiling point",
        "melting point", "vapor pressure", "solubility", "water solubility", "density",
        "molecular formula", "flammable", "flash point", "octanol-water", "log p",
        "partition coefficient", "refractive index", "specific gravity", "appearance",
        "odor", "viscosity", "surface tension", "heat of vaporization",
        "henry's law", "autoignition", "explosive limit", "vapor density",
        "freezing point", "critical temperature",
        "molecular structure", "aromatic", "combustion", "reactivity", "halogen", "oxidiz",
    ],
    "environmental_behavior": [
        "environmental fate", "environmental transport", "degradation", "biodegradation",
        "photolysis", "hydrolysis", "volatilization", "bioaccumulation", "bioconcentration",
        "half-life", "atmospheric", "hydroxyl radical", "soil", "groundwater",
        "surface water", "persistence", "partitioning", "evaporation",
        "air-water", "koc", "sorption", "aquatic", "terrestrial",
        "environmental behavior", "environmental distribution", "photodegradation",
        "atmospheric lifetime", "environmental monitoring", "ambient concentration",
        "food chain", "accumulate", "breakdown", "atmosphere",
    ],
    "toxicokinetics": [
        "toxicokinetics", "pharmacokinetics", "absorption", "distribution",
        "metabolism", "excretion", "adme", "biotransformation",
        "metabolic pathway", "cyp2e1", "cyp1a2", "metabolite",
        "phenol", "muconic acid", "catechol", "hydroquinone",
        "phenylmercapturic", "benzene oxide", "epoxide",
        "first-pass", "bioavailability", "clearance", "half-life",
        "urinary metabolite", "blood concentration", "tissue distribution",
    ],
    "exposure_consumer": [
        "consumer product", "consumer exposure", "paint", "glue", "adhesive",
        "cleaning product", "detergent", "solvent", "household", "indoor",
        "furniture", "cosmetic", "wax", "ink", "dye", "lacquer", "varnish",
        "building material", "off-gas", "indoor air quality", "home",
        "domestic use", "residential", "personal care", "consumer",
        "cigarette", "tobacco", "emission", "ventilation",
    ],
    "exposure_occupational": [
        "occupational exposure", "workplace", "worker", "industrial",
        "petrochemical", "refinery", "manufacturing", "factory", "laboratory",
        "gas station", "firefighter", "printing", "rubber", "shoe",
        "petroleum", "coke", "coal", "chemical plant", "oil",
        "dermal exposure", "inhalation exposure", "work environment",
        "occupational setting", "industrial hygiene", "occupational",
        "service station", "attendant", "mechanic", "painter",
    ],
    "exposure_environmental": [
        "environmental exposure", "ambient air", "outdoor air", "emission",
        "vehicle exhaust", "industrial emission", "tobacco smoke", "cigarette",
        "urban", "traffic", "atmospheric concentration", "air pollution",
        "air quality", "general population", "community exposure",
        "personal exposure", "outdoor concentration",
    ],
    "exposure_water_soil": [
        "water contamination", "soil contamination", "groundwater", "drinking water",
        "underground storage tank", "fuel spill", "leachate", "runoff",
        "water quality", "effluent", "aquifer", "well water", "tap water",
        "water supply", "water treatment", "soil remediation",
    ],
    "health_effects_acute": [
        "acute effect", "acute exposure", "acute toxicity", "dizziness", "headache",
        "nausea", "drowsiness", "unconsciousness", "respiratory failure",
        "irritation", "narcosis", "central nervous system depression",
        "short-term exposure", "acute symptom", "immediate effect",
        "inhalation effect", "skin contact", "eye contact",
        "acute oral toxicity", "ld50", "lc50",
        "acute inhalation toxicity", "acute dermal toxicity",
        "skin irritation", "eye irritation", "corrosion",
        "skin sensitization", "allergic", "dermatitis",
        "respiratory irritation", "cough", "dyspnea",
    ],
    "health_effects_chronic": [
        "chronic exposure", "long-term", "carcinogen", "leukemia", "lymphoma",
        "aplastic anemia", "bone marrow", "hematotoxicity", "myelodysplastic",
        "pancytopenia", "cancer", "tumor",
        "chronic effect", "long term health", "prolonged exposure",
        "hematopoietic", "blood disorder", "malignancy",
        "carcinogenicity", "carcinogenic", "oncogenic",
        "acute myeloid leukemia", "aml", "non-hodgkin",
        "multiple myeloma", "clonal hematopoiesis",
        "thrombocytopenia", "anemia", "neutropenia", "white blood cell",
        "red blood cell", "platelet", "hemoglobin",
    ],
    "genotoxicity_mutagenicity": [
        "genotoxicity", "genotoxic", "mutagenicity", "mutagenic", "mutagen",
        "dna damage", "dna adduct", "chromosome aberration", "micronucleus",
        "ames test", "clastogenic", "clastogenicity", "aneuploidy",
        "sister chromatid", "comet assay", "strand break",
        "in vitro genotoxicity", "in vivo genotoxicity",
        "gene mutation", "point mutation", "frameshift",
        "oxidative dna damage", "8-ohdg", "reactive oxygen",
    ],
    "neurotoxicity": [
        "neurotoxicity", "neurotoxic", "nervous system", "neurological",
        "central nervous system", "cns", "peripheral neuropathy",
        "cognitive", "neurobehavioral", "tremor", "ataxia",
        "brain", "cerebral", "encephalopathy", "neuropathy",
        "nerve conduction", "neurodevelopmental",
        "memory", "concentration", "psychomotor",
        "seizure", "convulsion", "headache", "dizziness",
    ],
    "immunotoxicity": [
        "immunotoxicity", "immunotoxic", "immune system", "immunosuppression",
        "lymphocyte", "t-cell", "b-cell", "natural killer", "nk cell",
        "antibody", "immunoglobulin", "cytokine", "inflammation",
        "immune function", "immune response", "autoimmune",
        "white blood cell", "leukocyte", "neutrophil",
        "spleen", "thymus", "bone marrow immune",
    ],
    "reproductive_developmental": [
        "reproductive toxicity", "reproductive", "fertility", "sperm",
        "developmental toxicity", "teratogenicity", "teratogenic", "birth defect",
        "pregnancy", "fetal", "embryo", "embryotoxic",
        "placenta", "breast milk", "lactation", "maternal",
        "congenital", "malformation", "prenatal", "postnatal",
        "endocrine disruption", "hormonal", "estrogen", "testosterone",
        "menstrual", "ovarian", "testicular", "spermatogenesis",
        "in utero", "gestational", "perinatal",
    ],
    "organ_specific_toxicity": [
        "hepatotoxicity", "liver damage", "liver toxicity", "hepatic",
        "nephrotoxicity", "kidney damage", "kidney toxicity", "renal",
        "cardiotoxicity", "cardiovascular", "cardiac", "heart",
        "respiratory toxicity", "pulmonary", "lung damage", "lung toxicity",
        "gastrointestinal", "gi tract", "stomach", "intestinal",
        "ocular", "ototoxicity", "hearing loss",
        "target organ", "organ toxicity", "systemic toxicity",
    ],
    "health_effects_occupational": [
        "occupational health", "occupational risk", "worker health",
        "occupational disease", "occupational cancer", "work-related",
        "industrial health", "worker safety", "workplace health",
        "occupational illness", "work exposure disease",
    ],
    "regulations_international": [
        "international guideline", "ilo", "who guideline", "iarc classification",
        "group 1 carcinogen", "international standard", "global", "convention",
        "european union", "eu directive", "eu clp", "reach", "echa",
        "world health organization", "iarc monograph",
    ],
    "regulations_osha": [
        "osha", "permissible exposure limit", "pel", "time-weighted average",
        "twa", "stel", "short-term exposure limit", "niosh", "acgih", "tlv",
        "occupational exposure limit", "oel", "rel", "recommended exposure",
        "threshold limit value", "workplace standard",
    ],
    "regulations_epa": [
        "epa", "environmental protection agency", "tsca", "risk management program",
        "rmp", "clean air act", "maximum contaminant level", "mcl",
        "ambient water quality", "naaqs", "safe drinking water",
        "national emission standard", "hazardous air pollutant",
    ],
    "regulations_community": [
        "community", "public participation", "community involvement",
        "environmental justice", "disadvantaged", "public health",
        "community safety", "neighborhood", "resident",
        "citizen", "stakeholder", "public comment", "community engagement",
        "local government", "fenceline", "disproportionate",
    ],
    "regulations_legal": [
        "legal", "liability", "employer responsibility", "compliance",
        "enforcement", "penalty", "lawsuit", "litigation",
        "obligation", "regulation compliance", "fine",
    ],
    "risk_assessment_occupational": [
        "occupational risk assessment", "exposure assessment", "dose-response",
        "risk characterization", "exposure monitoring", "biomonitoring",
        "health surveillance", "risk model", "exposure level",
        "benchmark dose", "noael", "loael", "reference concentration",
    ],
    "risk_assessment_vulnerable": [
        "vulnerable population", "children", "pregnant", "elderly",
        "fetal", "infant", "breast milk", "placenta", "susceptible",
        "pediatric", "maternal", "newborn", "neonatal", "in utero",
        "sensitive population", "age-related", "immunocompromised",
    ],
    "risk_assessment_genetic": [
        "genetic", "polymorphism", "cyp2e1", "nqo1", "gst",
        "genetic susceptibility", "metabolic variant", "enzyme",
        "genotype", "phenotype", "allele", "snp",
        "pharmacogenomic", "individual susceptibility", "genetic variation",
    ],
    "risk_assessment_regulatory": [
        "regulatory framework", "tsca", "risk evaluation", "unreasonable risk",
        "risk management", "regulatory action", "risk determination",
        "chemical regulation", "toxic substances",
        "risk-benefit", "cost-benefit", "regulatory science",
        "weight of evidence", "hazard identification", "risk governance",
    ],
    "mitigation_strategies": [
        "mitigation", "prevention", "reduce exposure", "alternative solvent",
        "substitution", "phase out", "elimination", "exposure reduction",
        "safer alternative", "reformulat", "replace",
    ],
    "mitigation_workplace": [
        "workplace safety", "engineering control", "ventilation", "ppe",
        "protective equipment", "administrative control", "training",
        "safety measure", "respiratory protection", "exhaust system",
        "personal protective", "safety program", "worker training",
    ],
    "mitigation_regulatory": [
        "regulatory change", "proposed rule", "standard update",
        "stricter regulation", "policy change", "rule change",
        "regulation update", "new standard", "revised standard",
        "rulemaking", "federal register", "final rule",
        "regulatory reform", "compliance deadline", "phase-out",
    ],
    "mitigation_community": [
        "community health", "air quality guideline", "public awareness",
        "education", "health advisory", "environmental health",
        "public education", "community program", "awareness campaign",
        "outreach", "screening program", "health literacy",
        "right to know", "community monitoring", "citizen science",
    ],
    "recent_research": [
        "recent study", "recent research", "new finding", "emerging evidence",
        "latest", "novel", "updated", "current understanding",
        "2023", "2024", "2025", "new evidence", "recently",
    ],
}


# ═════════════════════════════════════════════════════════════════════════════
#  Aggregated views + helpers
# ═════════════════════════════════════════════════════════════════════════════
ALL_SECTION_KEYWORDS: list[str] = sorted({
    kw.lower()
    for kws in SECTION_KEYWORDS.values()
    for kw in kws
})


def keywords_for_section(section_id: str) -> list[str]:
    """Return the keyword list for a section, or [] if unknown."""
    return list(SECTION_KEYWORDS.get(section_id, []))


def section_for_hit(text: str) -> list[str]:
    """
    Given a block of text, return the list of section IDs whose keyword bank
    has at least one match. Deterministic order (matches LITERATURE_SECTIONS).
    """
    low = text.lower()
    return [sec for sec in LITERATURE_SECTIONS
            if any(kw in low for kw in SECTION_KEYWORDS.get(sec, []))]


def format_queries(name: str, cas: str = "") -> list[str]:
    """Return every query template formatted with this chemical's name/CAS."""
    return [t.format(name=name, cas=cas or name) for t in LITERATURE_QUERY_TEMPLATES]


__all__ = [
    "LITERATURE_QUERY_TEMPLATES",
    "LITERATURE_SECTIONS",
    "SECTION_TITLES",
    "SECTION_GROUPS",
    "SECTION_KEYWORDS",
    "ALL_SECTION_KEYWORDS",
    "keywords_for_section",
    "section_for_hit",
    "format_queries",
]
