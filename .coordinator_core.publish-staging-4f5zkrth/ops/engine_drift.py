"""
coordinator_core.ops.engine_drift — JSON-RPC "engine.drift" operation.

Purpose: a read-only drift PROBE for the running coordinator_core engine — compares the
engine SHA actually executing (resolve_engine_sha(), engine_version.py) against the
committed known-good floor (MIN_KNOWN_GOOD_SHA) and reports which of THREE states apply.
Mirrors the offers-not-nags shape of DoE-claude's check-plugin-drift.sh (adapted, not
re-derived): silent when clean, an offer naming the concrete refresh action when behind,
and — the one genuine correctness gap this op closes relative to a naive two-state
behind/clean check — a DISTINCT notice when freshness cannot be verified at all.

THREE states (do not collapse to two):
  - "clean"        — running SHA resolved and at-or-ahead of the floor. Silent; no offer.
  - "behind"       — running SHA resolved and strictly behind the floor. An offer naming
                      the delta and the refresh action (offers-not-nags: lead with the fix).
  - "indeterminate"— running SHA is unresolved (git absent / not a repo / detached HEAD /
                      shallow clone) OR ancestry could not be computed. This is NEITHER
                      silent-clean NOR a behind-floor alarm: treating "unknown" as clean
                      is a false all-clear on exactly the broken-checkout condition most
                      likely to accompany a stale engine; treating it as behind is a false
                      alarm on every legitimately git-less deployment (e.g. a tarball
                      install with no .git dir). The indeterminate notice names WHY
                      freshness is unverifiable and how to make it resolvable.

Decision provenance: chunk C3 of the makima-engine-version-surface-drift-probe plan.
Spec backlink: pln-makima-engine-version-surface--c130a8

Self-registration: importing this module calls register_op("engine.drift", ...) as a
side-effect (same pattern as ops/emit_cadence.py / ops/artifact_emit.py).
"""

from __future__ import annotations
import sys

from pathlib import Path
from typing import Callable, Optional

from coordinator_core.engine_version import MIN_KNOWN_GOOD_SHA, resolve_engine_sha
from coordinator_core.git_scope import PROBE_UNKNOWN, PROBE_YES, git_predicate
from coordinator_core.ipc import register_op


def classify_drift(
    running_sha: Optional[str],
    floor_sha: str,
    is_behind: Callable[[str, str], Optional[bool]],
) -> dict:
    """Pure decision logic — no git I/O, deterministically testable.

    Args:
        running_sha: the SHA the engine actually resolved for itself, or None if
            unresolvable (engine_version.resolve_engine_sha()'s sentinel).
        floor_sha: the committed known-good floor (MIN_KNOWN_GOOD_SHA).
        is_behind: callable(running, floor) -> True if running is strictly an ancestor
            of floor (older than the floor — genuinely behind), False if running is
            at-or-ahead of floor, or None if ancestry is indeterminable (e.g. git
            errored, unrelated histories, shallow clone missing the floor commit).

    Returns a result dict with a "state" key set to one of "clean" | "behind" |
    "indeterminate", plus state-specific fields (see each branch below). The dict
    shape is stable across states only for "state"/"running_sha"/"floor_sha" — callers
    should branch on "state" before reading state-specific keys.
    """
    if running_sha is None:
        return {
            "state": "indeterminate",
            "running_sha": None,
            "floor_sha": floor_sha,
            "reason": "engine SHA unresolved (not a git repo, detached HEAD, or git unavailable)",
            "notice": (
                "engine version indeterminate — cannot verify freshness. "
                "The running engine checkout does not resolve a git HEAD SHA "
                "(not a git repo / git not installed / detached HEAD with no ref). "
                "To make this resolvable, ensure the engine's checkout "
                "(coordinator_core/) is a normal git working tree with git on PATH."
            ),
        }

    behind = is_behind(running_sha, floor_sha)

    if behind is None:
        return {
            "state": "indeterminate",
            "running_sha": running_sha,
            "floor_sha": floor_sha,
            "reason": "ancestry between running SHA and floor SHA could not be determined",
            "notice": (
                "engine version indeterminate — cannot verify freshness. "
                f"Ancestry between the running SHA ({running_sha[:12]}) and the known-good "
                f"floor ({floor_sha[:12]}) could not be computed (shallow clone missing the "
                "floor commit, unrelated histories, or a git error). Fetch full history "
                "for the engine checkout to make this resolvable."
            ),
        }

    if behind:
        return {
            "state": "behind",
            "running_sha": running_sha,
            "floor_sha": floor_sha,
            "offer": (
                f"engine is behind the known-good floor — running {running_sha[:12]}, "
                f"floor is {floor_sha[:12]}. Refresh/pull the engine checkout "
                "(coordinator_core/) to pick up the fix this floor tracks."
            ),
        }

    return {
        "state": "clean",
        "running_sha": running_sha,
        "floor_sha": floor_sha,
    }


