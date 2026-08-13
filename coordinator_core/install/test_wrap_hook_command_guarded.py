"""
coordinator_core.install.test_wrap_hook_command_guarded — exit-code-hygiene
tests for `wrap_hook_command_guarded` (coordinator_core.install._shared).

Verifies by OBSERVATION, not string-shape assertion alone: every guard here
is actually run via `sh -c` and its real exit code checked. The binding
constraint (2026-07-28 leg-0 handoff): "every guard must be watched blocking
something *and* passing something" — an unresolvable
`COORDINATOR_CONTENT_ROOT` must exit 127 (PreToolUse non-blocking warning),
never 2 (PreToolUse BLOCKING DENY — the sentinel that collided with a
file-not-found exit and bricked a machine four times on 2026-07-28), and a
genuine policy deny from the wrapped script must still surface as 2 (the
guard must not swallow real denies).

Spec backlink: coordinator_core/install/_shared.py `wrap_hook_command_guarded`
Port/incident backlink: docs/plans/2026-07-04-doe-maximalist-execution-plugin-dir.md § M1
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install._shared import (
    COORDINATOR_CONTENT_ROOT_ENV_KEY,
    COORDINATOR_PYTHON_BIN_ENV_KEY,
    _cmd_path,
    _split_hook_command,
    wrap_hook_command_guarded,
)
from coordinator_core.install.gen_settings_hooks import generate
from coordinator_core.win_portability import no_console_creationflags

# Real `sh -c` execution is load-bearing per this file's own module
# docstring: every guard's exit code is verified by observation, not
# string-shape assertion -- the exit-127-vs-2 sentinel collision this suite
# exists to catch can only be proven by a real shell exit code. Already
# POSIX-only (skipif below), so no Windows spawn-cost concern.
pytestmark = [
    pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX `sh -c` execution tests — PowerShell shape is asserted structurally, not executed, on Windows (see test_guarded_powershell_shape_only below).",
    ),
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _run(command: str, env: dict) -> subprocess.CompletedProcess:
    # This whole module is skipped on Windows (`pytestmark` above) since it
    # execs POSIX `sh`; `no_console_creationflags()` is a no-op here but keeps
    # the call shape consistent with `_shared.py`'s own convention.
    return subprocess.run(
        ["sh", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        **no_console_creationflags(),
    )


@pytest.fixture()
def passing_script(tmp_path: Path) -> Path:
    script = tmp_path / "hooks" / "scripts" / "passes.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.exit(0)\n")
    return script


@pytest.fixture()
def denying_script(tmp_path: Path) -> Path:
    """A script that raises a GENUINE policy deny — exit 2 — which the
    guard must let through unchanged (never swallow a real deny)."""
    script = tmp_path / "hooks" / "scripts" / "denies.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.exit(2)\n")
    return script


def _guarded_command_for(script: Path) -> str:
    # `python3` (bareword, matching `_INTERPRETER_PREFIXES`) — NOT
    # `sys.executable`, which on this dev machine resolves to a versioned
    # absolute path (e.g. `/opt/homebrew/opt/python@3.14/bin/python3.14`)
    # that `_split_hook_command` correctly refuses to recognize as an
    # interpreter prefix (real hooks.json entries only ever use the bare
    # `python3`/`python`/`bash`/`node` interpreter names).
    raw = f"python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/{script.name}"
    return wrap_hook_command_guarded(raw, windows=False)


def test_unset_content_root_exits_127_not_2(passing_script: Path):
    env = {k: v for k, v in os.environ.items() if k != COORDINATOR_CONTENT_ROOT_ENV_KEY}
    result = _run(_guarded_command_for(passing_script), env=env)
    assert result.returncode == 127
    assert result.returncode != 2


def test_empty_content_root_exits_127(passing_script: Path):
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = ""
    result = _run(_guarded_command_for(passing_script), env=env)
    assert result.returncode == 127
    assert result.returncode != 2


def test_content_root_pointing_at_nonexistent_script_exits_127(tmp_path: Path):
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(tmp_path / "nonexistent-root")
    command = wrap_hook_command_guarded(
        "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/absent.py", windows=False
    )
    result = _run(command, env=env)
    assert result.returncode == 127
    assert result.returncode != 2


def test_real_root_and_existing_script_exits_0(passing_script: Path):
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(passing_script.parents[2])
    result = _run(_guarded_command_for(passing_script), env=env)
    assert result.returncode == 0


def test_genuine_deny_passes_through_as_2_not_swallowed(denying_script: Path):
    """The guard must not turn a REAL policy deny into anything else — the
    resolution-failure guard and the hook's own deny sentinel share exit 2
    on this harness, so this is the case that proves the guard only
    intercepts RESOLUTION failures, not the wrapped script's own decisions."""
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(denying_script.parents[2])
    result = _run(_guarded_command_for(denying_script), env=env)
    assert result.returncode == 2


