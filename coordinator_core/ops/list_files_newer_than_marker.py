"""
coordinator_core.ops.list_files_newer_than_marker — JSON-RPC
"percolate.list_files_newer_than_marker" operation.

Purpose: surface percolate coverage drift by listing every file under a
caller-supplied source directory whose mtime is newer than that directory's
`.percolate-ignore` marker file. Port of the `find <source_dir> -type f -newer
<source_dir>/.percolate-ignore` fence (skills/percolate/SKILL.md:206),
capped at a caller-supplied result limit (the oracle's fence hardcoded 20;
this op makes the cap a param instead).

Self-registration: importing this module calls
register_op("percolate.list_files_newer_than_marker", ...) as a side-effect —
same pattern as ops/ping.py. coordinator_core.ops.__init__ imports this
module, so the registration fires when `import coordinator_core.ops`
executes.

Wire params:
    source_dir  (str, required) — directory to scan. Resolved by the CALLER
                                   from the percolate TARGETS entry (mirrors
                                   percolate.run/percolate.validate_store's
                                   existing precedent, § op-classification
                                   audit scope-verdict) — this op never
                                   derives it from the caller's own worktree.
    limit       (int, optional)  — maximum number of paths to return.
                                   Defaults to 20 (the oracle fence's
                                   hardcoded cap).

Reply fields (result object in JSON-RPC response):
    files          (list[str]) — paths (relative to source_dir, POSIX-style
                                  separators, sorted ascending) of files newer
                                  than the marker, capped at `limit`.
    marker_missing (bool)      — True when `.percolate-ignore` does not exist
                                  under source_dir. Silent (no error, no
                                  drift panel) — matches the oracle fence's
                                  existing intended behavior of doing nothing
                                  when the marker is absent (§ distinct-ops-new
                                  risk-rationale column: "not a bug to fix in
                                  the port").

Idempotency (AC7): pure read-only directory walk — a second invocation with
identical on-disk state and identical params returns byte-identical output.
No state is mutated (§ op-classification audit idempotency-hazard column:
"none — read-only listing capped at 20 results; no state mutated").

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
               § Wave 2 — Low-risk new modules, "list" cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

_DEFAULT_LIMIT = 20
_MARKER_NAME = ".percolate-ignore"


def list_files_newer_than_marker(source_dir: str, limit: int = _DEFAULT_LIMIT) -> dict:
    """Return files under `source_dir` newer than its `.percolate-ignore` marker.

    Walks `source_dir` recursively (files only; the marker itself is
    excluded from its own drift listing). When the marker is missing,
    returns `{"files": [], "marker_missing": True}` without raising — this
    mirrors the oracle fence's silent no-drift-panel behavior for an absent
    marker rather than treating it as an error.

    Paths in `files` are POSIX-style (forward-slash), relative to
    `source_dir`, sorted ascending for deterministic output, and capped at
    `limit`.
    """
    root = Path(source_dir)
    marker = root / _MARKER_NAME

    if not marker.is_file():
        return {"files": [], "marker_missing": True}

    marker_mtime = marker.stat().st_mtime
    newer: list[str] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate == marker:
            continue
        if candidate.stat().st_mtime > marker_mtime:
            newer.append(candidate.relative_to(root).as_posix())

    newer.sort()
    return {"files": newer[:limit], "marker_missing": False}


@register_op("percolate.list_files_newer_than_marker")
def _list_files_newer_than_marker(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "percolate.list_files_newer_than_marker" handler.

    Required params: source_dir.
    Optional params: limit (defaults to 20).

    Returns: {files, marker_missing} (see module docstring "Reply fields").

    Raises ValueError (propagated as a JSON-RPC error) when source_dir is
    missing/empty, or when source_dir does not resolve to a directory.
    """
    source_dir = params.get("source_dir")
    if not source_dir:
        raise ValueError("percolate.list_files_newer_than_marker requires param: source_dir")

    if not Path(source_dir).is_dir():
        raise ValueError(
            f"percolate.list_files_newer_than_marker: source_dir is not a directory: {source_dir!r}"
        )

    limit = params.get("limit", _DEFAULT_LIMIT)

    return list_files_newer_than_marker(source_dir, limit=limit)
