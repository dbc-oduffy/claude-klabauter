"""
coordinator_core.session.core — Python engine port of the retained-in-hub
helpers from coordinator-session.sh (Port of: DoE e34f2484, 2026-07-22) —
the functions T0-decompose could NOT bash-extract into
``coordinator/lib/session/*.sh``:
git-root/session-dir resolution, clock helpers, PID/liveness primitives,
meta.json read/write, the confined-findings-agent SSOT, and ``cs_init``).

Every other session-registry resolver in this package transitively depends
on ``git_root()`` via cwd — see that function's docstring for the
NOT-cached-across-calls constraint this preserves from the bash original.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § core.py
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4a-g1

Negative-spec:
    - Do NOT call ``ps -p``/``kill -0``/``psutil.pid_exists`` on a stored
      ``pid`` field for a LIVENESS decision — that field is a dead
      per-hook-subshell ``$$``, not the long-lived session process (see
      ``pid_alive()`` docstring). Only ``stable_pid_alive()`` (keyed on the
      separate ``stable_pid`` field) is a legitimate liveness signal.
    - Do NOT duplicate PID/liveness fields into any structure outside the
      session registry's ``meta.json`` — RAW-PID-LIVENESS floor
      (docs/wiki/coordinator-tripwires.md) and the single-liveness-key
      invariant (D5, pcore-03) both forbid it.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional

from coordinator_core.git.repo_root import git_common_dir, show_toplevel

# Deferred-import accessor (hot-path import diet round 2 — docs/plans/
# 2026-08-08-seven-measured-levers-load-norm.md § C1). psutil measured
# 6.31ms self-time on a bare `ping`, which needs none of the liveness
# machinery below. See coordinator_core.ipc's `_log()` for the general
# pattern this mirrors (identity-preserving deferred import, resolved once
# and cached — never re-attempted on a hot path).
#
# The cache is the module attribute `psutil` itself (not a private
# `_psutil_mod`) so the pre-existing test-suite monkeypatch surface
# (`monkeypatch.setattr(core, "psutil", None)` to simulate absence) keeps
# working unmodified. `_UNRESOLVED` (not `None`) is the "not yet imported"
# sentinel so a prior ImportError can be cached as `None` without being
# confused with "haven't tried yet" on the next call. This preserves the
# ORIGINAL try/except ImportError semantics exactly: every call site below
# still sees either the real module or `None`, and every load-bearing site
# still raises MissingPsutilError instead of silently degrading (it is NOT
# license for silent degradation anywhere psutil is the sole liveness
# mechanism — Windows entirely, POSIX's stable_pid_alive Layer 1 since the
# 2026-07-27 ps-to-psutil port). ``pid_alive`` on POSIX is the sole
# surviving `os.kill` user; see that docstring / ``_win_create_time_epoch``.
# Generator-provenance declaration (generator_provenance.py). update_meta_field/
# update_meta_fields write only `<session_dir>/meta.json` under
# `.git/coordinator-sessions/<sid>/` -- git-internal session-hub state, never a
# tracked repo artifact.
GENERATES = []

_UNRESOLVED = object()
psutil = _UNRESOLVED  # type: ignore[assignment]

#: Session directories this PROCESS has already tried, and failed, to stamp a
#: ``stable_pid`` onto. Read only by ``ensure_session``'s re-stamp arm.
#:
#: Why this exists, measured. The re-stamp arm re-runs ``init()`` on every call
#: whose record carries an EMPTY ``stable_pid``. That is correct as a REPAIR --
#: it is the only path that can stamp a record created record-but-unstamped --
#: but ``session/scope.py::touch`` calls ``ensure_session`` on every sanctioned
#: mutating op, so on a box where the stamp can never succeed the repair
#: re-runs Guard-1's psutil parent inspection on EVERY touch instead of once.
#: Measured 2026-08-26 on this box (RUSAGE process time, n=400): the re-stamp
#: arm costs **0.29ms per call against the stamped fast arm's 0.021ms -- 13.5x**
#: -- and the live corpus shows a median of 19 touches per session and a
#: maximum of 65, so the waste is ~5ms typical and ~19ms worst observed, per
#: session, spent re-deriving an answer that provably cannot change.
#:
#: The affected population is not hypothetical and is exactly the one K-006 is
#: about: POSIX hosts where Guard-1's parent-name check misses AND ``CLAUDE_PID``
#: does not resolve. Every session on host `machine-b` stamped
#: ``posix-parent-miss:name-mismatch`` on 2026-08-22 before the CLAUDE_PID leg
#: landed (see ``init``'s POSIX leg (b) comment).
#:
#: PROCESS-LOCAL BY DESIGN, and that is the whole safety argument -- it is why
#: this is a memo and not a marker file. Every input Guard-1 reads is constant
#: for the life of a process (``os.getppid()``, the parent's own name,
#: ``CLAUDE_PID`` in this process's environment, whether psutil imported), so
#: within one process a second attempt cannot produce a different answer. A
#: NEW process re-attempts from scratch. A session that becomes stampable later
#: therefore still gets stamped by the next process to touch it, and K-006's
#: Layer-1 gap cannot be silently reopened the way a persisted "gave up" marker
#: on disk would reopen it.
#:
#: Keyed on the session DIRECTORY, not the bare sid: two worktrees can hold the
#: same sid, and they are different records.
_STAMP_ATTEMPTED: "set[str]" = set()

#: Bound on ``_STAMP_ATTEMPTED``. The warm server is long-lived and serves many
#: sessions, so an unbounded set is a slow leak. On overflow the whole set is
#: dropped rather than evicted one-by-one: the cost of forgetting is one extra
#: attempt per session, which is the behaviour this memo started from, so the
#: cheap policy is also the safe one.
_STAMP_ATTEMPTED_MAX = 512


def reset_stamp_attempt_memo() -> None:
    """Clear the process-local stamp-attempt memo. For tests, and for any
    caller that has reason to believe Guard-1's inputs changed under it."""
    _STAMP_ATTEMPTED.clear()


def _psutil():
    global psutil
    if psutil is _UNRESOLVED:
        try:
            import psutil as _imported_psutil
        except ImportError:
            psutil = None
        else:
            psutil = _imported_psutil
    return psutil


class MissingPsutilError(RuntimeError):
    """Raised when a Windows-only, psutil-dependent liveness path is reached
    with psutil absent. psutil is a required engine dependency — there is no
    correct fallback for "is this Windows PID alive?" without it, and
    returning/propagating False here is indistinguishable from a genuinely
    dead process (the exact wrongful-claim-takeover shape this class exists
    to prevent: every session would read DEAD)."""

#: Platform seam for the PID-liveness branches (POSIX ``ps`` vs Windows
#: ``psutil.create_time()``). A dedicated constant — not an inline
#: ``os.name == "nt"`` — so a test can exercise the Windows create_time path on
#: a POSIX host (psutil is cross-platform) WITHOUT globally forcing
#: ``os.name = "nt"`` (which would flip ``pathlib`` to the un-instantiable
#: ``WindowsPath`` on POSIX). Read at call time, so a test may monkeypatch it.
_IS_WINDOWS = os.name == "nt"

#: Port of ``_cs_is_confined_findings_agent``'s CONFINED SET — a single
#: member as of the 2026-07-01 findings-agents-self-persist change. Review
#: personas were removed from this set on 2026-07-01 (see bash source
#: comment for rationale — they are trusted full-tool Opus agents, not
#: confined). Adding a member here is the single edit point.
_CONFINED_FINDINGS_AGENTS = frozenset({"coordinator:code-reviewer"})

#: Port of ``_cs_lstart_to_epoch``'s python-fallback strptime format —
#: matches ``ps -o lstart=`` output exactly (e.g. "Tue Jul 14 15:26:28 2026").
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

#: Tolerance (seconds) for comparing a psutil-derived ``create_time()``
#: epoch against a STORED ``stable_pid_start_epoch``. Every ``meta.json``
#: on disk as of the 2026-07-27 ps-to-psutil port was written with an epoch
#: derived from ``lstart_to_epoch(ps -o lstart=)`` — a different derivation
#: path than ``int(psutil.Process(pid).create_time())`` for the SAME
#: process, and the two can legitimately differ by ~1s (independent
#: truncation/rounding; ``/proc`` btime rounding on Linux). Under exact
#: equality that skew reads every currently-live persisted session as DEAD —
#: precisely the wrongful-takeover failure this function exists to prevent.
#: A recycled PID whose birth instant falls within this window of the
#: original process's is not a realistic collision, so the marginal
#: PID-reuse detection loss is negligible against the certainty of breaking
#: every persisted record. See ``docs/plans/2026-06-27-liveness-first-claim-staleness.md``.
_STABLE_PID_EPOCH_TOLERANCE_SECS = 2

#: Bound on how many rungs the Windows Guard-1 ancestor walk climbs looking
#: for a "claude"-named process. Depth is caller-dependent and has measured
#: differently for the two callers: 4 hops from ``os.getppid()`` in a Bash
#: TOOL subprocess (trampoline x3), but only 2 in a HOOK — the caller that
#: actually reaches Guard-1 — whose measured chain is ``python3.exe <-
#: bash.exe <- bash.exe <- claude.exe`` (15 sessions, 2026-08-08). That the
#: constant's basis moved once already is why ``CLAUDE_PID`` is now preferred
#: over this walk — preferred for topology-drift resilience (a new
#: trampoline shape needs no re-measurement), NOT because the walk is
#: broken or narrowed for the hook path; the bound below is unchanged and
#: still correctly generous for both callers, kept only as the fallback.
#: The value is the Bash-tool figure doubled plus a couple, for headroom against a
#: deeper trampoline (e.g. an extra shell layer) without walking unbounded —
#: an unbounded walk on a psutil call per rung risks a slow/hung hot-path
#: init() if the process tree is malformed or absurdly deep.
_STABLE_PID_WINDOWS_ANCESTOR_DEPTH = 10

#: Collapse runs of whitespace — mirrors the bash helper's ``tr -s ' '``
#: BSD-double-space normalization (single-digit %e day pad on macOS/BSD ps).
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# git-root / session-dir resolution
# ---------------------------------------------------------------------------