def test_guard_costs_zero_extra_spawns_via_exec(passing_script: Path):
    """`exec` replaces the guard shell rather than forking — assert the
    guarded command actually contains the `exec` builtin immediately before
    the interpreter invocation (the zero-extra-spawn property this design
    exists for), not just that it exits 0."""
    command = _guarded_command_for(passing_script)
    assert "&& exec " in command
    assert "$(" not in command  # no command substitution anywhere (extra spawn)


# ---------------------------------------------------------------------------
# PowerShell shape — structural assertion only (no `pwsh`/`powershell.exe`
# execution harness on this platform); the POSIX suite above is the one
# empirically fired against a real shell for the exit-code contract.
# ---------------------------------------------------------------------------


def test_guarded_powershell_shape_only():
    raw = "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag"
    command = wrap_hook_command_guarded(raw, windows=True)
    assert command.startswith("if ($env:COORDINATOR_CONTENT_ROOT -and (Test-Path -LiteralPath ")
    assert "; exit $LASTEXITCODE } else { exit 127 }" in command
    # No `exec` (PowerShell has none) — one extra process vs. POSIX, flagged
    # in the function's own docstring, not hidden.
    assert "exec " not in command
    # Round-trips through _cmd_path exactly like the POSIX form.
    assert _cmd_path(command) == "$env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag"


# ---------------------------------------------------------------------------
# Idempotency + no-duplication at the generator level — the trap named in
# the dispatch brief: if `_cmd_path` didn't understand the guarded shape,
# every generated group would stop classifying as generated, get
# "preserved" verbatim, and the next regeneration would APPEND a second
# copy of all hooks. Silent duplication, not a loud error.
# ---------------------------------------------------------------------------


def _oracle_fixture_paths():
    claude_klabauter_root = Path(__file__).resolve().parents[2]
    colocated = claude_klabauter_root / "coordinator" / "bin" / "fixtures" / "gen-settings-hooks"
    if (colocated / "hooks.json").is_file():
        return colocated / "hooks.json"
    from coordinator_core.testing.doe_root import resolve_doe_root

    doe_root = Path(resolve_doe_root() or "/doe-root-unresolved")
    doe_fixtures = doe_root / "coordinator" / "bin" / "fixtures" / "gen-settings-hooks"
    if (doe_fixtures / "hooks.json").is_file():
        return doe_fixtures / "hooks.json"
    raise RuntimeError("test_wrap_hook_command_guarded: oracle hooks.json fixture not found")


@pytest.fixture()
def coordinator_root(tmp_path: Path) -> Path:
    root = tmp_path / "coordinator"
    (root / "hooks").mkdir(parents=True)
    # 2026-07-28 polarity inversion (landed concurrently in
    # gen_settings_hooks.py / test_gen_settings_hooks.py while this dispatch
    # was in flight): generation now defaults OFF absent the positive
    # per-machine marker — see gen_settings_hooks.ensure_positive_marker and
    # the sibling fixture's matching comment in test_gen_settings_hooks.py.
    (root.parent / ".coordinator-hooks-enabled").touch()
    return root


def _hook_count(settings: dict) -> int:
    return sum(
        len(group.get("hooks", []) or [])
        for groups in (settings.get("hooks") or {}).values()
        for group in groups
    )


