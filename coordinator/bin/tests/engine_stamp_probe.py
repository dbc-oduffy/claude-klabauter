from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_ENGINE_ROOT_VAR = "COORDINATOR_ENGINE_ROOT"

#: `coordinator_core.ipc`'s `_ENGINE_STAMP_RELATIVE_PARTS`. Restated rather than
_STAMP_PARTS = ("coordinator_core", "_engine_stamp")


def _stamped_dispatch_root() -> "str | None":
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

