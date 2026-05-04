"""
Services: PubChem verify + PubMed search + PMC PDF download.

PubMed queries use AND "free full text"[sb] so only papers with PMC full text
are returned, ensuring the downloader has a real PDF to fetch.
"""
from __future__ import annotations
import asyncio, json, os, xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlencode
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

NCBI      = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBCHEM   = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PDF_DIR   = Path(os.getenv("PDF_DIR", "../pdfs"))
NCBI_KEY  = os.getenv("NCBI_API_KEY", "")
NCBI_MAIL = os.getenv("NCBI_EMAIL", "researcher@example.com")
TIMEOUT   = 30.0

HEADERS = {"User-Agent": "Mozilla/5.0 (pubmed-pipeline; mailto:researcher@example.com)"}


# ─── PubChem ──────────────────────────────────────────────────────────────────

async def verify_chemical(chemical: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        r = await c.get(f"{PUBCHEM}/compound/name/{quote(chemical)}/property/IUPACName/JSON")
        if r.status_code == 404:
            return _chem_fail(chemical, "Not found in PubChem")
        r.raise_for_status()
        props = r.json().get("PropertyTable", {}).get("Properties", [])
        if not props:
            return _chem_fail(chemical, "No properties returned")
        cid  = props[0]["CID"]
        name = props[0].get("IUPACName")

        rn_r  = await c.get(f"{PUBCHEM}/compound/cid/{cid}/xrefs/RN/JSON")
        rns   = rn_r.json().get("InformationList", {}).get("Information", [{}])[0].get("RN", []) if rn_r.status_code == 200 else []
        casrn = next((r for r in rns if r.count("-") == 2), rns[0] if rns else None)

        syn_r = await c.get(f"{PUBCHEM}/compound/cid/{cid}/synonyms/JSON")
        syns  = syn_r.json().get("InformationList", {}).get("Information", [{}])[0].get("Synonym", []) if syn_r.status_code == 200 else []
        syns  = [s for s in syns if len(s) < 80][:8]

    return {"input": chemical, "preferred_name": name, "CASRN": casrn,
            "PubChem_CID": str(cid), "synonyms": syns, "success": True, "error": None}


def _chem_fail(chemical: str, msg: str) -> dict:
    return {"input": chemical, "preferred_name": None, "CASRN": None,
            "PubChem_CID": None, "synonyms": [], "success": False, "error": msg}


# ─── Query builder ────────────────────────────────────────────────────────────

def build_query(chem: dict, keyword: str, free_full_text: bool = True) -> str:
    """Builds a PubMed query that restricts to free-full-text papers so PDFs exist."""
    name  = chem.get("preferred_name") or chem["input"]
    casrn = chem.get("CASRN")
    syns  = (chem.get("synonyms") or [])[:2]

    parts = [f'"{name}"']
    if casrn:
        parts.append(f'"{casrn}"')
    for s in syns:
        if s.lower() != name.lower():
            parts.append(f'"{s}"')

    chem_clause = " OR ".join(parts)
    q = f'({chem_clause}) AND "{keyword}"'
    if free_full_text:
        q += ' AND "free full text"[sb]'   # ← key: only papers with actual PDFs
    return q


# ─── PubMed search ────────────────────────────────────────────────────────────

async def search_pubmed(query: str, max_results: int = 20) -> tuple[list[str], int]:
    params = {"db": "pubmed", "term": query, "retmax": max_results,
              "retmode": "json", "sort": "relevance"}
    if NCBI_KEY:  params["api_key"] = NCBI_KEY
    if NCBI_MAIL: params["email"]   = NCBI_MAIL
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        r = await c.get(f"{NCBI}/esearch.fcgi?{urlencode(params)}")
        if r.status_code != 200:
            return [], 0
        d = r.json().get("esearchresult", {})
        return d.get("idlist", []), int(d.get("count", 0))


async def fetch_metadata(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "rettype": "abstract"}
    if NCBI_KEY: params["api_key"] = NCBI_KEY
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        r = await c.post(f"{NCBI}/efetch.fcgi", data=params)
    return _parse_xml(r.text, pmids) if r.status_code == 200 else [{} for _ in pmids]


async def fetch_pmc_ids(pmids: list[str]) -> dict[str, str]:
    if not pmids:
        return {}
    # PMC ID converter — converts PMIDs to PMCIDs in one call
    url = (f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1/"
           f"?ids={','.join(pmids)}&format=json")
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
        r = await c.get(url)
    if r.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for rec in r.json().get("records", []):
        pmid = rec.get("pmid")
        pmcid = rec.get("pmcid")
        if pmid and pmcid:
            out[str(pmid)] = pmcid  # already in PMC123456 format
    return out



def _parse_xml(text: str, pmids: list[str]) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [{} for _ in pmids]
    parsed: dict[str, dict] = {}
    for art in root.findall(".//PubmedArticle"):
        pmid_el = art.find(".//PMID")
        if pmid_el is None: continue
        pmid = pmid_el.text or ""
        abstract = " ".join(
            (el.text or "").strip()
            for el in art.findall(".//AbstractText") if el.text
        )
        authors = [
            f"{(a.findtext('LastName') or '')} {(a.findtext('ForeName') or '')}".strip()
            for a in art.findall(".//Author") if a.findtext("LastName")
        ]
        doi = next(
            (el.text for el in art.findall(".//ArticleId") if el.get("IdType") == "doi"), None
        )
        year_el = art.find(".//PubDate/Year") or art.find(".//PubDate/MedlineDate")
        parsed[pmid] = {
            "title":    (art.findtext(".//ArticleTitle") or "").strip(),
            "abstract": abstract,
            "authors":  json.dumps(authors),
            "journal":  (art.findtext(".//Journal/Title") or "").strip(),
            "year":     (year_el.text or "")[:4] if year_el is not None else "",
            "doi":      doi,
        }
    return [parsed.get(p, {}) for p in pmids]


# ─── PDF Downloader ───────────────────────────────────────────────────────────

async def download_pdf(paper: dict) -> dict:
    """Download free PDF from PMC (PubMed Central only)."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    pmid   = paper.get("pmid", "")
    pmc_id = paper.get("pmc_id")

    if not pmc_id:
        return {**paper, "pdf_status": "no_pdf_found", "pdf_path": None, "pdf_source": None}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers=HEADERS) as c:
        url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/"
        if res := await _fetch_pdf(c, url):
            path = PDF_DIR / f"pmc_{pmc_id}_{pmid}.pdf"
            path.write_bytes(res)
            return {**paper, "pdf_status": "success", "pdf_url": url, "pdf_path": str(path), "pdf_source": "pmc"}

    return {**paper, "pdf_status": "no_pdf_found", "pdf_path": None, "pdf_source": None}


async def _fetch_pdf(c: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        r = await c.get(url)
        if r.status_code == 200 and (b"pdf" in r.headers.get("content-type", "").lower() or r.content[:4] == b"%PDF"):
            return r.content
    except Exception:
        pass
    return None
