"""Is there a stamped engine on this box, and where?

Split out of this directory's `conftest.py` so a unittest-style suite can
import it by name. A `conftest` import resolves to the NEAREST conftest on
`sys.path` -- `coordinator/bin/conftest.py`, not this directory's -- so the
helper cannot live only in the conftest and still be reachable from a
`TestCase` method, which cannot request a pytest fixture by parameter.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_ENGINE_ROOT_VAR = "COORDINATOR_ENGINE_ROOT"

#: Repo-relative parts to an engine build stamp, mirroring
#: `coordinator_core.ipc`'s `_ENGINE_STAMP_RELATIVE_PARTS`. Restated rather than
#: imported: importing `coordinator_core` here to ask the question would bind the
#: package from whichever tree pytest happens to have on `sys.path` first, which
#: is the very ambiguity this fixture exists to remove.
_STAMP_PARTS = ("coordinator_core", "_engine_stamp")


def _stamped_dispatch_root() -> "str | None":
    """The box's own dispatch engine root, or None when it is absent or unstamped.

    Asks `cc_invoke`'s existing dispatch ladder -- the same seam a real caller
    goes through -- never a hardcoded sibling path: which clone is the published
    engine is a property of the box, and a literal here would be wrong on every
    machine but one.
    """
    try:
        import cc_invoke
    except Exception:
        return None
    try:
        root = cc_invoke._resolve_claude_klabauter_root()
    except Exception:
        return None
    if not root:
        return None
    try:
        if Path(root).joinpath(*_STAMP_PARTS).stat().st_size <= 0:
            return None
    except OSError:
        return None
    return str(root)


