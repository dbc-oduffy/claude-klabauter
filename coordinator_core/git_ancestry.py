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

Spec backlink: coordinator-content-repo:pln-computed-skills-b5-planning-cl-a28764 § C1

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

Batched forms: `resolve_commit_prefixes` and `ancestors_of` answer many
abbreviations / many targets in exactly one `run_git` call each (an empty
input spawns nothing). They lean on the repo's commit-graph, so cost is the
spawn, not history depth; `is_ancestor` stays the single-commit probe.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

from coordinator_core.git.run import run_git

_HEX_PREFIX = re.compile(r"[0-9a-f]{4,40}")


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

    Bounded, as of the migration onto `git.run`, at
    `LOCAL_PLUMBING_BUDGET_SECS`. It previously carried NO `timeout=` at
    all, so a wedged `git merge-base` held its caller indefinitely; the
    tri-state above already had the honest outcome for that case
    (`(False, None)`) and simply never reached it. The two sentinel
    returncodes the shared runner emits both land there: `-1` (timeout) and
    `127` (git absent) are neither 0 nor 1.
    """
    result = run_git(["merge-base", "--is-ancestor", commit, ref], cwd=cwd)
    if result.returncode == 0:
        return True, True
    if result.returncode == 1:
        return True, False
    return False, None


def _is_ancestor(commit: str, ref: str, cwd: Optional[str] = None) -> bool:
    read_ok, observed = is_ancestor(commit, ref, cwd=cwd)
    return read_ok and bool(observed)


def resolve_commit_prefixes(
    prefixes: Iterable[str], cwd: Optional[str] = None
) -> Tuple[dict, Optional[str]]:
    """Resolve hex abbreviations to full commit shas in ONE `git rev-parse`.

    Returns `({prefix: full_sha}, None)` or `({}, refusal)` naming the first
    failing prefix ("ambiguous" or "matches no commit"). Inputs must be
    `[0-9a-f]{4,40}`. Trap: git resolves a ref name ahead of an object
    prefix, so a result that does not start with its prefix refuses as
    ambiguous.
    """
    unique = list(dict.fromkeys(prefixes))
    if not unique:
        return {}, None
    malformed = next((p for p in unique if not _HEX_PREFIX.fullmatch(p)), None)
    if malformed is not None:
        return {}, f"sha prefix {malformed!r} is not 4-40 lowercase hex"
    result = run_git(["rev-parse", *[f"{p}^{{commit}}" for p in unique]], cwd=cwd)
    if result.returncode in (-1, 127):
        return {}, "sha prefixes could not be resolved (git unavailable or timed out)"
    lines = result.stdout.split()
    resolved: dict = {}
    for prefix, line in zip(unique, lines):
        if len(line) == 40 and line.startswith(prefix.lower()):
            resolved[prefix] = line
        elif len(line) == 40:
            return {}, f"sha prefix {prefix} is ambiguous (also names a ref)"
        else:
            break
    if result.returncode == 0 and len(resolved) == len(unique):
        return resolved, None
    failing = next((p for p in unique if p not in resolved), unique[0])
    if "short object ID" in result.stderr and "is ambiguous" in result.stderr:
        return {}, f"sha prefix {failing} is ambiguous"
    return {}, f"sha prefix {failing} matches no commit"


def ancestors_of(
    targets: Iterable[str], ref: str, cwd: Optional[str] = None
) -> Tuple[bool, Optional[frozenset]]:
    """Which of `targets` are `ref` or its ancestors, in ONE `git rev-list`.

    `rev-list <targets> ^<ref>` prints every commit reachable from a target
    and not from `ref`; a target is `ref`-or-ancestor iff it is absent from
    the output. Returns `(True, frozenset)` or `(False, None)` on any failed
    read (never "not an ancestor"). Never passes `--max-count`: truncation
    would read an unmerged target as an ancestor. `targets` are full shas.
    """
    unique = list(dict.fromkeys(targets))
    if not unique:
        return True, frozenset()
    result = run_git(["rev-list", *unique, f"^{ref}"], cwd=cwd)
    if result.returncode != 0:
        return False, None
    unreached = set(result.stdout.split())
    return True, frozenset(t for t in unique if t not in unreached)


def is_covered(commit: str, start_sha: str, end_sha: str, cwd: Optional[str] = None) -> bool:
    return _is_ancestor(commit, end_sha, cwd=cwd) and not _is_ancestor(commit, start_sha, cwd=cwd)
