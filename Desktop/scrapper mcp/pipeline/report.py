"""
pipeline.report — ReportGenerator + data classes.

Composes the final .docx: cover page, chapter banners, body paragraphs,
figures (inline), tables, references, appendices. Uses python-docx.
"""
from __future__ import annotations
import datetime
import io
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import docx as _docx  # python-docx
    from docx.shared import Pt, Inches, RGBColor  # type: ignore
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK  # type: ignore
    from docx.oxml.ns import qn as _qn  # type: ignore
    from docx.oxml import OxmlElement as _OxmlElement  # type: ignore
    HAS_PYTHON_DOCX = True
except Exception:
    HAS_PYTHON_DOCX = False


@dataclass
class ReportReference:
    """A single reference entry with formatted citation and URL."""
    number: int
    authors: str = ""
    year: str = ""
    title: str = ""
    journal: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    url: str = ""
    database: str = ""          # e.g. "PubMed", "PMC", "ATSDR"

    def format(self) -> str:
        bits: list[str] = []
        if self.authors: bits.append(f"{self.authors}.")
        if self.year: bits.append(f"{self.year}.")
        if self.title: bits.append(f"{self.title}.")
        if self.journal: bits.append(f"{self.journal}.")
        if self.doi: bits.append(f"DOI: {self.doi}.")
        return " ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportSection:
    """One section of the report. Supports nested subsections.

    When ``chapter_number`` is set, a benzene-style colored chapter banner
    is rendered on its own page *before* the section's heading — this gives
    the report ten large "Chapter N" dividers matching the L-cysteine v3
    and Benzene reference documents.
    """
    title: str
    level: int = 1                  # 1 = H1, 2 = H2, 3 = H3
    paragraphs: list[str] = field(default_factory=list)
    subsections: list["ReportSection"] = field(default_factory=list)
    chapter_number: int | None = None       # if set: render Chapter banner
    chapter_subtitle: str | None = None     # optional italic subtitle line
    inline_figures: list["ReportFigure"] = field(default_factory=list)
    inline_tables: list["ReportTable"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "level": self.level,
            "paragraphs": self.paragraphs,
            "subsections": [s.to_dict() for s in self.subsections],
            "chapter_number": self.chapter_number,
            "chapter_subtitle": self.chapter_subtitle,
            "inline_figures": [f.to_dict() for f in self.inline_figures],
            "inline_tables": [t.to_dict() for t in self.inline_tables],
        }


