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

Measured at the time of the split (macOS, median of 21 runs):
    before — dispatch import 65.0ms, 58 ``coordinator_core.*`` modules resident
    after  — dispatch import 50.9ms, 32 ``coordinator_core.*`` modules resident
    end-to-end PreToolUse(Bash) hook: 84.1ms -> 72.6ms
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GUARDS_DIR = pathlib.Path(__file__).resolve().parents[1] / "bash_guards"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The import the guards must not use at MODULE level. Anchored at column 0 on
#: purpose: a function-local (indented) import is deferred and costs nothing at
#: module-import time, which is the cost this split exists to remove.
#: ``check_test_suite_invocation`` legitimately does this — it reaches into
#: ``hooks.track_touched_files`` inside a function, on the rare subagent-identity
#: path only. Deferred imports on a HOT path would still be a defect, and are
#: caught by ``test_evaluating_a_benign_command_does_not_load_the_hooks_package``
#: below rather than by this source scan.
#: Three shapes, because all three trigger the same eager ``hooks/__init__``:
#: ``from coordinator_core.hooks... import x``, ``import coordinator_core.hooks...``,
#: and ``from coordinator_core import hooks`` (which reads as a plain name import but
#: pulls the package just the same, and which the first two alternatives do not match).
_FORBIDDEN_IMPORT = re.compile(
    r"^(?:from\s+coordinator_core\.hooks"
    r"|import\s+coordinator_core\.hooks"
    r"|from\s+coordinator_core\s+import\s+(?:[^\n#]*[\s,(])?hooks\b)",
    re.MULTILINE,
)


def test_no_bash_guard_module_imports_the_hooks_package() -> None:
    """Source-level check — gives a precise file:line on failure."""
    offenders = []
    for path in sorted(_GUARDS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text()
        for match in _FORBIDDEN_IMPORT.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line_no}: {line}")

    assert not offenders, (
        "bash_guards modules must not import coordinator_core.hooks — it eagerly "
        "imports 13 hook modules the guard chain never uses, on every Bash tool "
        "call. Import the envelope builders from coordinator_core._hook_envelope "
        "instead (same five functions, dependency-free leaf).\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


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
    )
    assert result.returncode == 0, (
        "probe failed to import the guard chain:\n" + result.stderr[-2000:]
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, (
        "importing coordinator_core.bash_guards.dispatch pulled "
        f"{len(leaked)} coordinator_core.hooks module(s) into sys.modules:\n  "
        + "\n  ".join(leaked)
        + "\n\nRun the source-level test in this file for the offending import line."
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
    )
    assert result.returncode == 0, (
        "probe failed to evaluate a benign command:\n" + result.stderr[-2000:]
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, (
        "evaluating a benign Bash command pulled coordinator_core.hooks module(s) "
        "into sys.modules via a deferred import on the hot path:\n  "
        + "\n  ".join(leaked)
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
