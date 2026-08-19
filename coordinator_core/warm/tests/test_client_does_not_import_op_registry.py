"""The warm client must not drag the op registry in at import time.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15.
Found by claude-klabauter-44, 2026-08-19.

WHAT THIS PROTECTS. `invoke/__main__.py` imports `warm.client`
unconditionally, ahead of its warm/cold branch — so anything this module
imports at module scope is paid by EVERY engine invocation: warm hit, cold
fallback, and `COORDINATOR_WARM=0` alike. `warm/client.py` used to do
`from coordinator_core.ops.ceremony.detached_spawn import spawn_detached` at
module scope, which executes `ops/__init__.py` and registers the whole op
surface. Measured on this box: 527 `coordinator_core` modules imported (316
of them `coordinator_core.ops.*`), ~330 ms over a ~40 ms interpreter floor,
against 4 modules and ~53 ms for `invoke.__main__` by itself.

The warm preamble was therefore paying, up front, the exact engine-import
cost warmth exists to avoid — which is why a confirmed warm hit measured no
faster than cold (221 ms vs 224 ms, CLI `ping`, 2026-08-19). It also undid
`invoke/__main__.py`'s own stated "No eager `import coordinator_core.ops`"
property through a package-`__init__` side door, where no reader of that
comment would look for it.

A prose comment did not hold that line. This test is the artifact that does.

NEGATIVE-SPEC:
  - Does NOT forbid importing `ops` inside a function — `client.spawn_detached`
    does exactly that, on the one branch that is about to start a server.
  - Does NOT assert a timing. Wall-clock on this box is load-dominated and a
    threshold would flake; the module count is the stable proxy for the same
    fact.
  - Does NOT cover the server's own startup path, which legitimately needs
    the registry.
"""

from __future__ import annotations

import subprocess
import sys

_COUNT_AFTER_CLIENT_IMPORT = (
    "import sys;"
    "import coordinator_core.warm.client;"
    "print(len([m for m in sys.modules if m.startswith('coordinator_core.ops')]))"
)


def _ops_modules_after(code: str) -> int:
    """Count in a FRESH interpreter, never this one: pytest has already
    imported much of the tree, so an in-process `sys.modules` check would
    read the suite's own imports and pass unconditionally."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_importing_the_warm_client_registers_no_ops():
    loaded = _ops_modules_after(_COUNT_AFTER_CLIENT_IMPORT)
    assert loaded == 0, (
        f"importing coordinator_core.warm.client pulled in {loaded} "
        "coordinator_core.ops.* module(s). Every engine invocation imports "
        "this module before deciding warm-vs-cold, so an ops import here is "
        "paid by all of them — including the warm hits it exists to make "
        "cheap. Move the import inside the function that needs it, keeping a "
        "module-level name for the tests that patch it."
    )


def test_the_spawn_seam_is_still_a_patchable_module_attribute():
    """The trap the fix had to avoid: inlining the import at the call site
    would bypass `test_client_fallback.py`'s five
    `monkeypatch.setattr(client, "spawn_detached", ...)` sites and let the
    suite spawn real detached processes."""
    from coordinator_core.warm import client

    assert callable(getattr(client, "spawn_detached", None)), (
        "warm.client.spawn_detached must remain a module-level callable — "
        "test_client_fallback.py substitutes it by name"
    )
