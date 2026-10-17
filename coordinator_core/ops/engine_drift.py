"""
coordinator_core.ops.engine_drift — JSON-RPC "engine.drift" operation.

Purpose: a read-only drift PROBE for the running coordinator_core engine — compares the
running engine's SOURCE-HEAD (resolve_engine_source_head(), engine_version.py: HEAD for a
source tree, the publish stamp for the mirror) against the committed known-good floor
(MIN_KNOWN_GOOD_SHA, a claude-klabauter sha) and reports which of THREE states apply. Ancestry runs
in a git dir that holds claude-klabauter history: the engine's own repo for a source tree,
engine_root.engine_source_root() for the mirror (history is distinct from claude-klabauter's).
Mirrors the offers-not-nags shape of coordinator-content-repo's check-plugin-drift.sh (adapted, not
re-derived): silent when clean, an offer naming the concrete refresh action when behind,
and — the one genuine correctness gap this op closes relative to a naive two-state
behind/clean check — a DISTINCT notice when freshness cannot be verified at all.

THREE states (do not collapse to two):
  - "clean"        — running SHA resolved and at-or-ahead of the floor. Silent; no offer.
  - "behind"       — running SHA resolved and strictly behind the floor. An offer naming
                      the delta and the refresh action (offers-not-nags: lead with the fix).
  - "indeterminate"— running SHA is unresolved (git absent / not a repo / detached HEAD /
                      shallow clone, unstamped mirror) OR ancestry could not be computed
                      (floor absent, shallow, no registered claude-klabauter source tree, git error).
                      Result carries "cause". This is NEITHER
                      silent-clean NOR a behind-floor alarm: treating "unknown" as clean
                      is a false all-clear on exactly the broken-checkout condition most
                      likely to accompany a stale engine; treating it as behind is a false
                      alarm on every legitimately git-less deployment (e.g. a tarball
                      install with no .git dir). The indeterminate notice names WHY
                      freshness is unverifiable and how to make it resolvable.

Decision provenance: chunk C3 of the claude-klabauter-engine-version-surface-drift-probe plan.
Spec backlink: pln-claude-klabauter-engine-version-surface--c130a8

Self-registration: importing this module calls register_op("engine.drift", ...) as a
side-effect (same pattern as ops/emit_cadence.py / ops/artifact_emit.py).
"""

from __future__ import annotations
import sys

from pathlib import Path
from typing import Callable, Optional

from coordinator_core.engine_version import MIN_KNOWN_GOOD_SHA, resolve_engine_source_head
from coordinator_core.git.run import run_git
from coordinator_core.git_scope import (
    PROBE_NO,
    PROBE_UNKNOWN,
    PROBE_YES,
    git_predicate,
    scoped_git_env,
)
from coordinator_core.ipc import register_op

CAUSE_FLOOR_ABSENT = "floor_absent"
CAUSE_SHALLOW = "shallow"
CAUSE_GIT_ERROR = "git_error"
CAUSE_MIRROR_UNSTAMPED = "mirror_unstamped"
CAUSE_SOURCE_HISTORY_UNAVAILABLE = "source_history_unavailable"