def test_regenerate_over_already_guarded_settings_is_idempotent_byte_identical(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings.json"
    hooks_json = _oracle_fixture_paths()

    generate(out_path=str(out_path), hooks_json_override=str(hooks_json), coordinator_root_override=str(coordinator_root))
    pass1 = out_path.read_bytes()

    generate(out_path=str(out_path), hooks_json_override=str(hooks_json), coordinator_root_override=str(coordinator_root))
    pass2 = out_path.read_bytes()

    assert pass1 == pass2


# ---------------------------------------------------------------------------
# code-reviewer F1 (2026-07-28) — the unwrap regexes must be anchored at the
# START of the command and require the FULL leading guard-condition literal,
# not just the trailing invocation shape. A hand-authored hook that merely
# ENDS in a coincidentally similar shape must NOT be unwrapped/misclassified.
# ---------------------------------------------------------------------------


def test_hand_authored_lookalike_command_is_not_unwrapped():
    # Deliberately ends in the exact trailing shape `_GUARDED_POSIX_RE` used
    # to match unanchored — but its leading clause is NOT the guard's
    # `[ -n "..." ] && [ -f "..." ] &&` literal, so an anchored regex must
    # refuse to unwrap it.
    hand_authored = 'some-precondition && exec bash "/opt/my-tool.sh" || exit 127'
    assert _cmd_path(hand_authored) == hand_authored


def test_hand_authored_lookalike_powershell_command_is_not_unwrapped():
    hand_authored = (
        'if (some-other-condition) { python3 "/opt/my-tool.py"; exit $LASTEXITCODE } '
        'else { exit 127 }'
    )
    assert _cmd_path(hand_authored) == hand_authored


# ---------------------------------------------------------------------------
# code-reviewer F2 (2026-07-28) — the interpreter allowlist is a stated,
# closed contract; an unrecognized interpreter must fail loud with a message
# naming the trigger as an unrecognized hooks.json interpreter, not "a
# generator bug".
# ---------------------------------------------------------------------------


def test_split_hook_command_raises_on_unrecognized_interpreter():
    with pytest.raises(ValueError, match="unrecognized interpreter"):
        _split_hook_command("pwsh $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.ps1")


# ---------------------------------------------------------------------------
# Identity round-trip across all FOUR command-shape eras (plan C2 dispatch
# brief) — `_cmd_path` must strip every era back to the SAME script-path
# string, or `_group_is_generated`/`_stray_check` silently misclassify a
# generated group as foreign on the next regeneration (the `bb88f375e`
# silent-duplication failure this chunk exists to prevent).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "era,make_guarded",
    [
        (
            "legacy-baked-absolute",
            lambda raw: wrap_hook_command_guarded(raw, windows=False),
        ),
        (
            "bare-portable",
            lambda raw: wrap_hook_command_guarded(raw, windows=False),
        ),
        (
            "guarded-classic-unresolved-interpreter",
            lambda raw: wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=False),
        ),
        (
            "guarded-resolved-interpreter",
            lambda raw: wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=True),
        ),
    ],
)
def test_cmd_path_round_trips_identically_across_all_four_eras(era, make_guarded):
    raw = {
        "legacy-baked-absolute": "python3 /abs/coordinator/hooks/scripts/x.py --flag",
        "bare-portable": "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag",
        "guarded-classic-unresolved-interpreter": "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag",
        "guarded-resolved-interpreter": "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag",
    }[era]
    guarded = make_guarded(raw)
    assert _cmd_path(guarded) == _cmd_path(raw)


@pytest.mark.parametrize("python_bin_resolved", [False, True])
def test_cmd_path_round_trips_identically_on_windows_for_both_python_bin_states(python_bin_resolved):
    raw = "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag"
    guarded = wrap_hook_command_guarded(raw, windows=True, python_bin_resolved=python_bin_resolved)
    assert _cmd_path(guarded) == _cmd_path(raw)


# ---------------------------------------------------------------------------
# C4 dispatch, points 1-2 — emitted shape (both dialects) for the plan-C2
# resolved-interpreter guard, and the AC3 unresolved fall-through, asserted
# against the EXACT strings `wrap_hook_command_guarded` ships at HEAD (its own
# docstring's `python_bin_resolved` branch), not a paraphrase.
# ---------------------------------------------------------------------------


def test_wrap_hook_command_guarded_resolved_posix_exact_shape():
    raw = "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag"
    command = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=True)
    assert command == (
        '[ -n "$COORDINATOR_CONTENT_ROOT" ] && [ -x "$COORDINATOR_PYTHON_BIN" ] && '
        '[ -f "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" ] && '
        'exec "$COORDINATOR_PYTHON_BIN" "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" --flag || exit 127'
    )


