"""
coordinator_core.hooks.subagent_zero_tool_use_surface — thin pure-read surfacing op.

Purpose: Stage 2 (read side) of the zero-tool-use detection cross-repo contract with
DoE-claude. Returns this session's `kind == "zero-tool-use"` durable records — written
by hooks.subagent_zero_tool_use — in append order, as structured JSON-RPC result
data. This is new ground: no existing hooks.* op returns structured data (every prior
op returns an advisory envelope via _envelope.py); this op returns a plain dict
instead, because there is no model-facing advisory here to shape.

NOT a zero-filter: `records` contains one entry per verified `tool_use_count` this
session — every completed subagent Stage 1 was able to count, not only the ones that
did zero tool calls (see hooks.subagent_zero_tool_use's module docstring, "Naming
note"). `kind == "zero-tool-use"` identifies which detector wrote the record, not
its count. A caller that treats every returned record as a zero-tool-use detection
will misreport healthy agents; filter each record's own `tool_use_count == 0`
yourself (DoE's consumer does this — see
cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-store-records-every-count.md).
hooks.subagent_zero_tool_use_resolve does this filtering per-agent already, if a
single verdict rather than the raw record list is what you need.

Return shape (pinned):
    {
        "records": [<record dicts, in append order>],
        "record_count": <int>,
        "skipped_lines": <int>,
        "store_present": <bool>,
    }

Append order is load-bearing: DoE's own consumer tracks delivery with a count-or-offset
cursor on their side (per-session, exactly-once), which is only stable if this op
always returns records in the same order they were written. Do not sort, dedup, or
reorder records here.

Negative-spec:
    THIN and PURE-READ — no state mutation of any kind:
      - Does NOT mark anything "surfaced". There is no surfaced/unsurfaced state
        anywhere in this op or in the on-disk schema; exactly-once delivery is
        tracked entirely DoE-side via their own per-session cursor file.
      - Does NOT perform "dispatched but no record arrived" reconciliation. That gap
        detection is DoE-side, layered onto their own existing dispatch-tracking loop
        which already owns dispatched-agents.txt / agent-audit.jsonl and the
        false-positive suppressors a fresh engine-side reconciler would have to
        re-derive from scratch.
      - Does NOT add atomicity/locking machinery. Pure read of an append-only file;
        no shared mutable state to race, and each session owns its own store file.
      - Does NOT register or handle a `TaskCompleted` case — evaluated and rejected
        DoE-side (no `agent_id`, no transcript pointer in that payload); a handler
        here would be a dead trigger.

    A missing store file is NOT an error — it means the session dispatched no
    zero-tool-use-verified subagents yet (or none at all); returns the empty-result
    shape (`store_present: false`, `records: []`, `record_count: 0`,
    `skipped_lines: 0`), never raises.

    A malformed line (not valid JSON, not a JSON object) is skipped and counted in
    `skipped_lines` — never fatal to the read.

    A record whose `kind` is not `"zero-tool-use"` is filtered out silently (it
    belongs to some other future detector sharing this per-session store via the
    same kind-discriminated shape) — it is NOT counted in `skipped_lines`, since it
    parsed correctly and is simply out of scope for this surface op.

Spec backlink: cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-engine-op-contract.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.hooks._payload import field
from coordinator_core.lifecycle import git_common_dir

_RECORD_KIND = "zero-tool-use"


def _empty_result() -> dict:
    """Return the pinned empty-result shape for a missing/absent store."""
    return {
        "records": [],
        "record_count": 0,
        "skipped_lines": 0,
        "store_present": False,
    }


def _read_store_sync(store_path: str) -> dict:
    """Read and filter the per-session store (blocking I/O).

    Called exclusively via asyncio.to_thread() — must not be awaited directly.
    Returns the pinned result shape: records filtered to kind == "zero-tool-use",
    in append order; skipped_lines counts lines that failed to parse as a JSON
    object at all (kind-mismatched lines are NOT counted as skipped — see module
    negative-spec).
    """
    if not os.path.exists(store_path):
        return _empty_result()

    try:
        with open(store_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        # Present-but-unreadable is treated the same as absent for this pure-read
        # surface op — there is no write-side decision to protect here, unlike the
        # UNKNOWN-vs-zero distinction in hooks.subagent_zero_tool_use.
        return _empty_result()

    records: list[dict] = []
    skipped_lines = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            skipped_lines += 1
            continue
        if not isinstance(parsed, dict):
            skipped_lines += 1
            continue
        if parsed.get("kind") != _RECORD_KIND:
            continue
        records.append(parsed)

    return {
        "records": records,
        "record_count": len(records),
        "skipped_lines": skipped_lines,
        "store_present": True,
    }


@register_op("hooks.subagent_zero_tool_use_surface")
async def _handler(params: dict, repo_root=None) -> dict:
    """Pure-read op: return this session's zero-tool-use-detector records (all
    verified counts, not only zero — see module docstring) in append order.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as absent):
        session_id, hook_event_name.

    Returns the pinned {"records", "record_count", "skipped_lines", "store_present"}
    shape directly (structured JSON-RPC result, not an advisory envelope) — a missing
    repo_root or session_id resolves to the same empty-result shape as a missing
    store file, never an error.
    """
    import asyncio

    session_id = field(params, "session_id")

    if not repo_root or not session_id:
        return _empty_result()

    try:
        _sessions_base = git_common_dir(repo_root) / "coordinator-sessions"
    except RuntimeError:
        _sessions_base = Path(str(repo_root)) / "coordinator-sessions"
    store_path = str(_sessions_base / session_id / "subagent-zero-tool-use.jsonl")

    return await asyncio.to_thread(_read_store_sync, store_path)
