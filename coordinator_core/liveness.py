"""liveness.py — Canonical liveness seam for coordinator_core.

Purpose: Provide the two liveness predicates (+ the heartbeat write) that
coordinator_core operations depend on. As of the de-bash W2 leg (2026-07-19)
this module DELEGATES to the in-process native port
``coordinator_core.session.liveness`` / ``coordinator_core.session.core``
rather than shelling out to the example-doctrine-repo bash ``coordinator-session.sh`` — the
Windows critical path can no longer depend on a POSIX shell (PM mandate:
kill ALL bash on the critical path). The public surface
(``resolve_live_session_ids`` / ``cs_claim_holder_live`` /
``update_last_activity``) is UNCHANGED so no caller regresses.

``cs_claim_holder_live`` error contract (2026-07-21 fix, cross-repo memo
2026-07-14 claude-central-em): this function PROPAGATES exceptions from the
native port rather than catching them and returning ``False``. A caught
exception is an INDETERMINATE liveness read, never a confirmed-dead one, and
collapsing it to ``False`` silently authorized claim takeover / reaping of a
session that might still be alive. See that function's own docstring for the
full rationale and the call-site fail-closed contract every current caller
now implements.

RAW-PID-LIVENESS floor (docs/wiki/coordinator-tripwires.md § RAW-PID-LIVENESS):
this module still MUST NOT call ps -p, kill -0, or psutil.pid_exists on any
stored ``pid`` field. The native port it delegates to preserves the two-layer
model — Layer 1 keys on the separate ``stable_pid`` (POSIX ``ps -o lstart=``,
Windows ``psutil.create_time()``), never on the dead per-hook ``pid``.

Single-liveness-key invariant (D5, pcore-03): every consumer routes through the
one ``session.liveness.session_live`` decision; no PID fields are duplicated
into claim dirs or any Python structure here.

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § D5,
§ C0, § AC9; docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1
(native port); de-bash W2 liveness leg (seam wire).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _session_liveness

# Review: code-reviewer (F1) — module-level logger matches every other coordinator_core module;
# allows debug-level signal on update_last_activity failures instead of silent pass.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lib path resolution — RETAINED for the ceremony_lock test that imports
# _lib_path, and for callers that still want the on-disk successor path (e.g.
# diagnostics). Production liveness no longer shells to any coordinator-
# session lib — it delegates to the native session.* port above. The
# 3-rung __file__-walk + resolve-coordinator-clone subprocess ladder this
# function used to run is GONE: coordinator-session.sh was retired
# repo-wide (migrated to claude-klabauter's coordinator/lib/coordinator_session.py,
# not to a example-doctrine-repo-side sibling __file__ can walk to), so every one of those
# rungs always missed. The ladder collapses to a single call through the
# canonical claude-klabauter-root resolver (coordinator_core.claude_klabauter_root) — no
# __file__-walking, no hardcoded sibling names, no subprocess spawn (and
# therefore no 15s hang path if CLAUDE_KLABAUTER_ROOT can't be resolved).
# ---------------------------------------------------------------------------
_CACHED_LIB: Optional[str] = None


def _lib_path() -> Optional[str]:
    """Return path to the successor of coordinator-session.sh
    (<claude_klabauter_root>/coordinator/lib/coordinator_session.py), or None if
    CLAUDE_KLABAUTER_ROOT can't be resolved or the file isn't there."""
    global _CACHED_LIB
    if _CACHED_LIB is not None:
        return _CACHED_LIB

    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        logger.debug("coordinator_core.liveness: _lib_path: CLAUDE_KLABAUTER_ROOT unresolvable: %s", exc)
        return None

    candidate = Path(claude_klabauter_root) / "coordinator" / "lib" / "coordinator_session.py"
    if candidate.is_file():
        _CACHED_LIB = str(candidate)
        return _CACHED_LIB

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Short-TTL per-process cache for resolve_live_session_ids() (C12 — originally
# Windows bash-spawn cost hardening; RETAINED post-native-port).
#
# The native live_session_ids() pass is a single in-process meta.json scan — no
# per-dir subprocess spawns — so the raw cost is far lower than the old bash
# shell-out. The TTL cache is kept anyway because the archival/pickup hot path
# (archive_handoffs._is_terminal) calls this once per scanned handoff, and the
# underlying 30-minute recency window makes sub-second staleness immaterial: the
# cached frozenset IS the exact value the uncached pass would return, just reused
# within one scan pass. cs_claim_holder_live is deliberately NOT cached — its
# callers (session.reap) take two sequential fresh reads of the SAME claim_path
# for TOCTOU detection, and caching would silently defeat that race check.
# ---------------------------------------------------------------------------
_LIVE_IDS_CACHE_TTL_SEC = 2.0
_live_ids_cache: Optional[Tuple[float, FrozenSet[str]]] = None


def _reset_live_ids_cache() -> None:
    """Test-only helper: clear the resolve_live_session_ids() TTL cache."""
    global _live_ids_cache
    _live_ids_cache = None


def resolve_live_session_ids() -> FrozenSet[str]:
    """Return the set of currently-live session IDs.

    Delegates to the native ``coordinator_core.session.liveness.live_session_ids``
    (a single in-process meta.json scan preserving the two-layer verdict) — no
    bash shell-out. Both this seam and ``core.resolve_session_id``'s tier-4 guard
    route through the same native decision so there is exactly one liveness
    implementation.

    Returns frozenset (empty on any error) so callers can use set operations
    without error handling.

    RAW-PID-LIVENESS: does NOT call ps -p / kill -0 / psutil.pid_exists on any
    stored ``pid`` field (the native port keys Layer 1 on ``stable_pid``).

    Cached for _LIVE_IDS_CACHE_TTL_SEC seconds (monotonic clock) — see the
    module-level cache comment above for the correctness rationale (30-min
    liveness recency window >> a few seconds of staleness).
    """
    global _live_ids_cache
    now = time.monotonic()
    if _live_ids_cache is not None:
        cached_at, cached_value = _live_ids_cache
        if now - cached_at < _LIVE_IDS_CACHE_TTL_SEC:
            return cached_value
    result = _resolve_live_session_ids_uncached()
    _live_ids_cache = (now, result)
    return result


def _resolve_live_session_ids_uncached() -> FrozenSet[str]:
    """Uncached body of resolve_live_session_ids() — see that function's
    docstring for the contract. Split out so the TTL-cache wrapper above can sit
    in front of it. Delegates straight to the native port; a defensive
    ``except`` preserves the seam's empty-on-error contract."""
    try:
        return _session_liveness.live_session_ids()
    except Exception as exc:
        # Empty-on-error seam contract (see docstring) — should never fire on
        # a healthy run; logged at debug so a systematic native-port failure
        # is diagnosable rather than silently degrading every liveness check.
        logger.debug("coordinator_core.liveness: live_session_ids() failed: %s", exc)
        return frozenset()


