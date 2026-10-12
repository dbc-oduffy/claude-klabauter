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
it. This is a passive REPORT, mirroring `budget_breach_signal`'s posture -- it never deletes anything.

Cost shape (must stay cheap -- session-start orientation regen, not a
hot path): a FIXED set of conventional venv locations is stat'ed -- the repo
root's own `pyvenv.cfg`, `<root>/<name>/pyvenv.cfg`, and
`<root>/<child>/<name>/pyvenv.cfg` for `name` in `.venv`, `venv`, `env`,
`.env`. The repo root is listed once to name its children; no child is ever
listed, so a deep or unlisted directory is never read. No tree walk, no
subprocess. A venv under another name, or two or more levels down, is not
reported -- the venvs the retired provisioners created and
`python -m venv .venv` sit in the covered set.

`.git`, `node_modules` and `vendor` children are skipped.

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

_VENV_NAMES = (".venv", "venv", "env", ".env")

_SKIP_CHILDREN = frozenset({".git", "node_modules", "vendor"})

_MAX_NAMED = 5


def _scan(repo_root: Path) -> List[str]:
    hits: List[str] = []
    if (repo_root / "pyvenv.cfg").is_file():
        hits.append("pyvenv.cfg")
    children: List[str] = []
    with os.scandir(repo_root) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False) and entry.name not in _SKIP_CHILDREN:
                children.append(entry.name)
    for name in _VENV_NAMES:
        if (repo_root / name / "pyvenv.cfg").is_file():
            hits.append(f"{name}/pyvenv.cfg")
    for child in children:
        for name in _VENV_NAMES:
            if (repo_root / child / name / "pyvenv.cfg").is_file():
                hits.append(f"{child}/{name}/pyvenv.cfg")
    return sorted(hits)


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