def test_wrap_hook_command_guarded_resolved_windows_exact_shape():
    raw = "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py --flag"
    command = wrap_hook_command_guarded(raw, windows=True, python_bin_resolved=True)
    assert command == (
        'if ($env:COORDINATOR_CONTENT_ROOT -and '
        '(Test-Path -LiteralPath "$env:COORDINATOR_PYTHON_BIN" -PathType Leaf) -and '
        '(Test-Path -LiteralPath "$env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" -PathType Leaf)) '
        '{ & $env:COORDINATOR_PYTHON_BIN "$env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" --flag; '
        'exit $LASTEXITCODE } else { exit 127 }'
    )


def test_wrap_hook_command_guarded_ac3_unresolved_falls_through_to_bare_token():
    """AC3: with the resolver's value absent/unresolved (``python_bin_resolved
    =False``, also the default), the emitted command carries the BARE
    ``python3`` token — no ``COORDINATOR_PYTHON_BIN`` reference anywhere —
    and stays the pre-C2 two-test guard shape."""
    raw = "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py"
    explicit = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=False)
    default = wrap_hook_command_guarded(raw, windows=False)
    assert explicit == default
    assert COORDINATOR_PYTHON_BIN_ENV_KEY not in explicit
    assert explicit == (
        '[ -n "$COORDINATOR_CONTENT_ROOT" ] && '
        '[ -f "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" ] && '
        'exec python3 "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py" || exit 127'
    )


# ---------------------------------------------------------------------------
# C4 dispatch, point 3 — AC8 non-Python passthrough: `bash`/`node` pass
# through VERBATIM regardless of `python_bin_resolved`. Driven over the
# multi-interpreter oracle fixture for the `bash` leg, per the brief's
# instruction to prefer the fixture over a hand-built string.
#
# FINDING (not silently worked around): the brief describes this fixture as
# containing "both" bash and node entries. At HEAD,
# coordinator/bin/fixtures/gen-settings-hooks/hooks.json has 5 CPR command
# hooks — bash x2 (session-guard.sh, block-some-write.sh), python3 x3 — and
# NO `node` entry at all (confirmed: `grep -n '"node' hooks.json` is empty).
# The `node` leg below is therefore driven directly against
# `wrap_hook_command_guarded` (real production code, not a fixture that
# doesn't exist), not through `generate()` + the oracle fixture.
# ---------------------------------------------------------------------------


def test_ac8_bash_passthrough_via_oracle_fixture_no_interpreter_substitution(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-ac8-bash.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_oracle_fixture_paths()),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())
    all_commands = [
        h["command"]
        for groups in settings.get("hooks", {}).values()
        for g in groups
        for h in g.get("hooks", [])
    ]
    bash_commands = [
        c for c in all_commands if "session-guard.sh" in c or "block-some-write.sh" in c
    ]
    assert len(bash_commands) == 2, "sanity: the oracle fixture's two bash CPR hooks actually emitted"
    for command in bash_commands:
        assert COORDINATOR_PYTHON_BIN_ENV_KEY not in command
        # bareword, unquoted, un-`&`-prefixed — the classic guard's exact
        # interpreter-invocation shape, untouched by the C2 widening (which
        # only ever applies to a python3/python interpreter).
        assert "exec bash " in command
        assert '"bash"' not in command
        assert "& bash" not in command


def test_ac8_node_passthrough_direct_call_fixture_has_no_node_entry():
    """See module-level FINDING above `test_ac8_bash_passthrough_...` — the
    oracle fixture has no `node` entry, so this leg is driven directly
    against `wrap_hook_command_guarded` instead."""
    raw = "node $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.js --flag"
    for python_bin_resolved in (False, True):
        command = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=python_bin_resolved)
        assert COORDINATOR_PYTHON_BIN_ENV_KEY not in command
        assert command == (
            '[ -n "$COORDINATOR_CONTENT_ROOT" ] && '
            '[ -f "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.js" ] && '
            'exec node "$COORDINATOR_CONTENT_ROOT/hooks/scripts/x.js" --flag || exit 127'
        )


# ---------------------------------------------------------------------------
# C4 dispatch, points 4-6 — AC2, EXECUTED against a real shell.
# ---------------------------------------------------------------------------


def test_ac2_resolved_python_bin_missing_interpreter_exits_127_not_2(passing_script: Path):
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(passing_script.parents[2])
    env[COORDINATOR_PYTHON_BIN_ENV_KEY] = str(passing_script.parent / "nonexistent-python-bin")
    raw = f"python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/{passing_script.name}"
    command = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=True)
    result = _run(command, env=env)
    assert result.returncode == 127
    assert result.returncode != 2, (
        "127 (resolution failure) must never collide with 2 (the PreToolUse "
        "blocking-deny sentinel) -- the whole reason this guard exists"
    )