def git_root(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_git_root``: the git root for the given/current cwd, or
    ``""`` on failure (not a git repo, git missing, etc.).

    Deliberately NOT cached across calls — cwd can legitimately change
    mid-process for a long-lived import (e.g. a reaper's documented
    cwd-relative resolution discipline). Every caller that needs a
    fixed root must pass ``cwd`` explicitly rather than rely on process-wide
    caching.
    """
    return show_toplevel(cwd) or ""


def _sessions_dir_resolve(cwd: Optional[str]) -> str:
    """Uncached resolution of the session hub path, shared by
    ``sessions_dir()``'s ``cwd is None`` branch and by
    ``_sessions_dir_cached()``; see ``sessions_dir`` docstring for the
    caching policy layered on top of this.

    Delegates to ``coordinator_core.git.repo_root.git_common_dir``, which
    answers from a filesystem walk and only falls back to a
    ``git rev-parse --git-common-dir`` spawn when no ``.git`` entry is
    found (and caches that fallback). This function therefore usually makes
    NO subprocess call at all — it previously spawned inline and its
    docstring still claimed to be "the sole place this repo shells out for
    the session hub path", which outlived the delegation. Three tests in
    ``tests/test_core.py`` were written against that claim and counted
    ``core.subprocess.run``; they now count resolver calls, because the
    property worth pinning here is the CACHE, not a spawn the engine
    deliberately stopped making.
    """
    common = git_common_dir(cwd)
    if not common:
        return ""
    return str(Path(common) / "coordinator-sessions")


class _SessionsDirResolutionFailed(Exception):
    """Internal-only signal raised by ``_sessions_dir_cached`` on a failed
    resolution, so ``functools.lru_cache`` does NOT memoize it —
    ``lru_cache`` never caches a call that raises. See ``sessions_dir``
    docstring, point (3): a failure must not poison the cache entry for the
    rest of the process the way a successful resolution legitimately can.
    """


@functools.lru_cache(maxsize=32)
def _sessions_dir_cached(cwd: str) -> str:
    result = _sessions_dir_resolve(cwd)
    if not result:
        raise _SessionsDirResolutionFailed(cwd)
    return result


def reset_sessions_dir_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``sessions_dir()`` cache. Call this in test teardown/setup for any test
    that creates a git repo under a path that could collide with an entry
    left behind by an earlier test in the same process (e.g. reusing a
    fixed ``tmp_path``-adjacent path across parametrized runs); ``tmp_path``
    itself is already unique per test so ordinary tests do not need this."""
    _sessions_dir_cached.cache_clear()


def sessions_dir(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_sessions_dir``: ``<git-common-dir>/coordinator-sessions``,
    or ``""`` if not in a git repo.

    Resolves the session hub via ``git rev-parse --git-common-dir`` rather
    than joining a literal ``".git"`` onto ``git_root()``'s toplevel. In a
    linked git worktree, ``<worktree>/.git`` is a gitdir-pointer FILE, not a
    directory — joining onto it and calling ``mkdir`` raises
    ``NotADirectoryError``. ``--git-common-dir`` instead resolves to the MAIN
    worktree's real ``.git`` directory from any worktree of the repo. In a
    normal (non-worktree) repo this is byte-identical to the prior
    ``<root>/.git`` behavior — no existing session hub relocates.

    Deliberately uses ``--git-common-dir``, NOT ``--git-dir``: ``--git-dir``
    inside a worktree returns the worktree-PRIVATE
    ``<main>/.git/worktrees/<name>`` directory, which would give each
    worktree of one repo its own private claim namespace under
    ``memo-claims/``, ``handoff-claims/``, ``plan-claims/`` — silently
    permitting two sessions in different worktrees of the SAME repo to claim
    the same memo/handoff/plan concurrently, exactly the collision the claim
    locks exist to prevent. Claims must contend across every worktree of one
    repo, so the hub is the shared common dir, never the worktree-local one.

    Uses ``--path-format=absolute`` so git emits an absolute path directly —
    avoids the trap where ``Path(x).resolve()`` on a relative
    ``--git-common-dir`` result (the common case, a bare ``".git"``) would
    resolve against the CURRENT PROCESS cwd instead of the subprocess's
    ``cwd``, silently producing a wrong path.

    Caching policy (added to eliminate repeat byte-identical spawns —
    ``session_dir()``/``session_live()`` call this multiple times per
    op-invocation with the same explicit ``cwd``): cached, process-local,
    keyed on ``cwd`` ONLY when ``cwd is not None``. A caller passing an
    explicit ``cwd`` has asked for a FIXED root and can be served from
    cache; ``cwd=None`` means "resolve against whatever the process cwd is
    right now" — the same case ``git_root()``'s docstring documents as able
    to legitimately change mid-process — so that branch is never cached and
    always re-spawns, mirroring ``git_root()``'s own uncached contract.

    A FAILED resolution (not a git repo, git missing, transient spawn
    error) is deliberately NOT cached even for an explicit ``cwd`` — only a
    successful resolution is memoized. A transient failure (e.g. a git lock
    contention, or a repo mid-initialization) caching as authoritative would
    poison every subsequent call for that ``cwd`` for the rest of the
    process; the cost of re-spawning on the (rare, off-hot-path) failure
    case is far cheaper than that failure mode. See ``reset_sessions_dir_cache()``
    for the cache-clear escape hatch, and ``lifecycle.git_common_dir`` for
    the sibling ``--git-common-dir`` resolver this module deliberately does
    NOT route through (different failure contract — raises ``RuntimeError``
    rather than returning ``""`` — and different worktree semantics
    downstream; two resolvers stay, this one alone gains a cache).
    """
    if cwd is None:
        return _sessions_dir_resolve(cwd)
    try:
        return _sessions_dir_cached(cwd)
    except _SessionsDirResolutionFailed:
        return ""


def session_dir(sid: str, cwd: Optional[str] = None) -> str:
    """Port of ``_cs_session_dir <session_id>``."""
    if not sid:
        raise ValueError("session_id required")
    base = sessions_dir(cwd)
    if not base:
        return ""
    return str(Path(base) / sid)


def ensure_session(
    sid: str,
    cwd: Optional[str] = None,
    *,
    goal: str = "",
    sessions_base: Optional[str] = None,
    root: Optional[str] = None,
) -> str:
    """The session directory's ONE constructor: return ``<hub>/<sid>`` carrying
    a ``meta.json`` session RECORD, having produced the directory and the record
    in the SAME call, or neither.

    Every writer that needs a session directory to exist calls this. Nothing
    else in this corpus may ``mkdir`` a session directory — that rule is not a
    convention, it is a guard
    (``coordinator_core/tests/test_session_dir_has_one_constructor.py``).

    Why one owner, in facts rather than principle. ``init`` is the only writer
    of ``meta.json`` and it used to run LAZILY, from whichever bookkeeping
    writer happened to reach the hub first; meanwhile a dozen writers reached a
    session directory into being with ``mkdir(parents=True, exist_ok=True)`` and
    dropped their own file in it. Which sessions got a record was therefore a
    RACE between lazy initializers, and a session that lost it was not degraded
    but INVISIBLE:

      - every ``update_meta_field``/``update_meta_fields`` write silently
        no-ops (both return False on an absent file, by contract, matching the
        bash original which requires ``cs_init`` to have run first);
      - ``goal`` has no harness-registry substitute, so the session renders to
        every peer as ``holder_goal_state: undeclared`` -- exactly what a
        peer's claim-contention check reads when it asks what a live holder is
        doing;
      - ``ops/session/reap.py`` fail-closes to KEEP a directory it cannot read,
        so such a session is both invisible and unreapable, accumulating
        forever.

    Cost, spiked before this was built rather than assumed and re-measured
    after (2026-08-26, this box, RUSAGE process time, n=200): the marginal cost
    of the record write at a site that previously only ``mkdir``-ed is
    **+0.57ms on the CREATE path and +0.30ms on the idempotent refresh, with
    ZERO process creations** -- ``init`` reads ``head_at_start``/``branch`` from
    ``.git/HEAD`` in-process (see its own DR-344 note) and comm-verifies the
    parent through psutil, so nothing here spawns. (The pre-build spike read
    +0.42ms / +0.28ms; the atomic meta.json create added below accounts for the
    difference, once per session.) The overwhelmingly common call -- a session
    whose record already exists and is stamped -- short-circuits at the
    ``is_file()`` below for 0.013ms and imports nothing. A caller that has never
    imported ``psutil``/``git_state`` pays ~11ms ONCE per process, on the first
    create only.
      -> docs/research/spike-verdicts/2026-08-26-what-the-record-write-costs-at-a-mkdir-site.md

    ``goal``/``sessions_base``/``root`` are pass-throughs to ``init`` -- see that
    function's own negative-spec for the pre-resolved seam's contract
    (``sessions_base`` is the HUB, ``root`` is a WORKTREE root, neither is
    validated against the other). A caller holding both answers already, such as
    ``hooks/track_touched_files``, must pass them: that handler is pinned at ZERO
    ``core.git_root`` calls by its own guard.

    Return contract, carried over unchanged from the ``ensure_meta`` this
    replaced (deleted 2026-08-26 once this became the only constructor):
    the resolved path is returned even when the create FAILED, so a caller's
    existing no-op-and-warn branch stays reachable rather than a diagnostic field
    turning into a new failure mode. ``""`` means the HUB itself was
    unresolvable (not a git repo). A caller that must fail CLOSED checks
    ``os.path.isdir()`` on the result -- "or neither" is observable there, not by
    a raise.

    Negative-spec:
        - Does NOT lock. ``init`` is an idempotent CREATE here, never a
          read-modify-write, so it cannot clobber a concurrent writer's
          ``last_activity`` stamp -- the same bounded exception
          ``hooks/session_heartbeat._bootstrap_meta`` already relies on. Do not
          add a locking scheme on this path; it is on the hook hot path and the
          race it would close does not exist.
        - Does NOT repair an existing ``meta.json`` that is unreadable,
          non-JSON, or not a dict. ``update_meta_field`` still returns False for
          those cases and the caller still handles it.
        - Does NOT raise on a failed create (see the return contract above).
        - Is NOT the owner of every session-id-named directory on the machine.
          Three other corpora key directories on a session id and are NOT
          sessions: ``state/subagent-share/<sid>`` (a tracked repo artifact),
          ``$COORDINATOR_SETTINGS_HOME``'s write-bump anchor and context-window
          sidecar, and the tempdir guard-unlock sentinels. Routing any of those
          through here would mint a session record for a directory that is not a
          session.

    Re-stamp arm (C4, guard-claim-ceremony-stable-pid): an EXISTING
    ``meta.json`` with an EMPTY ``stable_pid`` is re-run through ``init()``
    instead of being returned untouched. Without this, an existing record is
    permanently unstamped once created record-but-unstamped (Guard-1 missed at
    ``init()`` time -- psutil absent, or a partial write) -- the early
    ``is_file()`` return below never reaches ``init()``'s own refresh arm, which
    is the only other place that writes ``stable_pid``. ``init()`` is still
    idempotent-safe to call here: its refresh branch only rewrites
    ``pid``/``last_activity``/``branch``/``stable_pid*``, never ``goal`` (see
    that branch's own comment), so a repeat call cannot clobber a concurrent
    writer's ``goal``/other fields.
    """
    if not sid:
        raise ValueError("session_id required")
    base = sessions_base if sessions_base else sessions_dir(cwd)
    if not base:
        return ""
    sdir = str(Path(base) / sid)
    meta_path = Path(sdir) / "meta.json"
    if meta_path.is_file():
        if read_meta_field(sdir, "stable_pid"):
            return sdir
        # Re-stamp arm. Attempt ONCE per process per session dir -- see
        # `_STAMP_ATTEMPTED` for the measurement and for why process-local is
        # what keeps this a bound rather than a permanent give-up.
        if sdir in _STAMP_ATTEMPTED:
            return sdir
        if len(_STAMP_ATTEMPTED) >= _STAMP_ATTEMPTED_MAX:
            _STAMP_ATTEMPTED.clear()
        _STAMP_ATTEMPTED.add(sdir)
    try:
        init(sid, goal, cwd=cwd, sessions_base=base, root=root)
    except Exception:  # noqa: BLE001 -- best-effort; caller handles a still-absent record
        pass
    return sdir


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Port of ``_cs_now_iso``: ISO-8601 timestamp, second resolution, UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> int:
    """Port of ``_cs_now_epoch``: seconds since epoch."""
    return int(datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# PID / liveness primitives
# ---------------------------------------------------------------------------


def pid_alive(pid) -> bool:
    """Port of ``_cs_pid_alive <pid>``.

    NOT a session-liveness signal in-harness — every Bash/hook tool call
    has a fresh, short-lived ``$$``. Retained only for the legacy pid-only
    claim-dir fallback and diagnostics; never gate session liveness on
    this. Distinct from ``stable_pid_alive`` — see that function's
    docstring.

    Parity note: the bash original is ``kill -0 "$pid" 2>/dev/null``,
    which reports DEAD (nonzero exit) on EPERM (process exists but is
    owned by another uid) as well as ESRCH — ``kill -0`` treats
    "can't signal it" the same as "can't find it". ``psutil.pid_exists``
    has no such notion (it reports True for any visible-but-unowned PID),
    so on POSIX we shell out to the same ``os.kill(pid, 0)`` primitive
    bash uses to preserve that EPERM-is-dead parity exactly.
    Review: code-reviewer P2 — psutil.pid_exists() and the prior
    PermissionError->True branch both diverged from bash's kill-0-as-dead
    EPERM handling.

    Raises :class:`MissingPsutilError` on Windows when psutil is absent —
    there is no POSIX-equivalent fallback there, and returning False would
    silently report every PID dead (see that class's docstring).
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False  # non-numeric/absent pid -> not alive
    if pid_int <= 0:
        return False
    if _IS_WINDOWS:
        # No kill(pid, 0) equivalent on native Windows; psutil is
        # authoritative there (bash's kill -0 doesn't run natively either).
        _ps = _psutil()
        if _ps is None:
            raise MissingPsutilError(
                "psutil is a required dependency and is not installed in this "
                "interpreter, but Windows PID-liveness (pid_alive) has no "
                "fallback without it — reporting every PID dead here would "
                "risk wrongful claim takeover. Install it "
                "(`pip install psutil` / `pip install -e .` from the "
                "coordinator_core project root) or re-run claude-klabauter's setup "
                "(scripts/setup.py) to provision the engine venv."
            )
        return _ps.pid_exists(pid_int)
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # bash `kill -0` also reports dead (nonzero) on EPERM
    except OSError:
        return False  # any other kill(2) failure -> treat as dead (same parity)
    return True


class _WinLivenessAmbiguous(Exception):
    """Review: code-reviewer P2 — ``psutil.Process.create_time()`` raised
    something other than ``NoSuchProcess`` (``AccessDenied``, ``ZombieProcess``,
    etc.): the QUERY failed, not necessarily the process. Signalled distinctly
    from "definitely dead" so ``stable_pid_alive`` can fail toward ALIVE
    rather than collapsing an ambiguous read into DEAD — the exact
    wrongful-takeover shape (2026-06-23) this leg exists to close."""


def _win_create_time_epoch(pid_int: int) -> Optional[int]:
    """``stable_pid_alive``'s Layer-1 birth-instant on BOTH platforms:
    ``psutil.Process(pid).create_time()`` as integer epoch seconds, or
    ``None`` when the process is gone / ``psutil`` is absent. Name retained
    from its Windows-only origin (test call sites pin it directly) — it is
    no longer Windows-exclusive.

    Originally added for Windows (no ``ps`` binary there), then converged
    onto by POSIX too in the 2026-07-27 ps-to-psutil port (DoE
    cross-repo memo ``2026-07-27-doe-claude-em-ac6-ps-to-psutil-yes-oracle-
    retired.md`` confirmed the bash ``_cs_stable_pid_alive`` parity oracle
    this POSIX arm preserved was itself retired 2026-07-22 — there is no
    remaining counterparty to diff against). ``create_time()`` is an
    absolute Unix epoch, directly comparable to the ``stable_pid_start_epoch``
    captured (ALSO via ``create_time()`` on the writing side) at ``init()``
    time — the SAME comparison now used on both platforms rather than two
    independent implementations (see ``stable_pid_alive``). ``create_time()``
    is a fixed per-process value, so ``int()`` truncation is deterministic
    across capture and check for a given derivation path (see
    ``_STABLE_PID_EPOCH_TOLERANCE_SECS`` for why the CAPTURE path can still
    disagree by ~1s against an OLDER ps-derived stored value).

    Returns ``None`` on ``NoSuchProcess`` (the ESRCH-equivalent -> genuinely
    dead). Raises :class:`MissingPsutilError` when ``psutil`` is absent —
    this is the sole liveness mechanism for ``stable_pid_alive``'s Layer 1
    on both platforms now, so collapsing "psutil absent" into ``None``/dead
    here would be the exact every-session-reads-DEAD wrongful-takeover shape
    this module's negative-spec exists to close; NOT the same case as
    ``NoSuchProcess``, which is a real dead-process signal. Raises
    ``_WinLivenessAmbiguous`` on any OTHER ``psutil`` error (``AccessDenied``
    / ``ZombieProcess`` / etc.): unlike POSIX's OLD ``ps -p`` mechanism
    (unprivileged even for other users' processes), ``psutil.Process
    .create_time()`` CAN raise ``AccessDenied`` for a perfectly live process
    (documented Windows behavior; possible in restrictive POSIX sandboxes
    too), so collapsing that into "dead" (the EPERM-is-dead parity
    ``pid_alive`` keeps on POSIX, for a field that is explicitly NOT a
    liveness signal — see ``pid_alive`` docstring) would be a genuinely new
    false-DEAD path. The caller fails toward ALIVE on this signal instead.
    NEVER ``ps -p``; NEVER gate on the dead per-hook ``pid`` field — the
    caller passes the separate long-lived ``stable_pid`` (RAW-PID-LIVENESS
    floor).
    """
    _ps = _psutil()
    if _ps is None:
        raise MissingPsutilError(
            "psutil is a required dependency and is not installed in this "
            "interpreter, but session-liveness (stable_pid_alive) has no "
            "fallback without it — collapsing this into 'dead' would "
            "risk wrongful claim takeover across every session on this "
            "host. Install it (`pip install psutil` / `pip install -e .` "
            "from the coordinator_core project root) or re-run claude-klabauter's "
            "setup (scripts/setup.py) to provision the engine venv."
        )
    try:
        return int(_ps.Process(pid_int).create_time())
    except _ps.NoSuchProcess:
        return None
    except Exception as exc:
        raise _WinLivenessAmbiguous(str(exc)) from exc


def stable_pid_alive(pid, stored_lstart: str = "", stored_start_epoch: str = "") -> bool:
    """Port of ``_cs_stable_pid_alive <pid> <stored_lstart> <stored_start_epoch>``.

    Returns True (alive, same process) iff:
      1. The process still exists (``psutil`` resolves a ``create_time()``),
         AND
      2. Its birth instant matches the stored value WITHIN
         ``_STABLE_PID_EPOCH_TOLERANCE_SECS`` (same process, not a recycled
         PID that reused the number after the original died).

    Layered fallback:
      (1) If ``stored_start_epoch`` is present and nonzero: compare the
          current process's ``create_time()`` epoch against
          ``stored_start_epoch`` within tolerance. Match = alive; mismatch =
          recycled PID = dead.
      (2) On no such process (``psutil.NoSuchProcess``) -> dead (False).
      (3) If no ``stored_start_epoch`` (pre-upgrade meta with only
          ``stored_lstart``): derive an epoch from ``stored_lstart`` and
          reuse the SAME tolerant compare — see the PLATFORM SPLIT note
          below for how that derivation differs by platform. Self-heals on
          the session's next ``init()`` write of ``stable_pid_start_epoch``.

    This helper is called ONLY from liveness's session-live check. It is
    NOT ``pid_alive`` on the ``pid`` (``$$``) field — that path is
    prohibited from session liveness (see ``pid_alive`` docstring). This
    helper keys on the separate ``stable_pid`` field (the long-lived
    claude session process) captured at ``init()`` time.

    PLATFORM SPLIT (psutil-everywhere; POSIX converged onto the Windows
    ``create_time()`` path 2026-07-27 — the DoE bash ``_cs_stable_pid_alive``
    parity oracle this preserved was itself retired 2026-07-22, and there is
    no live counterparty left to diff against). The birth-instant fetch AND
    the tolerant epoch compare are ONE implementation shared by both
    platforms (``_win_create_time_epoch`` + the inline tolerance check
    below) — the only per-platform difference left is the LEGACY
    (no-stored-epoch) fallback's INPUT shape, because the two platforms
    wrote genuinely different token formats into ``stable_pid_lstart``
    before this port:
      - POSIX legacy tokens are ``ps -o lstart=`` date strings (e.g. "Tue
        Jul 14 15:26:28 2026") — parsed via ``lstart_to_epoch`` (pure
        Python, no subprocess) into an epoch, then tolerance-compared like
        every other case. POSIX's ``init()`` stopped WRITING this field
        2026-07-27 (writes ``stable_pid_start_epoch`` only); the read arm
        stays to resolve records already on disk. DEPRECATED read-only
        back-compat, not a live write path — do not resurrect writing it.
      - Windows legacy tokens are the ``create_time()`` epoch itself
        rendered as a string (Windows never had a ps-format lstart), so
        that arm is a direct string compare — no re-derivation needed, and
        Windows's ``init()`` is unaffected by this port (it never called
        ``ps`` and still writes both fields).
    The never-gate-on-``pid`` negative-spec is identical on both platforms.

    Spec: docs/plans/2026-06-27-liveness-first-claim-staleness.md
    § _cs_stable_pid_alive
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False  # non-numeric/absent pid -> not alive
    if pid_int <= 0:
        return False

    try:
        now_epoch_val = _win_create_time_epoch(pid_int)
    except _WinLivenessAmbiguous:
        # Review: code-reviewer P2 — an ambiguous psutil query failure
        # (AccessDenied, ZombieProcess, ...) is NOT the same as
        # NoSuchProcess: the process may well be alive, only the query
        # failed. Fail toward ALIVE, never collapse into DEAD (the
        # 2026-06-23 wrongful-takeover shape).
        return True
    if now_epoch_val is None:
        return False  # process gone (NoSuchProcess) -> dead; psutil-absent
        # raises MissingPsutilError from _win_create_time_epoch above and
        # is never reached here (see that function's docstring).

    if stored_start_epoch and stored_start_epoch != "0":
        try:
            stored_epoch_int = int(stored_start_epoch)
        except (TypeError, ValueError):
            # Corrupt STORED epoch must not fall through to a string/legacy
            # compare that could misread a recycled process as alive —
            # fail closed.
            return False
        return abs(now_epoch_val - stored_epoch_int) <= _STABLE_PID_EPOCH_TOLERANCE_SECS

    # Legacy fallback (pre-upgrade meta with no stored epoch).
    if not stored_lstart:
        return False
    if _IS_WINDOWS:
        # Windows never had a ps-date lstart string; init() stored the
        # create_time() int itself in this field, so an identity compare is
        # a direct string match against the freshly-fetched epoch.
        return str(now_epoch_val) == stored_lstart
    # POSIX legacy: stored_lstart is a `ps -o lstart=` date string from a
    # pre-2026-07-27 meta.json. Parse it (pure-Python, no subprocess) into
    # an epoch and reuse the SAME tolerant compare as the primary path —
    # this is the one epoch-comparison implementation, never a second one,
    # and it never spawns `ps` even for legacy records.
    legacy_epoch = lstart_to_epoch(stored_lstart)
    if not legacy_epoch:
        return False  # unparseable legacy token -> fail closed, not alive
    return abs(now_epoch_val - legacy_epoch) <= _STABLE_PID_EPOCH_TOLERANCE_SECS


# ---------------------------------------------------------------------------
# Timestamp conversion helpers
# ---------------------------------------------------------------------------


def mtime_epoch(path) -> int:
    """Port of ``_cs_mtime_epoch <file>``: mtime as epoch seconds, or 0 if
    the path is not a regular file (missing, a directory, a socket, etc.) —
    matches the bash original's ``[[ -f "$f" ]] || { echo 0; return; }``
    gate. Review: code-reviewer nit — a bare ``os.stat`` succeeds on
    directories too, silently returning a directory's mtime where bash
    returns 0.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0  # TOCTOU: path vanished between is_file() and stat()


def iso_to_epoch(iso: str) -> int:
    """Port of ``_cs_iso_to_epoch <iso>``: parse ``YYYY-MM-DDTHH:MM:SSZ``
    (our sole output format) to epoch seconds. UTC-anchored — the ``Z``
    suffix is stripped and the resulting naive datetime is treated as UTC,
    matching the bash original's ``-u`` / ``tzinfo=utc`` behavior on every
    branch. Returns 0 on empty input or parse failure.
    """
    if not iso:
        return 0
    text = iso[:-1] if iso.endswith("Z") else iso
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def lstart_to_epoch(lstart: str) -> int:
    """Port of ``_cs_lstart_to_epoch <lstart>``: parse a ``ps -o lstart=``
    local-time string (e.g. "Sat Jun 27 22:41:38 2026") to epoch seconds,
    interpreted in LOCAL TZ (no UTC coercion — opposite of ``iso_to_epoch``,
    since ``ps`` lstart carries no TZ marker and is rendered in the
    machine's local zone).

    BSD single-digit-day space-pad ("Sat Jun  7 ...", double space) is
    collapsed via whitespace normalization before parsing, matching the
    bash helper's ``tr -s ' '`` step.

    Returns 0 on empty input or parse failure — a real process birth at the
    Unix epoch (1970) is impossible, so 0 unambiguously signals "failed
    parse", matching the bash contract.

    Accepted residual (verbatim from bash source comment): a process born
    in the zone's fall-back DST repeated hour renders an ambiguous
    local-time string; disambiguation may be +/-3600s — negligible,
    strictly better than the status-quo false-dead on any TZ shift.
    """
    if not lstart:
        return 0
    normalized = _WHITESPACE_RE.sub(" ", lstart).strip()
    try:
        dt = datetime.strptime(normalized, _LSTART_FORMAT)
    except ValueError:
        return 0
    return int(dt.timestamp())  # naive datetime -> interpreted as LOCAL TZ


# ---------------------------------------------------------------------------
# meta.json read/write
# ---------------------------------------------------------------------------


def read_meta_field(sdir: str, field: str) -> str:
    """Port of ``_cs_read_meta_field <session_dir> <field>``: the string
    value of ``field`` in ``<session_dir>/meta.json``, or ``""`` if the
    file/field is missing/null.

    Non-string scalar values ARE stringified — this mirrors the bash
    original's ``jq -r ".${field} // empty"`` (raw output stringifies any
    non-null scalar, not just strings; ``// empty`` maps null/absent to
    ``""``). Booleans need an explicit branch: ``jq -r`` prints lowercase
    ``true``/``false``, whereas Python's ``str(True)`` is ``"True"`` —
    without this branch the two diverge on any boolean meta field.
    Review: code-reviewer P2 — docstring previously claimed non-string
    values return "", contradicting the (jq-parity-correct) implementation;
    fixed the one real divergence (boolean casing) and corrected the doc.
    """
    meta_path = Path(sdir) / "meta.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(field, "") if isinstance(data, dict) else ""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def update_meta_field(sdir: str, field: str, value) -> bool:
    """Port of ``_cs_update_meta_field <session_dir> <field> <value>``:
    updates (or adds) one field in ``<session_dir>/meta.json``, coercing
    ``value`` to its string form (matching the bash original's ``jq --arg``
    always-string write). Atomic rewrite via tempfile + ``os.replace`` in
    the same directory as the target, mirroring the bash ``mktemp`` +
    ``mv`` pattern (concurrent-reader safety — never a truncated
    mid-write read).

    Returns False (no-op) if ``meta.json`` does not exist yet — matches
    the bash original, which requires ``cs_init`` to have created the file
    first.

    Raises ``ValueError`` if ``value`` stringifies to the empty string —
    the bash original declares ``value="${3:?}"``, which hard-errors on
    an unset OR empty-string third arg; this is a required-non-empty
    contract, not an optional one. Review: code-reviewer P2 — the port
    previously accepted "" silently.
    """
    if str(value) == "":
        raise ValueError("value required (non-empty)")
    meta_path = Path(sdir) / "meta.json"
    if not meta_path.is_file():
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # unreadable or non-JSON meta.json -> no-op update
    if not isinstance(data, dict):
        return False
    data[field] = str(value)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="meta.json.", dir=str(meta_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_name, meta_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                # Best-effort tmp-file cleanup on the error path; the original
                # exception is re-raised below regardless.
                pass
            raise
    except OSError:
        return False
    return True


def update_meta_fields(sdir: str, fields: "Mapping[str, object]") -> bool:
    """Batch sibling of ``update_meta_field``: updates (or adds) MULTIPLE
    fields in ``<session_dir>/meta.json`` with exactly ONE atomic tempfile +
    ``os.replace`` rewrite, instead of one rewrite per field. Added for
    ``init()``'s refresh path (AC10, ``stable_pid_capture`` breadcrumb plan)
    — that path fires on every session start on a box whose load norm is
    50-70 concurrent LLMs (``docs/wiki/machine-load-norm.md``), so collapsing
    what was 3-5 sequential read-modify-atomic-rewrite calls into one is load
    -bearing, not cosmetic.

    Mirrors ``update_meta_field``'s semantics exactly, just batched:
      - same tempfile + ``os.replace`` atomic pattern, in the target's own
        directory.
      - same ``False`` no-op when ``meta.json`` is absent, unreadable, or
        not a JSON object.
      - same string coercion (``str(value)``) for every field.
      - same required-non-empty contract as ``update_meta_field``, applied
        per-key: if ANY value in ``fields`` stringifies to ``""``, the WHOLE
        call raises ``ValueError`` before anything is read or written (fail
        the call, not a silent partial write) — this is the documented
        choice between "reject the call" and "skip the empty key"; the
        former was picked to keep this helper's failure mode identical to
        ``update_meta_field``'s (raise, never a silent partial).

    ``fields`` empty is a no-op returning ``False`` — nothing to write is
    not the same outcome as "meta.json missing", but both correctly signal
    "no rewrite happened" to a caller checking the return value.

    ``update_meta_field`` itself is UNCHANGED and kept — it still has other
    callers; this is an addition, not a replacement.
    """
    if not fields:
        return False
    for key, value in fields.items():
        if str(value) == "":
            raise ValueError(f"value required (non-empty) for field {key!r}")
    meta_path = Path(sdir) / "meta.json"
    if not meta_path.is_file():
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # unreadable or non-JSON meta.json -> no-op update
    if not isinstance(data, dict):
        return False
    for key, value in fields.items():
        data[key] = str(value)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="meta.json.", dir=str(meta_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_name, meta_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                # Best-effort tmp-file cleanup on the error path; the original
                # exception is re-raised below regardless.
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Confined-findings-agent SSOT
# ---------------------------------------------------------------------------


def is_confined_findings_agent(effective_type: str) -> bool:
    """Port of ``_cs_is_confined_findings_agent <subagent_type>``.

    Returns True iff ``effective_type`` is a member of the confined
    findings-agent SET (currently ``{"coordinator:code-reviewer"}``);
    False otherwise (including for ``""``/``None``). SSOT for the
    guard-before-grant set consumed by the reviewer-bash-outside-allowlist
    guard's dual-resolver OR (primary ``agent_type`` leg, secondary
    back-pointer-resolved ``subagent_type`` leg) — callers OR the two
    booleans; this predicate itself is single-argument.
    """
    return (effective_type or "") in _CONFINED_FINDINGS_AGENTS


# ---------------------------------------------------------------------------
# Session-id resolution (3-tier chain)
# ---------------------------------------------------------------------------

#: Canonical env-var precedence for tiers 1-3 of ``resolve_session_id`` —
#: the SOLE source of truth for this chain. Previously duplicated across
#: three independent sites (this function's inline calls,
#: ``handoff_correct_body._SESSION_ENV_PRECEDENCE``, and
#: ``block_consumed_handoff_edit.check``'s inlined ``is_holder`` lookup); a
#: chain-review found a break-class defect (slice D, F1) caused precisely by
#: two of those copies disagreeing — the guard read only
#: ``COORDINATOR_SESSION_ID`` while the op it routes to walked the full
#: chain, so a real session with only ``CLAUDE_CODE_SESSION_ID`` set was
#: told "Not your claim." A guard and the op it routes to MUST agree on this
#: chain; centralize it here rather than re-inlining a fourth copy.
SESSION_ENV_PRECEDENCE = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


#: UUID-shape gate for `session_identity_override` — mirrors
#: `coordinator_core.git.commit_trailers._UUID_RE` exactly (same
#: fail-safe direction: a caller-supplied override that is not UUID-shaped
#: is treated as "no override", never trusted verbatim). A value crossing
#: a process boundary (the warm server's request wire) must be validated,
#: not trusted, before it can substitute for the server's own env-derived
#: identity — see `coordinator_core.warm.entry_seam.per_request_state`'s
#: `session_id` parameter, the sole production setter of this ContextVar.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Per-request override for `resolve_session_id`, bound via
#: `session_identity_override` for the life of one warm-server dispatch.
#: `default=None` reproduces today's env-only resolution for every caller
#: that never opens the override (every cold invocation, and any warm
#: request that arrived with no identity to carry) — see that context
#: manager's own docstring for the full contract this exists to serve.
_SESSION_ID_OVERRIDE: "ContextVar[Optional[str]]" = ContextVar(
    "coordinator_core_session_id_override", default=None
)


#: Whether the current in-process call is being served by the warm server,
#: bound via `warm_served_request` for the life of one dispatch. Distinct from
#: `_SESSION_ID_OVERRIDE` and NOT derivable from it: a warm request whose door
#: sent no `_session_id` leaves that ContextVar unset, which is byte-identical
#: to a cold invocation — and the two need OPPOSITE answers. Cold, `os.environ`
#: IS the caller's own; warm, it belongs to whoever spawned the server. Without
#: this flag every consumer of an unresolvable identity has to guess which case
#: it is in, and the guess that preserves cold behaviour is the one that
#: misattributes warm work.
_WARM_SERVED_REQUEST: "ContextVar[bool]" = ContextVar(
    "coordinator_core_warm_served_request", default=False
)


@contextlib.contextmanager
def warm_served_request(active: bool = True) -> Iterator[None]:
    """Mark the enclosed dispatch as warm-served, Token/reset-scoped.

    Bound by `warm.entry_seam.per_request_state`'s `warm_served` parameter,
    whose only production setters are `warm.server`'s two dispatch sites. A
    cold invocation never opens it, so `in_warm_served_request()` stays
    `False` for every existing caller and nothing about cold behaviour moves.

    `active=False` binds the flag OFF explicitly rather than not binding at
    all — used by a warm-side caller that must run a nested block under cold
    semantics, and by tests pinning the negative case. Nesting is safe: the
    Token is reset in a `finally` regardless of how the block exits.
    """
    token = _WARM_SERVED_REQUEST.set(bool(active))
    try:
        yield
    finally:
        _WARM_SERVED_REQUEST.reset(token)


def in_warm_served_request() -> bool:
    """True when this call is running inside a warm-server dispatch.

    The discriminator for the one question `carried_session_id()` cannot
    answer on its own: an empty carried id means "the caller did not send
    one", and what to DO about that is opposite in the two contexts.

    Cold (`False`): `resolve_session_id()`'s environment tiers are the
    caller's own environment and are the right answer — degrading to them is
    correct, and refusing instead would break every cold commit and every
    cold op that stamps identity.

    Warm (`True`): those same tiers belong to whoever spawned the server. A
    consumer that degrades to them there stamps a stranger — the defect this
    whole seam exists to close (`state/bug-backlog/2026-08-29-the-warm-door-
    s-exe-route-stamps-the-ser-47373b19c77e.yaml`, measured across three
    repos). A warm caller holding an empty `carried_session_id()` must OMIT
    the identity, never substitute the ambient one.

    Deliberately NOT folded into `carried_session_id()`'s return value: that
    accessor answers "what did the caller carry", one question with one
    honest answer, and overloading it with "and where am I" is how a resolver
    grows a second blended tier of exactly the kind
    `resolve_session_id`'s own docstring warns about.
    """
    return bool(_WARM_SERVED_REQUEST.get())


@contextlib.contextmanager
def session_identity_override(sid: Optional[str]) -> Iterator[None]:
    """Bind the CALLER's session id for the duration of one in-process
    dispatch, so `resolve_session_id()` returns it instead of reading this
    process's OWN environment.

    Exists to close the warm-engine identity-attribution defect (state/
    bug-backlog/2026-08-18-a-warm-server-stamps-every-op-it-serves-
    eeb801fc6bee.yaml): a long-lived warm server's `os.environ` reflects
    WHOEVER SPAWNED IT, not the many distinct sessions it goes on to serve
    requests for. `warm.entry_seam.per_request_state` binds this
    Token/reset-scoped around each dispatch — the same explicit-scope shape
    that module's own docstring already uses for `session.declared_writes`
    — using the identity `warm.client.py` resolved COLD, in the true
    caller's own environment, before the request ever crossed the pipe.

    `sid` is validated here, not trusted from the caller: anything that is
    not UUID-shaped (`_UUID_RE`) is silently treated as "no override" — the
    bind is a no-op and `resolve_session_id()` falls through to its
    ordinary env-var chain unchanged. This is the fail-safe direction
    (never fabricate or misattribute an identity; at worst, degrade to
    today's behaviour) — mirrors `commit_trailers.compute_missing_trailer_
    args`'s own `session_id_override` gate (`c425e181f`), which validates a
    caller-supplied override the identical way for the identical reason.

    `sid=None` (or empty) is always a no-op bind — the explicit "no
    identity to carry" case (module docstring's "no-identity fallback"),
    never silently substituting anything.
    """
    if not sid or not _UUID_RE.fullmatch(sid):
        yield
        return
    token = _SESSION_ID_OVERRIDE.set(sid)
    try:
        yield
    finally:
        _SESSION_ID_OVERRIDE.reset(token)


def carried_session_id() -> str:
    """The CALLER's session id when the request explicitly carried one, and
    the empty string otherwise. Tier 0 alone — never the ambient environment.

    `resolve_session_id` blends two materially different things behind one
    return value: an identity the caller CARRIED across the wire (tier 0), and
    an identity read out of THIS process's environment (tiers 1-3). Inside the
    warm server those are not interchangeable — `os.environ` there belongs to
    whoever spawned the server, not to the session being served — and tiers 1-3
    cannot be distinguished from tier 0 by reading the return value. This
    accessor is how a caller that must NOT accept the ambient answer asks the
    narrower question.

    Use it for any MUTATING op whose effect is attributed to a session, and
    commit+push above all: `warm.client` omits `_session_id` entirely when it
    cannot identify its own process, and `coordinator-invoke.exe` does not send
    the field at all, so identity is absent for a whole dial path rather than
    rarely. `resolve_session_id`'s degrade-to-env on absence is the documented
    fail-safe for a READ ("degrade to the server's pre-existing behaviour, not
    a broken one" — `warm/client.py`'s caller-identity seam), and it is exactly
    wrong for a write: committing under whichever session happened to start the
    engine is not a degrade, it is a silent misattribution of somebody's work.
    Measured live 2026-08-27: three different `cwd` values dialled from a peer
    session all returned the ENGINE owner's session id, and a non-dry run would
    have committed that owner's paths under the peer's ceremony.

    Callers gate on empty and REFUSE — never fall back to `resolve_session_id`
    on an empty return, which reinstates precisely the blend this exists to
    avoid.
    """
    return _SESSION_ID_OVERRIDE.get() or ""


def resolve_session_id(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_resolve_session_id`` / public alias ``cs_resolve_session_id``.

    Resolution order, current (2026-08-19):
      0. `session_identity_override`'s bound ContextVar, when a request has
         one bound (warm-server per-request identity carried from the
         caller — see that context manager's docstring). Never set outside
         an explicitly opened scope, so this is a no-op for every
         cold-path/unbound caller.
      1. ``COORDINATOR_SESSION_ID``  (explicit test override)
      2. ``CLAUDE_SESSION_ID``       (explicit override slot)
      3. ``CLAUDE_CODE_SESSION_ID``  (platform-injected, Claude Code >= ~2.1.150)

    The former tier 4 (``.git/coordinator-sessions/.current-session-id``
    sentinel, plus its concurrency-ambiguity guard) was REMOVED (KS-4,
    2026-08-07): unsound under concurrency (a last-writer-wins file; under
    ~18 concurrent sessions sharing this worktree it names whichever
    session most recently initialized, not necessarily the caller's — see
    coordinator_core/bash_guards/guard_inprocess_search.py ~L84) AND its
    sole writer (session-init.py, the DoE-claude SessionStart hook) was
    deleted by PM directive 2026-07-15 — no production writer survives, so
    it could never be refreshed. ``cwd`` is retained for API compatibility
    with existing callers even though tiers 0-3 do not use it.

    Tiers 1-3 (env vars) are byte-for-byte unchanged. Always returns
    successfully (empty string signals "unresolvable", never an
    exception) — callers gate on empty.
    """
    override = _SESSION_ID_OVERRIDE.get()
    if override:
        return override
    for var in SESSION_ENV_PRECEDENCE:
        sid = os.environ.get(var, "")
        if sid:
            return sid
    return ""


def _harness_process_comm(proc) -> str:
    """Derive the name token compared against ``"claude"`` by
    ``_is_harness_process`` for a given ``psutil.Process``.

    Preferred field: the basename of ``argv[0]`` (``proc.cmdline()[0]``).
    Measured live on this box (docs/research/
    2026-08-14-harness-process-identity-problem-set.md § Q2/Q4):
    ``psutil.Process.name()`` reports the version string (e.g.
    ``"2.1.231"``) for the harness process, not ``"claude"`` — a moving
    target that changes under a live process the instant the
    ``~/.local/bin/claude`` symlink is repointed to a new version, with
    nothing else about the process changing. ``cmdline()[0]``'s basename
    (``"claude"``) is the one field measured that does not embed the
    version.

    Falls back to ``proc.name()`` (unchanged behaviour, ``.exe``-stripped
    below like the ``cmdline()`` leg) when ``cmdline()`` raises
    (``AccessDenied`` / ``ZombieProcess`` / ``NoSuchProcess`` / any other
    error) or returns an empty list — a "field unreadable, use the older
    field" degrade, NOT a second witness: it can only ever fall back to
    what today's callers already accepted, never accept something today's
    ``name()``-only check would have rejected. That fallback call is left
    UNGUARDED here deliberately — its exceptions propagate to the caller's
    own existing ``except`` block (``AccessDenied`` / ``ZombieProcess`` /
    ``NoSuchProcess``), preserving each call site's pre-existing
    skip-vs-miss handling around that exact call unchanged. This function
    itself never raises past the ``cmdline()`` leg.

    Honesty note: ``argv[0]`` is a value the process itself controls, not
    a kernel-verified identity field — a hostile process could still spawn
    with ``argv[0]`` set to ``"claude"``. This derivation defends against a
    mis-derived ancestor or a future resolver bug landing the wrong PID in
    ``stable_pid``, not against a deliberately spoofing local process; it
    is not, and was never claimed to be, tamper-proof.

    ``.exe``-stripped identically on both legs so a Windows caller need not
    strip it again after calling this.
    """
    argv0 = ""
    try:
        cmdline = proc.cmdline()
    except Exception:
        cmdline = None
    if cmdline:
        argv0 = cmdline[0] or ""
    if argv0:
        # Split on BOTH separators regardless of the host running this
        # code: a Windows psutil target always renders argv0 with
        # backslashes, and this predicate is exercised on POSIX (via a
        # process double / tests, and any future cross-platform caller)
        # where ``os.path.basename`` would only split on ``/`` and leave a
        # Windows-shaped path un-split.
        base = re.split(r"[\\/]", argv0)[-1]
    else:
        base = proc.name() or ""  # unguarded — see docstring
    return base[:-4] if base.lower().endswith(".exe") else base


def _is_harness_process(name: str) -> bool:
    """Decide whether an already-normalized process name is the harness's
    own ``claude`` process.

    Takes the name AFTER whatever normalization the caller applies —
    ordinarily the result of ``_harness_process_comm(proc)`` (argv0-basename
    preferred, ``name()``-based ``.exe``-stripped fallback; see that
    function's docstring). This predicate does not itself derive or
    normalize the name — it stays a pure exact-match comparison so the
    derivation policy can be swapped (as it was, C2a -> C3) without
    touching this function. Callers pass the derived result in. Exact-match
    only — a byte-identical port of the three ``== "claude"`` /
    ``!= "claude"`` comparisons this consolidates (Windows ancestor walk,
    ``_resolve_claude_pid_from_env``, POSIX parent check). See
    ``docs/plans/2026-08-13-session-identity-earns-its-keep.md`` § C2, § C3.
    """
    return name == "claude"


def _find_windows_claude_ancestor(
    start_pid: int, max_depth: int = _STABLE_PID_WINDOWS_ANCESTOR_DEPTH
) -> "tuple[Optional[tuple[int, float]], str]":
    """Walk UPWARD from ``start_pid`` (inclusive) looking for the first
    ancestor whose ``psutil`` process name strips (after any ``.exe``
    suffix) to exactly "claude". Returns ``(match, reason)``: ``match`` is
    ``(pid, create_time)`` for the first "claude" ancestor found within
    ``max_depth`` rungs, or ``None`` if no match is found. ``reason`` names
    which of the walk's failure modes fired when ``match`` is ``None`` (the
    stable_pid-capture breadcrumb plan's C1 chunk — see
    ``docs/plans/2026-08-10-stable-pid-capture-breadcrumb-and-liveness-basis.md``
    § C1 for the full vocabulary this is part of):
      walk-hit:<rung>                           a match at this rung index
      walk-hit:<rung>+skipped:<exc>:<depth>     match, with 1+ skipped rungs
                                                 below it (one "+skipped:..."
                                                 segment per skipped rung, in
                                                 the order they were stepped
                                                 over — never "|", which
                                                 ``init()`` uses to join legs)
      walk-miss:depth-exhausted                 cap reached with no match
      walk-miss:rung-unreadable:<exc>:<depth>   a rung raised; which and where
      walk-miss:no-parent                       ppid chain terminated

    Windows has no ``exec``-replace: a hook subprocess's real parent is a
    Git-Bash trampoline rung (``bash.exe`` x2 on the measured hook topology,
    x3 from a Bash tool subprocess — see
    ``_STABLE_PID_WINDOWS_ANCESTOR_DEPTH``), not
    the ``claude.exe`` session process the POSIX single-parent check
    expects. This walk climbs ``.ppid()`` links until it finds a "claude"
    match or exhausts ``max_depth``.

    First match wins walking upward: the nearest "claude" ancestor is
    stamped, never a more distant one skipped past — a session can only
    ever stamp an ancestor of itself (never a descendant), so the failure
    direction of a wrong match is false-LIVE (stamping a process that
    outlives the caller), never false-DEAD.

    Robust to a vanished/inaccessible rung, but not uniformly: on the NAME
    read, ``psutil.AccessDenied`` and ``psutil.ZombieProcess`` are SKIPPED —
    the rung still exists (that is exactly what those two exceptions mean),
    so its ``.ppid()`` remains a verified link, and the walk steps over it
    and keeps climbing toward the next rung, consuming a depth unit like any
    other rung. ``psutil.NoSuchProcess`` is never skipped, on any read: the
    rung's process is gone, so there is no verified parent link to climb,
    and on a box whose load norm is 50-70 concurrent LLMs
    (``docs/wiki/machine-load-norm.md``) a recycled PID matching "claude" by
    coincidence is not theoretical. The asymmetry is deliberate: today's
    unresolved-walk failure mode is a bounded ~30-minute false-LIVE that
    expires on its own once the recency window lapses; a wrong-ancestor
    stamp from trusting a recycled PID would be unbounded, pinned to an
    unrelated live process for as long as that process lives. Widening the
    skip to ``NoSuchProcess`` would trade the bounded failure for the
    unbounded one, so it never does — that rung still ends the walk
    (return ``(None, reason)``) exactly as before, recorded as
    ``walk-miss:rung-unreadable:NoSuchProcess:<depth>``. This sits on the
    session-init hot path, where failing to stamp must degrade to today's
    recency-only Layer-2 behaviour, never raise.

    A skipped rung is never silently swallowed: each one stepped over is
    annotated onto the eventual ``walk-hit:<rung>`` code as
    ``+skipped:<exc>:<depth>``, one segment per skipped rung, in climb
    order (see the vocabulary block above). If the hit rung's
    ``create_time()`` read then raises, the same ``+skipped:...`` suffix is
    appended to the resulting ``walk-miss:rung-unreadable:...`` code instead,
    so the accumulated skip annotations are never dropped on that path. A
    walk that skips rungs and misses for any other reason keeps its
    existing ``walk-miss:*`` code unannotated — C4 (a later chunk) only
    needs skip visibility on the hit path (and the rung-unreadable miss
    that immediately follows a hit).
    """
    _ps = _psutil()
    pid = start_pid
    depth = 0
    skipped: "list[str]" = []
    while depth < max_depth:
        try:
            proc = _ps.Process(pid)
        except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as exc:
            return None, f"walk-miss:rung-unreadable:{type(exc).__name__}:{depth}"
        try:
            comm = _harness_process_comm(proc)
        except (_ps.AccessDenied, _ps.ZombieProcess) as exc:
            # The rung still exists (that is what these two exceptions
            # mean), so its .ppid() is a verified link even though its
            # name is not readable — try that link before giving up. If
            # ppid() itself cannot be obtained, there is no verified link
            # to climb and the walk ends as an ordinary no-parent miss.
            try:
                parent_pid = proc.ppid()
            except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as ppid_exc:
                # Review: coordinator:code-reviewer P2 — the skip branch's
                # ppid() failure must stay distinct from an ordinary
                # no-parent miss, matching the main (non-skip) path below.
                return None, f"walk-miss:rung-unreadable:{type(ppid_exc).__name__}:{depth}"
            if not parent_pid or parent_pid == pid:
                return None, "walk-miss:no-parent"
            skipped.append(f"skipped:{type(exc).__name__}:{depth}")
            pid = parent_pid
            depth += 1
            continue
        except _ps.NoSuchProcess as exc:
            return None, f"walk-miss:rung-unreadable:{type(exc).__name__}:{depth}"
        if _is_harness_process(comm):
            suffix = "".join(f"+{s}" for s in skipped)
            try:
                match = (pid, proc.create_time())
            except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as exc:
                # Review: coordinator:code-reviewer P3 — preserve any
                # accumulated skip annotations even when the hit rung's
                # create_time() itself raises, so the miss reason stays as
                # legible as the hit would have been.
                return None, f"walk-miss:rung-unreadable:{type(exc).__name__}:{depth}{suffix}"
            return match, f"walk-hit:{depth}{suffix}"
        try:
            parent_pid = proc.ppid()
        except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as exc:
            return None, f"walk-miss:rung-unreadable:{type(exc).__name__}:{depth}"
        if not parent_pid or parent_pid == pid:
            return None, "walk-miss:no-parent"
        pid = parent_pid
        depth += 1
    return None, "walk-miss:depth-exhausted"


def _resolve_claude_pid_from_env() -> "tuple[Optional[tuple[int, float]], str]":
    """Resolve ``CLAUDE_PID`` — the harness-exported PID of the session's own
    ``claude.exe`` process — as the preferred Windows Guard-1 source, ahead
    of the bounded ancestor walk (``_find_windows_claude_ancestor``).

    Returns ``(match, reason)``: ``match`` is ``(pid, create_time)`` on
    success, ``None`` on failure. ``reason`` names which of leg (a)'s
    failure modes fired when ``match`` is ``None`` (stable_pid-capture
    breadcrumb plan, C1 — see
    ``docs/plans/2026-08-10-stable-pid-capture-breadcrumb-and-liveness-basis.md``
    § C1 for the full vocabulary this is part of):
      env-hit                    leg (a) answered
      env-miss:absent            CLAUDE_PID not in environ
      env-miss:non-integer       present but not parseable
      env-miss:non-positive      parsed <= 0
      env-miss:name-mismatch     PID resolves, name does not strip to "claude"
      env-miss:<psutil-exc>      NoSuchProcess / AccessDenied / ZombieProcess
      psutil-absent               ``_psutil()`` returned ``None``

    Measured live from inside a real PreToolUse hook across concurrent
    sessions on this box (2026-08-08): ``os.environ["CLAUDE_PID"]`` is
    present in every hook fire and agrees exactly with the value the
    ancestor walk derives — see
    ``state/audits/2026-08-08-the-git-bash-trampoline-is-harness-owned.md``.
    Preferring it sidesteps the walk's fixed depth bound
    (``_STABLE_PID_WINDOWS_ANCESTOR_DEPTH``), which was measured against one
    observed trampoline topology — any host that inserts an extra rung pushes ``claude.exe``
    out of the walk's range, leaving the stamp empty and silently degrading
    liveness to the Layer-2 recency window even though the true PID was
    available all along.

    Still comm-verified exactly like the walk before being trusted: an
    absent, empty, or non-integer env var, a PID naming something other
    than "claude" (after any ``.exe`` suffix), or a PID that no longer
    resolves, all return ``None`` so the caller falls through to the walk —
    never raises, and never stamps an unverified PID. This holds
    self-contained regardless of caller: a non-positive PID (garbage env
    var) is rejected here, before ``psutil.Process(pid)`` — not left to
    depend on a caller-side blanket ``except Exception`` for safety.

    Review: code-reviewer P3 — subject to the SAME PID-reuse/TOCTOU window
    ``_find_windows_claude_ancestor`` already has (the name-read and the
    ``create_time()`` read below are two separate ``try`` blocks, so the
    PID could theoretically be recycled between them): no wider a window
    than the pre-existing walk, not a regression.
    """
    raw = os.environ.get("CLAUDE_PID", "")
    if not raw:
        return None, "env-miss:absent"
    try:
        pid = int(raw)
    except ValueError:
        return None, "env-miss:non-integer"
    if pid <= 0:
        return None, "env-miss:non-positive"
    _ps = _psutil()
    if _ps is None:
        return None, "psutil-absent"
    try:
        proc = _ps.Process(pid)
        comm = _harness_process_comm(proc)
    except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as exc:
        return None, f"env-miss:{type(exc).__name__}"
    if not _is_harness_process(comm):
        return None, "env-miss:name-mismatch"
    try:
        return (pid, proc.create_time()), "env-hit"
    except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.ZombieProcess) as exc:
        return None, f"env-miss:{type(exc).__name__}"


# ---------------------------------------------------------------------------
# cs_init
# ---------------------------------------------------------------------------


def init(
    session_id: str,
    goal: str = "",
    cwd: Optional[str] = None,
    *,
    sessions_base: Optional[str] = None,
    root: Optional[str] = None,
) -> bool:
    """Port of ``cs_init <session_id> [goal]``.

    Create the session directory and write initial files. Idempotent: if
    the session dir already exists, refreshes ``meta.json`` activity
    fields only (does not overwrite an existing ``goal``).

    Fires on EVERY session start (session-init.sh hook) — HOT. Per the
    recipe's GIVES-PAUSE item 5: ``session-init.sh``'s own comments
    describe a native/legacy dual-path for the REAP calls but NOT for
    ``cs_init`` itself (confirmed on HEAD: the hook still calls the plain
    bash ``cs_init "$SESSION_ID"``) — this function is therefore a NEW
    native op, not a repoint of an already-existing dual-path.

    ``sessions_base`` / ``root`` (keyword-only) are a PRE-RESOLVED seam for a
    caller that is already holding both answers, so ``init`` does not pay to
    rediscover them. Both default to None, which reproduces the resolving
    behaviour byte-for-byte -- no existing caller changes.

    The seam exists because ``init``'s two resolutions are the whole reason a
    hot-path caller could not afford to call it. ``hooks/track_touched_files``
    resolves the session hub and the worktree root itself before it appends a
    ``T`` event, and a ``T`` event IS a claim: that caller must be able to
    create the session's liveness record without re-deriving what it just
    resolved, or the record never gets written and a session holding claims
    stays invisible to ``liveness.live_session_ids``. Its handler is pinned at
    ZERO ``core.git_root`` calls by
    ``hooks/tests/test_track_touched_files_normalize.py``
    (``TestHandlerZeroSpawnFastArmAtCaller``), a guard defending
    docs/plans/2026-08-22-track-touched-files-pays-only-for-the-append.md § C1.
    This seam is what lets that guard stay green on its own terms instead of
    by exemption.

    Negative-spec:
        - ``sessions_base`` is the session HUB (``<git-common-dir>/coordinator-
          sessions``), NOT a session directory. Passing the latter nests the
          record one level deep and hides it from every reader.
        - ``root`` is a WORKTREE root, the same contract ``git_root`` returns —
          it is handed to ``git.git_state`` for ``head_at_start``/``branch``.
        - Neither is validated against the other, by design: this is a seam for
          a caller that ALREADY resolved both from one common dir, not a
          general-purpose relocation knob. A caller that has not resolved them
          passes neither and gets the resolving path.

    Returns True on success, False on failure (not in a git repo, etc.).
    """
    if not session_id:
        raise ValueError("session_id required")

    base = sessions_base if sessions_base else sessions_dir(cwd)
    if not base:
        return False
    sdir = Path(base) / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    started_at = sdir / "started_at"
    if not started_at.is_file():
        started_at.write_text(now_iso(), encoding="utf-8", newline="\n")

    # Both bookkeeping fields below (`head_at_start`, `branch`) are read from
    # `.git/HEAD` in-process rather than from two `git rev-parse` spawns.
    # DR-344: git justifies itself PER USE and process creation is the cost,
    # not the query -- these two answers are a file read. Measured on this box
    # (2026-08-25, job-object process time, k=20 x n=5): the spawning shape cost
    # +41.41ms / +3 spawns marginal in the dir-present/meta-absent shape and
    # +71.88ms / +6 spawns cold, on a function whose own docstring says it fires
    # on EVERY session start.
    #   -> docs/research/spike-verdicts/2026-08-25-does-the-first-fire-really-pay-core-init.md
    # Imported lazily, INSIDE init(), on purpose: `git_state` pulls in
    # `git_objects` (zlib/hashlib) which this module otherwise never needs, and
    # `core` is on the import-budget hot path. The only caller that pays this
    # import is the one that was about to pay two process creations.
    from coordinator_core.git import git_state as _git_state

    root = root if root else git_root(cwd)

    head_at_start = sdir / "head_at_start"
    if not head_at_start.is_file():
        # Fail-open to "unknown", exactly as the spawning form did on a
        # non-zero rc, an OSError, or a timeout: an unborn branch (HEAD names
        # a ref with no commit yet) reads None here and lands on the same
        # "unknown" `git rev-parse HEAD`'s own non-zero exit produced.
        head_sha = (_git_state.head_sha(root) if root else None) or "unknown"
        head_at_start.write_text(head_sha, encoding="utf-8", newline="\n")

    # A `touched.txt` placeholder used to be created here so a freshly-
    # created session dir always carried SOME record file for the recency
    # probes to key on. Removed 2026-08-25 (C5, docs/plans/2026-08-25-the-
    # legacy-touch-record-is-retired-by-repointing-its-writers.md § AC6):
    # `started_at` (above) and `head_at_start` (below) are already written
    # unconditionally on every dir creation, so they already discharge that
    # role for `liveness.newest_record_mtime`'s scan -- creating a file named
    # for the retired dialect was redundant for this writer's own dirs, not
    # load-bearing. NOT a swap onto a new literal (AC6's own "widen, do not
    # swap"): this writer simply stops minting the retired name; a dir
    # written ONLY by `hooks.track_touched_files` (never touching this
    # function) still carries its own record file independently.

    # UNCONDITIONAL on every call, including the idempotent refresh of an
    # already-initialised session -- so this was the one spawn `init()` could
    # never skip, whatever the directory state. That is why guarding the call
    # site is not a substitute for making the call cheap.
    branch = (_git_state.head_branch(root) if root else None) or "unknown"

    now = now_iso()
    pid = os.getpid()

    # Guard 1 — comm-verify the parent process before storing as stable_pid.
    # Requires an EXACT match on "claude" (Linux truncates comm to 15
    # chars; "claude" is 6 chars, safe) — a prefix match (e.g.
    # "claude-helper") would store the wrong stable_pid and defeat the
    # skip-safe purpose. Non-match (shell, CI runner, future binary)
    # leaves stable_pid empty -> recency-only fallback, zero regression.
    stable_pid = ""
    stable_pid_lstart = ""
    stable_pid_start_epoch = ""
    # stable_pid_capture — WHY the above stamp did or didn't happen, on
    # every run (AC1/AC2, stable-pid-capture-breadcrumb-and-liveness-basis
    # plan § C1). Never gates behaviour: `match`/`stable_pid` above are
    # computed exactly as they were before this breadcrumb existed, and
    # this string is purely observational — see AC3/AC11.
    stable_pid_capture = "psutil-absent"
    ppid = os.getppid()

    if _IS_WINDOWS:
        # Windows Guard-1 (CLAUDE_PID-preferred, 2026-08-08 fix): the harness
        # exports CLAUDE_PID — the session's own claude.exe PID — into every
        # command-hook environment (see _resolve_claude_pid_from_env's
        # docstring for the measurement). That is tried FIRST; the bounded
        # ancestor walk (_find_windows_claude_ancestor) is the fallback for
        # a host/harness where the env var is absent or fails comm-verify.
        # Native Windows has no `ps` binary, and unlike POSIX (where `sh -c
        # "<cmd>"` exec-replaces the shell so the hook's direct parent IS
        # claude), Windows has no exec-replace: the Git-Bash trampoline
        # inserts real persisting rungs between the hook subprocess and the
        # claude.exe session process (measured hook chain: python3.exe ->
        # bash.exe -> bash.exe -> claude.exe) — a parent-only check therefore
        # NEVER matched on Windows, which is why the walk exists as the
        # fallback. Both sources comm-verify identically: the process name
        # carries a `.exe` suffix ("claude.exe"); it is stripped and an EXACT
        # "claude" match is required (a prefix like "claude-helper" would
        # store the wrong stable_pid and defeat the skip-safe purpose,
        # mirroring the POSIX comm-verify). create_time() is captured as BOTH
        # the epoch identity (stable_pid_start_epoch) and the birth-instant
        # token (stable_pid_lstart) — there is no ps lstart string on
        # Windows, and a non-empty stable_pid_lstart is what gates
        # session_live's Layer 1. Neither source resolving leaves stable_pid
        # empty -> recency-only Layer-2 fallback, zero regression.
        #
        # Deliberate exception to fail-loud-on-missing-psutil: unlike
        # pid_alive / stable_pid_alive, an absent psutil HERE degrades to the
        # SAME recency-only Layer-2 path that a non-"claude" match (shell,
        # CI runner) already takes today — it never causes a live process to
        # read DEAD, so there is no wrongful-takeover shape to close. Raising
        # here would turn a harmless precision loss into a hard init()
        # failure for no correctness gain.
        if _psutil() is not None:
            try:
                match, env_reason = _resolve_claude_pid_from_env()
            except Exception as exc:
                match, env_reason = None, f"env-miss:{type(exc).__name__}"
            if match is None:
                try:
                    match, walk_reason = _find_windows_claude_ancestor(ppid)
                except Exception as exc:
                    match, walk_reason = None, f"walk-miss:{type(exc).__name__}"
                # Both legs RAN (leg (a) missed, so leg (b) was tried) —
                # record BOTH families regardless of leg (b)'s outcome. A
                # single "last answer wins" string would lose leg (a)'s
                # reason on the walk-hit path too — e.g. collapsing
                # "env-miss:absent, rescued by the walk" into the same
                # breadcrumb as "env-hit" outright, which is exactly the
                # env-vs-walk distinction the version-floor hypothesis in
                # this plan's Problem section turns on. A stable "|" join,
                # documented here as the separator between the env-miss and
                # walk-miss/walk-hit halves.
                stable_pid_capture = f"{env_reason}|{walk_reason}"
            else:
                stable_pid_capture = env_reason
            if match is not None:
                found_pid, ppid_ct = match
                stable_pid = str(found_pid)
                epoch_i = int(ppid_ct)
                # Review: code-reviewer nit — mirror the POSIX branch's `!= 0`
                # guard (`0` is lstart_to_epoch's "parse failed" sentinel; a
                # real process birth at the Unix epoch is impossible).
                stable_pid_start_epoch = str(epoch_i) if epoch_i != 0 else ""
                stable_pid_lstart = str(epoch_i)
    else:
        # POSIX Guard-1 (ps-to-psutil port, 2026-07-27): comm-verify the
        # parent via psutil instead of spawning `ps -p <ppid> -o
        # comm=,lstart=`. This removes ``lstart_to_epoch`` from the WRITE
        # path entirely — the persisted epoch is now derived the same way
        # ``stable_pid_alive``'s primary compare derives it
        # (``psutil.Process(ppid).create_time()``), so a stored record and
        # its freshest liveness check are derivation-consistent end to end.
        # ``lstart_to_epoch`` remains load-bearing only on the legacy READ
        # path (``stable_pid_alive``'s pre-2026-07-27-meta fallback above).
        #
        # ``_harness_process_comm`` — NOT ``psutil.Process.name()`` directly
        # (C3, DR-302): on this build ``name()`` reports the version string
        # (``2.1.231``), so it prefers the basename of ``cmdline()[0]``
        # (``claude``) and falls back to ``name()`` only when ``cmdline()``
        # is unreadable or empty. Read that function's docstring before
        # reasoning about which field this line compares.
        #
        # The truncation note this comment used to carry applies to the
        # FALLBACK leg only: ``name()`` is the POSIX equivalent of the
        # retired ``ps -o comm=`` column, both reporting a potentially
        # 15-char-truncated-on-Linux name, and "claude" (6 chars) is short
        # enough that the risk the exact-match guard was written to dodge
        # never applies. It says nothing about the preferred ``cmdline()``
        # leg, which is a full argv path and not truncated. ``.exe``
        # stripping is centralized inside ``_harness_process_comm`` and no
        # longer duplicated per branch.
        _ps = _psutil()
        posix_capture_exc = None
        try:
            parent = _ps.Process(ppid)
            ppid_comm = _harness_process_comm(parent)
            ppid_ct = parent.create_time()
        except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.Error) as exc:
            ppid_comm, ppid_ct = "", None
            posix_capture_exc = exc

        if _is_harness_process(ppid_comm) and ppid_ct is not None:
            stable_pid = str(ppid)
            epoch_i = int(ppid_ct)
            # `0` is lstart_to_epoch's historical "parse failed" sentinel;
            # a real process birth at the Unix epoch is impossible, so the
            # same `!= 0` guard is kept for the psutil-derived epoch too.
            stable_pid_start_epoch = str(epoch_i) if epoch_i != 0 else ""

        # POSIX leg (b) — CLAUDE_PID. The parent-only check above holds only
        # where the harness's ``sh -c "<cmd>"`` exec-replaces the shell,
        # leaving ``claude`` as the hook subprocess's direct parent. Measured
        # on macOS (2026-08-22, host `machine-b`): the wrapper the harness
        # runs is a COMPOUND command (``source <snapshot> ... && eval '<cmd>'
        # && pwd -P >| <file>``), so bash cannot exec-replace itself and a
        # real bash rung persists between the hook and ``claude`` — the same
        # shape the Windows trampoline has. Every session on that host
        # stamped ``posix-parent-miss:name-mismatch`` and left ``stable_pid``
        # empty, disarming ``session_live``'s Layer 1 and putting K-006's F0
        # hazard back in play (``state/kill-ledger.md`` § K-006).
        # ``CLAUDE_PID`` is exported into every hook environment and is
        # comm-verified by the same predicate before being trusted, so it
        # closes that gap without widening what counts as the harness
        # process.
        #
        # Ordered SECOND here, unlike the Windows branch where it is
        # preferred: on POSIX a direct parent that comm-verifies is the
        # stronger witness wherever it exists, and leg (b) therefore only
        # ever runs on a path that previously produced no stamp at all.
        env_reason = ""
        if not stable_pid:
            try:
                env_match, env_reason = _resolve_claude_pid_from_env()
            except Exception as exc:
                env_match, env_reason = None, f"env-miss:{type(exc).__name__}"
            if env_match is not None:
                env_pid, env_ct = env_match
                stable_pid = str(env_pid)
                epoch_i = int(env_ct)
                stable_pid_start_epoch = str(epoch_i) if epoch_i != 0 else ""

        # POSIX breadcrumb (EM decision, C1 dispatch brief — AC1 says
        # "every run" and the plan's vocabulary was Windows-only; extended
        # rather than left silent). Computed from the SAME state the
        # stamping logic above already derived, in a block wrapped so it
        # can never itself raise into init() (AC3) — the stamping logic
        # above it is untouched and unwrapped, so behaviour is unchanged.
        # When leg (b) ran, both halves are recorded "|"-joined exactly as
        # the Windows branch joins its env and walk halves: leg (a)'s reason
        # is never collapsed away by leg (b)'s answer.
        try:
            if posix_capture_exc is not None:
                posix_reason = f"posix-parent-miss:{type(posix_capture_exc).__name__}"
            elif _is_harness_process(ppid_comm) and ppid_ct is not None:
                posix_reason = "posix-parent-hit"
            elif not _is_harness_process(ppid_comm):
                posix_reason = "posix-parent-miss:name-mismatch"
            else:
                posix_reason = "posix-parent-miss:no-create-time"
            stable_pid_capture = f"{posix_reason}|{env_reason}" if env_reason else posix_reason
        except Exception:
            stable_pid_capture = "posix-parent-miss:unknown"

    meta_path = sdir / "meta.json"
    if meta_path.is_file():
        # Refresh path: pid/last_activity/branch/stable_pid*/
        # stable_pid_capture only. `goal` is deliberately NOT written back
        # here — bash `cs_init` writes goal only on first create (see its
        # "write goal only on first create" comment); a re-init with a new
        # non-empty goal does NOT overwrite the stored goal. Do not "fix"
        # this to write effective_goal.
        #
        # Batched into ONE `update_meta_fields` rewrite (AC10) rather than
        # the field-at-a-time `update_meta_field` calls this used to make —
        # `init()` fires at every session start on a box whose load norm is
        # 50-70 concurrent LLMs (docs/wiki/machine-load-norm.md); adding a
        # 6th sequential meta.json read-modify-atomic-rewrite for the
        # breadcrumb would have been a real hot-path cost, not a nitpick.
        refresh_fields: "dict[str, object]" = {
            "pid": pid,
            "last_activity": now,
            "branch": branch,
            "stable_pid_capture": stable_pid_capture,
        }
        if stable_pid:
            refresh_fields["stable_pid"] = stable_pid
            # POSIX stopped WRITING stable_pid_lstart 2026-07-27 (ps-to-
            # psutil port, stable_pid_alive docstring PLATFORM SPLIT note):
            # stable_pid_start_epoch supersedes it, and the read-only legacy
            # fallback exists solely for records already on disk. Windows
            # still writes it — that field IS its create_time() token, not a
            # derived-and-superseded duplicate.
            if _IS_WINDOWS:
                refresh_fields["stable_pid_lstart"] = stable_pid_lstart
        if stable_pid_start_epoch:
            refresh_fields["stable_pid_start_epoch"] = stable_pid_start_epoch
        update_meta_fields(str(sdir), refresh_fields)
    else:
        data = {
            "session_id": session_id,
            "branch": branch,
            "pid": str(pid),
            "last_activity": now,
            "goal": goal or "",
            "stable_pid_capture": stable_pid_capture,
        }
        if stable_pid:
            data["stable_pid"] = stable_pid
            if _IS_WINDOWS:
                data["stable_pid_lstart"] = stable_pid_lstart
            if stable_pid_start_epoch:
                data["stable_pid_start_epoch"] = stable_pid_start_epoch
        # Atomic, mirroring `update_meta_fields` rather than the plain
        # `write_text` this used to be. `ensure_session` makes this the
        # machine's ONE session-record create, reached concurrently by peers on
        # a box whose load norm is 50-70 LLMs: a truncate-then-write leaves a
        # window where a peer reads a torn meta.json, and a torn file is WORSE
        # than an absent one here -- `ensure_session`'s `is_file()` arm sees a
        # record and returns, while every `update_meta_field` write against it
        # no-ops on the JSON parse. os.replace closes the window on both
        # platforms.
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="meta.json.", dir=str(sdir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(data, fh, indent=2)
                    fh.write("\n")
                os.replace(tmp_name, meta_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    # Best-effort tmp-file cleanup; the original exception is
                    # re-raised below regardless.
                    pass
                raise
        except OSError:
            return False

    return True
