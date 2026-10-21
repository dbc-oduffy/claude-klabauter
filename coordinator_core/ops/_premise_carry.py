"""Pure carry core for the premise check: which citations owe a fresh check on a refire, and what
verdicts stand after a pass. No IO, no spawn. Contract: coordinator_core/contract/premise-carry-contract.md.

The diff is set-based over line hashes (``_research_grounding.line_hashes``), never positional, so
one inserted line does not stale the lines after it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.ops._research_grounding import line_hashes

SCHEMA = "premise-carry/1"
VERDICTS = frozenset({"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"})
FLAGGED = frozenset({"UNRESOLVED", "CONTRADICTED"})
TEXT_CAP = 300
_KEY = "line_hash"


def _norm(text: Any) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _bare(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != _KEY}


def parse_record(raw: str, plan: str) -> Optional[dict]:
    """The carry record in ``raw`` for ``plan``, or None when it is unreadable, of another schema
    or shape, or names another plan."""
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("plan") != plan:
        return None
    rows = record.get("rows")
    if not isinstance(record.get("brief_version"), str) or not isinstance(record.get("basis"), list):
        return None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None
    return record


def _first_pass(reason: str) -> dict:
    return {"first_pass": True, "reason": reason, "changed_lines": [], "recheck": [], "carried": []}


def _scope_rows(record: Optional[dict], plan_text: str, brief_version: str) -> Optional[tuple]:
    """(changed_lines, recheck rows, carried rows) with the record's line_hash kept, or None on a first pass."""
    if record is None or record.get("brief_version") != brief_version:
        return None
    hashes = line_hashes(plan_text)
    present = set(hashes)
    basis = set(record["basis"])
    lines = plan_text.splitlines()
    changed = [
        {"line": i + 1, "text": lines[i].rstrip()[:TEXT_CAP]}
        for i, h in enumerate(hashes)
        if lines[i].strip() and h not in basis
    ]
    recheck: List[dict] = []
    carried: List[dict] = []
    for row in record["rows"]:
        key = row.get(_KEY)
        stale = not isinstance(key, str) or key not in present
        (recheck if stale or row.get("verdict") in FLAGGED else carried).append(row)
    return changed, recheck, carried


def scope(record: Optional[dict], plan_text: str, brief_version: str) -> dict:
    """Reply body of ``plan.premise_scope`` (without ``plan``): which citations this refire owes a fresh check."""
    if record is None:
        return _first_pass("no carry record for this plan")
    got = _scope_rows(record, plan_text, brief_version)
    if got is None:
        return _first_pass("premise-checker brief version changed")
    changed, recheck, carried = got
    return {
        "first_pass": False,
        "reason": f"{len(changed)} changed line(s), {len(recheck)} citation(s) to re-check, "
        f"{len(carried)} carried",
        "changed_lines": changed,
        "recheck": [_bare(r) for r in recheck],
        "carried": [_bare(r) for r in carried],
    }


def _key(row: dict, plan_lines: List[str], hashes: List[str]) -> Optional[str]:
    line = row.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and 1 <= line <= len(hashes):
        return hashes[line - 1]
    cited = _norm(row.get("citation"))
    if cited:
        for text, h in zip(plan_lines, hashes):
            if cited in _norm(text):
                return h
    return None


def _check_rows(rows: Any) -> List[dict]:
    if not isinstance(rows, list):
        raise ValueError("rows must be a list of {verdict, citation} objects")
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("verdict") not in VERDICTS:
            raise ValueError(f"rows[{i}].verdict must be one of {', '.join(sorted(VERDICTS))}")
        if not isinstance(row.get("citation"), str):
            raise ValueError(f"rows[{i}].citation must be a string")
    return rows


def _retired_citations(retired: Any) -> set:
    if retired is None:
        return set()
    if not isinstance(retired, list):
        raise ValueError("retired must be a list of citations")
    out = set()
    for i, item in enumerate(retired):
        cited = item.get("citation") if isinstance(item, dict) else item
        if not isinstance(cited, str):
            raise ValueError(f"retired[{i}] must be a citation string")
        out.add(_norm(cited))
    return out


def merge(
    record: Optional[dict],
    plan_text: str,
    brief_version: str,
    rows: Any,
    retired: Any = None,
    citations_skipped: Any = 0,
    *,
    plan: str = "",
) -> Tuple[dict, List[dict]]:
    """(new carry record, merged rows) after a pass. Raises ValueError, writing nothing, when a
    prior flagged or stale citation is in neither ``rows`` nor ``retired``."""
    fresh = _check_rows(rows)
    gone = _retired_citations(retired)
    if isinstance(citations_skipped, bool) or not isinstance(citations_skipped, int) or citations_skipped < 0:
        raise ValueError("citations_skipped must be a non-negative integer")
    answered = {_norm(r["citation"]) for r in fresh} | gone
    got = _scope_rows(record, plan_text, brief_version)
    prior_basis: set = set()
    kept: List[dict] = []
    if got is not None:
        _, recheck, carried = got
        dropped = [r["citation"] for r in recheck if _norm(r.get("citation")) not in answered]
        if dropped:
            raise ValueError(
                "re-judge or retire every flagged or stale citation; unanswered: "
                + "; ".join(c[:120] for c in dropped)
            )
        kept = [r for r in carried if _norm(r.get("citation")) not in answered]
        prior_basis = set(record["basis"])

    plan_lines = plan_text.splitlines()
    hashes = line_hashes(plan_text)
    keyed = [{**r, _KEY: _key(r, plan_lines, hashes)} for r in fresh]
    if citations_skipped > 0:
        cap = max(
            (r["line"] for r in fresh
             if isinstance(r.get("line"), int) and not isinstance(r["line"], bool)),
            default=0,
        )
        basis = set(hashes[: max(cap, 0)]) | (prior_basis & set(hashes))
    else:
        basis = set(hashes)
    new_record = {
        "schema": SCHEMA,
        "plan": plan,
        "brief_version": brief_version,
        "basis": sorted(basis),
        "rows": [*kept, *keyed],
    }
    return new_record, [_bare(r) for r in new_record["rows"]]
