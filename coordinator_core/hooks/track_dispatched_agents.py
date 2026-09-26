"""
coordinator_core.hooks.track_dispatched_agents — PostToolUse Agent bookkeeping op.

Purpose: Records agent IDs dispatched by the EM into two session-runtime files:
    .git/coordinator-sessions/<session_id>/dispatched-agents.txt   (tab-delimited log)
    .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt (back-pointer)

Ported from the retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
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
Source: retired ~/.claude/plugins/coordinator-claude/coordinator/hooks/scripts/
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

GENERATES = []

from coordinator_core._hook_envelope import payload_of
from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

_locks_mutex: threading.Lock = threading.Lock()
_locks: Dict[str, asyncio.Lock] = {}


def _get_file_lock(path: str) -> "asyncio.Lock":
    import asyncio

    with _locks_mutex:
        if path not in _locks:
            _locks[path] = asyncio.Lock()
        return _locks[path]


_HEX_AGENT_RE = re.compile(r"^[a-f0-9]{12,}$")
_TEAMMATE_AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]+@session-[a-z0-9-]+$")


def _valid_agent_id(agent_id: str) -> bool:
    return bool(_HEX_AGENT_RE.match(agent_id) or _TEAMMATE_AGENT_RE.match(agent_id))


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_backpointer_sync(em_backpointer: str, session_id: str) -> None:
    try:
        st = os.stat(em_backpointer)
        if st.st_size > 0:
            return
    except FileNotFoundError:
        pass

    tmp = em_backpointer + f".tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(session_id + "\n")  # mirrors `echo "$SESSION_ID" > "$TMP_BP"`
        os.replace(tmp, em_backpointer)
    except OSError as exc:
        print(f"track_dispatched_agents: cannot write back-pointer {em_backpointer}: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _setup_dirs_sync(
    session_dir: str,
    agent_dir: str,
    em_backpointer: str,
    session_id: str,
) -> None:
    _ensure_dir(session_dir)
    _ensure_dir(agent_dir)
    _write_backpointer_sync(em_backpointer, session_id)


PLACEHOLDER_TYPE = "unknown"

AMBIGUOUS_TYPE = "AMBIGUOUS"


def _fold_agent_type(value: str) -> str:
    """Agent-type spelling folded FOR COMPARISON ONLY -- never for the write.

    Two call sites feed one dispatch: `SubagentStart` sends the real
    `agent_type`, `PostToolUse(Agent)` sends `tool_input.subagent_type`. If
    they ever spell one agent differently -- a `coordinator:` namespace on
    one side, a case difference -- the exact `==` below sends an ORDINARY
    dispatch to the AMBIGUOUS arm, which four bash guards read as hostile.
    That the class varies in spelling is not speculative: DoE hooks already
    fold it (`offer-exploration-tier-dispatch.py` for case).

    Reported by doe-claude-em 2026-08-18 with the sample size stated rather
    than rounded: zero AMBIGUOUS rows across 8 post-change dispatches --
    not reproduced, and not disproved either.

    COMPARISON ONLY is the whole constraint. Consumers keying on column 3
    need the exact real spelling on disk, so nothing here reaches `cols[2]`;
    every write below stores the caller's own value.

    break-class: an unconditional split(":")[-1]
    strips ANY namespace prefix, not just `coordinator:`, so two genuinely
    different agent types sharing a bare suffix across different namespaces
    (e.g. `vendor-a:reviewer` vs `vendor-b:reviewer`) would fold to the same
    key and silently skip the AMBIGUOUS arm. Scoped to the one namespace the
    docstring actually justifies.
    """
    folded = value.strip().casefold()
    prefix = "coordinator:"
    if folded.startswith(prefix):
        folded = folded[len(prefix):]
    return folded


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
    while len(cols) < 3:
        cols.append("")
    existing_type = cols[2]
    # Folded for the COMPARISON only (see `_fold_agent_type`); every write
    existing_folded = _fold_agent_type(existing_type)
    incoming_folded = _fold_agent_type(subagent_type)
    placeholder_folded = _fold_agent_type(PLACEHOLDER_TYPE)

    if existing_folded == incoming_folded:
        if cols[1] == PLACEHOLDER_TYPE and model != PLACEHOLDER_TYPE:
            cols[1] = model
            return cols
        return None
    if incoming_folded == placeholder_folded:
        return None

    if existing_folded == placeholder_folded:
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
            with open(dispatched, "a", encoding="utf-8", newline="\n"):
                pass
        except OSError as exc:
            print(f"track_dispatched_agents: cannot create {dispatched}: {exc}", file=sys.stderr)

    try:
        with open(dispatched, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"track_dispatched_agents: cannot read {dispatched}: {exc} (treating as empty)", file=sys.stderr)
        lines = []

    # Mirrors: cut -f1 "$DISPATCHED" | grep -qxF "$AGENT_ID"
    existing_idx: int | None = None
    for i, ln in enumerate(lines):
        cols = ln.rstrip("\n").split("\t")
        if cols and cols[0] == agent_id:
            existing_idx = i
            break

    if existing_idx is not None:
        # Mirrors: awk -F'\t' -v id="$AGENT_ID" 'BEGIN{OFS="\t"} $1==id{$3="AMBIGUOUS"} {print}'
        new_cols = _resolve_row_collision(
            lines[existing_idx].rstrip("\n").split("\t"), model, subagent_type
        )
        if new_cols is None:
            return
        lines[existing_idx] = "\t".join(new_cols) + "\n"

        tmp = dispatched + f".tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.writelines(lines)
            os.replace(tmp, dispatched)
        except OSError as exc:
            print(f"track_dispatched_agents: cannot rewrite {dispatched}: {exc}", file=sys.stderr)
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return

    # Mirrors: printf '%s\t%s\t%s\t%s\n' "$AGENT_ID" "$MODEL" "$SUBAGENT_TYPE" "$(date +%s)" >> "$DISPATCHED"
    epoch = int(time.time())
    row = f"{agent_id}\t{model}\t{subagent_type}\t{epoch}\n"
    try:
        with open(dispatched, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(row)
    except OSError as exc:
        print(f"track_dispatched_agents: cannot append to {dispatched}: {exc}", file=sys.stderr)


def _make_dispatch_mutate(agent_id: str, model: str, subagent_type: str):
    def mutate(old_text: str) -> str:
        lines = old_text.splitlines(keepends=True)

        existing_idx = None
        for i, ln in enumerate(lines):
            cols = ln.rstrip("\n").split("\t")
            if cols and cols[0] == agent_id:
                existing_idx = i
                break

        if existing_idx is not None:
            new_cols = _resolve_row_collision(
                lines[existing_idx].rstrip("\n").split("\t"), model, subagent_type
            )
            if new_cols is None:
                return old_text
            lines[existing_idx] = "\t".join(new_cols) + "\n"
            return "".join(lines)

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
    mutate = _make_dispatch_mutate(agent_id, model, subagent_type)
    try:
        locked_rmw(Path(dispatched), mutate, repo_root=repo_root_path, missing_ok=True)
    except MutateAbort:
        pass
    except LockTimeout:
        print(
            f"track_dispatched_agents: LockTimeout — dropped write for agent_id={agent_id!r} "
            f"to {dispatched} (another process held the lock)",
            file=sys.stderr,
        )
    except Exception:
        _process_dispatched_sync(dispatched, agent_id, model, subagent_type)


@register_op("hooks.track_dispatched_agents")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    import asyncio

    _entry_monotonic = time.monotonic()
    _checkpoint = "entered_handler"

    session_id = field(params, "session_id")
    agent_id = field(params, "dispatched_agent_id") or field(params, "dispatched_agent_id_snake")
    dispatched_model = field(params, "dispatched_model")
    subagent_type = field(params, "subagent_type")

    if not session_id:
        print(
            f"track_dispatched_agents: guard=empty_session_id — dropped agent_id={agent_id!r}",
            file=sys.stderr,
        )
        return no_advisory()
    if not agent_id:
        print(
            f"track_dispatched_agents: guard=empty_agent_id — dropped session_id={session_id!r}",
            file=sys.stderr,
        )
        return no_advisory()

    if not _valid_agent_id(agent_id):
        print(
            f"track_dispatched_agents: guard=invalid_agent_id — dropped agent_id={agent_id!r} "
            f"session_id={session_id!r}",
            file=sys.stderr,
        )
        return no_advisory()

    # so recording the harness value verbatim keys a DIFFERENT .agents/<id>/
    from coordinator_core.write_guards._subagent_identity import (
        normalize_teammate_agent_id,
    )

    try:
        agent_id = normalize_teammate_agent_id(agent_id, session_id)
    except Exception as exc:
        print(
            f"track_dispatched_agents: guard=normalize_teammate_agent_id_raised — dropped "
            f"agent_id={agent_id!r} session_id={session_id!r}: {exc}",
            file=sys.stderr,
        )
        return no_advisory()

    model = dispatched_model if dispatched_model else "unknown"

    # --- Subagent_type fallback (mirrors source line 139: SUBAGENT_TYPE="unknown") ---
    subagent_type = subagent_type if subagent_type else "unknown"

    if not repo_root:
        print(
            f"track_dispatched_agents: guard=empty_repo_root — dropped agent_id={agent_id!r} "
            f"session_id={session_id!r}",
            file=sys.stderr,
        )
        return no_advisory()
    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError as exc:
        print(
            f"track_dispatched_agents: guard=git_common_dir_raised — falling back to repo_root "
            f"agent_id={agent_id!r} session_id={session_id!r}: {exc}",
            file=sys.stderr,
        )
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    sessions_base = str(_sessions_base)
    session_dir = str(_sessions_base / session_id)
    agent_dir = str(_sessions_base / ".agents" / agent_id)
    dispatched = os.path.join(session_dir, "dispatched-agents.txt")
    em_backpointer = os.path.join(agent_dir, "em-session-id.txt")

    try:
        _checkpoint = "before_setup_dirs"
        await asyncio.to_thread(_setup_dirs_sync, session_dir, agent_dir, em_backpointer, session_id)
        _checkpoint = "after_setup_dirs"

        _checkpoint = "before_lock_acquire"
        lock = _get_file_lock(dispatched)
        async with lock:
            _checkpoint = "before_process_dispatched"
            await asyncio.to_thread(
                _process_dispatched_locked,
                dispatched,
                agent_id,
                model,
                subagent_type,
                Path(str(repo_root)),
            )
            _checkpoint = "after_process_dispatched"
    except asyncio.CancelledError:
        elapsed = time.monotonic() - _entry_monotonic
        print(
            f"track_dispatched_agents: cancelled — checkpoint={_checkpoint} "
            f"elapsed={elapsed:.3f}s agent_id={agent_id!r} session_id={session_id!r}",
            file=sys.stderr,
        )
        raise

    return no_advisory()

run = _handler
