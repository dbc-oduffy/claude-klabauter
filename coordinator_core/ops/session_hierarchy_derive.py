"""
coordinator_core.ops.session_hierarchy_derive — JSON-RPC "session_hierarchy.derive"
operation.

Purpose: query all handoffs (active + archived) in claude-klabauter's OWN checkout, derive
the session/workstream hierarchy projection (``coordinator_core.session_hierarchy.
derive.derive`` — the ported jq transform), and write a full-rebuild, atomic,
per-machine shard to ``state/session-hierarchy.<machine-slug>.json``.

Engine-self-checkout op (mirrors ``engine_drift.py``'s "inspects the engine's OWN
checkout" posture, but MUTATING rather than read-only): this op always targets
Claude-klabauter's own worktree, resolved via ``Path(__file__)`` + ``git rev-parse``, NOT any
caller-supplied ``repo_root`` — post-migration, handoffs live in claude-klabauter
(``coordinator_claude_klabauter_root``/state/handoffs, archive/handoffs) regardless of which
repo dispatches the call. ``repo_root`` is therefore accepted (handler signature
parity with every other op) and ignored. ``_OP_KEY_SCOPE`` classifies this op
``"none"`` for the same reason ``percolate.run``/``cartography.*``/``engine.drift``
are ``"none"`` — no per-caller repo state is consulted.

Self-registration: importing this module calls
``register_op("session_hierarchy.derive", _handler)`` as a side-effect. Add this
module to ``coordinator_core/ops/__init__.py`` (+ ``ops/_registry_map.py`` +
``ipc.py::_OP_KEY_SCOPE`` + ``authz/classification.py``) to complete registration —
central-registry edits are DEFERRED to the EM per the concurrent-build-wave
central-registry-deferral convention; see this chunk's
``central-reg/T3a-g3c.txt`` fragment.

CLI entrypoint: ``main(argv)`` is the direct-import target for the DoE trampoline
(``coordinator/bin/derive-session-hierarchy``, mirrors the ``handoff-gate-aging`` /
``scope-warning-resolve`` shape) — a plain synchronous call, NOT routed through the
JSON-RPC handler (no event loop needed for a CLI invocation with no repo_root
router to consult).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T3a-g3c
Port of: derive-session-hierarchy.sh (DoE f0aa2d56, 2026-07-16)

Negative-spec:
- Does NOT re-implement the query-records enumeration/frontmatter-parse — reuses
  ``coordinator_core.ops.ceremony.records_query.query_records`` (the in-process,
  synchronous, non-RPC helper) for both ``handoff`` and ``handoff-archived`` types.
- Does NOT validate the written JSON with a post-write parse-check (the bash
  oracle's ``jq empty`` belt-and-suspenders) — a Python object built in-memory and
  serialised via ``json.dump`` cannot itself produce invalid JSON, so this is a
  deliberate simplification, not an omission (see the T3a-g3 recipe § 3 "Python
  port notes").
"""

from __future__ import annotations

MUTATES = ["state/session-hierarchy.*.json"]  # per-machine full-rebuild shard; filename varies by machine_slug(), e.g. state/session-hierarchy.machine-b-local.json / state/session-hierarchy.machine-a.json (both tracked)

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session_hierarchy.derive import derive
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)

_PROG = "derive-session-hierarchy"


def _engine_worktree_root() -> Optional[Path]:
    """Resolve claude-klabauter's OWN worktree root via ``Path(__file__)`` + ``git rev-parse``.

    Mirrors ``engine_version.resolve_engine_sha``'s "always the executing copy"
    posture. Returns ``None`` (never raises) when git is unavailable or this
    file's directory is not inside a git repo — callers treat ``None`` as
    "cannot resolve, do nothing" (same fail-soft posture as
    ``handoff_lineage_ancestry``'s absent-``repo_root`` branch).

    Review: code-reviewer (P1) — pre-conversion this called
    ``git -C <engine_dir> rev-parse --path-format=absolute --show-toplevel``;
    ``show_toplevel()`` (``coordinator_core.git.repo_root``) does not forward
    ``--path-format=absolute`` to its own spawn fallback. Verified NOT a
    regression: ``--show-toplevel``'s own default (no ``--path-format`` flag
    at all) is already absolute — confirmed against real git, see
    ``test_show_toplevel_spawn_fallback_matches_path_format_absolute`` in
    ``coordinator_core/git/test_repo_root.py``. ``--path-format=absolute``
    was a no-op for this specific form even in the pre-conversion call.
    """
    engine_dir = Path(__file__).resolve().parent
    out = show_toplevel(str(engine_dir))
    if not out:
        return None
    return Path(out)


