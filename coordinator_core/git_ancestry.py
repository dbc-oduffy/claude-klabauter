"""
git_ancestry.py

Shared git-ancestry range-membership helper, extracted from coverage.py's
per-commit `git merge-base --is-ancestor` probe (see coverage.py's
`_classify_out_of_window`) and completion_ops.py's origin/main merge-base
pattern. Single source of truth for the load-bearing covered-by-range
polarity that plan-delivery-audit's Oracle 3 depends on:

    is_covered(commit, start_sha, end_sha) == (
        is-ancestor-of(commit, end_sha) AND NOT is-ancestor-of(commit, start_sha)
    )

Negative-spec — this module is an ADDITIVE extraction for NEW callers, not
a rip-and-replace of coverage.py. It deliberately does NOT re-batch or
re-implement coverage.py's own per-range-union semantics (build_reviewed_set's
one-`git rev-list --parents`-graph-build strategy, coverage.py L438-524):
that batching is a distinct, already-correct optimization over MANY ranges
at once, and coverage.py's own docstring negative-spec (L518-526) forbids
naively re-merging ranges into a single combined rev-list. This module's
`is_covered` is a plain two-probe check for ONE commit against ONE range —
callers needing coverage.py's batched multi-range performance should keep
using coverage.py directly.

Spec backlink: docs/plans/2026-07-24-computed-skills-b5-planning-cluster.md § C1

Promotion note (2026-07-26, docs/plans/2026-07-26-structured-sibling-evidence-gates.md
§ C2, the Staff Engineer F7): `_is_ancestor`'s bare-bool return collapsed "not an ancestor",
"repo unreachable", "SHA does not exist" and "git not on PATH" into a single
`False` — a fail-open ambiguity `coordinator_core.sibling_fact`'s honest-
indeterminate contract cannot inherit. `is_ancestor` is the public, tri-state
promotion (`(read_ok, observed)`, mirroring `cutover_gate.py`'s
`_reverify_commit_sha` reachability shape); `_is_ancestor` is kept, signature
unchanged, as a thin wrapper over it for `is_covered` and this module's
pre-existing callers. Behaviour is unchanged for the returncode-based paths
only — the git-not-on-PATH/timeout exception path now fails closed (returns
`False`) instead of propagating an uncaught exception; see `_is_ancestor`'s
own docstring.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, Optional, Tuple

_NO_CONSOLE: Dict[str, Any] = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if os.name == "nt"
    else {}
)


def is_ancestor(commit: str, ref: str, cwd: Optional[str] = None) -> Tuple[bool, Optional[bool]]:
    """Resolve whether `commit` is an ancestor of (or equal to) `ref`, honestly.

    Returns `(read_ok, observed)`:
      - `(True, True)`  — `commit` IS an ancestor of `ref` (`merge-base
        --is-ancestor` exit 0).
      - `(True, False)` — `commit` is NOT an ancestor of `ref` (exit 1) — a
        real, asked-and-answered negative.
      - `(False, None)` — could not ask: `git` is not on PATH, the subprocess
        timed out, or `git merge-base` exited with any code other than 0/1
        (unknown SHA, not-a-commit object, unreachable repo root, etc.).

    `read_ok is False` must never be read as "not an ancestor" — see this
    module's docstring promotion note and `coordinator_core.sibling_fact`'s
    own negative spec, which depends on this distinction to avoid silently
    treating "could not verify" as a negative verdict.
    """
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, ref],
            capture_output=True,
            text=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode == 0:
        return True, True
    if result.returncode == 1:
        return True, False
    return False, None


def _is_ancestor(commit: str, ref: str, cwd: Optional[str] = None) -> bool:
    """True iff `commit` is an ancestor of (or equal to) `ref`.

    Thin wrapper over the tri-state `is_ancestor` — kept for `is_covered` and
    this module's pre-existing callers. Signature and behaviour are unchanged
    for the returncode-based paths (collapses the tri-state back to a bare
    bool: only `(True, True)` is `True`; both `(True, False)` and
    `(False, None)` are `False`, exactly as the original bare-`returncode ==
    0` implementation behaved) — but the exception path is NOT
    behaviourally identical to the pre-promotion implementation: the old
    code called `subprocess.run` directly with no try/except, so a missing
    `git` binary (or a timeout) raised an uncaught `OSError` /
    `subprocess.TimeoutExpired` straight out of this function. `is_ancestor`
    now catches those and returns `(False, None)`, so this wrapper returns
    `False` in that case instead of raising — a deliberate fail-closed
    change (Review: code-reviewer — Finding 3, 2026-07-27), not a bug, but
    real callers relying on the old crash-on-missing-git behaviour would
    observe a different outcome."""
    read_ok, observed = is_ancestor(commit, ref, cwd=cwd)
    return read_ok and bool(observed)


def is_covered(commit: str, start_sha: str, end_sha: str, cwd: Optional[str] = None) -> bool:
    """A commit C is covered by the range (start_sha, end_sha] iff BOTH hold:

      (1) C is an ancestor of end_sha (within the reviewed window, at or before B)
      (2) C is NOT an ancestor of start_sha (not before the window start — after A,
          exclusive)

    This is the exact two-clause formula plan-delivery-audit's Oracle 3 documents
    (`git merge-base --is-ancestor C B` succeeds AND `git merge-base --is-ancestor
    C A` FAILS). The polarity of clause (2) is load-bearing: inverting it (checking
    is-ancestor-of-start instead of NOT-is-ancestor-of-start) silently widens the
    covered set to include commits that predate the review window — this is the
    single source of truth for that polarity; no second copy of the warning belongs
    in skill prose after this lands.
    """
    return _is_ancestor(commit, end_sha, cwd=cwd) and not _is_ancestor(commit, start_sha, cwd=cwd)
