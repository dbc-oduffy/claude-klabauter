"""
coordinator_core.ops.handoff_housekeeping — `handoff.housekeeping`, the ONE
handoff-housekeeping job.

Purpose: close finished handoffs, file them into `archive/handoffs/`, and sweep
up consumed ones — as a single call over a single sweep scan, under the 200ms
process-time brightline. Governing plan:
`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`.

WHY THIS MODULE EXISTS AT ALL, since "there were already three ops for this" is
the obvious objection. There were, and all three are dead:

    handoff.reconcile_open            SUSPENDED (registered AND suspended)
    handoff.archive_transition        SUSPENDED (and decorator-removed)
    session.sweep_consumed_handoffs   SUSPENDED

They were killed under a 200ms process-time bar for the cost of a corpus walk
they CALLED rather than work they did — `handoff_reconcile` computed the
live+archived gate index unconditionally and consumed it only for handoffs in
`deployment_state: awaiting_gate`, 16 of 253 on the real corpus. That walk is
now lazy (see `handoff_reconcile._load_gate_index`). Reviving any of those three
KEYS is forbidden — kill means kill forever, PM 2026-08-23 — so this is a new
name over the surviving computes, not a restoration. Do not re-decorate them.

Negative spec:
  - This op does NOT reimplement scanning, terminality, or archival. It composes
    `handoff_reconcile`'s close pass with `archive_terminal_handoffs.plan_sweep`
    and `fleet._common.archive_and_commit`. A fourth mechanism that decides
    terminality is the shape to refuse.
  - No live-children/childlessness ground anywhere in this path. Deleted from
    all four sites on 2026-08-28 (PM: "has a child means nothing to whether it
    should be archived or not... either a baton is used up or it's not"). A
    live claim HOLDER still retains; that is a different ground.
  - `cap` is REQUIRED and positive. No unbounded default — both legs this
    replaces refuse an absent cap as a setup error, and so does this.
  - Never measured in wall clock. On a box running 50-70 concurrent sessions
    wall clock measures the peers, not the op.

STEP ORDER IS LOAD-BEARING, and the second read is not redundant:

    0. transition       — OPTIONAL, one named handoff (see `_transition_one`).
                          Stamps `deployment_state: continued` on d6's predecessor,
                          which is a state step 2 already treats as terminal — so
                          the stamp and the sweep compose rather than duplicating
    1. close finished   — live-only corpus read (~15.6ms), mutates deployment_state
    2. plan_sweep       — ONE scan, covering both what step 1 just made terminal
                          AND already-consumed handoffs; classification only
    3. archive_and_commit — one commit for the whole set

Step 2 must re-scan because step 1 mutates the states step 2 selects on. Fusing
them into one read would mean filing against a pre-close view and missing every
handoff this very call just closed — the exact gap that made the sweep leg a
separate op in the first place.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import archive_and_commit, main_worktree_root
from coordinator_core.ops.fleet.archive_terminal_handoffs import plan_sweep

_LOG = logging.getLogger(__name__)

OP_KEY = "handoff.housekeeping"


def _setup_error(reason: str) -> Dict[str, Any]:
    """A caller/setup fault, not a repository state — exit_code 1, nothing done."""
    return {
        "exit_code": 1,
        "error": reason,
        "closed": [],
        "surfaced": [],
        "archived": [],
        "skipped": [],
        "failed": [],
        "close_error": None,
        "transition": None,
    }


def _close_finished(repo_root: Path) -> Dict[str, Any]:
    """Step 1 — the close pass, reached in-process.

    `handoff_reconcile._handler` is SUSPENDED as an op and survives as a
    library. Reaching a suspended op's compute in-process is an existing,
    sanctioned shape here (`archive_stamp._call_handoff_archive_transition`
    does the same for `handoff_archive_transition`) — it is a fact to work
    with, never a licence to re-register the killed key.

    Deferred import: keeps this module's body inert for the warm-serve
    classifier and keeps the import off the path when the caller only sweeps.
    """
    from coordinator_core.ops import handoff_reconcile

    return asyncio.run(handoff_reconcile._handler({}, repo_root))


def _transition_one(params: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    """Step 0 — the TARGETED transition, for a caller naming one handoff.

    Why this leg exists at all, given steps 1-3 already close and file the whole
    corpus. The corpus legs are population-shaped: they answer "which handoffs
    are terminal?" and file everything that is. A succession is not that. `d6`
    (`baton_assemble/apply.py :: _dispatch_handoff_supersede_predecessor`) has
    one named predecessor and one named successor, and must stamp
    `deployment_state: continued` + `continued_into: <successor>` on THAT file
    specifically — a fact no scan can derive, because the successor was minted
    seconds ago by the same run. Without this leg, d6 has no door and the
    predecessor is stranded non-terminal forever, which is the outage this whole
    plan exists to end.

    THE STAMP AND THE SWEEP COMPOSE, which is why this is step 0 and not a
    separate op. `continued` is in `_TERMINAL_DEPLOYMENT_STATES` (verified:
    `abandoned`, `closed`, `continued`, `shipped`), so a predecessor stamped here
    is terminal by the time `plan_sweep` runs at step 2 and gets filed by the
    same call — the ordering argument in this module's docstring, reached from
    the other end.

    `handoff_archive_transition._handler` is reached as a LIBRARY, exactly as
    `_close_finished` reaches `handoff_reconcile`. Its op key is suspended and
    stays suspended; its four modes and the Position-A no-branch-tip-fallback
    contract are load-bearing and already correct, so this composes them rather
    than re-typing them (`state/lessons/2026-08-26-a-killed-op-s-name-is-not-
    the-capability-*.yaml`: the kill bar binds the offending WRAPPER, and the
    mechanism underneath can be untouched and still load-bearing).

    Returns the op's result VERBATIM. Callers key on its own fields — `superseded`
    for d6, `retained`/`moved` for `archive_stamp`'s wrappers — and a re-shaped
    envelope here would silently break every one of those predicates.
    """
    from coordinator_core.ops.handoff_archive_transition import _handler as _transition

    return asyncio.run(_transition(dict(params), repo_root=repo_root))


def _handler(params: dict, repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """`handoff.housekeeping` — close finished handoffs, file them, sweep consumed.

    SYNCHRONOUS at this boundary, mirroring `archive_terminal_handoffs._handler`
    and `sweep_consumed_handoffs._handler`: `plan_sweep` is sync and only the
    commit leg is a coroutine, so every `asyncio.run(...)` here is scoped to one
    call rather than making the whole op async for its callers.

    `repo_root` is the git common dir, matching the `_OP_KEY_SCOPE` convention
    `fleet.archive_completed_handoffs` uses.

    Params:
        cap (int, REQUIRED)   positive move cap; absent/non-positive is a setup
                              error, never an unbounded default.
        close (bool)          run step 1. Default True. False sweeps only —
                              for a caller that has just closed records itself.
        transition (dict)     OPTIONAL step 0 — a targeted transition on ONE
                              named handoff, passed verbatim to
                              `handoff_archive_transition._handler` (see
                              `_transition_one`). Absent means corpus-only
                              housekeeping. The op's own result comes back
                              under `transition`, unmodified; a caller keying
                              on `superseded`/`retained`/`moved` reads it there.

    A FAILED TRANSITION STOPS THE CALL, unlike a failed close pass. The two are
    not the same kind of failure: the close pass is a population sweep whose
    failure leaves the archival job still worth doing, while a transition is a
    caller's one named handoff — d6's predecessor — and sweeping on past it
    would report a green housekeeping run over a succession that never landed.
    That silent-green-over-a-stranded-predecessor shape is the exact defect d6's
    own fail posture was written against.
    """
    if repo_root is None:
        return _setup_error("repo_root handler arg is None")

    common_dir = repo_root if isinstance(repo_root, Path) else Path(repo_root)

    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return _setup_error(
            f"cap is required and must be a positive int, got {cap!r} — no "
            f"unbounded default (mirrors fleet.archive_completed_handoffs's "
            f"own cap-axis decision)"
        )

    transition_params = params.get("transition")
    if transition_params is not None and not isinstance(transition_params, dict):
        return _setup_error(
            f"transition must be a dict of handoff_archive_transition params, "
            f"got {type(transition_params).__name__}"
        )

    worktree = main_worktree_root(common_dir)

    transition: Optional[Dict[str, Any]] = None
    if transition_params:
        try:
            transition = _transition_one(transition_params, common_dir)
        except Exception as exc:
            _LOG.warning("%s: transition failed — %s", OP_KEY, exc, exc_info=True)
            out = _setup_error(f"transition failed: {exc}")
            out["transition"] = None
            return out
        if transition.get("exit_code") != 0:
            # Stop before the sweep. The named handoff may be half-stamped, and
            # a commit landed by step 3 on top of that state would bury the
            # failure under a green result.
            out = _setup_error(
                f"transition on {transition_params.get('handoff_path')!r} failed: "
                f"{transition.get('error')!r}"
            )
            out["transition"] = transition
            return out

    closed: List[Any] = []
    surfaced: List[Any] = []
    close_error: Optional[str] = None
    if params.get("close", True):
        try:
            close_result = _close_finished(common_dir)
        except Exception as exc:  # a close failure must not eat the sweep
            _LOG.warning("%s: close pass failed — %s", OP_KEY, exc, exc_info=True)
            close_result = {"gates_cleared": [], "surfaced": [], "error": str(exc)}
        closed = close_result.get("gates_cleared") or []
        surfaced = close_result.get("surfaced") or []
        # A close pass that failed must not eat the sweep, but it must not
        # vanish either: `handoff_reconcile._handler` reports its own failures
        # as `exit_code: 1` with empty lists, which is byte-identical to a
        # clean run over a corpus with nothing to close. Without this field a
        # caller cannot tell "nothing needed closing" from "the close pass
        # died", and the archival outage this op exists to end would recur
        # silently one layer down.
        if close_result.get("error"):
            close_error = str(close_result["error"])
        elif close_result.get("exit_code"):
            close_error = f"close pass returned exit_code {close_result['exit_code']}"

    try:
        moves, skipped = plan_sweep(worktree, common_dir, cap)
    except Exception as exc:
        _LOG.warning("%s: plan_sweep failed — %s", OP_KEY, exc, exc_info=True)
        out = _setup_error(f"plan_sweep failed: {exc}")
        out["closed"] = closed
        out["surfaced"] = surfaced
        out["close_error"] = close_error
        out["transition"] = transition
        return out

    if not moves:
        return {
            "exit_code": 0,
            "closed": closed,
            "surfaced": surfaced,
            "archived": [],
            "skipped": skipped,
            "failed": [],
            "close_error": close_error,
            "transition": transition,
        }

    subject = (
        f"housekeeping: close, file and sweep {len(moves)} handoff(s)\n\n"
        f"Archived via {OP_KEY}."
    )
    archived, failed = asyncio.run(archive_and_commit(worktree, moves, subject))

    return {
        "exit_code": 0,
        "closed": closed,
        "surfaced": surfaced,
        "archived": archived,
        "skipped": skipped,
        "failed": failed,
        "close_error": close_error,
        "transition": transition,
    }


register_op(OP_KEY, _handler)
