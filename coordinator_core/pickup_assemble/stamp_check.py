"""
coordinator_core.pickup_assemble.stamp_check — thin additive CLI verb wrapping
`compute_execution_stamp_match` (the `gates.execution_stamp_match` gate
surface) with an external, standalone entrypoint.

Purpose: `compute_execution_stamp_match` was reachable ONLY from inside
`brief()` (verified at review: the CLI was a closed 3-verb apply/drop/brief
table, no external caller could invoke the staleness-recompute-and-classify
check directly). This module adds `pickup-assemble stamp-check <plan-path>`,
resolving the repo root and the artifact's own frontmatter, then delegating
straight to `compute_execution_stamp_match` — zero behavior change to that
function or to the existing apply/brief/drop arms (additive-CLI).

Spec backlink: docs/plans/2026-07-24-computed-skills-b5-planning-cluster.md, chunk C3a
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.pickup_assemble import (
    EXIT_BUSINESS_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    _parse_fm_dict,
    compute_execution_stamp_match,
    resolve_repo_root,
)


def stamp_check(
    artifact_path: str, repo_root: Optional[Path] = None
) -> tuple[int, dict[str, Any]]:
    """Resolves the repo root and `artifact_path`'s own frontmatter, then
    calls `compute_execution_stamp_match(root, fm, artifact_path)` and
    returns its gate unpacked from the `(gate, target_path)` tuple.

    Returns `(exit_code, gate_dict)`: `EXIT_OK` with the
    `{verdict, stamped_sha, computed_sha, stamp_commit, delta_class,
    next_move}` gate on a computed hit; `EXIT_BUSINESS_FAIL` with an
    `{"error": ...}` object when the repo root can't be resolved, the
    artifact is unreadable/has no parseable frontmatter, or
    `compute_execution_stamp_match` returns `None` (the artifact carries
    neither its own `execution_authorized_sha` nor a `## Plan to Execute`
    pointer to a stamped plan).
    """
    root = repo_root or resolve_repo_root()
    if root is None:
        return EXIT_BUSINESS_FAIL, {"error": "could not resolve a git worktree root"}

    live_path = (
        Path(artifact_path)
        if Path(artifact_path).is_absolute()
        else root / artifact_path
    )
    if not live_path.is_file():
        return EXIT_BUSINESS_FAIL, {"error": f"{artifact_path}: not found"}

    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {"error": f"{artifact_path}: could not read ({exc})"}

    split = split_frontmatter(text)
    if split is None:
        return EXIT_BUSINESS_FAIL, {"error": f"{artifact_path}: no parseable frontmatter"}

    fm = _parse_fm_dict(split.fm_text)
    hit = compute_execution_stamp_match(root, fm, artifact_path)
    if hit is None:
        return EXIT_BUSINESS_FAIL, {
            "error": (
                f"{artifact_path} carries no execution_authorized_sha of its own and no "
                "'## Plan to Execute' pointer to a stamped plan"
            ),
        }

    gate, _target_path = hit
    return EXIT_OK, gate


def main_stamp_check(argv: list[str]) -> int:
    """`stamp-check <plan-path>` — parses argv, calls `stamp_check`, prints
    the gate as JSON on stdout, returns its exit code."""
    if not argv:
        print("usage: pickup-assemble stamp-check <plan-path>", file=sys.stderr)
        return EXIT_USAGE

    exit_code, gate = stamp_check(argv[0])
    print(json.dumps(gate, indent=2, sort_keys=True))
    return exit_code
