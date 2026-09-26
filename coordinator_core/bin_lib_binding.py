"""coordinator_core.bin_lib_binding — the one stdlib-only leaf that binds a
bare `import lib` inside a path-loaded `coordinator/bin` script to
`coordinator/bin/lib`, the package whose `__init__` is the single declared
`sys.path` bootstrap for the bin CLIs.

Why this exists: those scripts resolve their siblings with a bare `import
lib`, which the lib package's own docstring documents as working "because a
script's own directory is `sys.path[0]`". That holds when a CLI is EXECUTED.
It does not hold when something loads one by file path with
`importlib.util.spec_from_file_location` — and the failure is not a clean
`ModuleNotFoundError` for `lib`. On any box with pywin32 installed,
`site-packages/win32/lib` is an importable PEP 420 namespace package, so the
bare import SUCCEEDS, binds a third-party directory, runs no bootstrap, and
the error surfaces one line later on an unrelated-looking `import cc_invoke`.
A successful import that does nothing is the same fail-open shape as a
reader pointed at a retired root.

Two things are needed, and the second is the one that is easy to miss: the
bin directory has to be on `sys.path` AHEAD of site-packages, and any
foreign `lib` already bound in `sys.modules` has to be evicted — once
win32's is cached, no amount of path repair changes what `import lib`
returns. Eviction is scoped to a `lib` that is NOT ours; a correctly bound
one is left alone, so this stays idempotent and cannot thrash a warm server
that ~50 sessions share.

The two-root invariant: not every caller's bin directory is this engine's
own `coordinator/bin` — `workstream_complete._load_bin_module` and
`baton_assemble/apply.py` read the operator config's `claude_klabauter_bin`,
`hooks/assert_em_role.py` reads `CLAUDE_PLUGIN_ROOT`, and `sentinel.
probe_p22` takes its `claude_klabauter_root` as a parameter. Any of those may name a
different clone. Two clones bound durably in one warm process would thrash:
binding root B puts B first on `sys.path` and evicts A's `lib`; the next
bind of A finds A already on the path (behind B), evicts B's `lib`, and the
re-import of A binds A again — a live re-eviction loop. So
`ensure_bin_lib_bound` binds durably, and evicts a foreign `lib`, ONLY when
the given directory is THIS engine's own bin; for any other directory it is
a no-op that changes neither `sys.path` nor `sys.modules`.
`exec_module_bin_bound` is the loader-side answer for a caller whose root
may be foreign and which has no transient exec of its own: it calls
`ensure_bin_lib_bound` first (a no-op unless the root is ours), then either
runs `exec_module` directly (root is ours — already bound durably) or
inserts the given directory transiently for the duration of the exec and
removes it afterward (root is foreign).

Negative-spec:
    - Does NOT add `<bin>/lib` to `sys.path` itself. That stays
      `coordinator/bin/lib/__init__.py`'s own job — there is one bootstrap,
      not two competing ones.
    - Never durably binds any bin directory other than this engine's own.
    - Never evicts a `lib` whose `__path__` already contains ours.

Imports: stdlib only (`os`, `sys`, `threading`) — this module must be cheap
enough for a hook to import unconditionally (see the plan's importtime
census row; the old home of this helper, `workstream_complete`, cost 212ms
to import for a 15-line helper).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

_ENGINE_BIN_DIR = str(Path(__file__).resolve().parents[1] / "coordinator" / "bin")
_ENGINE_BIN_DIR_NORMCASED = os.path.normcase(os.path.realpath(_ENGINE_BIN_DIR))

_TRANSIENT_LOAD_LOCK = threading.Lock()


def ensure_bin_lib_bound(bin_dir: str) -> bool:
    if os.path.normcase(os.path.realpath(bin_dir)) != _ENGINE_BIN_DIR_NORMCASED:
        return False

    bin_dir = str(Path(bin_dir))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    bound = sys.modules.get("lib")
    if bound is None:
        return True
    bound_paths = [os.path.normcase(os.path.abspath(p)) for p in getattr(bound, "__path__", [])]
    ours = os.path.normcase(os.path.abspath(os.path.join(bin_dir, "lib")))
    if ours in bound_paths:
        return True
    del sys.modules["lib"]
    return True


def exec_module_bin_bound(loader: Any, module: ModuleType, bin_dir: str) -> None:
    """Runs `loader.exec_module(module)` so that a bare `import lib` inside
    `module` resolves to `bin_dir`'s own `lib` package, whether `bin_dir` is
    this engine's own bin (durable bind, § `ensure_bin_lib_bound`) or a
    caller-named foreign root (transient: `bin_dir` inserted at
    `sys.path[0]` for the duration of the call, then removed by value under
    `_TRANSIENT_LOAD_LOCK` — never a positional pop, since a concurrent
    caller's own insert could land between this insert and its restore)."""
    if ensure_bin_lib_bound(bin_dir):
        loader.exec_module(module)
        return

    with _TRANSIENT_LOAD_LOCK:
        added = bin_dir not in sys.path
        if added:
            sys.path.insert(0, bin_dir)
        try:
            loader.exec_module(module)
        finally:
            if added and bin_dir in sys.path:
                sys.path.remove(bin_dir)
