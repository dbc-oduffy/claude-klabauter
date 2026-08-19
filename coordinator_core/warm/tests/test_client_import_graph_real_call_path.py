"""AC5 guard: the REAL call path's import graph, not an isolated `warm.client` import.

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ Hard constraints (AC5), § C9.

WHAT THIS PINS. `coordinator_core.invoke.__main__.main()` imports BOTH
`coordinator_core.invoke.dispatch` (for `dispatch_message`/`STRUCTURAL_PIN_ERROR`)
AND `coordinator_core.warm.client` (for `try_warm_dispatch`) before it decides
whether a given call is served warm or cold. `test_client_does_not_import_op_registry.py`
already pins that importing `warm.client` ALONE registers no
`coordinator_core.ops.*` modules -- but that is an ISOLATED import: it never
exercises `invoke.dispatch`/`coordinator_core.ipc`, the other half of what
every real invocation actually imports ahead of the warm/cold branch. AC5
calls that isolated form "the weaker test" and asks for a guard against the
REAL combined import graph instead, so that a future change making
`invoke.dispatch` (or the `coordinator_core.ipc` module it re-exports from)
eagerly pull the op registry -- "pulled ahead of the warm branch" -- is
caught here even though `warm.client` in isolation would still look clean.

Counted in a FRESH interpreter, never this one: pytest has already imported
much of the tree, so an in-process `sys.modules` check would read the
suite's own imports and pass unconditionally (same rationale as
`test_client_does_not_import_op_registry.py`).

NEGATIVE-SPEC:
    - Does NOT assert a timing -- module count is the stable proxy, matching
      the sibling isolated-import test's own convention.
    - Does NOT call `main()` or dispatch a real op -- `invoke.__main__`'s own
      module docstring guarantees "All coordinator_core imports are deferred
      inside main()", so importing BOTH modules at top level (mirroring the
      two import statements `main()` issues before its warm/cold branch) is
      sufficient to exercise the graph this row is about, with no os._exit
      or event-loop machinery to work around.
    - Does NOT cover the server's own startup path, which legitimately needs
      the registry (same carve-out as the sibling isolated test).
"""

from __future__ import annotations

import subprocess
import sys

_COUNT_AFTER_REAL_CALL_PATH_IMPORTS = (
    "import sys;"
    "import coordinator_core.invoke.dispatch;"
    "import coordinator_core.warm.client;"
    "print(len([m for m in sys.modules if m.startswith('coordinator_core.ops')]))"
)


def _ops_modules_after(code: str) -> int:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_the_real_call_paths_combined_imports_register_no_ops():
    loaded = _ops_modules_after(_COUNT_AFTER_REAL_CALL_PATH_IMPORTS)
    assert loaded == 0, (
        f"the combined import graph {'invoke.dispatch', 'warm.client'} pulled "
        f"in {loaded} coordinator_core.ops.* module(s). invoke.__main__.main() "
        "imports both of these modules ahead of its warm/cold branch on every "
        "invocation -- an isolated `import coordinator_core.warm.client` alone "
        "passing tells you nothing about whether `invoke.dispatch` (or the "
        "`coordinator_core.ipc` module it draws from) is the one that pulled "
        "the registry ahead of the warm branch. See AC5, "
        "docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md."
    )


def test_invoke_dispatch_alone_registers_no_ops():
    """Isolates the OTHER half: if this one alone fails while the combined
    test above passes (impossible, since 0 <= combined), the reader still
    gets a directly-actionable signal pointing at `invoke.dispatch`/`ipc`
    specifically rather than at the pair."""
    loaded = _ops_modules_after(
        "import sys;"
        "import coordinator_core.invoke.dispatch;"
        "print(len([m for m in sys.modules if m.startswith('coordinator_core.ops')]))"
    )
    assert loaded == 0, (
        f"importing coordinator_core.invoke.dispatch alone pulled in {loaded} "
        "coordinator_core.ops.* module(s) -- this is the module invoke.__main__ "
        "imports ahead of its warm/cold branch, so an eager ops import here is "
        "paid by every invocation, warm hits included. See AC5."
    )
