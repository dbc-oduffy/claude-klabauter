"""The POSIX door's stdin-reading-entrypoint gate: source-level, not compiled.

Spec backlink: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md
§ C4 (mirrors § C3's Windows twin, `door.c`'s own
`door_basename_declares_stdin_read` gate).

WHY THIS FILE NEVER SPAWNS A PROCESS. `door_posix.c` cannot be compiled or
run on this box (Windows) -- see `test_params_file_stdin_route.py`'s own
module docstring: "The source legs cover the half no Windows binary can
answer." Both directions the plan's exit criterion asks for -- a declared
basename takes the cold leg, an undeclared one still goes warm -- are
asserted structurally against the source text instead: the gate's condition,
its placement relative to the transport dial, and the fact that the function
body continues past the gate into the warm path rather than falling through
unconditionally.

THE FAIL-DIRECTION ASYMMETRY THIS FILE PINS. The argv-shaped gate at 0a
degrades WARM when its own precondition (a renderable argv) fails; this gate
degrades COLD when ITS precondition (a resolved own-basename) fails -- the
opposite direction, for the reason `door_posix.c`'s own comment gives: an
unresolved basename means the table is being asked about the wrong name
entirely, not that the argv gate's failure mode of "this argv could not be
rendered" applies. A test asserting only the declared/undeclared split and
missing this asymmetry would pass a "fix" that quietly asks the table about
`DOOR_DEFAULT_ENTRYPOINT` on every `resolve_own_basename()` failure.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"
_DOOR_CORE_H = _DOOR_DIR / "door_core.h"
_DOOR_CORE_C = _DOOR_DIR / "door_core.c"

_PREDICATE = "door_basename_declares_stdin_read"
# The door calls the invocation-level form (the basename table narrowed by
# subcommand scope, `door_core.h`); the basename predicate is its inner step.
_GATE_CALL = "door_invocation_declares_stdin_read("


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_predicate_is_shared_not_reimplemented():
    """The table lives once, in `door_core.c`/`door_core.h`; `door_posix.c`
    must call it rather than hand-rolling a second name list that could
    silently diverge from `door.c`'s."""
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_H)
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_C)
    assert f"int {_GATE_CALL}" in _read(_DOOR_CORE_H)
    source = _read(_DOOR_POSIX_C)
    assert _GATE_CALL in source, (
        "door_posix.c does not call door_invocation_declares_stdin_read -- "
        "it will still dispatch a stdin-reading entrypoint warm into a pool "
        "worker whose stdin is None"
    )


def test_the_gate_precedes_the_transport():
    """Pre-delivery is the whole property: a gate placed after the socket
    dial can still hand the request to a live server before falling
    through."""
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index(_GATE_CALL, main_at)
    connect_at = source.index("connect_socket(sock_path)", main_at)
    assert gate_at < connect_at, (
        "door_posix.c consults door_invocation_declares_stdin_read only after "
        "dialling the socket -- the request can still be delivered warm"
    )


def test_the_gate_excludes_hook_mode():
    """Hook mode's stdin is already drained above this gate, and its own
    fall-through disposition is a deny envelope, not a cold spawn -- the
    same exclusion the argv-shaped 0a gate applies."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_GATE_CALL)
    # The nearest enclosing `if` above the call must condition on
    # `!g_door_hook_mode`, matching the 0a gate's own wiring.
    if_at = source.rindex("if (", 0, gate_at)
    condition = source[if_at:gate_at]
    assert "!g_door_hook_mode" in condition


def test_the_fail_direction_is_cold_on_unresolved_basename():
    """`g_own_basename_ok == 0` means this image's own name could not be
    resolved, so `door_entrypoint_basename()` would answer for the pre-C0
    default rather than the name actually invoked -- asking the table about
    the wrong name is unsafe, so this must take the COLD leg
    UNCONDITIONALLY, the opposite fail direction from the argv gate's own
    (which stays warm on its own precondition failure). Asserted as an
    `||` disjunct with the declared-name predicate, both inside the SAME
    `if` that reaches `fall_through`, so neither condition alone decides."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_GATE_CALL)
    if_at = source.rindex("if (", 0, gate_at)
    # The `if` header runs until the line ending in `{`.
    header_end = source.index("{", gate_at)
    header = source[if_at:header_end]
    assert "!g_own_basename_ok" in header, (
        "the gate does not fall through cold when this image's own basename "
        "could not be resolved -- door_entrypoint_basename() would then "
        "answer for the wrong name and the table would be asked about it"
    )
    assert "||" in header, (
        "the unresolved-basename check and the declared-name predicate must "
        "be OR'd together in one gate, not two separate ifs"
    )

    body_start = header_end
    body_end = source.index("}", body_start)
    body = source[body_start:body_end]
    assert "fall_through(argc, argv, engine_root)" in body