def _atomic_write_json(path: Path, records: List[dict]) -> None:
    """Full-rebuild atomic write: mkstemp + os.replace (latest-wins, per-machine shard).

    Cross-platform-correct replacement for the bash oracle's ``mv "$TMP" "$OUT"``
    (POSIX rename) — ``os.replace`` is atomic-on-same-filesystem on Windows too,
    unlike a bare rename there. Parity-plus, not a behavior change (recipe § 3).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        # DR-276: declared AFTER the write lands, never before — the contract
        # is a report of what was ACTUALLY written, not of an intended
        # surface.
        declare_write(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            print(f"skip: _atomic_write_json: os.unlink(tmp) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise


def _stats(records: List[dict]) -> dict:
    """Compute the stderr-summary counts (cosmetic; port verbatim for operator parity)."""
    complete = sum(1 for r in records if (r.get("system") or {}).get("completeness") == "complete")
    partial = sum(1 for r in records if (r.get("system") or {}).get("completeness") == "partial")
    n_session = sum(1 for r in records if r.get("session_type") == "session")
    n_blitz = sum(1 for r in records if r.get("session_type") == "blitz")
    n_ws = sum(1 for r in records if r.get("session_type") == "workstream")
    return {
        "total": len(records),
        "session": n_session,
        "blitz": n_blitz,
        "workstream": n_ws,
        "complete": complete,
        "partial": partial,
    }


def _run(worktree_root: Path) -> dict:
    """Query, derive, write. Returns the stats dict (also the op result payload)."""
    handoffs_active = query_records("handoff", worktree_root, limit=0)
    handoffs_archived = query_records("handoff-archived", worktree_root, limit=0)
    created_by_session = os.environ.get("CS_SESSION_ID", "")

    records = derive(handoffs_active, handoffs_archived, created_by_session, repo_root=worktree_root)

    slug = machine_slug()
    output_file = worktree_root / "state" / f"session-hierarchy.{slug}.json"
    _atomic_write_json(output_file, records)

    stats = _stats(records)
    stats["output_file"] = str(output_file)
    stats["machine_slug"] = slug
    return stats


@register_op("session_hierarchy.derive")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session_hierarchy.derive" handler.

    Params: none consumed. ``repo_root`` is accepted for handler-signature
    parity but IGNORED — this op always targets claude-klabauter's own checkout (see
    module docstring "Engine-self-checkout op").

    Returns the stats dict (``total``/``session``/``blitz``/``workstream``/
    ``complete``/``partial``/``output_file``/``machine_slug``), or
    ``{"error": "engine worktree unresolvable"}`` if the engine's own git
    worktree cannot be resolved (fail-soft, mirrors ``handoff_lineage_
    ancestry``'s absent-``repo_root`` empty-payload posture rather than
    raising).
    """
    worktree_root = await asyncio.to_thread(_engine_worktree_root)
    if worktree_root is None:
        _LOG.warning("session_hierarchy.derive: engine worktree unresolvable")
        return {"error": "engine worktree unresolvable"}
    return _run(worktree_root)


def main(argv: List[str]) -> int:
    """CLI entry: resolve the engine worktree, run, print stats to stderr."""
    worktree_root = _engine_worktree_root()
    if worktree_root is None:
        print(f"{_PROG}: engine worktree unresolvable (git rev-parse failed)", file=sys.stderr)
        return 1

    print(f"[{_PROG}] Engine worktree: {worktree_root}", file=sys.stderr)
    stats = _run(worktree_root)
    print(f"[{_PROG}] Output: {stats['output_file']}", file=sys.stderr)
    print(f"[{_PROG}] Done.", file=sys.stderr)
    print(f"[{_PROG}]   Total records : {stats['total']}", file=sys.stderr)
    print(f"[{_PROG}]   session       : {stats['session']}", file=sys.stderr)
    print(f"[{_PROG}]   blitz         : {stats['blitz']}", file=sys.stderr)
    print(f"[{_PROG}]   workstream    : {stats['workstream']}", file=sys.stderr)
    print(f"[{_PROG}]   complete      : {stats['complete']}", file=sys.stderr)
    print(f"[{_PROG}]   partial       : {stats['partial']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
