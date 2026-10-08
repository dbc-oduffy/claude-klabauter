"""
coordinator_core.ops.baton_awaiting_gate_recheck — "baton.awaiting_gate_recheck" op (COMPUTE_ONLY).

Purpose: list every live ``state/handoffs/*.md`` baton or seed parked at
``deployment_state: awaiting_gate`` with who holds the gate, how long it has been
parked, and whether the gate's machine-checkable condition is now met. The ping
itself is a memo the EM sends; each row carries a ready-to-run ``cross-repo-memo
draft`` command line for the holder.

Params (all optional): ``holder`` (keep only rows whose holder matches,
case-insensitive), ``today`` (ISO date override for the parked-days arithmetic).

Row: ``{path, handoff_id, kind, holder, holder_source, parked_since, parked_days,
checks, machine_condition_met, memo_draft}``. ``holder`` is the ``gate_holder``
field when present (``holder_source: field``), else the name after "gated on /
awaiting / waiting on / blocked on" in the gate prose (``inferred``), else None.
``checks`` are the machine-checkable items: each ``blocked_by`` id (met when that
baton is ``shipped``) and each commit sha cited in the gate prose (met when it
names an object in this repo; a sha absent here is unverifiable and excluded).
``machine_condition_met`` is True when every check is met, False when any
``blocked_by`` baton is not shipped, None when nothing is machine-checkable.

Negative-spec:
  - Writes nothing and spawns nothing: cited shas resolve against the loose objects
    and pack indexes in-process.
  - Does NOT send the ping and does NOT stamp ``last_gate_recheck``.
"""

from __future__ import annotations

import re
import shlex
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from coordinator_core.dag import _parse_frontmatter
from coordinator_core.git.git_objects import _pack_indexes
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.roadmap.plan_gate import scan_batons

_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_HOLDER_RE = re.compile(
    r"(?:gated on|awaiting|waiting on|blocked on)\s+([A-Za-z][\w-]*)", re.IGNORECASE
)
_GATE_TEXT_FIELDS = ("gate_dependency", "gate_notes", "blocking_notes")


def _gate_text(fm: dict) -> str:
    return " ".join(str(fm.get(k) or "") for k in _GATE_TEXT_FIELDS).strip()


def _holder(fm: dict, text: str) -> tuple[Optional[str], str]:
    explicit = str(fm.get("gate_holder") or "").strip()
    if explicit:
        return explicit, "field"
    match = _HOLDER_RE.search(text)
    if match:
        return match.group(1), "inferred"
    return None, "unknown"


def _landed(shas: list[str], worktree: Path) -> set[str]:
    """Shas (full or abbreviated) that name an object in this repo; zero spawns."""
    if not shas:
        return set()
    common = git_common_dir(worktree)
    packs = _pack_indexes(common)
    return {sha for sha in shas if _has_object(common, packs, sha)}


def _has_object(common: Path, packs: list, sha: str) -> bool:
    fan = common / "objects" / sha[:2]
    if fan.is_dir() and any(f.name.startswith(sha[2:]) for f in fan.iterdir()):
        return True
    first = int(sha[:2], 16)
    for _idx, _pack, pidx in packs:
        lo = pidx.fanout[first - 1] if first else 0
        for i in range(lo, pidx.fanout[first]):
            if pidx.shas[i * 20 : i * 20 + 20].hex().startswith(sha):
                return True
    return False


def _parked_since(fm: dict) -> Optional[date]:
    raw = fm.get("created")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _draft(holder: Optional[str], row: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(row["handoff_id"]).lower()).strip("-")[:40]
    title = f"gate recheck: {row['handoff_id']} parked {row['parked_days']}d on your gate"
    return shlex.join(
        [
            "cross-repo-memo", "draft", f"gate-recheck-{slug}",
            "--to", holder or "<gate-holder-em>",
            "--kind", "ask",
            "--title", title,
        ]
    )


@register_op("baton.awaiting_gate_recheck")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{exit_code, count, rows}`` for every live awaiting_gate handoff."""
    if repo_root is None:
        return {"exit_code": 1, "error": "baton.awaiting_gate_recheck: repo_root is required"}
    worktree = main_worktree_root(repo_root)
    today = date.fromisoformat(str(params["today"])) if params.get("today") else date.today()
    wanted = str(params.get("holder") or "").strip().lower()

    by_id, records = scan_batons(worktree)
    parked = [r for r in records if r["live"] and r["deployment_state"] == "awaiting_gate"]

    staged: list[tuple[dict, dict, str, list[str]]] = []
    all_shas: list[str] = []
    for rec in parked:
        fm = _parse_frontmatter((worktree / rec["path"]).read_text(encoding="utf-8", errors="replace"))
        text = _gate_text(fm)
        shas = list(dict.fromkeys(_SHA_RE.findall(text)))
        all_shas.extend(s for s in shas if s not in all_shas)
        staged.append((rec, fm, text, shas))
    landed = _landed(all_shas, worktree)

    rows: list[dict[str, Any]] = []
    for rec, fm, text, shas in staged:
        holder, source = _holder(fm, text)
        if wanted and (holder or "").lower() != wanted:
            continue
        checks: list[dict[str, Any]] = []
        for blocker in rec["blocked_by"]:
            target = by_id.get(blocker)
            state = target["deployment_state"] if target else "unresolved"
            checks.append({"kind": "baton", "ref": blocker, "met": state == "shipped", "state": state})
        for sha in shas:
            if sha in landed:
                checks.append({"kind": "sha", "ref": sha, "met": True, "state": "landed"})
        since = _parked_since(fm)
        if any(not c["met"] for c in checks):
            met: Optional[bool] = False
        else:
            met = True if checks else None
        row = {
            "path": rec["path"],
            "handoff_id": str(fm.get("handoff_id") or rec["id"]),
            "kind": rec["kind"],
            "holder": holder,
            "holder_source": source,
            "parked_since": since.isoformat() if since else None,
            "parked_days": (today - since).days if since else None,
            "checks": checks,
            "machine_condition_met": met,
        }
        row["memo_draft"] = _draft(holder, row)
        rows.append(row)

    rows.sort(key=lambda r: -(r["parked_days"] or 0))
    return {"exit_code": 0, "count": len(rows), "rows": rows}
