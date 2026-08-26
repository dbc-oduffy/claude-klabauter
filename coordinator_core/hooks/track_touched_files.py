"""
coordinator_core.hooks.track_touched_files — PostToolUse bookkeeping hook op.

Purpose: Records the file path modified by the current Edit/Write/MultiEdit/NotebookEdit
tool call into two append-only T-event logs:
  - per-session:  .git/coordinator-sessions/<session_id>/touch-record.jsonl
  - per-agent:    .git/coordinator-sessions/.agents/<agent_id>/touch-record.jsonl
    (agent-keyed write fires only for subagent tool calls — agent_id present and
    resolving to a known agent shape.)

Port of the retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
track-touched-files.sh (deleted 2026-07-22, DoE ``3a561713``).

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
(1) an advisory write attributing a Workflow-internal agent() spawn to its
dispatching EM (Piece 2, docs/plans/2026-08-03-scope-guard-peer-claim-release.md
§ C7; skipped unless the resolved id is non-empty AND differs from `session_id`,
so it never misattributes the firing session's own work to itself), then (2) the
pre-existing `session_id` fallback (2026-08-03 break-class fix). Both reuse
track_dispatched_agents._write_backpointer_sync, so a real dispatch-time record
always wins (idempotent, non-empty-file-wins).

That first writer resolves the dispatching EM through
`session.core.resolve_session_id`, NOT a direct env read. This op
is registered, so it can be served by a resident warm engine whose own
environment names the session that spawned the server; reading the env there
yields a stranger, which passes the `!= session_id` test and gets written as the
owner. See coordinator_core/tests/test_warm_identity_env_reads.py, which pins the
absence of that read.

Negative-spec:
    Do NOT emit advisories — this op's value is the on-disk write side-effect.
    Do NOT write state/, archive/, or any path outside .git/coordinator-sessions/.
    Do NOT trust the resolved session id as a subagent-vs-EM discriminator anywhere
    else — it is used here for attribution-when-absent ONLY, gated by the
    != session_id guard above.
    Do NOT resolve that id by reading os.environ directly (see the back-pointer
    note above) — this op is warm-servable and the server's env names its spawner.

C7 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-its-
writers.md): this writer emits ``T``-verb events via ``touch_record.append_event``
(``session/touch_record.py::encode_line``) into the same self-describing
``touch-record.jsonl`` sink ``session/scope.py::touch`` (C4) and self_claim's
``atomic_dedup_append`` (C6) already write, so the one read seam
(``touch_record.project_live_claims``) resolves every path via one dialect across
all three writers, never a mixed record.

Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § C1
Spec backlink: pln-release-a-peer-session-s-path--d04deb § C7
Spec backlink: pln-track-touched-files-emits-t-ev-0befc7 § C1
Spec backlink: pln-the-legacy-touched-txt-record-44ce48 § C7
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

from coordinator_core.ipc import register_op

# Generator-provenance declaration (generator_provenance.py). Per this
# module's own "Write confinement (hard)" clause above: writes ONLY under
# .git/coordinator-sessions/ (touch-record.jsonl session/agent logs) -- never
# state/, archive/, or any tracked repo path.
GENERATES = []
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.session import touch_record
from coordinator_core.session.scope import normalize_touch_path

# C7 (docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-
# its-writers.md): the SAME filename session/scope.py's `touch()` (C4) and
# `self_claim` (C6) already write, so every writer of a session- or
# agent-keyed claim lands in ONE dialect, ONE file, per sink --
# `touch_record.project_live_claims` is the one seam that reads all three.
# Mirrors `session/scope.py`'s own (module-private) `_TOUCH_RECORD_FILENAME`
# literal; not re-imported because that name is private to that module and
# this hook owns its own copy of the literal it must match.
_TOUCH_RECORD_FILENAME = "touch-record.jsonl"

# ---------------------------------------------------------------------------
# D6 — per-target-file asyncio.Lock registry.
#
# The engine is a per-repo singleton shared by concurrent sessions. Two sessions
# returning simultaneously can invoke this op concurrently — both dispatch
# asyncio.to_thread() tasks that touch the same touch-record.jsonl sink. Process-
# isolation (which serialises the source bash hook's concurrent O_APPEND writes) is
# absent in-engine. An asyncio.Lock per target file serialises concurrent
# in-engine invocations targeting the same sink (C7: no longer a read-check-then-
# append cycle — touch_record.append_event's single atomic append needs no
# application-level lock of its own; this lock predates that flip and is kept
# unchanged — see the block comment above `_append_touch_record`).
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
#
# HELD-AWARE (docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md
# § C9). The prior eviction policy (FIFO pop, no `.locked()` check) could evict a lock
# a peer dispatch currently holds (`async with lock:` in progress in `_handler`). After
# eviction, `_get_lock` creates a FRESH `asyncio.Lock()` for the same path on the next
# call, so the held peer and the new caller serialise on TWO DIFFERENT lock objects for
# the SAME path — i.e. they do not serialise at all, defeating D6's entire purpose. This
# is a policy redesign, not a trigger tweak: both the stale sweep and the hard-cap
# eviction below now consult `lock.locked()` and NEVER remove an entry whose lock is
# currently held, regardless of table size. If every entry is held when the cap is
# reached, growing past `_MAX_FILE_LOCKS` is the correct behaviour — not evicting a held
# lock.
_MAX_FILE_LOCKS = 256


def _get_lock(path: str) -> "asyncio.Lock":
    """Return (creating if absent) the per-file asyncio.Lock for path.

    On new-path creation, evicts stale UNHELD entries (parent dir gone — session
    archived) to bound _FILE_LOCKS to O(active sessions × agents). Falls back to
    oldest-entry eviction among UNHELD entries if the stale sweep alone isn't
    sufficient (safety cap: _MAX_FILE_LOCKS). A currently-HELD lock (``lock.locked()``
    True — some concurrent caller is inside its ``async with`` block) is NEVER evicted,
    by either tier, regardless of table size: evicting a held lock would let two
    dispatches serialise on different lock objects for the same path, silently
    defeating D6's cross-dispatch serialisation. If the cap is reached and every
    entry is held, the table grows past `_MAX_FILE_LOCKS` — that is correct behaviour,
    not a bug.

    Accessed only from the event loop — no threading synchronisation needed on _FILE_LOCKS
    itself (all callers are async coroutines running in the event loop thread).
    """
    # asyncio deferred to first use here (not module scope). Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    if path not in _FILE_LOCKS:
        # Evict UNHELD entries for paths whose containing dir is gone (session
        # archived). A held lock is never a stale-sweep candidate even if its
        # session dir happens to be gone — that combination cannot happen for a
        # live caller, but the check is unconditional defense-in-depth.
        if len(_FILE_LOCKS) >= _MAX_FILE_LOCKS:
            stale = [
                p for p, lock in _FILE_LOCKS.items()
                if not lock.locked() and not os.path.isdir(os.path.dirname(p))
            ]
            for p in stale:
                del _FILE_LOCKS[p]
        # Hard cap: evict oldest insertion-order UNHELD entries one at a time until
        # under cap, or until no unheld entry remains (every entry held → stop and
        # let the table grow past the cap rather than evict a held lock).
        while len(_FILE_LOCKS) >= _MAX_FILE_LOCKS:
            evict_path = next(
                (p for p, lock in _FILE_LOCKS.items() if not lock.locked()), None
            )
            if evict_path is None:
                break
            del _FILE_LOCKS[evict_path]
        _FILE_LOCKS[path] = asyncio.Lock()
    return _FILE_LOCKS[path]


# ---------------------------------------------------------------------------
# Agent-id resolution — Port of: coordinator-session.sh::resolve_subagent_identity
# (DoE e34f2484, 2026-07-22)
#
# Three resolution paths (including the C10 named-teammate
# extension; docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C10):
#   (a) Bare hex  ^[a-f0-9]{12,}$  — unnamed agent; return unchanged.
#   (b) Named teammate  ^a(.+)-[a-f0-9]{16}$  — extract name, build canonical id
#       via cs_build_canonical_agent_id equivalent: "<name>@session-<short>".
#   (c) Unrecognised shape — return "" (fail-closed; agent-keyed write skipped).
# ---------------------------------------------------------------------------
#: Already-canonical teammate shape, matching _subagent_identity's
#: _TEAMMATE_CANONICAL_RE. A subagent-context PostToolUse fire can carry the
#: agent_id in this form too (not just the raw a<name>-16hex shape below) —
#: see the (d) branch's docstring note.
_TEAMMATE_CANONICAL_RE = re.compile(r"^[A-Za-z0-9_.-]+@session-[a-z0-9-]+$")


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

    # (d) Already-canonical <name>@session-<short> — rebuild against the LIVE
    # session_id rather than trusting the embedded short verbatim. The harness
    # stamps that short once at team creation and never refreshes it across
    # /clear, resume, compact, or fork, so a verbatim id here would key a
    # DIFFERENT .agents/<id>/ directory than the one branch (b) above (and
    # track_dispatched_agents, once normalized) uses for the same teammate.
    # See coordinator_core.write_guards._subagent_identity.
    # normalize_teammate_agent_id for the full mechanism.
    if _TEAMMATE_CANONICAL_RE.match(agent_id):
        from coordinator_core.write_guards._subagent_identity import (
            normalize_teammate_agent_id,
        )

        return normalize_teammate_agent_id(agent_id, session_id)

    # (c) Unrecognised shape — fail-closed.
    return ""


# ---------------------------------------------------------------------------
# C7 (the writer flip, part two): both appends below now route through
# ``touch_record.append_event`` -> ``atomic_append.append_line`` -- the same
# single-write-syscall primitive session/scope.py::touch (C4) and self_claim
# (C6) already use. No read-modify-write, no `locked_rmw`: that primitive's
# own negative-spec (touch_record.py module docstring) forbids `locked_rmw`
# on this append path -- the serialisation it bought was never cross-session,
# only within one session's own file, and atomic_append already gives O(1)
# cross-process safety without it (POSIX real O_APPEND kernel atomicity;
# Windows FILE_APPEND_DATA via CreateFileW -- see atomic_append.py's own
# negative-spec for the live-reproduced data-loss bug that backs this). The
# per-file asyncio.Lock (D6, ``_get_lock``/``_FILE_LOCKS`` above) is KEPT
# unchanged around each call: it still serialises concurrent in-engine
# invocations targeting the same sink, and removing it is out of this
# chunk's scope (external coverage in
# coordinator_core/tests/test_hooks_bookkeeping.py drives it directly).
# ---------------------------------------------------------------------------


def _append_touch_record(
    sink: str, session_id: str, agent_id: "str | None", path: str
) -> None:
    """Encode and append one ``T`` event to ``sink`` (blocking) via
    ``touch_record.append_event``.

    Caller MUST hold the per-file asyncio.Lock (D6) before dispatching this
    via ``asyncio.to_thread()`` — the lock still serialises concurrent
    in-engine invocations targeting the same sink (see the block comment
    above).

    Silent-failure contract, as everywhere else in this module:
    ``touch_record.LineTooLong`` (an absurdly long path) and
    ``touch_record.OutOfWorktreePath`` (AC3 — ``encode_line``'s own
    spawn-free containment check, reachable here now that
    ``normalize_touch_path`` is no longer the only thing holding the
    invariant) are both swallowed; a bookkeeping hook must never block or
    error-propagate to the tool call.
    """
    try:
        touch_record.append_event(
            sink,
            session_id=session_id,
            agent_id=agent_id,
            verb=touch_record.VERB_TOUCH,
            path=path,
        )
    except (touch_record.LineTooLong, touch_record.OutOfWorktreePath):
        pass
    except Exception:
        # Silent-failure: never raise from a bookkeeping hook.
        pass


def _ensure_session_record_sync(
    session_dir: str, session_id: str, sessions_base: str, worktree_root: str
) -> None:
    """Create the session directory and, ONCE per session, its ``meta.json``
    liveness record — a precondition ``_append_touch_record`` no longer
    depends on for itself (``touch_record.append_event`` creates its own
    sink's parent directory — see the block comment above), plus the
    registry entry that makes the claim this record is about visible to
    peers.

    Two jobs in one thread hop on purpose (C9's hop budget): the ``makedirs``
    is still this hook's ONLY mkdir, kept for cheap belt-and-suspenders
    defense even though C7 made it provably redundant on both halves of this
    call — ``session.core.init`` (called below when ``meta.json`` is absent)
    does its own ``sdir.mkdir(parents=True, exist_ok=True)``, and
    ``touch_record.append_event`` (the append half, back in ``_handler``)
    creates its own sink's parent directory on every call. See
    ``tests/test_track_touched_files_fresh_dir.py`` for the isolated proof
    of both self-creations; that module no longer treats this ``makedirs``
    as a guarded precondition. The ``isfile`` below is the whole
    steady-state cost of the record half — one stat per Edit/Write, one
    ``init`` per session lifetime.

    WHY THIS IS NOT THE BOOTSTRAP C1 REMOVED. C1 stripped session bootstrap
    from this hook on the premise that liveness stamping "belongs at the
    claiming ceremony ... which already performs the identical ``ensure_meta``
    write". That premise holds for every claim a *ceremony* makes and fails for
    the claim an *edit* makes: appending a ``T`` event IS claim acquisition,
    and a session that only ever edits through Write/Edit runs no ceremony and
    no CLI, so it holds claims while absent from the registry
    (``session/liveness.py`` keys liveness on ``meta.json``; with none,
    ``bash_guards/dispatch_checks.py::_rm_peer_claim_of`` cannot see the holder
    at all and degrades to its 30-minute mtime backstop). Sibling writer
    ``session/scope.py::touch`` already carries the identical absence-guarded
    fail-safe (defect A, 2026-07-24); this is that fail-safe on the other
    writer of the same record, not a reinstated ``_bootstrap_session``.

    Cost, measured on this box (2026-08-26, ``time.process_time``, psutil
    resident so Guard-1 really ran). **ZERO spawns in BOTH the resolving and
    the pre-resolved shape** — that is the number that matters, because it is
    what keeps C1's guard green on its own terms rather than by exemption, and
    it is the first thing a reader re-deriving this will want. Latency, for
    completeness: 0.39ms per resolving ``init`` (k=40), the pre-resolved call
    below under this box's 15.625ms tick at k=30 (independently re-measured by
    claude-klabauter-c2); through ``_handler``, 5.73ms first fire against 2.60ms
    steady state (k=30). Paid once per session lifetime.

    The ~36ms figure this hook's own closing note and C1 both argued from
    priced a per-call ``last_activity`` read-modify-write, i.e. a refresh
    cadence, which this deliberately is NOT. A "~41ms / 3 spawns" figure for
    ``init`` circulated briefly in the originating bug report; it was a cold
    fresh-process measurement restated as a property of the function, and its
    author has retracted it. Do not re-cite it.

    Negative-spec:
        - Guard on ABSENCE of ``meta.json``, never on staleness. This is
          record CREATION; it must never become a per-tool-call heartbeat —
          that is the distinction ``session/scope.py::touch``'s docstring
          pins, and DoE's ``session-heartbeat.py`` was retired over.
        - Do NOT route this through ``core.ensure_meta``: its re-stamp arm
          reads and parses ``meta.json`` on the PRESENT path, i.e. on every
          Edit/Write, which is the per-call cost this hook must not pay.
        - Write confinement: the hub and worktree root are handed to
          ``core.init`` PRE-RESOLVED, from the same ``_common_dir`` this
          handler already resolved ``session_dir`` from — ``init`` never
          re-derives either, so it cannot land the record in a tree this call
          did not resolve, and this handler stays at ZERO ``core.git_root``
          calls (``tests/test_track_touched_files_normalize.py``
          ``TestHandlerZeroSpawnFastArmAtCaller``). That guard is correct and
          must not be exempted; the seam on ``init`` is what keeps it green.
        - Silent-failure contract, as everywhere else in this module. The
          detection surface for a record that never appeared already exists:
          ``session/stable_pid_watch.py`` (widened off the single
          ``touched.txt`` literal, C5) counts a T-record-carrying dir with
          no ``meta.json`` as a ``no_meta_json`` miss.
    """
    os.makedirs(session_dir, exist_ok=True)
    if os.path.isfile(os.path.join(session_dir, "meta.json")):
        return
    try:
        # Imported directly from session.core (never through the ops package)
        # for the same import-graph reason the back-pointer resolver below is —
        # see tests/test_track_touched_files_no_ops_import.py.
        from coordinator_core.session.core import init as _session_init

        _session_init(session_id, sessions_base=sessions_base, root=worktree_root)
    except Exception:
        pass  # silent-failure contract; stable_pid_watch counts the miss


@register_op("hooks.track_touched_files")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse bookkeeping op: append T-events for touched file paths into per-session records.

    Records file_path (normalized to repo-relative) into:
      - .git/coordinator-sessions/<session_id>/touch-record.jsonl  (session-keyed, always)
      - .git/coordinator-sessions/.agents/<agent_id>/touch-record.jsonl  (agent-keyed, subagent only)

    Defense-in-depth: exits early on non-edit tool names — the hooks.json matcher
    already restricts to Write|Edit|MultiEdit|NotebookEdit; the check here is a
    redundant fast-exit.

    NAMED LIMIT (DR-258, ratified permanent — not a gap awaiting a fix). Because the
    matcher is exactly those four tools, a path written **through Bash** — a generator,
    a formatter, ``python bin/*.py``, an engine op rewriting a state file — records NO
    claim here. ``compute_scope`` then sees a dirty file with no record anywhere and treats it
    as an **mtime-only candidate**: Step 4(d) drops it from ``my_scope`` entirely and
    Step 5 reports it as an ORPHAN (``coordinator_core/session/scope.py ::
    compute_scope``). It is EXCLUDED, never joined.

    **CORRECTED 2026-08-19 -- this paragraph previously said the opposite**, citing
    DoE's ``scoped-safety-commits.md:131`` for a claim that such a file "joins it to
    the CALLING session: a co-toucher can take a live peer's Bash-authored content
    into ``my_scope``". That was true once and is not now. The same DoE doc section
    records the reversal: twelve SIGKILL runs measured that the population reaching
    this path is healthy live peers' Bash-mediated writes, not crashed peers, so the
    resolution moved to an orphans bucket -- "an orphan is visible and recoverable, a
    misattributed commit is silent and corrupts Session-Id-trailer-derived
    coverage/chain-ancestry accounting". ``ops/session/safe_commit_offer.py`` already
    cites DR-258 with the orphan framing; only this docstring lagged.

    The live consequence runs the OTHER way, and is why the limit below still matters:
    a Bash-written file is not stolen from a peer, it is silently **dropped from your
    own** commit. Guard: ``bash_guards`` ``heredoc-repo-write-advise`` advises at
    write time.

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
    touch_record_sink = os.path.join(session_dir, _TOUCH_RECORD_FILENAME)

    # --- Session dir + liveness record for the append below ---
    # C7: _ensure_session_record_sync's own ``makedirs`` is this hook's ONLY
    # mkdir, kept as cheap belt-and-suspenders defense, but is no longer a
    # guarded precondition for either half of this call --
    # ``session.core.init`` (meta.json half) and ``touch_record.append_event``
    # (append half, below) both self-create their own target's parent
    # directory now. See that function's own docstring and
    # ``tests/test_track_touched_files_fresh_dir.py`` for the isolated proof.
    #
    # _worktree_root is resolved HERE rather than at its former site below
    # (immediately before normalize_touch_path) because the record half needs
    # it too -- core.init resolves the session hub from a worktree root, and a
    # second resolution would be the same answer computed twice. Its
    # normalize_touch_path contract is unchanged; see the note at that call.
    _worktree_root = str(main_worktree_root(_common_dir)) if _common_dir else git_root
    await asyncio.to_thread(
        _ensure_session_record_sync,
        session_dir,
        session_id,
        str(_sessions_base),
        _worktree_root,
    )

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
    file_path_norm = await asyncio.to_thread(
        normalize_touch_path, file_path, _worktree_root, root=_worktree_root
    )
    if not file_path_norm:
        return no_advisory()

    # --- Session-keyed append (D6: asyncio.Lock; C7: touch_record.append_event,
    # no locked_rmw -- see the block comment above _ensure_session_record_sync
    # for why the cross-process serialisation moved to atomic_append itself) ---
    session_lock = _get_lock(touch_record_sink)
    async with session_lock:
        await asyncio.to_thread(
            _append_touch_record,
            touch_record_sink,
            session_id,
            None,
            file_path_norm,
        )

    # --- Agent-keyed append (only for subagent fires — mirrors sh:200-223) ---
    # Issue A + C10: resolve raw agent_id to the canonical EM-side id, then write
    # .agents/<canonical-id>/touch-record.jsonl — what coordinator-safe-commit
    # unions into commit scope via cs_compute_scope (through
    # touch_record.project_live_claims, C7). Empty resolver result → skip
    # (zero-overhead path for top-level EM writes that carry no agent_id).
    if raw_agent_id:
        canonical_agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
        if canonical_agent_id:
            agent_dir = str(_sessions_base / ".agents" / canonical_agent_id)
            agent_touch_record_sink = os.path.join(agent_dir, _TOUCH_RECORD_FILENAME)

            # Ensure agent dir exists (mirrors sh:220)
            await asyncio.to_thread(
                lambda: os.makedirs(agent_dir, exist_ok=True)
            )

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
            #   - warm-served dispatch -> the canonical resolver, never a raw env
            #     read. This handler is a REGISTERED op (`hooks.track_touched_files`),
            #     so it can execute inside a resident warm server whose own
            #     environment names whoever SPAWNED that server rather than the
            #     session on whose behalf it is serving. A raw env read there yields
            #     a STRANGER's id, which fails the `!= session_id` test above and so
            #     gets WRITTEN as this agent dir's owner back-pointer -- the one
            #     outcome this arm's fail-closed conditions exist to prevent.
            #     `resolve_current_session_id` reads the per-request identity binding
            #     first and lands on exactly the value this site wants: the id the
            #     hook's own (cold) process resolved before the call crossed the wire.
            #     Deliberate widening: the resolver's ladder also consults
            #     `COORDINATOR_SESSION_ID`/`CLAUDE_SESSION_ID` ahead of
            #     `CLAUDE_CODE_SESSION_ID`. Accepted rather than special-cased -- this
            #     write is advisory and idempotent (a real dispatch-time record always
            #     wins), and an identity ladder that disagrees with the canonical one
            #     is the defect class this whole seam is being swept for.
            # Function-local: this hooks module is eagerly imported by the
            # hooks package sweep. Imported directly from session.core rather
            # than through ops.session_context (2026-08-22, § C2) so this
            # hook's cost no longer depends on whether the invoking process
            # armed lazy ops — ops.session_context is a thin delegate to the
            # same core.resolve_session_id (KS-6, 2026-08-07), nothing lost.
            from coordinator_core.session.core import resolve_session_id

            # resolve_session_id returns "" (not None) when no tier resolves —
            # the `or ""` below is now a no-op for that path, kept because the
            # call site's shape (truthy-check + fallback) is unchanged.
            _em_session_id = resolve_session_id() or ""
            _piece2_fired = bool(_em_session_id and _em_session_id != session_id)
            if _piece2_fired:
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
            if not _piece2_fired:
                await asyncio.to_thread(
                    _write_backpointer_sync,
                    os.path.join(agent_dir, "em-session-id.txt"),
                    session_id,
                )

            # D6: asyncio.Lock (C7: touch_record.append_event, no locked_rmw)
            # — separate lock from the session lock
            agent_lock = _get_lock(agent_touch_record_sink)
            async with agent_lock:
                await asyncio.to_thread(
                    _append_touch_record,
                    agent_touch_record_sink,
                    session_id,
                    canonical_agent_id,
                    file_path_norm,
                )

    # Note: meta.json last_activity is NOT updated here (costs ~36ms on Windows;
    # matches sh:225-226). Activity is updated by cs_touch at commit time.
    # The record's CREATION is a different question and IS this hook's job --
    # see _ensure_session_record_sync above. Creation once per session, on
    # absence; refresh never. Do not read this note as an argument against the
    # former: it prices a per-call read-modify-write, not a one-time create.
    return no_advisory()
