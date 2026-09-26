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

from coordinator_core._hook_envelope import payload_of
from coordinator_core.ipc import register_op

GENERATES = []
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.session import touch_record
from coordinator_core.session.scope import normalize_touch_path

# Mirrors `session/scope.py`'s own (module-private) `_TOUCH_RECORD_FILENAME`
_TOUCH_RECORD_FILENAME = "touch-record.jsonl"

# isolation (which serialises the source bash hook's concurrent O_APPEND writes) is
_FILE_LOCKS: dict[str, asyncio.Lock] = {}

# Bound _FILE_LOCKS growth. The engine may run for a full
# HELD-AWARE (docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md
# call, so the held peer and the new caller serialise on TWO DIFFERENT lock objects for
# reached, growing past `_MAX_FILE_LOCKS` is the correct behaviour — not evicting a held
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
    import asyncio

    if path not in _FILE_LOCKS:
        if len(_FILE_LOCKS) >= _MAX_FILE_LOCKS:
            stale = [
                p for p, lock in _FILE_LOCKS.items()
                if not lock.locked() and not os.path.isdir(os.path.dirname(p))
            ]
            for p in stale:
                del _FILE_LOCKS[p]
        while len(_FILE_LOCKS) >= _MAX_FILE_LOCKS:
            evict_path = next(
                (p for p, lock in _FILE_LOCKS.items() if not lock.locked()), None
            )
            if evict_path is None:
                break
            del _FILE_LOCKS[evict_path]
        _FILE_LOCKS[path] = asyncio.Lock()
    return _FILE_LOCKS[path]


#: _TEAMMATE_CANONICAL_RE. A subagent-context PostToolUse fire can carry the
_TEAMMATE_CANONICAL_RE = re.compile(r"^[A-Za-z0-9_.-]+@session-[a-z0-9-]+$")


def _resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    if re.match(r"^[a-f0-9]{12,}$", agent_id):
        return agent_id

    m = re.match(r"^a(.+)-[a-f0-9]{16}$", agent_id)
    if m:
        name = m.group(1)
        if name and len(session_id) >= 8:
            short = session_id[:8]
            return f"{name}@session-{short}"
        return ""

    # DIFFERENT .agents/<id>/ directory than the one branch (b) above (and
    if _TEAMMATE_CANONICAL_RE.match(agent_id):
        from coordinator_core.write_guards._subagent_identity import (
            normalize_teammate_agent_id,
        )

        return normalize_teammate_agent_id(agent_id, session_id)

    return ""


# cross-process safety without it (POSIX real O_APPEND kernel atomicity;
# Windows FILE_APPEND_DATA via CreateFileW -- see atomic_append.py's own
# per-file asyncio.Lock (D6, ``_get_lock``/``_FILE_LOCKS`` above) is KEPT