def test_ac2_resolved_python_bin_directory_not_leaf_is_rejected(passing_script: Path, tmp_path: Path):
    """POSIX `[ -x <dir> ]` is true for a traversable directory, so this
    leaves the leaf-vs-directory distinction to `exec`'s own failure mode
    (the case PowerShell's `-PathType Leaf` closes structurally) -- assert
    the OBSERVED exit is non-zero and neither 2 (blocking deny) nor 0
    (silent success)."""
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(passing_script.parents[2])
    directory_as_bin = tmp_path / "a-directory-not-a-binary"
    directory_as_bin.mkdir()
    env[COORDINATOR_PYTHON_BIN_ENV_KEY] = str(directory_as_bin)
    raw = f"python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/{passing_script.name}"
    command = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=True)
    result = _run(command, env=env)
    assert result.returncode != 0
    assert result.returncode != 2


def test_ac2_resolved_python_bin_real_interpreter_and_script_exits_0(passing_script: Path):
    """The "passes something" half of the guard's own binding constraint
    (module docstring) for the RESOLVED-interpreter shape specifically --
    everything real and present must still exit 0, not just the classic
    (unresolved) shape already covered above."""
    env = dict(os.environ)
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = str(passing_script.parents[2])
    env[COORDINATOR_PYTHON_BIN_ENV_KEY] = sys.executable
    raw = f"python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/{passing_script.name}"
    command = wrap_hook_command_guarded(raw, windows=False, python_bin_resolved=True)
    result = _run(command, env=env)
    assert result.returncode == 0


_PWSH_BIN = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(
    _PWSH_BIN is None,
    reason="no pwsh/powershell.exe on this box -- executing the PowerShell leg requires one",
)
def test_powershell_resolved_guard_call_operator_executes_and_exits_127_for_missing_interpreter(
    tmp_path: Path,
):
    """AC2's silent-success leg: the `&` call operator before
    `$env:COORDINATOR_PYTHON_BIN` is what stops `exit $LASTEXITCODE`
    degrading to `exit $null` == exit 0 (see `wrap_hook_command_guarded`'s
    own docstring). `pwsh` IS available on this box, so this EXECUTES the
    PowerShell leg for real rather than falling back to a structural-only
    assertion."""
    scripts_dir = tmp_path / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "x.py"
    script.write_text("import sys\nsys.exit(0)\n")

    raw = "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py"
    command = wrap_hook_command_guarded(raw, windows=True, python_bin_resolved=True)

    env = dict(os.environ)
    env["COORDINATOR_CONTENT_ROOT"] = str(tmp_path)
    env["COORDINATOR_PYTHON_BIN"] = str(tmp_path / "nonexistent-interpreter")

    result = subprocess.run(
        [_PWSH_BIN, "-NoProfile", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 127
    assert result.returncode != 0


@pytest.mark.skipif(
    _PWSH_BIN is None,
    reason="no pwsh/powershell.exe on this box -- executing the PowerShell leg requires one",
)
def test_powershell_resolved_guard_real_interpreter_and_script_exits_0(tmp_path: Path):
    scripts_dir = tmp_path / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "x.py"
    script.write_text("import sys\nsys.exit(0)\n")

    raw = "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py"
    command = wrap_hook_command_guarded(raw, windows=True, python_bin_resolved=True)

    env = dict(os.environ)
    env["COORDINATOR_CONTENT_ROOT"] = str(tmp_path)
    env["COORDINATOR_PYTHON_BIN"] = sys.executable

    result = subprocess.run(
        [_PWSH_BIN, "-NoProfile", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0


def test_regenerate_over_already_guarded_settings_does_not_duplicate_hooks(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings.json"
    hooks_json = _oracle_fixture_paths()

    generate(out_path=str(out_path), hooks_json_override=str(hooks_json), coordinator_root_override=str(coordinator_root))
    settings_first = json.loads(out_path.read_text())
    count_first = _hook_count(settings_first)
    assert count_first == 5  # sanity: matches the oracle fixture's known CPR-command count

    generate(out_path=str(out_path), hooks_json_override=str(hooks_json), coordinator_root_override=str(coordinator_root))
    settings_second = json.loads(out_path.read_text())
    count_second = _hook_count(settings_second)

    assert count_second == count_first
