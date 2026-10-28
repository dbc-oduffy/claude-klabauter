"""coordinator_core.ops.content_root_resolve — in-process plugin content-root
resolution for the NotebookLM preflight.

The shell shim (`resolve-coordinator-clone.cmd`) can exit 0 with empty stdout
under an agent shell, which leaves the preflight with `plugin_root=unresolved`.
`resolve_plugin_content_root` never shells out: it walks the same ladder in
process, then falls back to the clone root and finally to this engine's own
checkout. It is a plain function plus a `python -m` entry that always prints a
newline-terminated path; it registers no JSON-RPC op.

Consumer: the deep-research `nlm-preflight` stage, which must invoke
`python -m coordinator_core.ops.content_root_resolve` in place of the shim
(the prompt-template edit is DoE-owned).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from coordinator_core._content_root_primitive import content_root_for


def _from_content_ladder() -> Optional[str]:
    from coordinator_core.resolve_coordinator_clone import (
        ResolveCoordinatorCloneError,
        resolve_content_root,
    )

    try:
        return resolve_content_root()
    except ResolveCoordinatorCloneError:
        return None


def _from_clone_root() -> Optional[str]:
    from coordinator_core.resolve_coordinator_clone import (
        ResolveCoordinatorCloneError,
        resolve_clone_root,
    )

    try:
        clone = resolve_clone_root()
    except ResolveCoordinatorCloneError:
        return None
    found = content_root_for(clone)
    return str(found) if found is not None else None


def _from_engine_checkout() -> Optional[str]:
    found = content_root_for(Path(__file__).resolve().parents[2])
    return str(found) if found is not None else None


_RUNGS: Tuple[Tuple[str, Callable[[], Optional[str]]], ...] = (
    ("content-ladder", _from_content_ladder),
    ("clone-root", _from_clone_root),
    ("engine-checkout", _from_engine_checkout),
)


def resolve_plugin_content_root() -> Tuple[Optional[str], str]:
    """Return `(path, rung)` for the first rung yielding an existing directory,
    or `(None, "unresolved")`. A rung that raises or returns a non-directory
    is skipped, never fatal."""
    for name, rung in _RUNGS:
        try:
            candidate = rung()
        except Exception:
            continue
        if candidate and os.path.isdir(candidate):
            return candidate, name
    return None, "unresolved"


def main(argv: Optional[List[str]] = None) -> int:
    path, rung = resolve_plugin_content_root()
    if path is None:
        sys.stderr.write("content_root_resolve: no plugin content root found\n")
        return 1
    sys.stdout.write(path + "\n")
    sys.stderr.write(f"rung={rung}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
