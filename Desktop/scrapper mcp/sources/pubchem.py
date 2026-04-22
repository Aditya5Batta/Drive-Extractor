"""
sources.pubchem — PubChem PUG-REST resolver.

PRIMARY chemical identity source. Given a name or CAS, returns CID, CAS,
SMILES, InChI, InChIKey, molecular formula, molecular weight, synonyms, and
regulatory/tox headings.
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

from core.http import HTTPFetcher
from sources._models import ChemicalIdentity


class PubChemResolver:
    """Resolve chemical identity via PubChem PUG REST API.

    Docs: https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest
    Input: name, CAS, SMILES, InChI, InChIKey, or CID.
    Returns: CID, CAS, MW, formula, SMILES, InChI, InChIKey, synonyms, common name.
    """

    BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    PROPS = ("IUPACName,MolecularFormula,MolecularWeight,"
             "CanonicalSMILES,IsomericSMILES,InChI,InChIKey")

    def __init__(self, fetcher: HTTPFetcher) -> None:
        self.fetcher = fetcher

    @staticmethod
    def _looks_like_cas(q: str) -> bool:
        return bool(re.fullmatch(r"\d{2,7}-\d{2}-\d", q.strip()))

    @staticmethod
    def _looks_like_inchikey(q: str) -> bool:
        return bool(re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", q.strip()))

    @staticmethod
    def _looks_like_cid(q: str) -> bool:
        return q.strip().isdigit()

    async def _get_json(self, url: str) -> tuple[dict | None, str | None]:
        data, _ct, status, err = await self.fetcher.fetch(url)
        if data is None:
            return None, err or f"fetch failed ({status})"
        if status >= 400:
            return None, f"HTTP {status}"
        try:
            return json.loads(data.decode("utf-8", errors="replace")), None
        except Exception as e:  # noqa: BLE001
            return None, f"json decode: {e}"

    async def _resolve_to_cid(self, query: str) -> tuple[int | None, str | None]:
        q = query.strip()
        if self._looks_like_cid(q):
            return int(q), None
        if self._looks_like_inchikey(q):
            url = f"{self.BASE}/compound/inchikey/{quote(q)}/cids/JSON"
        elif self._looks_like_cas(q):
            url = f"{self.BASE}/compound/xref/RN/{quote(q)}/cids/JSON"
        else:
            url = f"{self.BASE}/compound/name/{quote(q)}/cids/JSON"
        data, err = await self._get_json(url)
        if err and not q[0].isdigit():
            # Fallback: try by-name
            url2 = f"{self.BASE}/compound/name/{quote(q)}/cids/JSON"
            data, err2 = await self._get_json(url2)
            if err2:
                return None, f"{err}; name-fallback: {err2}"
        elif err:
            return None, err
        try:
            cids = data["IdentifierList"]["CID"]
            if cids:
                return int(cids[0]), None
        except (KeyError, TypeError, IndexError):
            pass
        return None, "no CID found"

    async def _fetch_properties(self, cid: int) -> tuple[dict | None, str | None]:
        url = f"{self.BASE}/compound/cid/{cid}/property/{self.PROPS}/JSON"
        data, err = await self._get_json(url)
        if err:
            return None, err
        try:
            return data["PropertyTable"]["Properties"][0], None
        except (KeyError, IndexError):
            return None, "properties missing"

    async def _fetch_synonyms(self, cid: int, max_n: int = 25) -> list[str]:
        url = f"{self.BASE}/compound/cid/{cid}/synonyms/JSON"
        data, err = await self._get_json(url)
        if err or not data:
            return []
        try:
            syns = data["InformationList"]["Information"][0].get("Synonym", [])
            return [s for s in syns[:max_n] if s]
        except (KeyError, IndexError):
            return []

    @staticmethod
    def _extract_cas(synonyms: list[str]) -> str | None:
        for s in synonyms:
            if re.fullmatch(r"\d{2,7}-\d{2}-\d", s.strip()):
                return s.strip()
        return None

    @staticmethod
    def _pick_common_name(synonyms: list[str]) -> str | None:
        for s in synonyms:
            if s and s[0].isalpha() and len(s) < 40:
                return s
        return synonyms[0] if synonyms else None

    async def resolve(self, query: str, cas: str | None = None) -> "ChemicalIdentity":
        """Resolve chemical by name or CAS. If `cas` is provided, we try that
        first, then fall back to the name query. The `cas` kwarg makes the
        resolver callable directly from the orchestrator and ad-hoc smoke
        tests with the same `(chemical, cas=...)` signature used elsewhere."""
        ident = ChemicalIdentity(query=query)
        # Try CAS first if provided — it's usually the most unambiguous
        cid: str | None = None
        err: str | None = None
        if cas:
            cid, err = await self._resolve_to_cid(cas)
        if cid is None:
            cid, err = await self._resolve_to_cid(query)
        if cid is None:
            ident.error = f"resolve_to_cid: {err}"
            return ident
        ident.cid = cid
        ident.pubchem_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"

        props_task = asyncio.create_task(self._fetch_properties(cid))
        syns_task = asyncio.create_task(self._fetch_synonyms(cid))
        props, props_err = await props_task
        syns = await syns_task

        if props:
            ident.iupac_name = props.get("IUPACName")
            ident.canonical_smiles = props.get("CanonicalSMILES")
            ident.isomeric_smiles = props.get("IsomericSMILES")
            ident.inchi = props.get("InChI")
            ident.inchikey = props.get("InChIKey")
            ident.molecular_formula = props.get("MolecularFormula")
            mw = props.get("MolecularWeight")
            if mw is not None:
                try:
                    ident.molecular_weight = float(mw)
                except (TypeError, ValueError):
                    pass
        elif props_err:
            ident.error = f"properties: {props_err}"

        ident.synonyms = syns
        ident.cas = self._extract_cas(syns)
        ident.common_name = self._pick_common_name(syns)
        return ident

    @staticmethod
    def format_md(ident: "ChemicalIdentity") -> str:
        lines = [f"# PubChem identity: {ident.common_name or ident.query}", ""]
        if ident.error and ident.cid is None:
            lines.append(f"❌ **Resolution failed:** {ident.error}")
            lines.append(f"Query: `{ident.query}`")
            return "\n".join(lines)
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| Query | {ident.query} |")
        lines.append(f"| CID | {ident.cid} |")
        lines.append(f"| CAS | {ident.cas or '-'} |")
        lines.append(f"| Common name | {ident.common_name or '-'} |")
        lines.append(f"| IUPAC name | {ident.iupac_name or '-'} |")
        lines.append(f"| Molecular formula | {ident.molecular_formula or '-'} |")
        lines.append(f"| Molecular weight | {ident.molecular_weight or '-'} g/mol |")
        lines.append(f"| Canonical SMILES | `{ident.canonical_smiles or '-'}` |")
        if ident.isomeric_smiles and ident.isomeric_smiles != ident.canonical_smiles:
            lines.append(f"| Isomeric SMILES | `{ident.isomeric_smiles}` |")
        lines.append(f"| InChI | `{(ident.inchi or '-')[:120]}` |")
        lines.append(f"| InChIKey | `{ident.inchikey or '-'}` |")
        lines.append(f"| PubChem URL | {ident.pubchem_url or '-'} |")
        if ident.synonyms:
            lines.append(f"\n## Synonyms ({len(ident.synonyms)} shown)")
            for s in ident.synonyms[:25]:
                lines.append(f"- {s}")
        if ident.error:
            lines.append(f"\n_Note: {ident.error}_")
        return "\n".join(lines)
