"""coordinator_core.ops.reachability_targets — registered-target resolver for review.reachability.

Entry: a dotted `coordinator_core.` module string a registration surface names. Surfaces are a file
ending `_registry_map.py`, an op/hook registry (`ops/__init__.py`, `hooks/__init__.py`) and
`hooks.json`. Verdicts read only the diff's `added` lines and stat the worktree: no spawn, no tree walk.

Negative-spec:
  - A test file is never a surface.
  - In `hooks.json` only a `command` line names a target.
  - A target is `reachable` when `<path>.py` or `<path>/__init__.py` exists, else `missing-target`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core.ops.reachability_registration import _OP_REGISTRY_TAILS

KIND = "registered-target"
_QUOTED = re.compile(r"""['"](coordinator_core(?:\.\w+)+)['"]""")
_BARE = re.compile(r"(?<![\w.])(coordinator_core(?:\.\w+)+)")


def _is_surface(rel: str) -> bool:
    return rel.endswith("_registry_map.py") or rel.endswith(_OP_REGISTRY_TAILS) or os.path.basename(rel) == "hooks.json"


def _targets(rel: str, lines: list[str]) -> list[str]:
    if os.path.basename(rel) == "hooks.json":
        found = [t for ln in lines if '"command"' in ln for t in _BARE.findall(ln)]
    else:
        found = [t for ln in lines for t in _QUOTED.findall(ln)]
    return list(dict.fromkeys(found))


def _exists(root: Path, dotted: str) -> bool:
    base = root / dotted.replace(".", "/")
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def registered_target_verdicts(root: Path, added: dict[str, list[str]]) -> dict[tuple[str, str, str], str]:
    """{("registered-target", dotted_target, surface_rel): "reachable" | "missing-target"}."""
    from coordinator_core.ops.review_reachability import _TEST_RE

    out: dict[tuple[str, str, str], str] = {}
    for rel, lines in added.items():
        if _TEST_RE.search(rel) or not _is_surface(rel):
            continue
        for target in _targets(rel, lines):
            out[(KIND, target, rel)] = "reachable" if _exists(root, target) else "missing-target"
    return out
