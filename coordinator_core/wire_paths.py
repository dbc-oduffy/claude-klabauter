"""
coordinator_core.wire_paths — the single sanctioned construction for repo-relative
wire identifiers.

Promoted here (2026-07-20) from ``coordinator_core.ops.fleet._common``, which was the
first module to need it (commit ``4dc764b6``, rationale in ``d4833c69``).  The same
defect class turned out to span ``ops/handoff_*``, ``ops/records_query``,
``ops/changelog_ops``, ``ops/cruft_sweep``, ``distill/delete_guard``,
``percolate/guards``, ``percolate/rewrite_basename`` and
``session_ledger/aggregate_chain_loe`` — none of which may sanely depend on a
*private* module of the ``ops.fleet`` subpackage (wrong dependency direction, and
``fleet._common`` drags in asyncio/dag/lifecycle).  So the helper moved up to a
genuinely shared, dependency-free home; ``ops/fleet/_common.py`` now re-exports it,
keeping every existing ``from ...fleet._common import rel_id`` call site valid.

This module MUST stay import-free beyond the stdlib ``pathlib`` — everything in
``coordinator_core`` is allowed to depend on it, so it can depend on nothing.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["rel_id"]


def rel_id(path: Path, root: Path) -> str:
    """Return the repo-relative wire ``id`` for ``path`` — ALWAYS forward-slash.

    THE single source of truth for any repo-relative path string that leaves the
    process: JSON-RPC result fields, op params handed to a sibling op, git/rg
    subprocess arguments, rendered artifact text, and any value another module keys
    a lookup map on.  ``str(path.relative_to(root))`` is WRONG for all of those: it
    renders with ``os.sep``, so the same artifact gets id ``state/handoffs/x.md`` on
    POSIX and ``state\\handoffs\\x.md`` on Windows.  A wire value whose shape depends
    on the producing host's OS is a contract defect — cockpit, rag, and coordinator-claude tooling
    all key on this string, and git and ripgrep only ever speak forward-slash paths
    (so any comparison against ``git ls-files`` / ``git status --porcelain`` output,
    or any ``rg --fixed-strings`` needle built from a path, silently mismatches on
    Windows too).

    Round-trip safety: consumers that rebuild a filesystem path do so as
    ``root / cid`` — ``pathlib`` accepts an embedded ``/`` on every platform
    including Windows, so posix ids remain round-trippable.

    Raises ValueError when ``path`` is not under ``root`` (same as relative_to);
    callers that must tolerate that classify the item themselves.
    """
    return path.relative_to(root).as_posix()
