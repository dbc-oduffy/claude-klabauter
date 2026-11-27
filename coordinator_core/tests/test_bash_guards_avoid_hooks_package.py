"""Pin the bash-guard chain's import graph away from ``coordinator_core.hooks``.

Why this test exists: ``coordinator_core.hooks.__init__`` eagerly imports 13 hook
modules for their ``register_op()`` side effects. That eagerness is deliberate and
correct for the IPC server (partial imports make the op-registry drift guards
order-dependent — see that ``__init__``'s own docstring), but the PreToolUse(Bash)
guard chain needs none of those registrations and pays for them on EVERY Bash tool
call in every session on every platform.

The guards need only the five return-envelope builders, which is why they live at
``coordinator_core._hook_envelope`` — a dependency-free leaf at the package root —
rather than under ``coordinator_core.hooks``.

The regression this guards against is SILENT: a single new guard module written with
the natural-looking ``from coordinator_core.hooks._envelope import deny`` re-drags the
whole package back in and every guard keeps working, just slower. That is exactly what
happened while the split was being made — one newly-added guard module held the old
import and reduced the entire optimization to zero until it was found by tracing.
Nothing about the guards' behaviour reveals it; only a measurement does. Hence a test.
The module-level source edge is pinned by ``test_import_layers.py``, which names the
offending file:line; the probes below catch what that scan cannot (deferred imports on
the hot path).

Measured at the time of the split (macOS, median of 21 runs):
    before — dispatch import 65.0ms, 58 ``coordinator_core.*`` modules resident
    after  — dispatch import 50.9ms, 32 ``coordinator_core.*`` modules resident
    end-to-end PreToolUse(Bash) hook: 84.1ms -> 72.6ms
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

def test_importing_the_dispatch_chain_does_not_load_the_hooks_package() -> None:
    """Behavioural check — the property that actually matters at runtime.

    Runs in a subprocess because an in-process assertion would be defeated by any
    earlier test in the session having already imported ``coordinator_core.hooks``.
    """
    probe = (
        "import coordinator_core.bash_guards.dispatch, sys; "
        "leaked = sorted(m for m in sys.modules "
        "if m == 'coordinator_core.hooks' or m.startswith('coordinator_core.hooks.')); "
        "print('\\n'.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    **no_console_creationflags())
    assert result.returncode == 0, (
        "probe failed to import the guard chain:\n" + result.stderr[-2000:]
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, (
        "importing coordinator_core.bash_guards.dispatch pulled "
        f"{len(leaked)} coordinator_core.hooks module(s) into sys.modules:\n  "
        + "\n  ".join(leaked)
        + "\n\nRun coordinator_core/tests/test_import_layers.py for the offending import line."
    )


def test_evaluating_a_benign_command_does_not_load_the_hooks_package() -> None:
    """The hot-path property: a real PreToolUse(Bash) evaluation stays clear too.

    The import-time test above cannot see a function-local import on a code path
    that actually runs per Bash call. This one drives the chain end to end on a
    benign command and asserts the package is still absent afterwards.
    """
    probe = (
        "import json, sys; "
        "from coordinator_core.bash_guards.dispatch import evaluate_payload_json; "
        "payload = json.dumps({'tool_name': 'Bash', "
        "'tool_input': {'command': 'echo hi'}, 'session_id': 'test', 'cwd': '.'}); "
        # The verdict itself is deliberately NOT asserted. What matters here is that
        # driving a real evaluation leaves the hooks package unloaded; which envelope a
        # given command earns is a different invariant, owned by the guards' own tests.
        # Pinning it here would break this test whenever a guard legitimately starts
        # advising on a trivial command, for a reason unrelated to import hygiene.
        "evaluate_payload_json(payload); "
        "leaked = sorted(m for m in sys.modules "
        "if m == 'coordinator_core.hooks' or m.startswith('coordinator_core.hooks.')); "
        "print('\\n'.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    **no_console_creationflags())
    assert result.returncode == 0, (
        "probe failed to evaluate a benign command:\n" + result.stderr[-2000:]
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, (
        "evaluating a benign Bash command pulled coordinator_core.hooks module(s) "
        "into sys.modules via a deferred import on the hot path:\n  "
        + "\n  ".join(leaked)
        + "\n\nRun coordinator_core/tests/test_import_layers.py for the offending import line."
    )


def test_hooks_envelope_shim_still_serves_the_historical_path() -> None:
    """The relocation must not break in-package hook modules or existing callers."""
    from coordinator_core import _hook_envelope
    from coordinator_core.hooks import _envelope as shim

    # Derived from the shim's own __all__ rather than a hardcoded tuple: a sixth builder
    # (rewrite_input) was added to both modules shortly after this test landed, and a
    # literal list would have silently stopped covering the full re-export surface.
    assert shim.__all__, "the shim declares no __all__ — nothing would be checked"
    for name in shim.__all__:
        assert getattr(shim, name) is getattr(_hook_envelope, name), (
            f"{name} re-exported from coordinator_core.hooks._envelope is not the "
            "same object as the canonical definition in coordinator_core._hook_envelope"
        )