@dataclass
class ReportFigure:
    """An embedded figure (chart, diagram, image) with caption.

    path: filesystem path to PNG/JPEG image.
    caption: figure caption string, rendered italic under the image.
    width_inches: layout width (docx Inches), defaults to 5.625".
    placement: section title at which to inject figure (matches ReportSection.title).
    """
    path: str
    caption: str = ""
    width_inches: float = 5.625
    placement: str = ""             # section title anchor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportTable:
    """A standalone data table with caption & source.

    title: short title rendered as caption italic.
    headers: column header list.
    rows: list of row lists (each row is list of cell strings).
    placement: section title anchor for injection.
    source: optional source-citation text rendered under table.
    """
    title: str
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    placement: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReportGenerator:
    """Generate a Times-New-Roman-formatted Word (.docx) literature review.

    Section structure mirrors the canonical Benzene v3 reference document
    verbatim — any chemical fed through this generator produces an output
    with exactly the same chapter skeleton so downstream reviewers can
    navigate reports uniformly regardless of which chemical was run:

      Cover page (title + CAS + structure image)
      1. Summary
      2. Scope and Methodology of This Review
      3. Key Findings at a Glance
      4. Database Coverage and Retrieval Methodology
      5. Chemical Properties
            Physical Characteristics
            Solubility and Reactivity
            Industrial Production and Uses
            Structural and Spectroscopic Characteristics
            Thermodynamic and Kinetic Data
            Production Chemistry and Industrial Processes
            Analytical Methods and Sampling
      6. Environmental Behaviour
            Atmospheric Fate / Aquatic and Soil Fate / Bioaccumulation /
            Transport / Biodegradation / Monitoring / Climate / Reporting
      7. Sources of Exposure
            Consumer Products / Occupational / Environmental / Water & Soil /
            Mitigation / Historical Trends / Food & Indirect / Traffic / Natural
      8. Health Effects
            Acute / Long-Term / Carcinogenicity / Hematotoxicity /
            Genotoxicity / Neurotoxicity / Immunotoxicity / Reproductive /
            Organ-Specific / Cohort / Respiratory-Cardiovascular /
            Skin / Oxidative Stress / Clinical / Dose-Response / Biomarkers
      9. Toxicokinetics (ADME)
            PBPK / Species differences / Metabolomics / Co-exposure
     10. Regulations and Guidelines
            International / OELs / EPA / OSHA / Environmental Standards /
            IARC-NTP / REACH / Harmonisation / Emerging Trends
     11. Risk Assessment
            Occupational / Cancer / Uncertainty / Cumulative / BMD / Site-Specific
     12. Vulnerable Populations
            Children / Pregnant / Genetic / Co-Exposure / Environmental Justice /
            Elderly / Indigenous / Specialised Occupational
     13. Mitigation and Prevention
            Strategies / Workplace / Technology / Case Studies / Substitution /
            Community / PPE
     14. Recent Research Findings
            Omics / Future Research Priorities / Concluding Perspective
     15. References
     16. Appendix A — Per-Database Extraction Counts
     17. Appendix B — Extraction Ledger (Papers Used)

    Every call with the same inputs produces byte-identical output (except
    for the /docProps creation timestamp which python-docx sets internally).
    """

    # Section titles in stable order. Mirrors the canonical Benzene v3
    # literature-review layout exactly so every chemical produces the same
    # chapter skeleton: cover → Summary → Scope/Methodology → Key Findings →
    # Database Coverage → Chemical Properties → Environmental Behaviour →
    # Sources of Exposure → Health Effects → Toxicokinetics → Regulations →
    # Risk Assessment → Vulnerable Populations → Mitigation and Prevention →
    # Recent Research Findings → References → Appendix A.
    # The caller provides content for each; the generator lays them out.
    SECTIONS: list[tuple[str, list[tuple[str, int]]]] = [
        ("Summary", []),
        ("Scope and Methodology of This Review", []),
        ("Key Findings at a Glance", []),
        ("Database Coverage and Retrieval Methodology", []),
        ("Chemical Properties", [
            ("Physical Characteristics", 2),
            ("Solubility and Reactivity", 2),
            ("Industrial Production and Uses", 2),
            ("Structural and Spectroscopic Characteristics", 2),
            ("Thermodynamic and Kinetic Data", 2),
            ("Production Chemistry and Industrial Processes", 2),
            ("Analytical Methods and Sampling", 2),
        ]),
        ("Environmental Behaviour", [
            ("Atmospheric Fate", 2),
            ("Aquatic and Soil Fate", 2),
            ("Bioaccumulation", 2),
            ("Transport and Dispersion Modelling", 2),
            ("Biodegradation and Microbial Transformation", 2),
            ("Monitoring Networks and Data Sources", 2),
            ("Climate-Change Interactions", 2),
            ("Global and Regional Reporting Frameworks", 2),
        ]),
        ("Sources of Exposure", [
            ("Consumer Products", 2),
            ("Occupational Exposure", 2),
            ("Environmental Exposure", 2),
            ("Water and Soil Contamination", 2),
            ("Mitigation of Exposure", 2),
            ("Historical Trends in Production and Exposure", 2),
            ("Food, Consumer Products, and Indirect Exposures", 2),
            ("Traffic and Urban Air Sources", 2),
            ("Natural Sources and Baseline Contributions", 2),
        ]),
        ("Health Effects", [
            ("Acute Health Effects", 2),
            ("Long-Term Health Effects", 2),
            ("Carcinogenicity", 2),
            ("Hematotoxicity", 2),
            ("Genotoxicity and Mutagenicity", 2),
            ("Neurotoxicity", 2),
            ("Immunotoxicity", 2),
            ("Reproductive and Developmental Toxicity", 2),
            ("Organ-Specific Toxicity", 2),
            ("Key Pivotal Cohort Studies", 2),
            ("Non-Malignant Respiratory and Cardiovascular Effects", 2),
            ("Skin and Mucous-Membrane Effects", 2),
            ("Oxidative Stress and Epigenetic Effects", 2),
            ("Clinical Presentation and Diagnosis", 2),
            ("Dose-Response at Low Cumulative Exposures", 2),
            ("Biomarkers of Effect and Susceptibility", 2),
        ]),
        ("Toxicokinetics (ADME)", [
            ("PBPK Modelling of Disposition", 2),
            ("Species and Interindividual Differences", 2),
            ("Metabolomic Profiling and Dose Reconstruction", 2),
            ("Co-Exposure Interactions Affecting Kinetics", 2),
        ]),
        ("Regulations and Guidelines", [
            ("International Guidelines", 2),
            ("Occupational Exposure Limits", 2),
            ("U.S. EPA Assessments", 2),
            ("OSHA Standards", 2),
            ("Environmental Standards", 2),
            ("IARC, NTP, and Hazard Classification", 2),
            ("REACH Authorisation and Restrictions", 2),
            ("International Harmonisation and Transboundary Regulatory Issues", 2),
            ("Emerging Regulatory Trends and Policy Outlook", 2),
        ]),
        ("Risk Assessment", [
            ("Occupational Exposure Assessment", 2),
            ("Cancer Risk Calculation and Risk Communication", 2),
            ("Uncertainty and Probabilistic Risk Characterisation", 2),
            ("Cumulative and Aggregate Exposure Considerations", 2),
            ("Benchmark Dose and Threshold-of-Toxicological-Concern Approaches", 2),
            ("Site-Specific Human-Health Risk Assessment Frameworks", 2),
        ]),
        ("Vulnerable Populations", [
            ("Children", 2),
            ("Pregnant Women and Developing Foetus", 2),
            ("Genetic Factors Influencing Susceptibility", 2),
            ("Co-Exposure Contexts", 2),
            ("Environmental Justice and Low-Resource Populations", 2),
            ("Elderly and Medically Vulnerable Populations", 2),
            ("Indigenous and Remote Communities", 2),
            ("Occupational Populations with Specialised Exposure Scenarios", 2),
        ]),
        ("Mitigation and Prevention", [
            ("Strategies for Reducing Exposure", 2),
            ("Workplace Safety Measures", 2),
            ("Technological Innovations", 2),
            ("Case Studies — Successful Occupational Controls", 2),
            ("Technological Innovations and Substitution", 2),
            ("Community-Level Interventions and Risk Communication", 2),
            ("Personal Protective Equipment and Hierarchy of Controls", 2),
        ]),
        ("Recent Research Findings", [
            ("Omics-Based Signatures of Exposure", 2),
            ("Future Research Priorities", 2),
            ("Concluding Perspective", 2),
        ]),
    ]

    def __init__(self, font_name: str = "Times New Roman",
                 font_size_pt: int = 12) -> None:
        self.font_name = font_name
        self.font_size_pt = font_size_pt

    def _style_run(self, run, *, bold: bool = False, italic: bool = False,
                   size: int | None = None, color: tuple[int, int, int] | None = None) -> None:
        run.font.name = self.font_name
        run.font.size = Pt(size if size is not None else self.font_size_pt)
        run.bold = bold
        run.italic = italic
        if color is not None:
            run.font.color.rgb = RGBColor(*color)
        # Force Times New Roman for Asian-script runs too (Word default otherwise)
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(_qn("w:rFonts"))
        if rFonts is None:
            rFonts = _OxmlElement("w:rFonts")
            rPr.append(rFonts)
        rFonts.set(_qn("w:ascii"), self.font_name)
        rFonts.set(_qn("w:hAnsi"), self.font_name)
        rFonts.set(_qn("w:cs"), self.font_name)
        rFonts.set(_qn("w:eastAsia"), self.font_name)

    def _add_hyperlink(self, paragraph, url: str, text: str) -> None:
        """Add a clickable hyperlink run to a paragraph."""
        part = paragraph.part
        r_id = part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True,
        )
        hyperlink = _OxmlElement("w:hyperlink")
        hyperlink.set(_qn("r:id"), r_id)
        new_run = _OxmlElement("w:r")
        rPr = _OxmlElement("w:rPr")
        rFonts = _OxmlElement("w:rFonts")
        rFonts.set(_qn("w:ascii"), self.font_name)
        rFonts.set(_qn("w:hAnsi"), self.font_name)
        rFonts.set(_qn("w:cs"), self.font_name)
        rFonts.set(_qn("w:eastAsia"), self.font_name)
        rPr.append(rFonts)
        sz = _OxmlElement("w:sz")
        sz.set(_qn("w:val"), str(self.font_size_pt * 2))  # half-points
        rPr.append(sz)
        color_el = _OxmlElement("w:color")
        color_el.set(_qn("w:val"), "0563C1")
        rPr.append(color_el)
        u = _OxmlElement("w:u")
        u.set(_qn("w:val"), "single")
        rPr.append(u)
        new_run.append(rPr)
        t = _OxmlElement("w:t")
        t.text = text
        new_run.append(t)
        hyperlink.append(new_run)
        paragraph._p.append(hyperlink)

    def _add_heading(self, doc, text: str, level: int) -> None:
        p = doc.add_paragraph()
        if level == 1:
            size = self.font_size_pt + 4  # 16pt for H1
        elif level == 2:
            size = self.font_size_pt + 2  # 14pt for H2
        else:
            size = self.font_size_pt + 1  # 13pt for H3
        run = p.add_run(text)
        self._style_run(run, bold=True, size=size)
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(6)

    def _add_chapter_banner(self, doc, *, number: int, title: str,
                            subtitle: str | None = None) -> None:
        """Render a benzene-parity parent-chapter divider banner.

        Emits a centered, page-break banner that visually announces a new
        top-level chapter: a small blue "Chapter N" tag, a large navy title
        line (registered as outline-level 0 so Word's Navigation Pane picks
        it up), and an optional grey italic subtitle.
        """
        # "Chapter N" tag
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.page_break_before = True
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(f"Chapter {number}")
        self._style_run(run, bold=True, size=14, color=(0x33, 0x55, 0x88))

        # Big chapter title
        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.paragraph_format.space_before = Pt(0)
        p2.paragraph_format.space_after = Pt(6)
        p2.paragraph_format.keep_with_next = True
        run = p2.add_run(title)
        self._style_run(run, bold=True, size=22, color=(0x1F, 0x3D, 0x7A))
        # Register as outline-level 0 so it shows up in Word's Nav Pane
        pPr = p2._p.get_or_add_pPr()
        outline = _OxmlElement("w:outlineLvl")
        outline.set(_qn("w:val"), "0")
        pPr.append(outline)

        if subtitle:
            p3 = doc.add_paragraph()
            p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p3.paragraph_format.space_after = Pt(12)
            run = p3.add_run(subtitle)
            self._style_run(run, italic=True, size=11,
                            color=(0x40, 0x40, 0x40))

    def _add_body_paragraph(self, doc, text: str) -> None:
        if not text.strip():
            return
        p = doc.add_paragraph()
        # User requirement: all body paragraphs use full-justify alignment
        # (both left and right edges aligned).
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.line_spacing = 1.15
        # Inline parsing: split on [n] patterns and render bracketed numbers
        # as superscript references, matching the reference doc's style.
        parts = re.split(r"(\[\d+(?:\s*,\s*\d+)*\])", text)
        for part in parts:
            if re.fullmatch(r"\[\d+(?:\s*,\s*\d+)*\]", part):
                run = p.add_run(part)
                self._style_run(run, bold=True,
                                size=max(8, self.font_size_pt - 2))
                run.font.superscript = True
            else:
                run = p.add_run(part)
                self._style_run(run)

    def _add_section(self, doc, section: "ReportSection",
                     figures_by_anchor: dict[str, list["ReportFigure"]] | None = None,
                     tables_by_anchor: dict[str, list["ReportTable"]] | None = None) -> None:
        # Chapter banner (optional, only at H1 parent)
        if section.chapter_number is not None:
            self._add_chapter_banner(
                doc, number=section.chapter_number,
                title=section.title, subtitle=section.chapter_subtitle)
            # Don't duplicate the heading text immediately after the banner —
            # the banner itself carries the title. Skip to paragraphs.
        else:
            self._add_heading(doc, section.title, section.level)
        for para in section.paragraphs:
            self._add_body_paragraph(doc, para)
        # Inline figures/tables carried on the section itself
        for fig in (section.inline_figures or []):
            self._add_figure(doc, fig)
        for tbl in (section.inline_tables or []):
            self._add_table(doc, tbl)
        # Inject figures & tables anchored at this section title
        if figures_by_anchor:
            for fig in figures_by_anchor.get(section.title, []):
                self._add_figure(doc, fig)
        if tables_by_anchor:
            for tbl in tables_by_anchor.get(section.title, []):
                self._add_table(doc, tbl)
        for sub in section.subsections:
            self._add_section(doc, sub, figures_by_anchor, tables_by_anchor)

    def _add_figure(self, doc, figure: "ReportFigure") -> None:
        """Embed an image followed by an italic caption. Width in inches."""
        if not figure.path or not os.path.exists(figure.path):
            # Soft-fail: render caption as placeholder instead of breaking report.
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(f"[Figure missing: {figure.path}]")
            self._style_run(run, italic=True, color=(192, 0, 0))
            return
        # Image (centered)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        try:
            run.add_picture(figure.path, width=Inches(figure.width_inches))
        except Exception:
            run.add_picture(figure.path)
        # Caption
        if figure.caption:
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap.paragraph_format.space_before = Pt(2)
            cap.paragraph_format.space_after = Pt(8)
            run = cap.add_run(figure.caption)
            self._style_run(run, italic=True, size=max(10, self.font_size_pt - 1))

    def _add_table(self, doc, table_data: "ReportTable") -> None:
        """Render a free-standing data table with header row + caption."""
        # Spacer before table
        doc.add_paragraph()
        cols = max(len(table_data.headers), 1)
        table = doc.add_table(rows=1, cols=cols)
        table.style = "Light Grid Accent 1"
        # Header row
        hdr = table.rows[0].cells
        for i, h in enumerate(table_data.headers):
            hdr[i].text = ""
            p = hdr[i].paragraphs[0]
            run = p.add_run(h)
            self._style_run(run, bold=True, size=max(10, self.font_size_pt - 1))
        # Data rows
        for row_vals in table_data.rows:
            cells = table.add_row().cells
            for i in range(cols):
                val = row_vals[i] if i < len(row_vals) else ""
                cells[i].text = ""
                p = cells[i].paragraphs[0]
                run = p.add_run(str(val))
                self._style_run(run, size=max(10, self.font_size_pt - 1))
        # Caption
        if table_data.title:
            cap = doc.add_paragraph()
            cap.paragraph_format.space_before = Pt(2)
            cap.paragraph_format.space_after = Pt(8)
            run = cap.add_run(table_data.title)
            self._style_run(run, italic=True, size=max(10, self.font_size_pt - 1))
            if table_data.source:
                run2 = cap.add_run(f" {table_data.source}")
                self._style_run(run2, italic=True, size=max(10, self.font_size_pt - 1))

    def _add_cover_page(self, doc, chemical: str, cas: str | None,
                        generated_date: str,
                        structure_image_path: str | None = None) -> None:
        """Render a dedicated cover page with title block + chemical structure,
        followed by a hard page break. Matches the uploaded reference format."""
        # Main title
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Comprehensive Literature Review")
        self._style_run(run, bold=True, size=20)
        # Subheader
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Safety of")
        self._style_run(run, italic=True, size=14)
        # Chemical name
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(chemical.capitalize())
        self._style_run(run, bold=True, size=28)
        # CAS
        if cas:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(f"CAS No. {cas}")
            self._style_run(run, italic=True, size=13)
        # "for Human Health"
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("for Human Health")
        self._style_run(run, size=14)
        # Spacer
        doc.add_paragraph()
        # Chemical structure image (centered, moderate size)
        if structure_image_path and os.path.exists(structure_image_path):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run()
            try:
                run.add_picture(structure_image_path, width=Inches(2.29))
            except Exception:
                run.add_picture(structure_image_path)
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = cap.add_run(
                f"{chemical.capitalize()} molecular structure."
            )
            self._style_run(run, italic=True, size=max(10, self.font_size_pt - 1))
        # Spacer
        doc.add_paragraph()
        # Date
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"Date: {generated_date}")
        self._style_run(run, size=12)
        # Evidence base
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Evidence base: peer-reviewed literature (PubMed, EuropePMC)")
        self._style_run(run, italic=True, size=11)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("and regulatory/agency sources (IPCS, ICSC, EPA IRIS, NIOSH, OSHA, ATSDR, IARC)")
        self._style_run(run, italic=True, size=11)
        # Hard page break so subsequent content starts on page 2
        p = doc.add_paragraph()
        run = p.add_run()
        run.add_break(WD_BREAK.PAGE)

    def _add_title_block(self, doc, chemical: str, cas: str | None,
                        generated_date: str) -> None:
        # Main title
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Comprehensive Literature Review")
        self._style_run(run, bold=True, size=20)
        # Subheader
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Safety of")
        self._style_run(run, italic=True, size=14)
        # Chemical name
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(chemical.capitalize())
        self._style_run(run, bold=True, size=24)
        # CAS
        if cas:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(f"CAS No. {cas}")
            self._style_run(run, italic=True, size=12)
        # "for Human Health"
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("for Human Health")
        self._style_run(run, size=14)
        # Date
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"Date: {generated_date}")
        self._style_run(run, size=11)
        # Evidence base
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("Evidence base: peer-reviewed literature and regulatory/agency sources")
        self._style_run(run, italic=True, size=11)
        # Spacer
        doc.add_paragraph()

    def _add_references_section(self, doc, references: list[ReportReference]) -> None:
        self._add_heading(doc, "References", 1)
        p = doc.add_paragraph()
        run = p.add_run(
            "Every bracketed [n] citation above corresponds to an entry in this "
            "list. All references were retrieved during preparation of this review; "
            "direct links to open-access full text and PDFs are provided where available."
        )
        self._style_run(run, italic=True)
        for ref in references:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.left_indent = Inches(0.25)
            p.paragraph_format.first_line_indent = Inches(-0.25)
            # [n] marker
            run = p.add_run(f"[{ref.number}] ")
            self._style_run(run, bold=True)
            # Citation text
            run = p.add_run(ref.format() + " ")
            self._style_run(run)
            # URL (if present) as clickable hyperlink
            if ref.url:
                self._add_hyperlink(p, ref.url, ref.url)

    def _add_appendix_counts(
        self, doc, db_counts: list[dict[str, Any]],
    ) -> None:
        """Appendix: per-database paper count table. db_counts is a list of
        {database, status, result_count, url, notes?} dicts."""
        doc.add_paragraph()  # spacer
        self._add_heading(doc, "Appendix A — Per-Database Extraction Counts", 1)
        p = doc.add_paragraph()
        run = p.add_run(
            "Count of papers or documents returned from each consulted "
            "database at the time of retrieval. Databases marked 'N/A' "
            "use JavaScript-rendered search interfaces or require session "
            "cookies and were consulted manually; counts for those are "
            "not programmatically verifiable and are therefore omitted "
            "rather than estimated."
        )
        self._style_run(run, italic=True)

        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        headers = ["#", "Database", "Results", "Search URL"]
        for i, h in enumerate(headers):
            hdr[i].text = ""
            p = hdr[i].paragraphs[0]
            run = p.add_run(h)
            self._style_run(run, bold=True)
        total = 0
        for i, row in enumerate(db_counts, 1):
            cells = table.add_row().cells
            cells[0].text = ""
            p = cells[0].paragraphs[0]; run = p.add_run(str(i)); self._style_run(run)
            cells[1].text = ""
            p = cells[1].paragraphs[0]; run = p.add_run(str(row.get("database", ""))); self._style_run(run)
            cells[2].text = ""
            p = cells[2].paragraphs[0]
            status = row.get("status", "ok")
            count = row.get("result_count", 0)
            if status == "no_api":
                # Show count if provided (manual scrape may still yield one),
                # otherwise mark as manual retrieval.
                if int(count) > 0:
                    run = p.add_run(f"{int(count):,} (manual)")
                    self._style_run(run, italic=True, color=(0, 112, 54))
                    total += int(count)
                else:
                    run = p.add_run("N/A (no API)")
                    self._style_run(run, italic=True)
            elif status == "html_ok":
                # Always show the actual scraped count; suffix with (HTML)
                # so the provenance is visible but the number is primary.
                run = p.add_run(f"{int(count):,} (HTML)")
                self._style_run(run, color=(0, 112, 54))
                total += int(count)
            elif status == "error":
                run = p.add_run(f"error: {(row.get('error') or '')[:50]}")
                self._style_run(run, italic=True,
                                color=(192, 0, 0))
            else:
                run = p.add_run(f"{int(count):,}")
                self._style_run(run)
                total += int(count)
            cells[3].text = ""
            p = cells[3].paragraphs[0]
            run = p.add_run((row.get("url") or "")[:120])
            self._style_run(run, size=9)

        # Total row
        cells = table.add_row().cells
        cells[0].text = ""
        cells[1].text = ""
        p = cells[1].paragraphs[0]; run = p.add_run("TOTAL (API-counted)"); self._style_run(run, bold=True)
        cells[2].text = ""
        p = cells[2].paragraphs[0]; run = p.add_run(f"{total:,}"); self._style_run(run, bold=True)
        cells[3].text = ""

    def _add_evidence_ledger_appendix(
        self, doc, ledger_entries: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Appendix B — per-source extraction ledger (which papers were actually
        pulled into the report, per database). Comes from ExtractionLedger.
        """
        doc.add_paragraph()
        self._add_heading(doc, "Appendix B — Extraction Ledger (Papers Used)", 1)
        p = doc.add_paragraph()
        run = p.add_run(
            "Exhaustive list of every paper and document retrieved and used "
            "during report preparation, grouped by source database. Each entry "
            "lists title, identifiers (PMID / PMCID / DOI), and URL."
        )
        self._style_run(run, italic=True)
        for src in sorted(ledger_entries.keys()):
            entries = ledger_entries[src]
            self._add_heading(doc, f"{src} ({len(entries)})", 2)
            for i, e in enumerate(entries, 1):
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.25)
                p.paragraph_format.first_line_indent = Inches(-0.25)
                p.paragraph_format.space_after = Pt(2)
                run = p.add_run(f"{i}. ")
                self._style_run(run, bold=True)
                title = (e.get("title") or "(no title)")
                run = p.add_run(title + " ")
                self._style_run(run)
                ids: list[str] = []
                if e.get("pmid"): ids.append(f"PMID:{e['pmid']}")
                if e.get("pmcid"): ids.append(str(e["pmcid"]))
                if e.get("doi"): ids.append(f"DOI:{e['doi']}")
                if ids:
                    run = p.add_run(f"[{', '.join(ids)}] ")
                    self._style_run(run, italic=True, size=max(9, self.font_size_pt - 1))
                if e.get("url"):
                    self._add_hyperlink(p, e["url"], e["url"])

    def generate(
        self,
        output_path: str,
        chemical: str,
        cas: str | None,
        sections: list[ReportSection],
        references: list[ReportReference],
        db_counts: list[dict[str, Any]],
        ledger_entries: dict[str, list[dict[str, Any]]] | None = None,
        generated_date: str | None = None,
        figures: list["ReportFigure"] | None = None,
        tables: list["ReportTable"] | None = None,
        cover_image_path: str | None = None,
        include_appendices: bool = True,
    ) -> str:
        if not HAS_PYTHON_DOCX:
            raise RuntimeError("python-docx not installed (pip install python-docx)")

        if generated_date is None:
            import datetime
            generated_date = datetime.date.today().isoformat()

        doc = _docx.Document()

        # Set the default document style to Times New Roman 12pt
        style = doc.styles["Normal"]
        style.font.name = self.font_name
        style.font.size = Pt(self.font_size_pt)
        rPr = style.element.get_or_add_rPr()
        rFonts = rPr.find(_qn("w:rFonts"))
        if rFonts is None:
            rFonts = _OxmlElement("w:rFonts")
            rPr.append(rFonts)
        rFonts.set(_qn("w:ascii"), self.font_name)
        rFonts.set(_qn("w:hAnsi"), self.font_name)
        rFonts.set(_qn("w:cs"), self.font_name)
        rFonts.set(_qn("w:eastAsia"), self.font_name)

        # 1-inch margins
        for section in doc.sections:
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin = Inches(1)
            section.right_margin = Inches(1)

        # Build placement lookup for figures & tables
        figures_by_anchor: dict[str, list[ReportFigure]] = {}
        for f in (figures or []):
            figures_by_anchor.setdefault(f.placement, []).append(f)
        tables_by_anchor: dict[str, list[ReportTable]] = {}
        for t in (tables or []):
            tables_by_anchor.setdefault(t.placement, []).append(t)

        # Cover page (with chemical structure image) — matches the uploaded
        # reference document's first-page layout.
        self._add_cover_page(doc, chemical, cas, generated_date, cover_image_path)

        # Body sections — figures/tables injected at matching anchors
        for s in sections:
            self._add_section(doc, s, figures_by_anchor, tables_by_anchor)

        # References
        self._add_references_section(doc, references)

        if include_appendices:
            # Appendix A — per-DB counts
            self._add_appendix_counts(doc, db_counts)
            # Appendix B — extraction ledger
            if ledger_entries:
                self._add_evidence_ledger_appendix(doc, ledger_entries)

        doc.save(output_path)
        # Post-build docx repair: python-docx ships a default template with
        # (a) a schema-invalid <w:zoom w:val="bestFit"/> element in
        # word/settings.xml — the OOXML schema requires w:percent — and
        # (b) a word/stylesWithEffects.xml file that some web-based DOCX
        # viewers refuse to render. We strip both here so the output opens
        # cleanly in Word, LibreOffice, Google Docs, and Cowork's preview.
        self._repair_docx_postbuild(output_path)
        return output_path

    @staticmethod
    def _repair_docx_postbuild(docx_path: str) -> None:
        """Fix python-docx template issues:
          1. replace invalid <w:zoom w:val="bestFit"/> with <w:zoom w:percent="100"/>
          2. drop word/stylesWithEffects.xml and its Content_Types + rels refs
        Safe no-op if either condition is absent."""
        import re
        import shutil
        import zipfile

        tmp = docx_path + ".repair.tmp"
        with zipfile.ZipFile(docx_path, "r") as zin, \
             zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/stylesWithEffects.xml":
                    continue  # drop
                data = zin.read(item.filename)
                if item.filename == "word/settings.xml":
                    txt = data.decode("utf-8")
                    txt = txt.replace(
                        '<w:zoom w:val="bestFit"/>',
                        '<w:zoom w:percent="100"/>',
                    )
                    data = txt.encode("utf-8")
                elif item.filename == "[Content_Types].xml":
                    txt = data.decode("utf-8")
                    txt = txt.replace(
                        '<Override PartName="/word/stylesWithEffects.xml" '
                        'ContentType="application/vnd.ms-word.stylesWithEffects+xml"/>',
                        "",
                    )
                    data = txt.encode("utf-8")
                elif item.filename == "word/_rels/document.xml.rels":
                    txt = data.decode("utf-8")
                    # Remove any relationship pointing at stylesWithEffects.xml
                    txt = re.sub(
                        r'<Relationship[^>]*Target="stylesWithEffects\.xml"[^>]*/>',
                        "",
                        txt,
                    )
                    data = txt.encode("utf-8")
                zout.writestr(item, data)
        shutil.move(tmp, docx_path)


# ════════════════════════════════════════════════════════════════════════════
#  MCP SERVER  — wire tools to protocol
# ════════════════════════════════════════════════════════════════════════════