def update_last_activity(session_dir: str, iso: str) -> None:
    """Write the last_activity field in a session's meta.json (heartbeat).

    Purpose: Thin seam for the hooks.session_heartbeat bookkeeping op (pcore-08 C2).
    Delegates to the native ``core.update_meta_field`` (atomic tempfile +
    os.replace rewrite) — the SAME single-writer implementation every other meta
    write now routes through, which IS the enforcement of the single-liveness-key
    invariant (no dual Python/bash writer racing the same file). No bash
    shell-out.

    On any error (missing meta.json, non-writable dir) the call is a silent
    no-op — heartbeat is best-effort; a missed write does not warrant raising in
    the bookkeeping handler.

    Args:
        session_dir: Absolute path to the session registry dir
                     (e.g. .git/coordinator-sessions/<sid>/).
        iso:         ISO-8601 timestamp string to write as last_activity.

    Spec backlink: docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md § D4, C0
    """
    # Review: code-reviewer (F3) — guard against empty or relative session_dir and
    # empty iso; a relative path would resolve against the process cwd rather than
    # the session registry, and core.update_meta_field rejects an empty value.
    if not session_dir or not os.path.isabs(session_dir):
        return
    if not iso:
        return
    try:
        ok = _session_core.update_meta_field(session_dir, "last_activity", iso)
        if not ok:
            logger.debug(
                "coordinator_core.liveness: update_last_activity no-op (meta.json "
                "missing or unwritable) for %s",
                session_dir,
            )
    except Exception as exc:
        # best-effort; heartbeat miss is non-fatal — but log at debug so systematic
        # failures are diagnosable.
        logger.debug("coordinator_core.liveness: update_last_activity error: %s", exc)


def cs_claim_holder_live(claim_path: str) -> bool:
    """Return True iff the session holding claim_path is currently live.

    Delegates to the native ``coordinator_core.session.liveness.claim_holder_live``
    — no bash shell-out. session_id-bearing claim dirs cross-reference the held
    session against the registry via ``session_live`` (two-layer); legacy pid-only
    dirs fall back to the ephemeral-pid test ("always dead in-harness"), self-
    healing to session_id on first takeover.

    RAW-PID-LIVENESS: does NOT call ps -p / kill -0 / psutil.pid_exists on any
    stored ``pid`` field as a session-liveness gate. All liveness state is
    centralised in the session-registry meta.json (single-liveness-key invariant,
    D5). Deliberately UNCACHED — reap.py takes two sequential fresh reads of the
    same claim_path for TOCTOU detection.

    Args:
        claim_path: Path to the claim directory (e.g.
            .git/coordinator-sessions/<sid>/claims/<artifact>/).

    Returns:
        True  — the holder session is confirmed live.
        False — the holder session is confirmed NOT live (a clean, successful
                liveness read that resolved to dead).

    Raises:
        Whatever ``_session_liveness.claim_holder_live`` raises (e.g.
        ``ValueError`` on an empty/missing ``claim_path``, or
        ``coordinator_core.session.core.MissingPsutilError`` on Windows when
        psutil is absent). This function does NOT catch and does NOT
        collapse an indeterminate/errored read into ``False``.

        Cross-repo memo 2026-07-14 (claude-central-em, "the exception-swallow
        is on YOUR side") flagged that a prior version of this bridge caught
        every exception here and returned ``False`` — indistinguishable from
        a confirmed-dead verdict. Downstream that ``False`` authorizes claim
        takeover / reaping of a session that might still be alive; the error
        was never actually "indeterminate", it was silently promoted to
        "definitely dead". EVERY current caller of this function (session.reap,
        ops.fleet.archive_handoffs, ops.handoff_reconcile,
        ops.ceremony.ceremony_lock, ops.fleet.archive_actioned_memos) now
        wraps this call in its own try/except and fails closed toward
        "assume alive, do not reap/archive/reclaim" on any exception — so the
        indeterminate case is handled at the call site, with the fallback
        semantics appropriate to that call site (e.g. archive_handoffs falls
        through to a secondary consumed_by-liveness check rather than a
        blanket keep). Swallowing here would collapse all of that call-site
        nuance back into a single silent false-dead verdict.

    Negative-spec: does NOT catch ``Exception`` and return ``False`` — that is
    the exact defect this function exists to no longer have (see Raises above).
    """
    return _session_liveness.claim_holder_live(claim_path)
