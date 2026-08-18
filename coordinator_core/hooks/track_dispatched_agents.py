"""
coordinator_core.hooks.track_dispatched_agents — PostToolUse Agent bookkeeping op.

Purpose: Records agent IDs dispatched by the EM into two session-runtime files:
    .git/coordinator-sessions/<session_id>/dispatched-agents.txt   (tab-delimited log)
    .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt (back-pointer)

Ported from the retired ~/.claude/plugins/coordinator/hooks/scripts/
track-dispatched-agents.sh (deleted 2026-07-22, DoE ``3a561713``). Faithful port of all write logic and
conditionals — same 3-pass agent-id extraction (now pre-resolved to flat scalar
input by the manifest), same 4-source model cascade (now pre-resolved), same
tab-delimited format, same dedup / collision → AMBIGUOUS rewrite, same atomic
temp+rename back-pointer.

Record shape (tab-delimited, newline-terminated):
    <agentId>\\t<model>\\t<subagent_type>\\t<dispatched-at>

where <dispatched-at> is Unix epoch seconds at write time. Legacy 1-column
(bare agentId, no tabs) and 3-column (no dispatched-at) records still parse —
readers treat missing columns as sentinel values ("unknown").

Agent-id format guard (step b from source): accept bare lowercase hex (≥12 chars,
unnamed background agents) OR teammate canonical id (<name>@session-<short>) from
the harness. Reject anything else (fail-closed).

Dedup / collision guard (column-1 comparison, per source step e):
    Same agent_id + same subagent_type → silent idempotent dedup.
    Same agent_id + different REAL subagent_type → mark existing row's column 3 as
    AMBIGUOUS (detect-then-fail-loud; fail-closed for both colliding dispatches per
    AC14). The suffixed-row approach is NOT used: the subagent-side resolver
    reconstructs only the unsuffixed canonical id.

Two-phase write (create then enrich): the "unknown" sentinel is a PLACEHOLDER, not a
colliding value. A caller that knows an agent_id before it knows the agent's type —
SubagentStart fires with neither model nor subagent_type available — records an
identity-only row, and a later call carrying the real type enriches that row in place
instead of colliding with it. Both directions are covered, because the two calls race
on a machine running dozens of concurrent sessions: a placeholder arriving AFTER a
resolved row is a no-op and never downgrades it. Only two REAL, differing types are a
genuine collision, which is what the AMBIGUOUS sentinel is read as downstream — four
bash guards treat it as a hostile shape, so widening it to cover in-order enrichment
would disarm them on every dispatch. See _resolve_row_collision for the full table.

Write atomicity (D6 — shared singleton engine): concurrent sessions sharing the
engine can invoke this op simultaneously. An asyncio.Lock keyed to the
dispatched-agents.txt file path serializes concurrent thread-pool invocations on
the same file. The source's process-isolation guarantee does NOT apply in-engine;
the "tolerated TOCTOU" comment in the source is not tolerated here.

The back-pointer (em-session-id.txt) uses atomic OS temp+rename — no additional
lock required because concurrent writes to a non-empty, already-written file are
suppressed by the `os.stat` size check before the write.

R-1 contingency: dispatched_agent_id and dispatched_model arrive as flat scalars.
The manifest extracts them from tool_response.agentId (3-pass cascade: agentId →
agent_id → regex) and tool_response.resolvedModel (4-source cascade:
resolvedModel → response.model → input.model → "unknown"). This op is
dormant-correct if the manifest cannot flatten nested tool_response.* inputs.

Negative-spec:
    - MUST NOT write state/, archive/, or any path outside .git/coordinator-sessions/.
      Write confinement: session-runtime layer only (SC-2, pcore-08 plan § ipc.py D2).
    - Returns no_advisory() (empty dict) — product is the on-disk write side-effect,
      not an advisory string.
    - All file / os.stat / rename I/O is wrapped in asyncio.to_thread()
      (mcp-async-handler-discipline — binds unconditionally for write ops).
    - Always returns without blocking the harness — advisory bookkeeping only.

Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § C4
Source: retired ~/.claude/plugins/coordinator/hooks/scripts/
track-dispatched-agents.sh (deleted 2026-07-22, DoE ``3a561713``).
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    import asyncio

# Generator-provenance declaration (generator_provenance.py). This module
# writes only session-runtime bookkeeping under .git/coordinator-sessions/
# (dispatched-agents.txt, em-session-id.txt) -- never a tracked repo artifact.
GENERATES = []

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

# ---------------------------------------------------------------------------
# Per-file asyncio.Lock registry — D6 write-atomicity.
#
# The engine is a per-repo singleton shared by concurrent sessions. When two
# sessions return agents simultaneously, two asyncio.to_thread() tasks run
# _process_dispatched() concurrently on the same dispatched-agents.txt file.
# Without a lock, the read→detect→(rewrite|append) sequence races: one thread
# can read stale content and overwrite the other's append.
#
# _locks maps absolute file-path → asyncio.Lock. _locks_mutex (threading.Lock)
# protects the dict during lazy creation — asyncio.Lock instances are created
# on the event loop that is running at call time, so lazy creation is correct.
# ---------------------------------------------------------------------------
_locks_mutex: threading.Lock = threading.Lock()
_locks: Dict[str, asyncio.Lock] = {}


def _get_file_lock(path: str) -> "asyncio.Lock":
    """Return (creating if needed) an asyncio.Lock keyed to the given file path.

    Safe to call from an async handler: _locks_mutex is a threading.Lock (not
    asyncio), so it never blocks the event loop. Lock objects are created lazily
    when the event loop is guaranteed to be running.
    """
    # asyncio deferred to first use here (not module scope) — module-scope
    # `import asyncio` dragged asyncio.base_events (~5ms) into every eager op/hook
    # import even for callers that never dispatch this PostToolUse hook. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    with _locks_mutex:
        if path not in _locks:
            _locks[path] = asyncio.Lock()
        return _locks[path]


# ---------------------------------------------------------------------------
# Agent-id format guards.
# Accept bare lowercase hex (≥12 chars, unnamed background agents) OR
# teammate canonical id (<name>@session-<short>) from the harness (2.1.185).
# Reject anything else fail-closed (AC5).
# Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C1(b)
# ---------------------------------------------------------------------------
_HEX_AGENT_RE = re.compile(r"^[a-f0-9]{12,}$")
_TEAMMATE_AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]+@session-[a-z0-9-]+$")


def _valid_agent_id(agent_id: str) -> bool:
    """Return True iff agent_id matches the bare-hex or teammate canonical format."""
    return bool(_HEX_AGENT_RE.match(agent_id) or _TEAMMATE_AGENT_RE.match(agent_id))


# ---------------------------------------------------------------------------
# Sync I/O helpers — ALL called inside asyncio.to_thread() in the handler.
# Never call these directly from async context (mcp-async-handler-discipline).
# ---------------------------------------------------------------------------

def _ensure_dir(path: str) -> None:
    """Create directory (and parents) if not already present — mirrors mkdir -p."""
    os.makedirs(path, exist_ok=True)


def _write_backpointer_sync(em_backpointer: str, session_id: str) -> None:
    """Atomically write session_id to em-session-id.txt if absent or empty.

    -s test: file exists AND is non-empty. Empty back-pointers (partial-write
      survivors from a prior crash) trigger re-write.
      Temp+rename pattern: a concurrent fire either succeeds-second or cleans-up —
      no orphan temp files (the Staff Engineer v2 finding 3).
    """
    # -s equivalent: exists and non-empty.
    try:
        st = os.stat(em_backpointer)
        if st.st_size > 0:
            return  # Already written; idempotent.
    except FileNotFoundError:
        # Expected on first write for this session — fall through to the
        # create-and-write path below.
        pass

    # Review: code-reviewer — F3 (P2): add thread-id to temp name so concurrent
    # asyncio.to_thread() invocations sharing this PID get distinct temp paths.
    # Bash source used $$ (per-process PID); in-engine all invocations share the PID.
    tmp = em_backpointer + f".tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(session_id + "\n")  # mirrors `echo "$SESSION_ID" > "$TMP_BP"`
        os.replace(tmp, em_backpointer)  # atomic rename
    except OSError as exc:
        print(f"track_dispatched_agents: cannot write back-pointer {em_backpointer}: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass  # tmp may not exist (e.g. the initial open failed) — best-effort cleanup


def _setup_dirs_sync(
    session_dir: str,
    agent_dir: str,
    em_backpointer: str,
    session_id: str,
) -> None:
    """Create session and agent dirs, then write the back-pointer — one thread-pool dispatch.

    Review: code-reviewer — F5 (nit): collapses 3 sequential asyncio.to_thread() calls
    for independent pre-write setup into a single dispatch; session_dir and agent_dir
    creation are independent; backpointer write follows agent_dir creation.
    """
    _ensure_dir(session_dir)
    _ensure_dir(agent_dir)
    _write_backpointer_sync(em_backpointer, session_id)


#: Sentinel both `model` and `subagent_type` degrade to when the harness supplies neither
#: (see _handler's fallback pair). In column 3 it doubles as the two-phase placeholder.
PLACEHOLDER_TYPE = "unknown"

#: Collision sentinel written into column 3. Read downstream by the bash guards
#: (block_subagent_destructive_action names it in its refusal text) as a hostile shape.
AMBIGUOUS_TYPE = "AMBIGUOUS"


def _resolve_row_collision(
    existing_cols: list[str],
    model: str,
    subagent_type: str,
) -> list[str] | None:
    """Decide what a second write for an already-recorded agent_id does to its row.

    Single source of truth for the dedup / enrich / collision table, shared by both
    write arms (_process_dispatched_sync and _make_dispatch_mutate.mutate) so the
    POSIX and non-POSIX paths cannot drift apart on it.

    Returns the replacement column list, or None when the existing row stands unchanged.

        existing type == incoming type          → upgrade cols[1] in place when it still
                                                  holds the model placeholder and the
                                                  incoming model does not (a real type at
                                                  create time must not strand model);
                                                  otherwise None (idempotent dedup)
        incoming type is the placeholder        → None (a late or out-of-order
                                                  identity-only write never downgrades
                                                  an already-resolved row)
        existing type is the placeholder        → enrich in place: adopt the real type,
                                                  and the real model when the incoming
                                                  one is not itself a placeholder
        two real, differing types               → AMBIGUOUS (detect-then-fail-loud, AC14)

    Enrichment deliberately PRESERVES column 4: the create call fires at SubagentStart,
    closer to the true dispatch moment than the enriching PostToolUse call, and the
    runtime tripwire measures elapsed time against that column.

    A legacy short record carries "" in column 3, which is NOT the placeholder — it
    stays on the AMBIGUOUS arm exactly as before, and padding stops at 3 columns so a
    collision against one does not grow a trailing empty field it never had.
    """
    cols = list(existing_cols)
    # Pad to at least 3 columns (0: agent_id, 1: model, 2: subagent_type).
    while len(cols) < 3:
        cols.append("")
    existing_type = cols[2]

    if existing_type == subagent_type:
        # A real type at create time must not strand model at the placeholder:
        # upgrade cols[1] here rather than relying on a later enrich leg that,
        # for a Workflow agent() spawn, never fires (no PostToolUse Agent call).
        if cols[1] == PLACEHOLDER_TYPE and model != PLACEHOLDER_TYPE:
            cols[1] = model
            return cols
        return None
    if subagent_type == PLACEHOLDER_TYPE:
        return None

    if existing_type == PLACEHOLDER_TYPE:
        cols[2] = subagent_type
        if model != PLACEHOLDER_TYPE:
            cols[1] = model
        return cols

    cols[2] = AMBIGUOUS_TYPE
    return cols


def _process_dispatched_sync(
    dispatched: str,
    agent_id: str,
    model: str,
    subagent_type: str,
) -> None:
    """Read, dedup-or-collision-rewrite, or append to dispatched-agents.txt.

    Called under the per-file asyncio.Lock so concurrent engine invocations serialize —
    the "tolerated TOCTOU" of the source is NOT tolerated in-engine (D6).

    Dedup logic:
        Column-1 match + same subagent_type → idempotent silent exit (same dispatch shape).
        Column-1 match + different subagent_type → mark existing row col-3 as AMBIGUOUS;
            atomic temp+rename rewrite (no suffixed-row approach; resolver uses unsuffixed id).
        No column-1 match → append new tab-delimited row.

    Format: <agentId>\\t<model>\\t<subagent_type>\\t<unix-epoch>\\n
    Column-1 (agent_id) is the dedup key; runtime-tripwire greps this column.
    """
    # Ensure file exists — mirrors `touch "$DISPATCHED"`.
    if not os.path.exists(dispatched):
        try:
            # Review: code-reviewer — F4 (nit): use context manager; bare open().close()
            # relies on CPython refcount for resource release (ResourceWarning in linters).
            with open(dispatched, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            print(f"track_dispatched_agents: cannot create {dispatched}: {exc}", file=sys.stderr)

    # Read current content.
    try:
        with open(dispatched, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"track_dispatched_agents: cannot read {dispatched}: {exc} (treating as empty)", file=sys.stderr)
        lines = []

    # Dedup / collision detection — column-1 (agent_id) comparison.
    # Mirrors: cut -f1 "$DISPATCHED" | grep -qxF "$AGENT_ID"
    # Spec backlink: docs/plans/2026-06-30-loe-dispatch-undercount-teammate-shape.md § C1(e)
    existing_idx: int | None = None
    for i, ln in enumerate(lines):
        cols = ln.rstrip("\n").split("\t")
        if cols and cols[0] == agent_id:
            existing_idx = i
            break

    if existing_idx is not None:
        # Dedup / enrich / collision — the shared table, not a second copy of it.
        # Mirrors: awk -F'\t' -v id="$AGENT_ID" 'BEGIN{OFS="\t"} $1==id{$3="AMBIGUOUS"} {print}'
        new_cols = _resolve_row_collision(
            lines[existing_idx].rstrip("\n").split("\t"), model, subagent_type
        )
        if new_cols is None:
            # Row stands as written — idempotent dedup, or a placeholder that must
            # not downgrade it. Silent exit.
            return
        lines[existing_idx] = "\t".join(new_cols) + "\n"

        # Atomic rewrite — temp+rename (D6; mirrors the awk > tmp && mv tmp dispatched pattern).
        # Tolerated TOCTOU for external-process concurrent appends; in-engine races
        # are serialized by the asyncio.Lock the caller holds.
        # Review: code-reviewer — F3 sibling sweep: same PID-uniqueness fix as
        # _write_backpointer_sync; the lock serializes same-file concurrent callers
        # but defence-in-depth and pattern consistency warrant the thread-id suffix.
        tmp = dispatched + f".tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, dispatched)  # atomic rename
        except OSError as exc:
            print(f"track_dispatched_agents: cannot rewrite {dispatched}: {exc}", file=sys.stderr)
            try:
                os.unlink(tmp)
            except OSError:
                pass  # tmp may not exist — best-effort cleanup
        return

    # New entry: append tab-delimited row.
    # Mirrors: printf '%s\t%s\t%s\t%s\n' "$AGENT_ID" "$MODEL" "$SUBAGENT_TYPE" "$(date +%s)" >> "$DISPATCHED"
    epoch = int(time.time())
    row = f"{agent_id}\t{model}\t{subagent_type}\t{epoch}\n"
    try:
        with open(dispatched, "a", encoding="utf-8") as fh:
            fh.write(row)
    except OSError as exc:
        print(f"track_dispatched_agents: cannot append to {dispatched}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Cross-process locked dedup/collision/append helpers — wraps _process_dispatched_sync
# logic inside locked_rmw for cross-process write serialisation.
#
# Structured as a factory (_make_dispatch_mutate) that returns a mutate callable for
# locked_rmw, plus a wrapper (_process_dispatched_locked) that invokes locked_rmw with
# that mutate and falls back to _process_dispatched_sync when locked_rmw is unavailable
# (non-POSIX, non-git dir).
# ---------------------------------------------------------------------------

def _make_dispatch_mutate(agent_id: str, model: str, subagent_type: str):
    """Return a locked_rmw mutate callable that implements dedup/collision/append logic.

    The returned function receives the current file text (or "" for absent file) and
    returns the new full text with the dispatch recorded, deduplicated, or collision-marked.
    Mirrors _process_dispatched_sync logic in a pure str→str form suitable for locked_rmw.
    """
    def mutate(old_text: str) -> str:
        lines = old_text.splitlines(keepends=True)

        # Dedup / collision detection — column-1 (agent_id) comparison.
        existing_idx = None
        for i, ln in enumerate(lines):
            cols = ln.rstrip("\n").split("\t")
            if cols and cols[0] == agent_id:
                existing_idx = i
                break

        if existing_idx is not None:
            # Dedup / enrich / collision — the shared table, not a second copy of it.
            new_cols = _resolve_row_collision(
                lines[existing_idx].rstrip("\n").split("\t"), model, subagent_type
            )
            if new_cols is None:
                # Row stands as written; locked_rmw skips the write (byte-identical).
                return old_text
            lines[existing_idx] = "\t".join(new_cols) + "\n"
            return "".join(lines)

        # New entry: append tab-delimited row.
        epoch = int(time.time())
        row = f"{agent_id}\t{model}\t{subagent_type}\t{epoch}\n"
        return old_text + row

    return mutate


def _process_dispatched_locked(
    dispatched: str,
    agent_id: str,
    model: str,
    subagent_type: str,
    repo_root_path: Path,
) -> None:
    """Dedup/collision/append to dispatched-agents.txt via locked_rmw; fall back to _process_dispatched_sync.

    Caller MUST hold the per-file asyncio.Lock (D6) before dispatching via asyncio.to_thread()
    — the lock prevents intra-process re-entrancy on the same file before the flock reaches the OS.

    When locked_rmw succeeds (POSIX + valid git working directory), the read-modify-write is
    serialised across processes. When it raises MutateAbort or LockTimeout, NEITHER routes to the
    fallback _process_dispatched_sync — see the two error legs below for why they diverge despite
    both leaving the file unwritten. Only a third class of failure (non-POSIX, git unavailable —
    the generic `except Exception` leg) falls back to it.

    Silent-failure contract: MutateAbort (clean, nothing to write) is swallowed silently, as
    before. LockTimeout (a LOST WRITE — another process demonstrably holds the lock) is no longer
    silent: it leaves a durable, greppable stderr breadcrumb naming the agent id and the dropped
    file, then is swallowed the same as before (no raise into the caller). A stderr breadcrumb was
    chosen over a marker file because it needs zero I/O of its own — no path to create, no
    directory to ensure, nothing that could itself contend for the lock this leg just failed to
    take. It costs a single already-buffered write to an already-open stream.
    """
    mutate = _make_dispatch_mutate(agent_id, model, subagent_type)
    try:
        locked_rmw(Path(dispatched), mutate, repo_root=repo_root_path, missing_ok=True)
    except MutateAbort:
        pass  # clean abort — mutate declined to write; nothing lost, stays silent
    except LockTimeout:
        # Lost write: another process demonstrably holds the lock (that's what timed out
        # waiting on). Do NOT fall back to _process_dispatched_sync — it has no cross-process
        # serialisation, so taking it here would write unserialised at exactly the moment a
        # peer process holds the lock (see the module's Anti-scope in the owning plan).
        # Control flow is otherwise unchanged: the write is dropped, no fallback fires, and
        # this still doesn't raise into the caller — only the drop stops being invisible.
        print(
            f"track_dispatched_agents: LockTimeout — dropped write for agent_id={agent_id!r} "
            f"to {dispatched} (another process held the lock)",
            file=sys.stderr,
        )
    except Exception:
        # locked_rmw unavailable (non-POSIX, non-git dir, RuntimeError from git_common_dir).
        # Fall back to the original sync helper. Cross-process protection absent on this path;
        # the asyncio.Lock held by the caller serialises concurrent invocations in-process.
        _process_dispatched_sync(dispatched, agent_id, model, subagent_type)


@register_op("hooks.track_dispatched_agents")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse Agent bookkeeping: record dispatched agent id + back-pointer.

    Inputs (flat scalar, forwarded by mcp_tool hooks.json input: declaration):
        session_id:          EM session id (top-level field; firing session).
        dispatched_agent_id: resolved agent id — 3-pass cascade pre-resolved by manifest:
                             (a) tool_response.agentId (camelCase, unnamed/background),
                             (a) tool_response.agent_id (snake_case, named teammates),
                             (a2) regex fallback over tool_response substring.
        dispatched_model:    resolved model string — 4-source cascade pre-resolved by manifest:
                             resolvedModel → response.model → input.model → "unknown".
        subagent_type:       from tool_input.subagent_type; graceful-degrade to "unknown".

    # R-1: dispatched_agent_id and dispatched_model are flattened tool_response.agentId /
    # tool_response.resolvedModel — nested tool_response.* substitution is pending
    # claude-central-em confirmation. Op is dormant-correct if the manifest cannot
    # flatten these inputs — the flat-scalar contract is correct regardless of R-1 outcome.

    Write targets (D2 sanctioned session-runtime layer):
        .git/coordinator-sessions/<session_id>/dispatched-agents.txt
        .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt

    Returns no_advisory() — product is the on-disk write side-effect.
    Always exits cleanly (advisory bookkeeping; never blocks tool calls).
    """
    # asyncio deferred to first use here (not module scope). Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    session_id = field(params, "session_id")
    # R-1: dispatched_agent_id is the flattened tool_response.agentId; snake fallback
    # dispatched_agent_id_snake consumes tool_response.agent_id (snake_case) from named-teammate
    # dispatch returns — F2 resolved, manifest f4f150a1d.
    agent_id = field(params, "dispatched_agent_id") or field(params, "dispatched_agent_id_snake")
    dispatched_model = field(params, "dispatched_model")
    subagent_type = field(params, "subagent_type")

    # --- Guard: required inputs (mirrors source exits at lines 88-90, 124) ---
    if not session_id:
        return no_advisory()
    if not agent_id:
        return no_advisory()

    # --- Agent-id format guard (mirrors source step (b), lines 125-128) ---
    # Accept bare lowercase hex (≥12 chars) OR teammate canonical <name>@session-<short>.
    # Reject anything else fail-closed.
    if not _valid_agent_id(agent_id):
        return no_advisory()

    # --- Rewrite a stale harness-embedded short against the LIVE session_id ---
    # The harness hands back a named teammate's agent_id already in canonical
    # <name>@session-<short> form, with <short> stamped once at team creation
    # (survives /clear, resume, compact, fork). Every OTHER writer in this
    # codebase (track_touched_files, session/identity.py, _subagent_identity.py)
    # builds this same teammate's canonical id fresh from the LIVE session_id —
    # so recording the harness value verbatim keys a DIFFERENT .agents/<id>/
    # directory than every other bookkeeping surface uses for the same
    # teammate, and every cross-writer join against it silently misses. See
    # docs/research/spike-verdicts/2026-08-10-session-scoped-hooks-inside-a-
    # teammate-session.md and normalize_teammate_agent_id's own docstring.
    from coordinator_core.write_guards._subagent_identity import (
        normalize_teammate_agent_id,
    )

    agent_id = normalize_teammate_agent_id(agent_id, session_id)

    # --- Model fallback (mirrors source step (c), line 138: MODEL="unknown") ---
    # The manifest resolved the 4-source cascade to a flat scalar; "" means none resolved.
    model = dispatched_model if dispatched_model else "unknown"

    # --- Subagent_type fallback (mirrors source line 139: SUBAGENT_TYPE="unknown") ---
    subagent_type = subagent_type if subagent_type else "unknown"

    # --- Resolve write paths from repo_root ---
    # Guard against absent/None value: write to .git/ only when a valid repo root is available.
    if not repo_root:
        return no_advisory()
    # C1d: route through git_common_dir so linked worktrees resolve to the main .git
    # directory (a real dir) rather than the worktree's .git FILE.
    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError:
        # Review: code-reviewer — fallback had ".git" doubled: repo_root IS git_common_dir,
        # so Path(repo_root) / ".git" / "coordinator-sessions" → <repo>/.git/.git/… (never exists).
        # Fix: drop the redundant ".git" join in this fallback branch.
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    sessions_base = str(_sessions_base)
    session_dir = str(_sessions_base / session_id)
    agent_dir = str(_sessions_base / ".agents" / agent_id)
    dispatched = os.path.join(session_dir, "dispatched-agents.txt")
    em_backpointer = os.path.join(agent_dir, "em-session-id.txt")

    # --- Init session-dir + agent-dir + atomic back-pointer ---
    # Source tries cs_init via coordinator-session.sh lib; falls back to mkdir -p.
    # In-engine: always repo-keyed, so the lib-resolution dance is unnecessary.
    # Review: code-reviewer — F5 (nit): collapsed 3 sequential to_thread() round-trips
    # for independent pre-write operations into one _setup_dirs_sync dispatch.
    await asyncio.to_thread(_setup_dirs_sync, session_dir, agent_dir, em_backpointer, session_id)

    # --- Dedup / collision-guard + append — asyncio.Lock (D6) + locked_rmw cross-process layer ---
    # asyncio.Lock serializes concurrent asyncio.to_thread() invocations within this process.
    # locked_rmw (flock-backed) adds cross-process serialisation via _process_dispatched_locked.
    # Falls back to _process_dispatched_sync when locked_rmw is unavailable (non-POSIX, non-git).
    lock = _get_file_lock(dispatched)
    async with lock:
        await asyncio.to_thread(
            _process_dispatched_locked,
            dispatched,
            agent_id,
            model,
            subagent_type,
            Path(str(repo_root)),
        )

    return no_advisory()