def _append_touch_record(
    sink: str,
    session_id: str,
    agent_id: "str | None",
    path: str,
    content_hash: "str | None" = None,
) -> None:
    try:
        touch_record.append_event(
            sink,
            session_id=session_id,
            agent_id=agent_id,
            verb=touch_record.VERB_TOUCH,
            path=path,
            content_hash=content_hash,
            # KIND_WRITE unconditionally: this hook is PostToolUse on
            kind=touch_record.KIND_WRITE,
        )
    except (touch_record.LineTooLong, touch_record.OutOfWorktreePath):
        pass
    except Exception:
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

    ONE call, to ``core.ensure_session`` — the session directory's single
    constructor, which produces the directory and the record together or
    neither. This function used to open with its own
    ``os.makedirs(session_dir, exist_ok=True)`` ahead of an
    absence-guarded ``core.init``, described as "belt-and-suspenders" and
    provably redundant on both halves. That mkdir is deleted: belt-and-
    suspenders is exactly how a session directory came to exist without a
    record, because a mkdir that succeeds while the record write is skipped
    or fails is precisely the litter this constructor exists to prevent. The
    ``session_dir`` parameter is retained as the resolved path this hook
    already computed (its ``touch_record`` sink's parent) and for the
    existing call shape; ``ensure_session`` re-derives nothing, since the hub
    and worktree root are handed over pre-resolved below.

    Steady-state cost of the record half: **+0.018ms** per Edit/Write against
    the ``makedirs``+``isfile`` pair it replaces (RUSAGE process time, n=2000,
    2026-08-26), and NO ``mkdir`` syscall at all once the record exists — one
    ``init`` per session lifetime.

    WHY THIS IS NOT THE BOOTSTRAP C1 REMOVED. C1 stripped session bootstrap
    from this hook on the premise that liveness stamping "belongs at the
    claiming ceremony ... which already performs the identical ``ensure_meta``
    write" (that function was deleted 2026-08-26; ``ensure_session`` is the
    constructor now, and the ceremony still performs the write). That premise holds for every claim a *ceremony* makes and fails for
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
        - Routed through ``core.ensure_session`` (the session directory's one
          constructor) as of 2026-08-26. This bullet used to forbid the
          predecessor ``core.ensure_meta`` on the grounds that its re-stamp arm
          reads and parses ``meta.json`` on the PRESENT path, i.e. on every
          Edit/Write. Re-measured on this box (RUSAGE process time, n=2000):
          that present-path read costs **+0.018ms** against the
          ``makedirs``+``isfile`` pair it replaces, and ``ensure_session``
          issues NO ``mkdir`` syscall at all once the record exists. The
          objection was real and is retired by the number, not waived — and the
          hook stops being one of the lazy initializers whose race decided
          which sessions got a record at all.
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
    try:
        from coordinator_core.session.core import ensure_session as _ensure_session

        _ensure_session(session_id, sessions_base=sessions_base, root=worktree_root)
    except Exception:
        pass


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
    record and a memo to DoE-claude BEFORE any code, never after.

    All disk I/O is dispatched via asyncio.to_thread(). Per-file asyncio.Lock (D6)
    serialises concurrent append invocations on shared files in the singleton engine.
    """
    params = payload_of(params)
    import asyncio

    session_id = field(params, "session_id")
    tool_name = field(params, "tool_name")
    file_path = field(params, "file_path")
    raw_agent_id = field(params, "agent_id")

    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return no_advisory()

    if not session_id or not file_path:
        return no_advisory()

    _effective_root = repo_root
    git_root = str(_effective_root) if _effective_root else ""
    _common_dir: Path | None = None
    try:
        _common_dir = git_common_dir(_effective_root) if _effective_root else None
        _sessions_base = _common_dir / "coordinator-sessions" if _common_dir else None
        if _sessions_base is None:
            return no_advisory()
    except RuntimeError:
        _sessions_base = Path(git_root) / "coordinator-sessions"
    session_dir = str(_sessions_base / session_id)
    touch_record_sink = os.path.join(session_dir, _TOUCH_RECORD_FILENAME)

    _worktree_root = str(main_worktree_root(_common_dir)) if _common_dir else git_root
    await asyncio.to_thread(
        _ensure_session_record_sync,
        session_dir,
        session_id,
        str(_sessions_base),
        _worktree_root,
    )

    file_path_norm = await asyncio.to_thread(
        normalize_touch_path, file_path, _worktree_root, root=_worktree_root
    )
    if not file_path_norm:
        return no_advisory()

    # against the ABSOLUTE path (worktree root + repo-relative norm form):
    _content_hash = await asyncio.to_thread(
        touch_record.compute_content_hash,
        os.path.join(_worktree_root, file_path_norm),
    )

    session_lock = _get_lock(touch_record_sink)
    async with session_lock:
        await asyncio.to_thread(
            _append_touch_record,
            touch_record_sink,
            session_id,
            None,
            file_path_norm,
            _content_hash,
        )

    if raw_agent_id:
        canonical_agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
        if canonical_agent_id:
            agent_dir = str(_sessions_base / ".agents" / canonical_agent_id)
            agent_touch_record_sink = os.path.join(agent_dir, _TOUCH_RECORD_FILENAME)

            await asyncio.to_thread(
                lambda: os.makedirs(agent_dir, exist_ok=True)
            )

            from coordinator_core.hooks.track_dispatched_agents import (
                _write_backpointer_sync,
            )

            # ever runs for it — and `session_id` there is the SUBAGENT's own distinct
            # CLAUDE_CODE_SESSION_ID is inherited by this hook's own process from its
            # `wf_b7ef5d89-7ca`, single `env` read) to equal the DISPATCHING EM's
            # CLAUDE_CODE_SESSION_ID as a subagent-vs-EM discriminator (see
            #     read. This handler is a REGISTERED op (`hooks.track_touched_files`),
            #     a STRANGER's id, which fails the `!= session_id` test above and so
            #     `COORDINATOR_SESSION_ID`/`CLAUDE_SESSION_ID` ahead of
            #     `CLAUDE_CODE_SESSION_ID`. Accepted rather than special-cased -- this
            from coordinator_core.session.core import resolve_session_id

            _em_session_id = resolve_session_id() or ""
            _piece2_fired = bool(_em_session_id and _em_session_id != session_id)
            if _piece2_fired:
                await asyncio.to_thread(
                    _write_backpointer_sync,
                    os.path.join(agent_dir, "em-session-id.txt"),
                    _em_session_id,
                )

            if not _piece2_fired:
                await asyncio.to_thread(
                    _write_backpointer_sync,
                    os.path.join(agent_dir, "em-session-id.txt"),
                    session_id,
                )

            agent_lock = _get_lock(agent_touch_record_sink)
            async with agent_lock:
                await asyncio.to_thread(
                    _append_touch_record,
                    agent_touch_record_sink,
                    session_id,
                    canonical_agent_id,
                    file_path_norm,
                    _content_hash,
                )

    # The record's CREATION is a different question and IS this hook's job --
    return no_advisory()
