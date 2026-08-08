"""
coordinator_core.hooks.track_touched_files — PostToolUse bookkeeping hook op.

Purpose: Records the file path modified by the current Edit/Write/MultiEdit/NotebookEdit
tool call into two append-only T-event logs:
  - per-session:  .git/coordinator-sessions/<session_id>/touched.txt
  - per-agent:    .git/coordinator-sessions/.agents/<agent_id>/touched.txt
    (agent-keyed write fires only for subagent tool calls — agent_id present and
    resolving to a known agent shape.)

Port of the retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
track-touched-files.sh (deleted 2026-07-22, example-doctrine-repo ``3a561713``).

Bookkeeping op (MUTATING) — the product is the on-disk write side-effect, NOT an advisory.
Returns no_advisory() (empty dict) on every invocation path.

Write confinement (hard): writes ONLY under .git/coordinator-sessions/ (session-runtime
layer); NEVER writes state/, archive/, or any path outside that tree.

D6 write-atomicity: uses a module-level asyncio.Lock per target file to serialise
concurrent append invocations in the shared singleton engine. Process-isolation
is absent in-engine — the source's POSIX O_APPEND atomicity contract does not transfer
to in-engine thread-pool concurrency.

Input contract (flat-scalar, _payload.field() — treat "" as absent):
    session_id  — the firing session's id
    tool_name   — Write | Edit | MultiEdit | NotebookEdit
    file_path   — the file path declared by the tool call
    agent_id    — raw subagent id (present only for subagent fires)

Ownership back-pointer (subagent fires only): writes
.agents/<agent_id>/em-session-id.txt when absent, via two writers in order —
(1) an advisory CLAUDE_CODE_SESSION_ID-derived write attributing a
Workflow-internal agent() spawn to its dispatching EM (Piece 2,
docs/plans/2026-08-03-scope-guard-peer-claim-release.md § C7; skipped unless the
env var is set AND differs from `session_id`, so it never misattributes the
firing session's own work to itself), then (2) the pre-existing `session_id`
fallback (2026-08-03 break-class fix). Both reuse
track_dispatched_agents._write_backpointer_sync, so a real dispatch-time record
always wins (idempotent, non-empty-file-wins).

Negative-spec:
    Do NOT emit advisories — this op's value is the on-disk write side-effect.
    Do NOT write state/, archive/, or any path outside .git/coordinator-sessions/.
    Do NOT trust CLAUDE_CODE_SESSION_ID as a subagent-vs-EM discriminator anywhere
    else — it is read here for attribution-when-absent ONLY, gated by the
    env != session_id guard above.

This writer emits ``T``-verb events (``scope.format_touch_event``) into the shared
append-only ``touched.txt`` log — the same event dialect ``session/scope.py::touch``
and ``session/claims.py::atomic_dedup_append`` already write, so the claim/release
projection (``_last_verb_map``) reads one dialect across all three writers instead of
mis-reading a bare-line legacy record from this one.

Spec backlink: docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md § C1
Spec backlink: docs/plans/2026-08-03-scope-guard-peer-claim-release.md § C7
Spec backlink: docs/plans/2026-08-03-track-touched-files-emits-t-events.md § C1
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.session.scope import format_touch_event, normalize_touch_path
from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# D6 — per-target-file asyncio.Lock registry.
#
# The engine is a per-repo singleton shared by concurrent sessions. Two sessions
# returning simultaneously can invoke this op concurrently — both dispatch
# asyncio.to_thread() tasks that touch the same touched.txt. Process-isolation
# (which serialises the source bash hook's concurrent O_APPEND writes) is absent
# in-engine. An asyncio.Lock per target file serialises the read-check-then-append
# cycle, converting the TOCTOU window from "tolerated duplicate" (bash) to "hard
# disallowed" (in-engine).
#
# Lazily created on first access; accessed ONLY from the event loop (async handler),
# so no cross-thread contention on the dict itself.
# ---------------------------------------------------------------------------
_FILE_LOCKS: dict[str, asyncio.Lock] = {}

# Review: code-reviewer F2 — bound _FILE_LOCKS growth. The engine may run for a full
# workday; sessions archive but locks were never evicted, accumulating O(sessions×agents)
# entries indefinitely. Two-tier eviction: (1) on new-path creation, sweep entries whose
# parent directory no longer exists (session archived → dir gone — cheap isdir check);
# (2) hard cap via oldest-entry eviction if the stale sweep wasn't sufficient.
_MAX_FILE_LOCKS = 256


def _get_lock(path: str) -> "asyncio.Lock":
    """Return (creating if absent) the per-file asyncio.Lock for path.

    On new-path creation, evicts stale entries (parent dir gone — session archived) to
    bound _FILE_LOCKS to O(active sessions × agents). Falls back to oldest-entry eviction
    if the stale sweep alone isn't sufficient (safety cap: _MAX_FILE_LOCKS).

    Accessed only from the event loop — no threading synchronisation needed on _FILE_LOCKS
    itself (all callers are async coroutines running in the event loop thread).
    """
    # asyncio deferred to first use here (not module scope). Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    if path not in _FILE_LOCKS:
        # Evict entries for paths whose containing dir is gone (session archived).
        stale = [p for p in _FILE_LOCKS if not os.path.isdir(os.path.dirname(p))]
        for p in stale:
            del _FILE_LOCKS[p]
        # Hard cap: evict oldest insertion-order entries if stale sweep wasn't sufficient.
        while len(_FILE_LOCKS) >= _MAX_FILE_LOCKS:
            _FILE_LOCKS.pop(next(iter(_FILE_LOCKS)))
        _FILE_LOCKS[path] = asyncio.Lock()
    return _FILE_LOCKS[path]


# ---------------------------------------------------------------------------
# Agent-id resolution — Port of: coordinator-session.sh::resolve_subagent_identity
# (example-doctrine-repo e34f2484, 2026-07-22)
#
# Three resolution paths (including the C10 named-teammate
# extension; docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C10):
#   (a) Bare hex  ^[a-f0-9]{12,}$  — unnamed agent; return unchanged.
#   (b) Named teammate  ^a(.+)-[a-f0-9]{16}$  — extract name, build canonical id
#       via cs_build_canonical_agent_id equivalent: "<name>@session-<short>".
#   (c) Unrecognised shape — return "" (fail-closed; agent-keyed write skipped).
# ---------------------------------------------------------------------------
def _resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    """Translate a raw subagent-side agent_id to the canonical EM-side id.

    Returns "" on unrecognised shape (fail-closed) — the caller skips the
    agent-keyed write when the result is empty.
    """
    # (a) Bare hex — unnamed agent fast path; session_id ignored.
    if re.match(r"^[a-f0-9]{12,}$", agent_id):
        return agent_id

    # (b) Named teammate: a<name>-<16hex>
    m = re.match(r"^a(.+)-[a-f0-9]{16}$", agent_id)
    if m:
        name = m.group(1)
        # (.+) guarantees non-empty name when the match succeeds — `if name` is
        # unreachable, kept for explicitness. Review: code-reviewer F5.
        if name and len(session_id) >= 8:
            short = session_id[:8]
            return f"{name}@session-{short}"
        return ""

    # (c) Unrecognised shape — fail-closed.
    return ""


# ---------------------------------------------------------------------------
# Session-dir lazy init — mirrors the "lib missing — minimal bootstrap" branch.
#
# Fires on first touch per session (SESSION_DIR not yet present). The lib-sourced
# cs_init path is omitted; the Python engine runs standalone. This bootstrap is
# byte-compatible with the bash lib-missing fallback — same files, same formats.
# ---------------------------------------------------------------------------
def _ensure_session_dir(
    session_dir: str,
    session_id: str,
    touched_file: str,
    git_root: str,
) -> None:
    """Create the per-session coordinator-sessions directory (blocking — call via to_thread).

    Minimal bootstrap:
    - mkdir -p session_dir
    - touch touched.txt
    - write started_at   (ISO 8601 UTC)
    - write head_at_start (git HEAD sha or "unknown")
    - write meta.json    (minimal session fields)

    Idempotent: each file is written only if absent, so a concurrent caller
    racing on first-touch — or a call against a dir another bookkeeping writer
    already created (push cursor, session-shape) with meta.json still missing —
    backfills the missing files rather than short-circuiting on dir existence.
    """
    import subprocess

    os.makedirs(session_dir, exist_ok=True)

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # Review: code-reviewer F4 — utcnow() deprecated Python 3.12+

    # Touch touched.txt
    if not os.path.exists(touched_file):
        Path(touched_file).touch()

    # Write started_at
    started_at_path = os.path.join(session_dir, "started_at")
    if not os.path.exists(started_at_path):
        with open(started_at_path, "w", encoding="utf-8") as fh:
            fh.write(now_iso + "\n")

    # Write head_at_start (git HEAD sha; fall back to "unknown" on any error)
    head_at_start_path = os.path.join(session_dir, "head_at_start")
    if not os.path.exists(head_at_start_path):
        try:
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=git_root,
                stderr=subprocess.DEVNULL,
                **no_console_creationflags(),
            ).decode("utf-8", errors="replace").strip()
        except Exception:
            head = "unknown"
        with open(head_at_start_path, "w", encoding="utf-8") as fh:
            fh.write(head + "\n")

    # Write meta.json (minimal fields — mirrors the bash printf format exactly)
    meta_path = os.path.join(session_dir, "meta.json")
    if not os.path.exists(meta_path):
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=git_root,
                stderr=subprocess.DEVNULL,
                **no_console_creationflags(),
            ).decode("utf-8", errors="replace").strip()
        except Exception:
            branch = "unknown"
        meta = {
            "session_id": session_id,
            "branch": branch,
            "pid": str(os.getpid()),
            "last_activity": now_iso,
            "goal": "",
        }
        with open(meta_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Session bootstrap gate + canonical-writer dispatch (defect A, 2026-07-24).
#
# meta.json — and its Layer-1 `stable_pid` liveness signal — was landing only
# when a meta-writing op happened to win the race to create the session dir.
# Because the push-failure cursor / session-shape / dispatched-agents writers
# usually create the dir first, meta.json was absent for ~all sessions, forcing
# liveness onto the 30-min Layer-2 recency fallback — so a killed session read
# LIVE for up to half an hour and held /pickup claims hostage. The fix routes
# meta.json creation through the canonical core.init() writer whenever it is
# missing OR unstamped, decoupled from dir creation.
# ---------------------------------------------------------------------------
def _needs_session_init(session_dir: str, meta_file: str) -> bool:
    """True when session bootstrap should run (blocking — call via to_thread).

    Fires when the session dir or meta.json is absent, or when meta.json exists
    but lacks the Layer-1 `stable_pid` signal. Cheap (one isdir + at most one
    small JSON read) and returns False in the steady state — meta.json present
    WITH a stable_pid — so the hot edit path pays no git-subprocess cost once
    liveness is stamped.
    """
    if not os.path.isdir(session_dir):
        return True
    try:
        with open(meta_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return True  # absent / unreadable / malformed → (re)init
    return not (isinstance(data, dict) and data.get("stable_pid"))


def _bootstrap_session(
    session_id: str, git_root: str, session_dir: str, touched_file: str
) -> None:
    """Create/refresh the session dir + meta.json (blocking — call via to_thread).

    Primary: the canonical ``coordinator_core.session.core.init`` writer, which
    is idempotent (backfills into an existing dir) and stamps ``stable_pid`` via
    Guard-1 when the parent process resolves to ``claude`` — the case on this
    in-process PostToolUse hook path. Falls back to the self-contained
    ``_ensure_session_dir`` bootstrap when core.init cannot resolve a git session
    hub (non-git test fixtures) or is unavailable.
    """
    try:
        from coordinator_core.session import core as _session_core

        if _session_core.init(session_id, cwd=git_root or None):
            return
    except Exception:
        pass
    _ensure_session_dir(session_dir, session_id, touched_file, git_root)


# ---------------------------------------------------------------------------
# Atomic append (blocking — call via to_thread WHILE holding the per-file
# asyncio.Lock).
#
# Port of: cs_atomic_dedup_append from coordinator-session.sh (example-doctrine-repo e34f2484,
# 2026-07-22), made EVENT-AWARE (plan docs/plans/2026-08-03-track-touched-
# files-emits-t-events.md § C1, matching the EM-ratified precedent already
# landed for session/claims.py::atomic_dedup_append). The dedup fast-exit that
# enforced "exactly one line per path" is RETIRED — append-only last-event-wins
# is the invariant now, so duplicate T-events for the same path are expected,
# not a bug; the projection resolves the path via its LAST recorded event, not
# via line uniqueness.
#
# Silent-failure contract: all errors are swallowed — this is a bookkeeping hook;
# it MUST NOT block or error-propagate to the tool call.
# ---------------------------------------------------------------------------
def _append(target_file: str, entry: str) -> None:
    """Unconditionally append entry to target_file (blocking).

    Caller MUST hold the per-file asyncio.Lock (D6) before dispatching this via
    asyncio.to_thread() — the lock serialises concurrent appends in-engine.

    ``entry`` is expected to already be a formatted event line
    (``scope.format_touch_event(...)``), not a bare path.
    """
    try:
        with open(target_file, "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except Exception:
        # Silent-failure: never raise from a bookkeeping hook.
        pass


# ---------------------------------------------------------------------------
# Cross-process locked append (blocking — call via to_thread WHILE holding
# the per-file asyncio.Lock).
#
# Wraps locked_rmw (flock-backed) alongside the per-file asyncio.Lock (D6) to add
# cross-process write serialisation. The asyncio.Lock (caller-held) prevents
# intra-process re-entrancy on the same target before the flock reaches the OS.
# Falls back to _append when locked_rmw is unavailable (non-POSIX, non-git
# working directory) so that tests and non-git environments degrade gracefully.
#
# This writer keeps the flock even after the dedup retirement — it is the only
# one of the three touched.txt writers with cross-process locking, and that
# remains the better side to be on; only the dedup logic goes, not safe-append.
# ---------------------------------------------------------------------------
def _append_locked(target_file: str, entry: str, repo_root_path: Path) -> None:
    """Unconditionally append entry to target_file via locked_rmw; fall back to
    _append on failure.

    Caller MUST hold the per-file asyncio.Lock (D6) before dispatching this via
    asyncio.to_thread() — the lock prevents intra-process re-entrancy on the same
    target before the flock reaches the OS.

    When locked_rmw succeeds (POSIX + valid git working directory), the read-modify-
    write is serialised across processes. When it raises (non-POSIX, git unavailable,
    LockTimeout), the fallback _append provides intra-process-only serialisation
    (same guarantee as the original path).

    Silent-failure contract: all errors in both paths are swallowed.
    """
    def mutate(old_text: str) -> str:
        if old_text and not old_text.endswith("\n"):
            return old_text + "\n" + entry + "\n"
        return old_text + entry + "\n"

    try:
        locked_rmw(Path(target_file), mutate, repo_root=repo_root_path, missing_ok=True)
    except (LockTimeout, MutateAbort):
        pass  # lock timeout or clean abort — silent-failure for bookkeeping
    except Exception:
        # locked_rmw unavailable (non-POSIX, non-git dir, RuntimeError from git_common_dir).
        # Fall back to in-process append. Cross-process protection absent on this path;
        # the asyncio.Lock held by the caller serialises concurrent invocations in-process.
        _append(target_file, entry)


# ---------------------------------------------------------------------------
# Per-file "touch if absent" helper (blocking — call via to_thread).
# ---------------------------------------------------------------------------
def _touch_if_absent(path: str) -> None:
    """Create path as an empty file if it does not already exist (blocking)."""
    try:
        Path(path).touch()
    except Exception:
        pass  # best-effort pre-create; the actual append below will surface any real write failure


@register_op("hooks.track_touched_files")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse bookkeeping op: append T-events for touched file paths into per-session records.

    Records file_path (normalized to repo-relative) into:
      - .git/coordinator-sessions/<session_id>/touched.txt  (session-keyed, always)
      - .git/coordinator-sessions/.agents/<agent_id>/touched.txt  (agent-keyed, subagent only)

    Defense-in-depth: exits early on non-edit tool names — the hooks.json matcher
    already restricts to Write|Edit|MultiEdit|NotebookEdit; the check here is a
    redundant fast-exit.

    NAMED LIMIT (DR-258, ratified permanent — not a gap awaiting a fix). Because the
    matcher is exactly those four tools, a path written **through Bash** — a generator,
    a formatter, ``python bin/*.py``, an engine op rewriting a state file — records NO
    claim here. ``compute_scope`` then sees a dirty file with no record anywhere and,
    per example-doctrine-repo's ``scoped-safety-commits.md:131``, joins it to the CALLING session: a
    co-toucher can take a live peer's Bash-authored content into ``my_scope``. This
    predates the claim-release workstream and is accepted on example-doctrine-repo's side too.

    Do NOT "fix" this by widening the matcher to Bash. Three mechanisms were tried and
    each is unsound in the WIDENING direction, which is the direction this record exists
    to prevent: shell parsing (SC-DR-001 — heredocs, xargs, subshell redirection), a
    PostToolUse mtime scan (cannot tell "my Bash did it" from "a peer wrote it during my
    Bash call" — an attribution race that falsely claims a peer's path), and a pre/post
    ``git status`` delta (same race, plus two git spawns on a hot path). Widening this
    matcher is a doctrine reversal in a repo claude-klabauter does not own; it needs a decision
    record and a memo to claude-central-em BEFORE any code, never after.

    All disk I/O is dispatched via asyncio.to_thread(). Per-file asyncio.Lock (D6)
    serialises concurrent append invocations on shared files in the singleton engine.
    """
    # asyncio deferred to first use here (not module scope). Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    session_id = field(params, "session_id")
    tool_name = field(params, "tool_name")
    file_path = field(params, "file_path")
    raw_agent_id = field(params, "agent_id")

    # --- Defense-in-depth: fast-exit on non-edit tools (mirrors sh:59-62) ---
    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return no_advisory()

    # --- Required fields: session_id and file_path must both be present ---
    if not session_id or not file_path:
        return no_advisory()

    _effective_root = repo_root
    git_root = str(_effective_root) if _effective_root else ""
    # C1d: route through git_common_dir so linked worktrees resolve to the main .git
    # directory (a real dir) rather than the worktree's .git FILE. Fallback to the
    # legacy path when git is unavailable (e.g. non-git test fixtures).
    _common_dir: Path | None = None
    try:
        _common_dir = git_common_dir(_effective_root) if _effective_root else None
        _sessions_base = _common_dir / "coordinator-sessions" if _common_dir else None
        if _sessions_base is None:
            return no_advisory()
    except RuntimeError:
        # git_root already IS git_common_dir; do not re-append ".git" here.
        _sessions_base = Path(git_root) / "coordinator-sessions"
    session_dir = str(_sessions_base / session_id)
    touched_file = os.path.join(session_dir, "touched.txt")
    meta_file = os.path.join(session_dir, "meta.json")

    # --- Session-dir + meta.json bootstrap (defect A, 2026-07-24) ---
    # DECOUPLED from a "dir absent" fast-path: another bookkeeping writer (the
    # push-failure cursor, session-shape.json, dispatched-agents) can create the
    # session dir before the first edit, which previously skipped bootstrap and
    # left meta.json — the Layer-1 stable_pid liveness signal — unwritten. Route
    # through the canonical core.init() writer whenever meta.json is missing OR
    # its stable_pid is unpopulated; on this in-process path the hook's parent is
    # `claude`, so Guard-1 stamps stable_pid with the live claude pid on the
    # first edit. The gate goes quiet once stable_pid lands (no per-edit git
    # subprocess cost in steady state). See _needs_session_init / _bootstrap_session.
    if await asyncio.to_thread(_needs_session_init, session_dir, meta_file):
        await asyncio.to_thread(
            _bootstrap_session, session_id, git_root, session_dir, touched_file
        )

    # --- Ensure touched.txt exists (belt-and-suspenders — mirrors sh:124) ---
    if not await asyncio.to_thread(os.path.exists, touched_file):
        await asyncio.to_thread(_touch_if_absent, touched_file)

    # --- Normalize file_path to repo-relative (mirrors sh:130-147) ---
    # normalize_touch_path's ``cwd`` MUST be the worktree root, NOT git_common_dir
    # (git_root here is the common dir — <repo>/.git for this hook's common_dir-
    # scoped resolution — and passing it directly makes every internal git
    # subprocess call cwd into a .git directory, which always fails). Derive the
    # worktree root via the canonical main_worktree_root(common_dir) helper.
    # NOTE: when the `except RuntimeError` branch above fired, `_common_dir` is
    # still None here, so this falls back to `git_root` for BOTH `_sessions_base`
    # and `_worktree_root` — the single-root collapse this plan otherwise fixes.
    # Documented, intentionally-inert: this op is `common_dir`-scoped (op_scopes.py),
    # so production always hands `_handler` an already-resolved common dir, and
    # `git_common_dir` on an already-common-dir cwd is idempotent — this branch
    # only fires for non-git test fixtures, where `git_root` already IS the
    # worktree root. Untested; see TestHandlerRuntimeErrorFallbackNonGitFixture.
    _worktree_root = str(main_worktree_root(_common_dir)) if _common_dir else git_root
    file_path_norm = await asyncio.to_thread(
        normalize_touch_path, file_path, _worktree_root
    )
    if not file_path_norm:
        return no_advisory()

    # --- Session-keyed append (D6: asyncio.Lock + locked_rmw cross-process layer) ---
    _repo_root_path = Path(str(_effective_root))
    session_lock = _get_lock(touched_file)
    async with session_lock:
        await asyncio.to_thread(
            _append_locked, touched_file, format_touch_event("T", file_path_norm), _repo_root_path
        )

    # --- Agent-keyed append (only for subagent fires — mirrors sh:200-223) ---
    # Issue A + C10: resolve raw agent_id to the canonical EM-side id, then write
    # .agents/<canonical-id>/touched.txt — what coordinator-safe-commit unions into
    # commit scope via cs_compute_scope. Empty resolver result → skip (zero-overhead
    # path for top-level EM writes that carry no agent_id).
    if raw_agent_id:
        canonical_agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
        if canonical_agent_id:
            agent_dir = str(_sessions_base / ".agents" / canonical_agent_id)
            agent_touched = os.path.join(agent_dir, "touched.txt")

            # Ensure agent dir exists (mirrors sh:220)
            if not await asyncio.to_thread(os.path.isdir, agent_dir):
                await asyncio.to_thread(
                    lambda: os.makedirs(agent_dir, exist_ok=True)
                )
            if not await asyncio.to_thread(os.path.exists, agent_touched):
                await asyncio.to_thread(_touch_if_absent, agent_touched)

            # Imported function-local: the module-level import sweep in
            # coordinator_core.hooks reaches both modules, and a top-level edge
            # here would order-depend on that sweep.
            from coordinator_core.hooks.track_dispatched_agents import (
                _write_backpointer_sync,
            )

            # Piece 2 — Workflow-internal agent-spawn attribution (2026-08-03,
            # docs/plans/2026-08-03-scope-guard-peer-claim-release.md § C7).
            #
            # A Workflow-internal `agent()` spawn never fires the Agent-tool-matched
            # track_dispatched_agents hook, so the fallback below (which attributes
            # ownership to `session_id`, the firing session) is the only writer that
            # ever runs for it — and `session_id` there is the SUBAGENT's own distinct
            # id, not the dispatching EM's (see the branch (b) rationale below), so a
            # Workflow-internal spawn's agent dir still ends up ownerless in practice.
            #
            # CLAUDE_CODE_SESSION_ID is inherited by this hook's own process from its
            # Workflow-internal parent — probe-confirmed (Workflow run
            # `wf_b7ef5d89-7ca`, single `env` read) to equal the DISPATCHING EM's
            # session id, byte-identical, for the Workflow-internal spawn shape (not
            # merely the Agent-tool shape it was previously documented for). Advisory
            # attribution ONLY — every other consumer deliberately distrusts
            # CLAUDE_CODE_SESSION_ID as a subagent-vs-EM discriminator (see
            # `nudge_unrouted_sizing._is_subagent_session` and
            # `runtime-tripwire-em-check.py`'s docstring); attribution-when-absent is
            # the one thing it is good for here.
            #
            # Fails closed on all four fronts — this arm can WIDEN `my_scope`:
            #   - env unset/empty -> skip (nothing to attribute).
            #   - env == session_id -> that IS the firing session (this hook's own
            #     session_id param), not a distinct dispatching parent; writing it
            #     would misattribute the firing session's own work to itself.
            #   - existing non-empty back-pointer -> never overwritten; the writer
            #     below is the idempotent (non-empty-file-wins) shared helper, so a
            #     real dispatch-time record always wins over this advisory write.
            #   - OSError -> swallowed inside `_write_backpointer_sync` itself; this
            #     call never raises, matching this hook's fail-open contract.
            _em_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            if _em_session_id and _em_session_id != session_id:
                await asyncio.to_thread(
                    _write_backpointer_sync,
                    os.path.join(agent_dir, "em-session-id.txt"),
                    _em_session_id,
                )

            # Ownership back-pointer parity with track_dispatched_agents
            # (2026-08-03 break-class fix). This hook and that one create the
            # SAME .agents/<id>/ directory from opposite sides, but only that
            # one wrote em-session-id.txt — so an agent dir born here, from a
            # subagent's first Write, carried touches with no recorded owner.
            # `cs_compute_scope` withholds every path such a dir claims from
            # ALL sessions for 30 minutes (coordinator_core/session/scope.py,
            # the em-session-id.txt-missing branch), so an EM could be blocked
            # from committing its own work by its own subagent. Live repro:
            # session f2a9e7b3, `.agents/a29c17237ceda22b1` — 343 of 1353 agent
            # dirs on this repo had touches and no owner, and NONE of them
            # appeared in any dispatched-agents.txt.
            #
            # `session_id` is provably the right owner, not a guess: branch (b)
            # of `_resolve_subagent_identity` builds the canonical dir name as
            # f"{name}@session-{session_id[:8]}", which is exactly what the
            # existing back-pointers in those dirs contain. The unnamed-agent
            # fast path (branch (a)) has the same value in hand and only
            # discards it. Reuses track_dispatched_agents' writer rather than
            # forking a second copy — it is idempotent (non-empty file wins,
            # so a real dispatch record is never overwritten) and atomic
            # (temp+rename). Runs AFTER the Piece 2 write above so a genuine
            # Workflow-internal attribution wins first; this remains the
            # fallback for the shapes Piece 2's env guard intentionally skips
            # (env unset — older harness contexts, non-Workflow test fixtures).
            await asyncio.to_thread(
                _write_backpointer_sync,
                os.path.join(agent_dir, "em-session-id.txt"),
                session_id,
            )

            # D6: asyncio.Lock + locked_rmw cross-process layer — separate lock from session lock
            agent_lock = _get_lock(agent_touched)
            async with agent_lock:
                await asyncio.to_thread(
                    _append_locked, agent_touched, format_touch_event("T", file_path_norm), _repo_root_path
                )

    # Note: meta.json last_activity is NOT updated here (costs ~36ms on Windows;
    # matches sh:225-226). Activity is updated by cs_touch at commit time.
    return no_advisory()
