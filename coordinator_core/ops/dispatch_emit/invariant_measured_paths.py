"""Map from each repo-wide invariant test to the path globs it measures.

A scoped test run picks tests by diff and a hand commit runs none, so an invariant that reads the
whole tree goes red unnoticed. ``invariants_for_paths`` is the lookup (one optional file read, no
spawn) that ``retest_targeted`` and ``landing_integrity`` use to add every invariant whose measured
set a diff touches.

The seed table names only this repo's invariants. Another repo declares its own in
``invariant-measured-paths.json`` at its working-repo root, a JSON object ``{test nodeid:
[path globs]}``; a declared node replaces a seeded node of the same id. Trap: an entry whose test
file is absent under the root is dropped by ``existing_invariants``, so a foreign invariant never
needs to be seeded here.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

DECLARATION_FILE = "invariant-measured-paths.json"

SEED_MAP: dict[str, tuple[str, ...]] = {
    "coordinator_core/session/tests/test_machinery_paths.py::test_every_tracked_state_first_segment_is_on_the_allowlist": (
        "state/*",
        "docs/reference/state-corpus-allowlist.txt",
        "coordinator_core/session/machinery_paths.py",
    ),
    "coordinator_core/session/tests/test_no_hand_built_record_home_joins.py::test_no_new_hand_built_record_home_joins": (
        "coordinator_core/*.py",
        "coordinator_core/session/tests/test_no_hand_built_record_home_joins.py",
    ),
    "coordinator_core/tests/test_raw_writes_have_a_disposition.py::test_the_live_gate_is_green": (
        "coordinator_core/*.py",
        "coordinator_core/tests/test_raw_writes_have_a_disposition.py",
    ),
}


def load_map(root: Optional[Path] = None) -> dict[str, tuple[str, ...]]:
    """The seed table overlaid with the declaration file at ``root``. A missing, unreadable or
    malformed file, or a non-list value, contributes nothing."""
    table = dict(SEED_MAP)
    if root is None:
        return table
    try:
        declared = json.loads((Path(root) / DECLARATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return table
    if not isinstance(declared, Mapping):
        return table
    for node, globs in declared.items():
        if isinstance(node, str) and isinstance(globs, list) and all(isinstance(g, str) for g in globs):
            table[node] = tuple(globs)
    return table


def _is_test_module(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or "/tests/" in f"/{path}"


def _matches(path: str, globs: Iterable[str], node: str) -> bool:
    """fnmatch ``*`` crosses ``/``. A coordinator_core source-scan invariant measures non-test
    modules plus its own file, so a test file elsewhere does not select it."""
    if node.startswith("coordinator_core/") and _is_test_module(path):
        return path == node.split("::")[0]
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


def invariants_for_paths(paths: Sequence[str], root: Optional[Path] = None) -> list[str]:
    """Invariant node ids whose measured globs a path in ``paths`` matches, in map order."""
    normalized = [p.replace("\\", "/") for p in paths]
    return [
        node
        for node, globs in load_map(root).items()
        if any(_matches(p, globs, node) for p in normalized)
    ]


def existing_invariants(root: Path, nodes: Sequence[str]) -> list[str]:
    """``nodes`` whose test file exists under ``root``."""
    return [n for n in nodes if (Path(root) / n.split("::")[0]).is_file()]
