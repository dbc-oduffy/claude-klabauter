"""
coordinator_core.hooks.track_dispatched_agents — PostToolUse Agent bookkeeping op.

Purpose: Records agent IDs dispatched by the EM into two session-runtime files:
    .git/coordinator-sessions/<session_id>/dispatched-agents.txt   (tab-delimited log)
    .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt (back-pointer)

Ported from the retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
track-dispatched-agents.sh (deleted 2026-07-22, example-doctrine-repo ``3a561713``). Faithful port of all write logic and
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
    Same agent_id + different subagent_type → mark existing row's column 3 as
    AMBIGUOUS (detect-then-fail-loud; fail-closed for both colliding dispatches per
    AC14). The suffixed-row approach is NOT used: the subagent-side resolver
    reconstructs only the unsuffixed canonical id.

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

Spec backlink: docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md § C4
Source: retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
track-dispatched-agents.sh (deleted 2026-07-22, example-doctrine-repo ``3a561713``).
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
    existing_type: str = ""
    for i, ln in enumerate(lines):
        cols = ln.rstrip("\n").split("\t")
        if cols and cols[0] == agent_id:
            existing_idx = i
            # Column 3 (subagent_type, 0-indexed as cols[2]); "" for legacy short records.
            existing_type = cols[2] if len(cols) > 2 else ""
            break

    if existing_idx is not None:
        if existing_type == subagent_type:
            # Same dispatch shape — idempotent dedup, silent exit.
            return

        # Collision: different subagent_type for the same canonical id.
        # Mark existing row's column 3 as AMBIGUOUS (detect-then-fail-loud).
        # Mirrors: awk -F'\t' -v id="$AGENT_ID" 'BEGIN{OFS="\t"} $1==id{$3="AMBIGUOUS"} {print}'
        cols = lines[existing_idx].rstrip("\n").split("\t")
        # Pad to at least 3 columns (0: agent_id, 1: model, 2: subagent_type).
        while len(cols) < 3:
            cols.append("")
        cols[2] = "AMBIGUOUS"
        lines[existing_idx] = "\t".join(cols) + "\n"

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
        existing_type = ""
        for i, ln in enumerate(lines):
            cols = ln.rstrip("\n").split("\t")
            if cols and cols[0] == agent_id:
                existing_idx = i
                existing_type = cols[2] if len(cols) > 2 else ""
                break

        if existing_idx is not None:
            if existing_type == subagent_type:
                # Same dispatch shape — idempotent dedup; locked_rmw skips write (byte-identical).
                return old_text

            # Collision: different subagent_type for the same canonical id.
            # Mark existing row col-3 as AMBIGUOUS (detect-then-fail-loud, AC14).
            cols = lines[existing_idx].rstrip("\n").split("\t")
            while len(cols) < 3:
                cols.append("")
            cols[2] = "AMBIGUOUS"
            lines[existing_idx] = "\t".join(cols) + "\n"
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
    serialised across processes. When it raises (non-POSIX, git unavailable, LockTimeout), the
    fallback _process_dispatched_sync provides intra-process-only serialisation (same guarantee
    as the original path).

    Silent-failure contract: all errors in both paths are swallowed.
    """
    mutate = _make_dispatch_mutate(agent_id, model, subagent_type)
    try:
        locked_rmw(Path(dispatched), mutate, repo_root=repo_root_path, missing_ok=True)
    except (LockTimeout, MutateAbort):
        pass  # lock timeout or clean abort — silent-failure for bookkeeping
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