def test_an_undeclared_name_falls_through_the_gate_and_stays_reachable():
    """A gate that always takes the cold leg is not a gate. Structurally:
    the function must continue past this `if` block into the warm-path
    steps (socket derivation, `connect_socket`) rather than returning
    unconditionally -- so a basename NOT in the table (and a resolved own
    basename) reaches the transport dial instead of a guaranteed
    fall-through."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_GATE_CALL)
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    after_gate = source[body_end + 1 :]
    # Warm-path steps 1-5 (identity/token/clone-hash/socket/connect) still
    # follow the gate in source order -- the gate is not the last statement
    # in `main` before an unconditional return.
    assert "connect_socket(sock_path)" in after_gate


def _respawn_arm(source: str) -> str:
    start = source.index("RESPAWN ON MISS -- the no-server branch only")
    return source[start : source.index("free(engine_root);", start)]


def test_dead_engine_non_hook_arm_turns_the_cold_boot_wait_off():
    """Non-hook cold child: boot wait 0, after the spawn, caller value wins."""
    source = _read(_DOOR_POSIX_C)
    arm = _respawn_arm(source)
    assert "#define DOOR_COLD_BOOT_WAIT_SECS \"0\"" in source
    assert "#define DOOR_COLD_BOOT_WAIT_ENV \"COORDINATOR_WARM_BOOT_WAIT_SECS\"" in source
    assert source.index("#define DOOR_DRAIN_WAIT_MS") < source.index(
        "#define DOOR_COLD_BOOT_WAIT_SECS"
    )
    assert arm.index("door_maybe_spawn_server(") < arm.index("setenv(DOOR_COLD_BOOT_WAIT_ENV")
    assert arm.index("setenv(") < arm.index("fall_through(argc, argv, engine_root)")
    assert "DOOR_COLD_BOOT_WAIT_SECS, 0)" in re.sub(r"\s+", " ", arm)
    assert "!g_door_hook_mode" in arm


def test_dead_engine_boot_wait_setenv_is_only_in_the_respawn_arm():
    """Hook leg and warm path untouched: the only setenv of the budget."""
    source = _read(_DOOR_POSIX_C)
    assert source.count("setenv(DOOR_COLD_BOOT_WAIT_ENV") == 1


# Behavior: the compiled door against a dead engine, cold child echoing its env.
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from coordinator_core.warm.tests.test_door_read_deadline_posix import (  # noqa: E402,F401 -- fixture
    _make_stub_engine_root,
    runtime_base,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

_WAIT_ENV = "COORDINATOR_WARM_BOOT_WAIT_SECS"
_ECHO_COLD = (
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    f"print(json.dumps({{'wait': os.environ.get({_WAIT_ENV!r})}}))\n"
)
_HOOK_PAYLOAD = b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo hi"}}'

_behavior = [
    pytest.mark.spawns_process,
    pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary"),
]


@pytest.fixture(scope="module")
def _door(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


def _dead_engine_wait(door, tmp_path, runtime_base, *, hook=False, caller=None):
    root = _make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(_ECHO_COLD, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != _WAIT_ENV}
    env.update(COORDINATOR_DOOR_ENGINE_ROOT=str(root), COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base))
    if caller is not None:
        env[_WAIT_ENV] = caller
    argv = [str(door), "hooks.preuse_bash_dispatch" if hook else "some.op"]
    if hook:
        env["COORDINATOR_DOOR_STDIN_MODE"] = "hook"
    proc = subprocess.run(
        argv, input=_HOOK_PAYLOAD if hook else b"", capture_output=True, env=env,
        cwd=str(root), timeout=60, **no_console_creationflags(),
    )
    return proc


@pytest.mark.parametrize("caller, expected", [(None, "0"), ("7", "7")], ids=["unset-gets-0", "caller-wins"])
def test_dead_engine_non_hook_cold_child_sees_the_budget(_door, tmp_path, runtime_base, caller, expected):
    """Runtime dir absent (no chosen service dir) and no server: cold child env is the budget."""
    proc = _dead_engine_wait(_door, tmp_path, runtime_base, caller=caller)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.decode().strip().splitlines()[-1])["wait"] == expected


def test_dead_engine_hook_cold_child_is_not_handed_the_budget(_door, tmp_path, runtime_base):
    """Refusal side: hook mode never gets the variable from the door."""
    root = _make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        f"    'permissionDecision': 'allow', 'permissionDecisionReason': repr(os.environ.get({_WAIT_ENV!r}))}}}}))\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != _WAIT_ENV}
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    proc = subprocess.run(
        [str(_door), "hooks.preuse_bash_dispatch"], input=_HOOK_PAYLOAD, capture_output=True,
        env=env, cwd=str(root), timeout=60, **no_console_creationflags(),
    )
    hso = json.loads(proc.stdout.decode().strip())["hookSpecificOutput"]
    assert hso["permissionDecisionReason"] == "None"
