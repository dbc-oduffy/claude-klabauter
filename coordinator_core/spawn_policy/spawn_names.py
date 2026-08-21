"""Spawn call-name universe, keyed by the module a call must resolve to.

Single source of truth for "which `subprocess`/`os`/`asyncio` attribute names
spawn a child process" — moved out of
``coordinator_core/tests/test_no_bare_hot_path_spawn.py`` (2026-08-21, DR-345
widening) so a production detector
(``coordinator_core/write_guards/nudge_windows_subprocess_popup.py``) can
import the identical table rather than re-deriving it, without creating a
production-import-from-test-tree dependency. That gate's own module now
imports this dict back rather than defining it locally; its behavior is
unchanged by the move — see its docstring for what it asserts.

Deliberately NOT ``coordinator_core.spawn_policy.detect._RECOGNIZED`` (a
broader ``(module, funcname)`` pair set additionally covering
``os.exec*``/``os.spawn*``/``os.posix_spawn``/``pty.spawn``): that table
answers a different, wider question ("is this call site spawn-shaped at
all" for the shell-spawn regrowth gate) and widening either table to match
the other is a separate, deliberate decision — not a side effect of sharing
this one.

Negative-spec: ``os.system``/``os.popen`` carry NO ``creationflags``
parameter at all (Win32/CPython signature fact, not an omission here) — a
console-target call through either is unsuppressable by construction, not
merely unsuppressed. Every consumer of this table must treat the ``"os"``
family that way rather than searching its keywords for a suppression kwarg
that cannot exist.
"""

from __future__ import annotations

SPAWN_NAMES_BY_MODULE: dict[str, set[str]] = {
    "subprocess": {"run", "Popen", "check_output", "call", "check_call"},
    "os": {"system", "popen"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}
