"""Leaf helpers that decide whether a declared product already landed before a halt commit. Shared by
``terminal_commit`` and ``land_run``; imports neither, since ``land_run`` must not pull in the
terminal-commit leg."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import CITED_DETAIL_KEY
from coordinator_core.ops.dispatch_emit.pre_halt_credit import SHA_TOKEN_PATTERN

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CITED_SHA_RE = re.compile(SHA_TOKEN_PATTERN)


def landed_before_halt(
    worktree_root: Path, base: str, paths: Sequence[str], owners: Sequence[str]
) -> Optional[str]:
    """The commit that carried the declared product when it landed strictly before the halt commit
    ``base`` and HEAD still holds it byte for byte; None otherwise. The commit's message must name
    one of ``owners`` (the plan path or run id): a path merely untouched since the halt was never
    this run's landing. The halt commit itself carrying the product is not a prior landing."""
    held = run_git(["--literal-pathspecs", "diff", "--quiet", "HEAD", "--", *paths], cwd=str(worktree_root))
    if held.returncode != 0:
        return None
    log = run_git(
        ["--literal-pathspecs", "log", "-1", "--format=%H%x00%B", "HEAD", "--", *paths],
        cwd=str(worktree_root),
    )
    sha, _, body = log.stdout.partition("\0") if log.returncode == 0 else ("", "", "")
    sha = sha.strip()
    if not _FULL_SHA_RE.fullmatch(sha) or sha == base or not any(o and o in body for o in owners):
        return None
    anc = run_git(["merge-base", "--is-ancestor", sha, base], cwd=str(worktree_root))
    return sha if anc.returncode == 0 else None


def landing_credited(verdict: object, ids: Sequence[object], sha: str) -> bool:
    """True unless a row in ``ids`` cites sha tokens in ``met_by`` none of which prefix-matches
    ``sha`` in either direction: a verdict crediting some other commit has not vouched for this
    landing. A sha-shaped token counts with or without a digit; ``met_by`` with none is no contrary
    citation. Trap: prose words of 7+ letters drawn from a-f (``defaced``) read as a sha token."""
    items = verdict.get("items") if isinstance(verdict, dict) else None
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        met_by = item.get("met_by")
        text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by or "")
        named = _CITED_SHA_RE.findall(text.lower())
        if named and not any(sha.startswith(n) or n.startswith(sha) for n in named):
            return False
    return True


# Must equal the reason ``requirements_ledger.judge`` writes for an essential:false not_met row;
# test_refire_land_refuses_unmet_essential_row drives the real judge, so drift fails it.
NON_ESSENTIAL_OPEN_REASON = "non-essential"
DOWNGRADED_REASON = (
    "blocked the halted pass and is now judged not_met essential:false; a refire cannot "
    "downgrade a row that refused the first pass"
)


def blocking_rows_downgraded(resume: object, result: dict) -> list:
    """``[{id, reason}]`` for each ``resume["blocking"]`` row the judge left open as
    ``non-essential``. A row that refused the halted pass stays essential on the refire: only the
    verdict's own ``essential`` flag separates the two, and the checker that wrote it is not the
    authority on its own downgrade. Gated, operator-owed and revision-withheld rows carry other
    open reasons and are not matched. Empty when the resume names no blocking row."""
    blocking = resume.get("blocking") if isinstance(resume, dict) else None
    if not isinstance(blocking, list) or not blocking:
        return []
    held = {str(b) for b in blocking}
    return [
        {"id": r["id"], "reason": DOWNGRADED_REASON}
        for r in result.get("open") or ()
        if isinstance(r, dict) and r.get("reason") == NON_ESSENTIAL_OPEN_REASON and str(r.get("id")) in held
    ]


def since_halt_close(since_halt: dict, failures: list, verdict: object) -> Optional[str]:
    """The landing sha when every failure is the unchanged-since-halt refusal and each failed row
    is cited at or before the halt or credited by ``landing_credited``; None otherwise."""
    sha = since_halt.get("landed_sha")
    if not sha or not failures:
        return None
    from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON

    if any(f["reason"] != UNCHANGED_SINCE_HALT_REASON for f in failures):
        return None
    ids = [f["id"] for f in failures]
    cited = since_halt.get(CITED_DETAIL_KEY) or {}
    if all(str(i) in cited for i in ids) or landing_credited(verdict, ids, sha):
        return sha
    return None
