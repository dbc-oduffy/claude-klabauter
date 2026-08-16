"""
coordinator_core.ops.session_hierarchy_query — CLI entry for the
DoE ``query-session-hierarchy.sh`` polyglot trampoline.

Purpose: reverse-query interface over the per-machine session/workstream
hierarchy projection shards (``state/session-hierarchy.<machine>.json``,
written by ``coordinator_core.ops.session_hierarchy_derive``). Given a
workstream slug, returns the ``session_id``s that belong to it. Given a
``session_id``, returns its workstream, branch, ``parent_session_id``, and
``session_type``. Operates over the UNION of all per-machine shards — a
query answers across every machine's derived projection, not just one.

Shard default location: this op resolves claude-klabauter's OWN worktree root the same
way ``session_hierarchy_derive._engine_worktree_root`` does (``Path(__file__)``
+ ``git rev-parse``), and defaults the shard directory to
``<engine-worktree>/state`` — NOT the caller's ``repo_root``. This mirrors
derive's "engine-self-checkout op" posture (module docstring there): post
T3a-g3c cutover, shards are written into claude-klabauter's own checkout regardless of
which repo dispatches the query, so the query default must look there too.
The bash oracle's default (``${REPO_ROOT}/state`` relative to the SCRIPT's
OWN location) predates that cutover and would silently find zero shards when
invoked from a DoE checkout after derive started writing engine-side — this
default is a deliberate parity-plus fix, not a behavior drift, matching the
derive module's own already-landed convention. ``QUERY_SHARD_DIR`` env
override (test isolation, mirrors ``CLAUDE_HOME``-style overrides elsewhere)
takes precedence over both.

CLI entrypoint: ``main(argv)`` is the direct-import target for the DoE
trampoline (``coordinator/bin/query-session-hierarchy.sh``, mirrors the
``derive-session-hierarchy`` / ``handoff-gate-aging`` shape) — a plain
synchronous call, NOT routed through the JSON-RPC handler (no registration;
this module is a plain CLI helper, not an op).

Spec backlink: docs/plans/2026-06-27-ccos-5-session-workstream-hierarchy-record.md § C3
Port of: query-session-hierarchy.sh (DoE b5a4192c, 2026-07-20)

Negative-spec:
- Does NOT parse handoff YAML; does NOT write any state; is NOT the derive
  script (that is ``session_hierarchy_derive``). Read-only; safe to run
  concurrently.
- Does NOT register a JSON-RPC op (no ``@register_op``, no registry-file
  edits) — a plain module with a ``main(argv) -> int`` function, called via
  direct in-process import exactly like ``handoff_gate_aging`` /
  ``scope_soak_enable``.
- Faithfully reproduces the bash oracle's argument-parsing quirk: only
  ``argv[0]``/``argv[1]`` are consulted (a `case "${1:-}"` switch) — any
  arguments beyond the first two are silently ignored, not rejected. This is
  a pre-existing oracle behavior, reproduced verbatim, not "fixed" mid-port.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.repo_root import show_toplevel

_PROG = "query-session-hierarchy.sh"

_USAGE = """\
query-session-hierarchy.sh — Query the session/workstream hierarchy projection.

Reads the union of all state/session-hierarchy.<machine>.json shards.
If no shards exist yet, run derive-session-hierarchy first.

USAGE
  query-session-hierarchy.sh --workstream <slug>
      Print all session_ids belonging to workstream <slug>, one per line.

  query-session-hierarchy.sh --session <session_id>
      Print a JSON object with workstream, branch, parent_session_id, and
      session_type for the given session_id.

  query-session-hierarchy.sh [--help | -h]
      Show this message.

EXAMPLES
  query-session-hierarchy.sh --workstream example-workstream
  query-session-hierarchy.sh --session d964cd5a-f3db-4d10-b906-9b63701ec4a4

NOTES
  - Shard location: state/session-hierarchy.<machine>.json (per-machine)
  - Query surface: UNION across all shards (all machines)
  - Requires: jq
  - Output for --workstream: one session_id per line (empty output = no sessions found)
  - Output for --session: JSON object, or error + exit 1 if not found

SPEC
  docs/plans/2026-06-27-ccos-5-session-workstream-hierarchy-record.md § C3
