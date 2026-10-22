"""Pure extractor deciding the write paths of a ledger-resume's remaining-work leg.

Stdlib only: no subprocess, no git.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_REFUSE, LEG_SKIP
from coordinator_core.ops.dispatch_emit.pre_halt_credit import note_rerules
from coordinator_core.session import record_homes

_STRIP_EDGES = "`'\"()[]<>{}"
_TRAILING = ".,;:)"
_LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?)+$")
_PATHLIKE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*$")
_DROP_HEADS = ("scratch/", Path(record_homes.home_dir("", "warp")).as_posix() + "/")
_REMEDY = "revise the plan with a row whose writes cover them, or send a re-ruling revise note"


def _clean(token: str) -> Optional[str]:
    t = token.replace("\\", "/").strip(_STRIP_EDGES)
    while t and t[-1] in _TRAILING:
        t = t[:-1].rstrip(_STRIP_EDGES)
    t = t.split("#", 1)[0]
    t = _LINE_SUFFIX.sub("", t)
    while t and t[-1] in _TRAILING:
        t = t[:-1]
    if t.startswith("./"):
        t = t[2:]
    if not t or t.startswith("/") or re.match(r"^[A-Za-z]:", t):
        return None
    if ".." in t.split("/") or not _PATHLIKE.match(t):
        return None
    if "/" not in t and not re.search(r"\.[A-Za-z0-9]+$", t):
        return None
    if t.startswith(_DROP_HEADS):
        return None
    return t


def _exists(rel: str, repo_root: Path) -> bool:
    p = repo_root / rel
    return p.is_file() or ("/" in rel and p.parent.is_dir())


def _named(remaining: Mapping[str, Mapping[str, Any]], repo_root: Path) -> list[str]:
    out: list[str] = []
    for row in remaining.values():
        for text in row.get("remaining") or []:
            for tok in str(text).split():
                rel = _clean(tok)
                if rel and rel not in out and _exists(rel, repo_root):
                    out.append(rel)
    return out


def remaining_leg_plan(
    remaining: Mapping[str, Mapping[str, Any]],
    notes: Sequence[str],
    manifest_writes: Sequence[str],
    doc_writes: Sequence[str],
    sizing_rel: Optional[str],
    repo_root: Path,
) -> dict[str, Any]:
    """Return `{action, writes, unwritable, reason}` for the ledger-remaining leg.

    `writes` = manifest writes plus the named remaining paths the run's commit can land (doc
    writes or the sizing); `unwritable` = named paths nothing can land.
    A note naming a commit sha (digit and letter, 7-40 hex) re-rules the owed rows as met, so
    unwritable paths alone do not refuse. The sha is not resolved against git: the verify leg judges.
    """
    named = _named(remaining, repo_root)
    allowed = set(doc_writes) | ({sizing_rel} if sizing_rel else set())
    writes: list[str] = []
    for p in [*manifest_writes, *(n for n in named if n in allowed)]:
        if p not in writes:
            writes.append(p)
    unwritable = [n for n in named if n not in writes]
    owed = [rid for rid, row in remaining.items() if row.get("remaining")]
    rerule = note_rerules(notes)
    if owed and ((unwritable and not rerule) or (not writes and not notes)):
        if unwritable:
            reason = "remaining names paths no row can land: " + ", ".join(unwritable)
        else:
            reason = "remaining of " + ", ".join(owed) + " names no landable path"
        return {"action": LEG_REFUSE, "writes": writes, "unwritable": unwritable,
                "reason": f"{reason}; {_REMEDY}"}
    if not writes and notes:
        return {"action": LEG_SKIP, "writes": [], "unwritable": unwritable,
                "reason": "re-ruling-only revise: no leg to run, the verify leg judges"}
    return {"action": LEG_DISPATCH, "writes": writes, "unwritable": unwritable, "reason": ""}
