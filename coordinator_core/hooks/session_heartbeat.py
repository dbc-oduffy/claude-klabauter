"""
coordinator_core.hooks.session_heartbeat — Pre+PostToolUse bookkeeping hook op.

Purpose: Write a last_activity heartbeat to the current session's meta.json so
the orphan sweep and the claim layer (_cs_is_session_live) can distinguish a
live long-running session (e.g. a multi-minute Bash extraction) from a truly
dead session that was never cleaned up.

Registered on BOTH PreToolUse:Bash and PostToolUse:Bash (hooks.json). The
PreToolUse leg stamps recency at the START of a Bash call; the PostToolUse leg
stamps it again at COMPLETION. This closes the F0 staleness hole (the Staff Engineer,
2026-06-23): a single Bash command longer than the 30-min liveness window (a UE
build, a large extraction) would otherwise freeze last_activity at its start
time and cross the liveness threshold mid-command, making a genuinely-live
session's claim wrongly takeable/reapable. With both legs, recency is stamped at
both ends of every command, so only a session idle >30 min BETWEEN commands ages
out. The two legs are idempotent — same throttle bucket, same write — so the
PostToolUse leg is a no-op when the PreToolUse leg already wrote within 60s.

Throttle: 60-second mtime check on meta.json (read-only stat, no dual-writer
risk). If meta.json was modified within the last 60 seconds, the write is
skipped. This prevents hot-loop writes on rapid Bash sequences while ensuring at
least one write per 60s during any continuous Bash activity.

Write target: .git/coordinator-sessions/<session_id>/meta.json (last_activity
field) — session-runtime layer. NOT state/ substrate (see SC-2 correction in
the pcore-08 plan). last_activity is written via liveness.py::update_last_activity,
which delegates to the native core.update_meta_field (atomic tempfile +
os.replace) — NOT a Python read-modify-write here (dual-writer hazard; see D4).

Input (flat scalar):
    session_id — the coordinator session identifier

Always returns no_advisory() — the product is the on-disk write side-effect.
Never blocks tool calls (bookkeeping op, not a gate).

Negative-spec:
    Do NOT do a Python read-modify-write of meta.json here — that would put a
    second writer beside the canonical one and clobber other fields. All
    last_activity writes MUST route through update_last_activity()
    (coordinator_core/liveness.py) and so through core.update_meta_field, the
    single-writer implementation every other meta write also routes through;
    that sole-routing IS the enforcement of the single-liveness-key invariant.
    Do NOT write to state/ or archive/ — only .git/coordinator-sessions/ is
    sanctioned for bookkeeping ops (D2, pcore-08).

Intentional engine-context deviation — no fallback writer (Review: code-reviewer F3):
    Port of: session-heartbeat.sh (DoE d39ab164, 2026-07-16), whose bash source
    fell back to an inline sed read-modify-write of meta.json when
    coordinator-session.sh was not found.
    The Python engine does NOT implement that fallback. update_last_activity()
    silently no-ops on a missing or non-writable meta.json instead. Heartbeats are
    best-effort (see "Never blocks tool calls" above), so silent omission under a
    broken install is acceptable, and a second writer for a scenario the engine's
    install contract is designed to prevent is not worth the clobber risk.

Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § C2, D4
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.liveness import update_last_activity

# Throttle window.
_THROTTLE_SECONDS = 60


def _stat_mtime(path: str) -> float:
    """Return the mtime (epoch float) of path, or -1.0 if the file is absent/unreadable.

    Blocking — must be called via asyncio.to_thread().
    """
    try:
        return os.stat(path).st_mtime
    except OSError:
        return -1.0


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SSZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bootstrap_meta(session_id: str, git_root: str) -> None:
    """Bootstrap an ABSENT meta.json via the canonical core.init writer (blocking).

    Defect A (2026-07-24) self-heal from the earliest-firing hook. When another
    bookkeeping writer created the session dir first, meta.json — and the Layer-1
    stable_pid liveness signal — was never written, and this heartbeat's
    throttle-stat then no-op'd forever on the absent file. Because Bash usually
    precedes the first edit, healing here lands meta.json sooner than the
    track_touched (edit) path.

    Idempotent CREATE — never a read-modify-write, so no dual-writer clobber of a
    concurrent last_activity stamp (there is no existing meta.json to race; this
    is called ONLY on the absent-file branch). This is the deliberate, bounded
    exception to the module's "no Python meta.json write" negative-spec: it does
    not modify an existing file. core.init stamps stable_pid via Guard-1 and
    writes last_activity as part of the create.

    Negative-spec — Guard-1 does NOT resolve `claude` from the immediate parent on
    every platform, and this hook path is no exception. On Windows the session
    binary sits several rungs up behind the Git-Bash trampoline; ``os.getppid()``
    has never named `claude` on a Windows host. Guard-1 therefore prefers the
    harness-exported ``CLAUDE_PID`` (comm-verified, measured present in every hook
    fire 2026-08-08) and falls back to ``core._find_windows_claude_ancestor``'s
    bounded ancestor walk. A stamp is still not guaranteed here — when neither
    source comm-verifies, stable_pid stays empty and liveness falls through to the
    Layer-2 recency window, which is the skip-safe design, not a defect at this
    call site.
    """
    try:
        from coordinator_core.session import core as _session_core

        # `ensure_meta` is the single named owner of "create the record if
        # the directory exists without one" (session/core.py); it performs
        # exactly the idempotent `init` CREATE this function used to call
        # inline, behind the same absent-file precondition. Routed through
        # it so the heal has one definition rather than a copy per consumer.
        _session_core.ensure_meta(session_id, git_root or None)
    except Exception:
        pass


@register_op("hooks.session_heartbeat")
async def _handler(params: dict, repo_root=None) -> dict:
    """Pre+PostToolUse bookkeeping op: stamp last_activity in meta.json.

    Reads session_id from the flat-scalar input; resolves the session dir via
    the repo_root parameter; applies the 60-second mtime throttle (stat in to_thread);
    then invokes update_last_activity() (in to_thread) to write the field via
    the canonical native writer, core.update_meta_field.

    Returns no_advisory() unconditionally — the product is the write side-effect.
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    session_id = field(params, "session_id")
    if not session_id:
        # No session_id — nothing to stamp; exit silently.
        return no_advisory()

    _effective_root = repo_root
    git_root = str(_effective_root) if _effective_root else ""
    if not git_root:
        return no_advisory()

    # Build the session dir and meta.json path.
    # Write scope: .git/coordinator-sessions/<session_id>/ (D2 sanctioned exception).
    # C1d: route through git_common_dir so linked worktrees resolve to the main .git
    # directory (a real dir) rather than the worktree's .git FILE.
    try:
        _sessions_base = git_common_dir(Path(git_root)) / "coordinator-sessions"
    except RuntimeError:
        # Review: code-reviewer — fallback had ".git" doubled: git_root IS git_common_dir,
        # so Path(git_root) / ".git" / "coordinator-sessions" → <repo>/.git/.git/… (never exists).
        # Fix: drop the redundant ".git" join in this fallback branch.
        _sessions_base = Path(git_root) / "coordinator-sessions"
    session_dir = str(_sessions_base / session_id)
    meta_json = os.path.join(session_dir, "meta.json")

    # --- Throttle: read meta.json mtime, skip if within 60 s ---
    # Read-only stat — no dual-writer risk.
    mtime = await asyncio.to_thread(_stat_mtime, meta_json)
    if mtime < 0:
        # meta.json absent (defect A, 2026-07-24). Two cases:
        #  (a) session dir EXISTS but meta.json was never written (another
        #      bookkeeping writer created the dir first) — the poisoned state.
        #      Bootstrap meta.json (with a Guard-1 stable_pid) via core.init;
        #      this is the earliest-firing self-heal (Bash precedes most edits).
        #  (b) session dir ABSENT — archived/reaped or never created; do NOT
        #      resurrect it. Skip silently.
        if await asyncio.to_thread(os.path.isdir, session_dir):
            await asyncio.to_thread(_bootstrap_meta, session_id, git_root)
        return no_advisory()

    now_epoch = time.time()
    if (now_epoch - mtime) < _THROTTLE_SECONDS:
        # Recently updated — skip write. PostToolUse leg is a no-op when PreToolUse
        # already wrote within the 60 s bucket (same idempotent throttle as source).
        return no_advisory()

    # --- Write last_activity via the canonical native writer ---
    # update_last_activity() delegates to core.update_meta_field — NOT a
    # read-modify-write of our own. Wrapped in to_thread because that writer does
    # blocking disk I/O (tempfile write + os.replace).
    iso = _now_iso()
    await asyncio.to_thread(update_last_activity, session_dir, iso)

    return no_advisory()