def classify_drift(
    running_sha: Optional[str],
    floor_sha: str,
    is_behind: Callable[[str, str], Optional[bool]],
    diagnose: Optional[Callable[[str], str]] = None,
    unresolved_cause: Optional[str] = None,
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
        diagnose: callable(floor_sha) -> CAUSE_*, consulted only when is_behind
            returns None; None means CAUSE_GIT_ERROR.

    Returns a result dict with a "state" key set to one of "clean" | "behind" |
    "indeterminate", plus state-specific fields (see each branch below). The dict
    shape is stable across states only for "state"/"running_sha"/"floor_sha" — callers
    should branch on "state" before reading state-specific keys.
    """
    if running_sha is None and unresolved_cause == CAUSE_MIRROR_UNSTAMPED:
        return {
            "state": "indeterminate",
            "cause": CAUSE_MIRROR_UNSTAMPED,
            "running_sha": None,
            "floor_sha": floor_sha,
            "reason": "mirror HEAD commit carries no source-head stamp",
            "notice": (
                "engine version indeterminate — cannot verify freshness. "
                "The published mirror's HEAD commit has no [source-head <sha>] stamp or "
                "Percolate-Source-Head trailer. Republish the mirror to stamp it."
            ),
        }

    if running_sha is None:
        return {
            "state": "indeterminate",
            "cause": CAUSE_GIT_ERROR,
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
        cause = diagnose(floor_sha) if diagnose is not None else CAUSE_GIT_ERROR
        head = (
            "engine version indeterminate — cannot verify freshness. "
            f"Ancestry between the running SHA ({running_sha[:12]}) and the known-good "
            f"floor ({floor_sha[:12]}) could not be computed"
        )
        if cause == CAUSE_SOURCE_HISTORY_UNAVAILABLE:
            reason = "no claude-klabauter source tree is registered on this box"
            head = (
                "engine version indeterminate — cannot verify freshness. "
                f"The engine's source-head ({running_sha[:12]}) is known, but comparing it "
                f"with the floor ({floor_sha[:12]}) needs the claude-klabauter source history"
            )
            tail = ": no source tree is registered on this box. Register the claude-klabauter checkout as the engine source."
        elif cause == CAUSE_FLOOR_ABSENT:
            reason = (
                f"floor commit {floor_sha[:12]} is not an object in this full (non-shallow) "
                "engine clone"
            )
            tail = (
                f": the floor commit is absent from the engine history. Fetching more history "
                "cannot add it; re-pin MIN_KNOWN_GOOD_SHA (engine_version.py) to a commit "
                "reachable from the engine's HEAD."
            )
        elif cause == CAUSE_SHALLOW:
            reason = "shallow engine clone does not contain the floor commit"
            tail = (
                ": the engine checkout is a shallow clone missing the floor commit. "
                "Fetch full history for the engine checkout to make this resolvable."
            )
        else:
            reason = "ancestry between running SHA and floor SHA could not be determined"
            tail = " (git error or unrelated histories). Run git in the engine checkout to see the error."
        return {
            "state": "indeterminate",
            "cause": cause,
            "running_sha": running_sha,
            "floor_sha": floor_sha,
            "reason": reason,
            "notice": head + tail,
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


def _same_commit(a: str, b: str) -> bool:
    """Prefix equality: a subject-only stamp may be abbreviated to 7+ hex."""
    return a.startswith(b) or b.startswith(a)


def _git_is_behind(running_sha: str, floor_sha: str, repo: Path) -> Optional[bool]:
    if running_sha == floor_sha:
        return False

    verdict, reason = git_predicate(
        repo,
        ["merge-base", "--is-ancestor", running_sha, floor_sha],
        timeout=10,
    )
    if verdict == PROBE_UNKNOWN:
        print(f"skip: _git_is_behind: could not determine ancestry: {reason}", file=sys.stderr)
        return None
    return verdict == PROBE_YES


def _diagnose_indeterminate(floor_sha: str, engine_dir: Path) -> str:
    """Why ancestry failed in `engine_dir` (the ancestry repo): runs only on the
    indeterminate path (at most two spawns).

    Floor present, or the probe itself failing, is a generic git error; floor absent splits
    on whether the clone is shallow.
    """
    verdict, _ = git_predicate(
        engine_dir,
        ["rev-parse", "--verify", "--quiet", f"{floor_sha}^{{commit}}"],
        timeout=10,
    )
    if verdict != PROBE_NO:
        return CAUSE_GIT_ERROR
    shallow = run_git(
        ["-C", str(engine_dir), "rev-parse", "--is-shallow-repository"],
        env=scoped_git_env(),
    )
    if shallow.ok and shallow.stdout.strip() == "true":
        return CAUSE_SHALLOW
    return CAUSE_FLOOR_ABSENT if shallow.ok else CAUSE_GIT_ERROR


def probe_engine_drift(engine_dir: Optional[Path] = None) -> dict:
    """Probe the engine at `engine_dir` (default: the running engine's own checkout).

    Resolves the source-head, picks the ancestry repo (the engine repo for a source
    tree; engine_root.engine_source_root() for a mirror, None when unregistered), and
    delegates the three-state decision to classify_drift(). Adds `basis` and
    `engine_head`. `running_sha` is the source-head that was checked.
    """
    from coordinator_core.engine_root import engine_source_root

    engine_dir = engine_dir if engine_dir is not None else Path(__file__).resolve().parent.parent
    head = resolve_engine_source_head(engine_dir)
    floor = MIN_KNOWN_GOOD_SHA
    mirror = head.basis == "mirror-stamp"

    repo: Optional[Path]
    if mirror:
        source = engine_source_root() if head.sha is not None else None
        repo = Path(source) if source else None
    else:
        repo = engine_dir

    def is_behind(running: str, floor_sha: str) -> Optional[bool]:
        if _same_commit(running, floor_sha):
            return False
        if repo is None:
            return None
        return _git_is_behind(running, floor_sha, repo)

    def diagnose(floor_sha: str) -> str:
        if repo is None:
            return CAUSE_SOURCE_HISTORY_UNAVAILABLE
        return _diagnose_indeterminate(floor_sha, repo)

    result = classify_drift(
        head.sha, floor, is_behind, diagnose,
        unresolved_cause=CAUSE_MIRROR_UNSTAMPED if mirror else None,
    )
    result["basis"] = head.basis
    result["engine_head"] = head.engine_head
    return result


@register_op("engine.drift")
def _engine_drift(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'engine.drift' handler — probe the running engine's freshness.

    Params: none consumed (no engine_dir over RPC).
    repo_root: unused — this op inspects the engine's OWN checkout (Path(__file__)),
    not the caller's repo.

    Returns probe_engine_drift()'s result: state, running_sha (the source-head
    checked), floor_sha, basis, engine_head, plus offer/notice/cause per state.
    """
    return probe_engine_drift()