def _git_is_behind(running_sha: str, floor_sha: str) -> Optional[bool]:
    """Real ancestry check: is `running_sha` a strict ancestor of `floor_sha`?

    Runs `git merge-base --is-ancestor running floor` in the engine's own git dir
    (Path(__file__)-derived, same self-locating convention as engine_version.py).
    Returns True (running is older / behind), False (running is at-or-ahead of the
    floor — merge-base --is-ancestor fails when running == floor or is a descendant),
    or None if git itself errors for a reason other than "not an ancestor" (git
    missing, not a repo, unrelated histories, shallow clone missing one of the SHAs)
    — the indeterminate sentinel classify_drift() treats as its own distinct state.
    """
    if running_sha == floor_sha:
        return False

    engine_dir = Path(__file__).resolve().parent.parent
    # Review: code-reviewer (Finding 3) — bounded timeout so this read-only probe
    # can never hang the op's per-invocation budget on a pathological checkout
    # (index lock held, network-mounted .git, corrupt history). Timeout collapses
    # to the same indeterminate sentinel as any other git failure — never a false
    # clean/behind on a merely-slow git call.
    #
    # `git_predicate` supplies both the 0/1/other tri-state and the stripped
    # repo-scoping environment `-C` needs to genuinely select the engine's own
    # checkout: an inherited GIT_DIR (git exports one to every hook it runs)
    # otherwise retargets this ancestry question at whatever repo the hook was
    # running in, and a `returncode != 0` reading would render that as a
    # confident False (engine at-or-ahead of the floor). See
    # `coordinator_core/git_scope.py`.
    verdict, reason = git_predicate(
        engine_dir,
        ["merge-base", "--is-ancestor", running_sha, floor_sha],
        timeout=10,
    )
    if verdict == PROBE_UNKNOWN:
        print(f"skip: _git_is_behind: could not determine ancestry: {reason}", file=sys.stderr)
        return None
    return verdict == PROBE_YES


@register_op("engine.drift")
def _engine_drift(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'engine.drift' handler — probe the running engine's freshness.

    Params: none consumed.
    repo_root: unused — this op inspects the engine's OWN checkout (Path(__file__)),
    not the caller's repo, mirroring engine_version.resolve_engine_sha()'s self-locating
    convention (a drift probe for the engine that ran must inspect the engine that ran).

    Resolves the running SHA via resolve_engine_sha(), builds the real ancestry check
    via _git_is_behind() against MIN_KNOWN_GOOD_SHA, and delegates the three-state
    decision to the pure classify_drift(). Returns classify_drift()'s result dict
    unmodified — this handler is a thin I/O-resolving wrapper, not a second decision
    point.
    """
    running_sha = resolve_engine_sha()
    return classify_drift(running_sha, MIN_KNOWN_GOOD_SHA, _git_is_behind)
