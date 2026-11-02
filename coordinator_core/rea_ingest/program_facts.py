"""Maps a ghidra-program-facts/v1 export to one claim plus per-fact evidence records and records them.

Purpose: facts parsed from a binary are projections (DR-2332); this module turns the export
into the ledger's existing ``claim`` and ``evidence_record`` shapes and hands them to the
registered ``rea_ledger.write`` op, the only ledger writer.

Invariants:
    - ``to_records`` is pure: the same export and ``observed_at`` give byte-identical records.
      The export carries no timestamp, so ``observed_at`` is a parameter defaulting to a fixed
      constant, never the wall clock.
    - Every evidence id matches ``^[A-Za-z0-9][A-Za-z0-9._-]*$``, is at most 128 characters and
      is a single safe path component; a repeat id is disambiguated by a content digest.
    - ``record`` writes the claim batch first, then the evidence batch; each batch is
      all-or-nothing in the op.

Negative-spec: no file or path access to the ledger here, no subprocess, no execution of the analysed binary.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from coordinator_core.rea_ledger.canonical import canonical_bytes

OBSERVED_AT_UNSET = "1970-01-01T00:00:00Z"
OBSERVER = "rea_ingest.program_facts"
_OP = "rea_ledger.write"
_MAX_ID = 128


class ProgramFactsWriteError(RuntimeError):
    """The ledger op refused a batch; nothing from that batch was written."""


def _digest(fact: dict, length: int) -> str:
    return hashlib.sha256(canonical_bytes(fact)).hexdigest()[:length]


def _addr_part(address: str) -> str:
    return address[2:] if address[:2].lower() == "0x" else address


def _fact_rows(export: dict):
    """Yield (kind, id-stem, locator, fact) per fact in export order."""
    for fn in export["functions"]:
        yield "function", _addr_part(fn["entry"]), fn["entry"], fn
    for xr in export["xrefs"]:
        yield "xref", _digest(xr, 16), f"{xr['from']}->{xr['to']}", xr
    for imp in export["imports"]:
        yield "import", _digest(imp, 16), f"{imp['library']}!{imp['symbol']}", imp
    for st in export["strings"]:
        yield "string", _addr_part(st["address"]), st["address"], st


def _safe(stem: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)


def to_records(export: dict, observed_at: str = OBSERVED_AT_UNSET) -> dict:
    """Return ``{"claim": [...], "evidence_record": [...]}`` for one export document.

    ``claim`` holds one record for the program; ``evidence_record`` holds one per function, xref,
    import and string, in export order. Raises KeyError when the export lacks a contract field.
    """
    program = export["program"]
    sha = program["sha256"]
    version = program["ghidra_version"]
    short = sha[:12]
    claim_id = f"program-{short}"
    source_ref = f"ghidra:{version}:sha256:{sha}"
    claim = {
        "id": claim_id,
        "claim_text": f"program sha256:{sha} as analysed by Ghidra {version}",
        "type": "fact",
        "confidence": "HIGH",
    }
    evidence = []
    seen = set()
    for kind, stem, locator, fact in _fact_rows(export):
        ev_id = f"{kind}-{short}-{_safe(stem)}"
        if ev_id in seen or len(ev_id) > _MAX_ID:
            ev_id = f"{kind}-{short}-{_digest(fact, 16)}"
        if ev_id in seen:
            ev_id = f"{ev_id}-{len(seen)}"
        seen.add(ev_id)
        evidence.append({
            "schema_version": "evidence-record/v1",
            "id": ev_id,
            "claim_id": claim_id,
            "stance": "context",
            "source_kind": "command-output",
            "source_ref": source_ref,
            "locator": locator,
            "excerpt": canonical_bytes(fact).decode("utf-8"),
            "observed_at": observed_at,
            "observer": OBSERVER,
        })
    return {"claim": [claim], "evidence_record": evidence}


def _write(kind: str, records: list, repo_root: Path) -> int:
    from coordinator_core import ipc

    handler = ipc.get_op_handler(_OP)
    if handler is None:
        raise ProgramFactsWriteError(f"{_OP} is not registered")
    result = handler({"kind": kind, "records": [canonical_bytes(r).decode("utf-8") for r in records]}, repo_root)
    if not isinstance(result, dict) or "error" in result:
        raise ProgramFactsWriteError(str(result.get("error") if isinstance(result, dict) else result))
    return result["written"]


def record(export: dict, repo_root: Path, observed_at: Optional[str] = None) -> dict:
    """Write the export's claim then its evidence records through ``rea_ledger.write``.

    Returns ``{"claim": n, "evidence_record": n}`` counting files the op created (an identical
    re-run writes 0). Raises ProgramFactsWriteError when the op refuses a batch; if the claim
    batch was written before an evidence refusal, a re-run completes it idempotently.
    """
    recs = to_records(export, observed_at or OBSERVED_AT_UNSET)
    return {kind: _write(kind, recs[kind], repo_root) for kind in ("claim", "evidence_record")}
