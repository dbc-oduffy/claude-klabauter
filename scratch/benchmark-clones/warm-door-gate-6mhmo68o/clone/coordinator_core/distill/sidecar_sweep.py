"""
coordinator_core.distill.sidecar_sweep — process-scaffolding sidecar sweep logic.

Purpose: scans a directory tree for process-scaffolding sidecar files (the
prior-art-check / plan-coverage-check / docs-check / review / c0-findings /
node-map / phase0 suffixes, plus their timestamped variants) and emits
deletion-manifest rows for candidates that clear the shared active-reference
guard. Suffix matching delegates entirely to `_common.is_sidecar_filename`
(full-suffix anchored, e.g. `<stem>.review.md` — never a bare "review"
substring hit) — this module does not re-author the suffix enumeration.

Ordering invariant: a candidate is emitted as a deletion-manifest row ONLY
AFTER `_common.active_reference_guard` clears it (returns False — not
actively referenced under docs/ tasks/ archive/). A candidate that is still
actively referenced is reported separately as retained, never as a deletion
row.

Read-only invariant: this module performs no writes. It does not delete
files, does not mutate repo state, and does not maintain a durable store —
it only walks the filesystem (read) and shells out to ripgrep (read) via the
shared guard.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C2
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coordinator_core.wire_paths import rel_id

from coordinator_core.distill._common import (
    active_reference_guard_many,
    is_sidecar_filename,
)

__all__ = [
    "SidecarSweepResult",
    "find_sidecar_candidates",
    "sweep_sidecars",
]


@dataclass(frozen=True)
class SidecarSweepResult:
    """Result of a sidecar sweep over a directory tree.

    deletion_manifest: candidates that cleared the active-reference guard
        (safe to delete), each as {"path": <str, relative to repo_root>}.
    retained: candidates that matched a sidecar suffix but are still
        actively referenced under the guard's scope, each as
        {"path": <str>, "reason": "active-reference"}.
    """

    deletion_manifest: list[dict]
    retained: list[dict]


def find_sidecar_candidates(scan_root: Path) -> list[Path]:
    """Return all files under `scan_root` whose filename matches a known
    sidecar suffix (full-suffix-anchored) or the timestamped-variant regex.

    Directory traversal is recursive. Files are returned in sorted order
    (deterministic output) as absolute Paths.
    """
    if not scan_root.is_dir():
        return []
    candidates = [
        p for p in scan_root.rglob("*") if p.is_file() and is_sidecar_filename(p.name)
    ]
    return sorted(candidates)


def sweep_sidecars(scan_root: Path, repo_root: Path) -> SidecarSweepResult:
    """Sweep `scan_root` for sidecar candidates and gate each on the shared
    active-reference guard (scoped to docs/ tasks/ archive/ under
    `repo_root`).

    A candidate clears the guard (emitted as a deletion-manifest row) only
    when `active_reference_guard` returns False for its repo-root-relative
    path. A candidate still actively referenced is reported under
    `retained`, never emitted as a deletion row — the ordering invariant
    (guard clears BEFORE emission) is enforced by construction: the guard
    check happens before any row is appended to either list.
    """
    deletion_manifest: list[dict] = []
    retained: list[dict] = []

    # SECURITY-ADJACENT — every rel_str MUST be forward-slash. Each is fed to
    # `active_reference_guard_many`, which passes the whole set to `rg --fixed-strings`,
    # and every in-repo document spells its references with '/'. A native-separator
    # needle (`docs\plans\x.md` on Windows) therefore matches NOTHING, the guard
    # returns "no active references found", and a still-referenced sidecar is
    # reported DELETE-ELIGIBLE. That is the guard failing OPEN on Windows, not a
    # cosmetic id nit. Mirrors the same fix in distill/delete_guard.py.
    # rel_str is also emitted as the wire `path` field below, so POSIX is
    # correct for both uses.
    candidates = find_sidecar_candidates(scan_root)
    rel_strs: list[str] = []
    for candidate in candidates:
        try:
            rel_str = rel_id(candidate, repo_root)
        except ValueError:
            rel_str = candidate.as_posix()
        rel_strs.append(rel_str)

    # Batched: one `rg -f <patternfile>` call for the whole candidate set instead of
    # one `active_reference_guard` (and one `rg` process) per candidate — the
    # ordering invariant (guard clears BEFORE emission) is preserved because the
    # batched lookup still fully resolves before any row is appended below.
    referenced = active_reference_guard_many(rel_strs, repo_root)

    for rel_str in rel_strs:
        if referenced[rel_str]:
            retained.append({"path": rel_str, "reason": "active-reference"})
        else:
            deletion_manifest.append({"path": rel_str})

    return SidecarSweepResult(deletion_manifest=deletion_manifest, retained=retained)
