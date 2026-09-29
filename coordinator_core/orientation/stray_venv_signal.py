"""coordinator_core.orientation.stray_venv_signal -- surfaces any
`pyvenv.cfg` found under a fleet repo root, the on-disk fingerprint of a
per-repo or shared Python venv.

Why this exists: PM directive 2026-09-29 bans per-repo/shared venvs
fleet-wide (system interpreter only) and the same chunk that adds this
detector retires `coordinator_core`'s own fleet-env/ensure-venv
provisioners. A provisioner disappearing does not un-provision what a
prior session already built -- a `pyvenv.cfg` left behind by an earlier
`fleet-env`/`ensure-venv`/hand-run `python -m venv` is a rogue venv this
box is still silently running against until someone notices and removes
it. This is a passive REPORT, mirroring `abandoned_claim_signal`'s and
`budget_breach_signal`'s posture -- it never deletes anything.

Cost shape (must stay cheap -- session-start orientation regen, not a
hot path): ONE bounded `os.walk` from `repo_root`, pruning `.git`,
`node_modules`, and any `vendor/` path segment before descending into
them (never AFTER, so the pruned subtrees are never even listed) --
exactly the same prune-before-descend shape `lifecycle.py`'s own
`os.walk(pkg_dir, ...)` uses. No subprocess, no per-file stat beyond
what `os.walk` itself already does, no recursive `Path.rglob` (which
cannot prune mid-walk the way `os.walk`'s `dirnames[:] = ...` in-place
rewrite can).

`vendor/` exclusion is a PATH-SEGMENT test (`"vendor" in path.parts`),
not a substring test on the joined path string -- so a repo genuinely
named e.g. `vendorized-tools/pyvenv.cfg` at the repo root (no `vendor`
SEGMENT) still reports, while `anything/vendor/sub/pyvenv.cfg` at any
depth does not, matching the brief's "excluding anything under a
`vendor/` path segment" wording exactly.

Fail-open throughout: an unreadable directory, a permission error, or
any exception at all resolves to `""` (section omitted) -- the same
posture every other `emit_*` helper in `regenerate_cache.py` uses; this
module never raises into orientation regen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

GENERATES = []

_PRUNE_DIRNAMES = frozenset({".git", "node_modules"})

_MAX_NAMED = 5


def _scan(repo_root: Path) -> List[str]:
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root, onerror=lambda _exc: None):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRNAMES and d != "vendor"]
        if "pyvenv.cfg" in filenames:
            cfg_path = Path(dirpath) / "pyvenv.cfg"
            try:
                rel = cfg_path.relative_to(repo_root)
            except ValueError:
                rel = cfg_path
            hits.append(str(rel))
    hits.sort()
    return hits


def emit_stray_venvs(repo_root: Path) -> str:
    try:
        repo_root = Path(repo_root)
        if not repo_root.is_dir():
            return ""

        hits = _scan(repo_root)
        if not hits:
            return ""

        named = hits[:_MAX_NAMED]
        lines = [
            f"- ⚠ {len(hits)} `pyvenv.cfg` found under this repo root -- per-repo/shared "
            f"venvs are banned fleet-wide (system interpreter only). Remove the stray "
            f"venv director{'y' if len(hits) == 1 else 'ies'}:"
        ]
        for rel in named:
            lines.append(f"  - `{rel}`")
        if len(hits) > len(named):
            lines.append(f"  - …and {len(hits) - len(named)} more")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — fail-open; an orientation section never breaks regen
        return ""