"""


def _engine_worktree_root() -> Optional[Path]:
    """Resolve claude-klabauter's OWN worktree root via ``Path(__file__)`` + ``git rev-parse``.

    Verbatim-shared posture with ``session_hierarchy_derive._engine_worktree_root``
    (duplicated rather than imported — this module intentionally carries zero
    dependency on the derive module, matching the "plain CLI helper" shape
    other direct-import trampolines use). Returns ``None`` (never raises) when
    git is unavailable or this file's directory is not inside a git repo.
    """
    engine_dir = Path(__file__).resolve().parent
    out = show_toplevel(str(engine_dir))
    if not out:
        return None
    return Path(out)


def _resolve_shard_dir() -> Optional[Path]:
    """QUERY_SHARD_DIR env override, else <engine-worktree>/state. None on unresolvable root."""
    override = os.environ.get("QUERY_SHARD_DIR", "")
    if override:
        return Path(override)
    worktree_root = _engine_worktree_root()
    if worktree_root is None:
        return None
    return worktree_root / "state"


def _find_shards(shard_dir: Path) -> List[Path]:
    """Glob ``session-hierarchy.*.json`` in shard_dir, sorted for deterministic union order."""
    if not shard_dir.is_dir():
        return []
    return sorted(shard_dir.glob("session-hierarchy.*.json"))


def _load_shard_records(shard_files: List[Path]) -> List[dict]:
    """Concatenate every shard's JSON-array records, in shard_files order.

    Mirrors jq's multi-file ``.[]`` streaming: each shard is a JSON array;
    records are yielded in file order, then array order within each file.
    """
    records: List[dict] = []
    for shard_file in shard_files:
        with open(shard_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError(f"shard {shard_file} is not a JSON array (got {type(data).__name__})")
        for rec in data:
            if isinstance(rec, dict):
                records.append(rec)
    return records


def main(argv: List[str]) -> int:
    """CLI entry: parse args, load shards, answer the query.

    Return codes mirror the bash oracle: 0 success (incl. empty --workstream
    result), 1 not-found / shard-resolution error, 2 argument error.
    """
    first = argv[0] if len(argv) >= 1 else ""

    if first in ("--help", "-h", ""):
        print(_USAGE, end="")
        return 0

    if first == "--workstream":
        mode = "workstream"
        arg_value = argv[1] if len(argv) >= 2 else ""
        if not arg_value:
            print("ERROR: --workstream requires a slug argument", file=sys.stderr)
            print(_USAGE, end="", file=sys.stderr)
            return 2
    elif first == "--session":
        mode = "session"
        arg_value = argv[1] if len(argv) >= 2 else ""
        if not arg_value:
            print("ERROR: --session requires a session_id argument", file=sys.stderr)
            print(_USAGE, end="", file=sys.stderr)
            return 2
    else:
        print(f"ERROR: unknown argument: {first}", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2

    shard_dir = _resolve_shard_dir()
    if shard_dir is None:
        print(f"ERROR: could not resolve repository root from {Path(__file__).resolve().parent}", file=sys.stderr)
        return 1

    shard_files = _find_shards(shard_dir)
    if not shard_files:
        print("ERROR: no session-hierarchy shards found — run derive-session-hierarchy first", file=sys.stderr)
        print(f"  Expected: {shard_dir}/session-hierarchy.<machine>.json", file=sys.stderr)
        return 1

    try:
        records = _load_shard_records(shard_files)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: failed to read session-hierarchy shard(s): {exc}", file=sys.stderr)
        return 1

    if mode == "workstream":
        for rec in records:
            if rec.get("workstream") == arg_value and rec.get("session_type") != "workstream":
                sid = rec.get("session_id")
                if sid is not None:
                    print(sid)
        return 0

    # mode == "session"
    result: Optional[dict] = None
    for rec in records:
        if rec.get("session_id") == arg_value:
            result = {
                "session_id": rec.get("session_id"),
                "session_type": rec.get("session_type"),
                "workstream": rec.get("workstream"),
                "branch": rec.get("branch"),
                "parent_session_id": rec.get("parent_session_id"),
            }
            break

    if result is None:
        print(f"ERROR: session_id not found in any shard: {arg_value}", file=sys.stderr)
        shard_list = " ".join(str(p) for p in shard_files)
        print(f"  Searched {len(shard_files)} shard(s): {shard_list}", file=sys.stderr)
        return 1

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
