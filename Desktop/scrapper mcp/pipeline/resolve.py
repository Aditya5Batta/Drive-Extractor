"""
pipeline.resolve — Chemical-identity resolution.

Backs the `resolve_chemical` MCP tool. Given a name, CAS, SMILES, InChI, or
CID, asks PubChem and returns a canonical identity dict. Pure wrapper around
sources.pubchem.PubChemResolver — the logic lives there; this module is the
stable contract the MCP surface calls.
"""
from __future__ import annotations
from typing import Any

from sources._models import ChemicalIdentity
from sources.pubchem import PubChemResolver


async def resolve_chemical(resolver: PubChemResolver, query: str) -> dict[str, Any]:
    """Resolve a chemical query to canonical identity.

    Parameters
    ----------
    resolver : PubChemResolver  (from ToolContext.pubchem)
    query    : str              — chemical name, CAS, SMILES, InChI, InChIKey, or CID

    Returns
    -------
    dict with keys:
      ok, query, cid, cas, iupac_name, canonical_smiles, isomeric_smiles,
      inchi, inchikey, molecular_formula, molecular_weight, synonyms,
      common_name, pubchem_url, error
    """
    if not query or not query.strip():
        return {"ok": False, "error": "empty query", "query": query}

    identity: ChemicalIdentity = await resolver.resolve(query.strip())
    d = identity.to_dict()
    d["ok"] = identity.error is None and identity.cid is not None
    return d
