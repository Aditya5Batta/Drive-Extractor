"""
tools.context — ToolContext: the one object every handler receives.

Creating a ToolContext boots up every shared client (HTTP fetcher, the 34-DB
searcher, PubChem resolver, PMC harvester, agency fetcher, JATS parser,
evidence builder, report generator, LLM adapter). The MCP server creates one
per process and passes it into every dispatch call.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Any

from config.settings import CONFIG
from core.http import HTTPFetcher
from core.llm import LLMAdapter
from core.tracing import Tracer
from core.provenance import EvidenceLedger
from pipeline.scraper import Scraper, BatchProcessor
from pipeline.evidence import EvidenceBuilder
from pipeline.ledger import ExtractionLedger
from pipeline.report import ReportGenerator
from sources.pubchem import PubChemResolver
from sources.pubmed import PaperFinder
from sources.openalex import OpenAlexClient
from sources.pmc import PMCHarvester
from sources.jats import JATSHarvester
from sources.agencies import AgencyProfileFetcher
from sources.db_search import DatabaseSearcher
from sources.harvester import MultiDatabaseHarvester


@dataclass
class ToolContext:
    """Holds every shared client. Pass one instance to dispatch(name, args, ctx)."""
    fetcher: HTTPFetcher
    scraper: Scraper
    batch: BatchProcessor
    pubchem: PubChemResolver
    papers: PaperFinder
    openalex: OpenAlexClient
    pmc: PMCHarvester
    agency: AgencyProfileFetcher
    evidence_builder: EvidenceBuilder
    jats: JATSHarvester
    ledger: ExtractionLedger
    db_searcher: DatabaseSearcher
    multi_harvester: MultiDatabaseHarvester
    report_gen: ReportGenerator
    llm: LLMAdapter
    figures_dir: str
    # Observability + provenance (optional; lazily bound per build_chemical_review run)
    tracer: Tracer | None = None
    evidence_ledger: EvidenceLedger | None = None

    def bind_run(self, chemical: str, runs_dir: str = "/tmp/tox_runs") -> tuple[Tracer, EvidenceLedger]:
        """Start a new traced run: attaches a Tracer and an EvidenceLedger for `chemical`."""
        import os as _os
        _os.makedirs(runs_dir, exist_ok=True)
        self.tracer = Tracer.new(chemical, base_dir=runs_dir)
        ledger_path = _os.path.join(runs_dir, f"{self.tracer.run_id}.ledger.jsonl")
        self.evidence_ledger = EvidenceLedger(ledger_path)
        return self.tracer, self.evidence_ledger


def build_context() -> ToolContext:
    """Instantiate every shared client. Called once by server.main()."""
    fetcher = HTTPFetcher(CONFIG)
    scraper = Scraper(fetcher, CONFIG)
    batch = BatchProcessor(scraper, CONFIG)
    pubchem = PubChemResolver(fetcher)
    papers = PaperFinder(fetcher)
    openalex = OpenAlexClient(fetcher)
    pmc = PMCHarvester(fetcher, scraper)
    agency = AgencyProfileFetcher(scraper)
    evidence_builder = EvidenceBuilder(scraper)
    jats = JATSHarvester(fetcher)
    ledger = ExtractionLedger()
    db_searcher = DatabaseSearcher(fetcher, scraper, papers, openalex)
    report_gen = ReportGenerator(font_name="Times New Roman", font_size_pt=12)
    llm = LLMAdapter(http=fetcher)

    # Output dir for extracted PDF figure images — portable across OS.
    # Resolution order: $TOX_SCRAPER_FIGURES_DIR → project_root/extracted_figures
    #                   → cwd/extracted_figures → tempdir/tox_scraper_figures
    import tempfile
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or "."
    figures_candidates = [
        os.environ.get("TOX_SCRAPER_FIGURES_DIR"),
        os.path.join(project_root, "extracted_figures"),
        os.path.join(os.getcwd(), "extracted_figures"),
        os.path.join(tempfile.gettempdir(), "tox_scraper_figures"),
    ]
    figures_dir = figures_candidates[-1]  # sane fallback
    for d in figures_candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            figures_dir = d
            break
        except Exception:
            continue

    multi_harvester = MultiDatabaseHarvester(fetcher=fetcher, figures_dir=figures_dir)

    return ToolContext(
        fetcher=fetcher, scraper=scraper, batch=batch, pubchem=pubchem,
        papers=papers, openalex=openalex, pmc=pmc, agency=agency,
        evidence_builder=evidence_builder, jats=jats, ledger=ledger,
        db_searcher=db_searcher, multi_harvester=multi_harvester,
        report_gen=report_gen, llm=llm, figures_dir=figures_dir,
    )
