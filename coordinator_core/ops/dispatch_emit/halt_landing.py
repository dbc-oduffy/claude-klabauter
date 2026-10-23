"""Leaf helpers that decide whether a declared product already landed before a halt commit. Shared by
``terminal_commit`` and ``land_run``; imports neither, since ``land_run`` must not pull in the
terminal-commit leg."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import CITED_DETAIL_KEY
from coordinator_core.ops.dispatch_emit.pre_halt_credit import sha_tokens

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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
    landing. Prose ``met_by`` (no sha token) is no contrary citation."""
    items = verdict.get("items") if isinstance(verdict, dict) else None
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        met_by = item.get("met_by")
        text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by or "")
        named = sha_tokens(text)
        if named and not any(sha.startswith(n) or n.startswith(sha) for n in named):
            return False
    return True


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
