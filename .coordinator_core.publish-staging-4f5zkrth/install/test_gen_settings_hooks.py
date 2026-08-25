"""
coordinator_core.install.test_gen_settings_hooks — parity tests for
coordinator_core.install.gen_settings_hooks.

Port of: gen-settings-hooks.sh (DoE a2078a9b, 2026-07-22).

Independently re-derives expected behavior from the bash oracle's OWN test
fixtures (``hooks.json`` and ``expected-generated.json``, co-located under
``coordinator/bin/fixtures/gen-settings-hooks/`` as of the 2026-07-22
executable-surface migration — see the fixture-location block below) rather
than re-asserting this port's own transcription — mirrors
gen-settings-hooks.test.sh (DoE c3322493, 2026-07-22) tests (a)-(g) 1:1 by
assertion intent, re-derived against the ported Python entrypoints, not
copy-pasted jq queries.

Spec backlink: DoE-claude:pln-doe-maximalist-execution-plugi-6d808d § M1
Port backlink: docs/plans/2026-07-16-clean-slate-residual-migration.md
    (BIG_PORT Wave B, item gen-settings-hooks)
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from pathlib import Path

import pytest

from coordinator_core.install import gen_settings_hooks as _gsh_module
from coordinator_core.install._shared import (
    COORDINATOR_CONTENT_ROOT_ENV_KEY,
    COORDINATOR_PYTHON_BIN_ENV_KEY,
    _cmd_path,
    hook_root_env_expr,
    wrap_hook_command_guarded,
)
from coordinator_core.install.gen_settings_hooks import (
    HOOK_TIMEOUT_CEILING_SECS,
    GenSettingsHooksError,
    _assert_portable_command,
    _clamp_hook_timeout,
    ensure_positive_marker,
    generate,
    kill_switch_marker_path,
    main,
    positive_marker_path,
    resolve_settings_out_path,
)
from coordinator_core.ops.session.guard_foreign_platform_paths import detect_foreign_platform_paths
from coordinator_core.ops.session.guard_settings_integrity import HookDeliveryReport
from coordinator_core.testing.doe_root import resolve_doe_root

# Matches a Windows drive-letter absolute path (``C:\`` or ``C:/``) anywhere
# in a string — the portability regression this whole test module guards
# against (2026-07-28 incident: `X:/DoE-claude/...` baked into a macOS
# host's settings.json).
_DRIVE_LETTER_RE = re.compile(r"[A-Za-z]:[\\/]")


class _OSNameProxy:
    """Delegates every attribute to the real ``os`` module except ``name``.

    `generate()` derives its POSIX-vs-Windows command shape from
    `os.name` at generation time (see the module-level comment above
    ``test_cross_surface_pin_detect_foreign_platform_paths_clean_for_posix_shape``),
    with no override parameter -- so a test that needs the POSIX shape
    specifically (the golden fixture below is POSIX-shaped, committed, and
    host-independent by design) cannot get it on a native-Windows test
    runner without forcing `os.name`. Proxying only `gen_settings_hooks.py`'s
    own `os` reference (rather than flipping the real global) avoids
    breaking `pathlib`'s own `os.name`-driven `WindowsPath`/`PosixPath`
    selection for the rest of the process -- see the identical proxy in
    `test_shell_rc_guard.py` for the full rationale.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


class _PosixPathModuleProxy:
    """Delegates every attribute to the real ``os.path`` except ``isdir``,
    which always reports ``True`` -- lets a synthetic, non-drive-lettered
    POSIX path (e.g. ``/fake/posix/coordinator``) stand in for
    ``coordinator_root_override`` on a real-Windows filesystem, where every
    genuine ``tmp_path`` fixture is unavoidably drive-lettered and so can
    never itself produce a clean POSIX-shaped env value (see
    `test_cross_surface_pin_detect_foreign_platform_paths_clean_for_posix_shape`).
    """

    def isdir(self, _path):
        return True

    def __getattr__(self, attr):
        return getattr(os.path, attr)


class _OSPosixPathOverrideProxy(_OSNameProxy):
    """`_OSNameProxy`, plus a `.path` swapped for `_PosixPathModuleProxy`."""

    def __init__(self) -> None:
        super().__init__("posix")
        self.path = _PosixPathModuleProxy()

# The env-var expression `generate()` bakes into hook commands ON THIS
# TEST-RUNNING MACHINE — matches `_rewrite_cpr`'s own `os.name == "nt"`
# check, so tests assert against whatever the generator under test actually
# produces here rather than assuming one platform.
_PORTABLE_EXPR = hook_root_env_expr(windows=(os.name == "nt"))

# ---------------------------------------------------------------------------
# fixture location — co-located first (the oracle fixtures moved TO makima
# as part of the 2026-07-22 executable-surface migration:
# coordinator/bin/fixtures/gen-settings-hooks/), DoE-resident sibling
# checkout second (pre-migration / alternate layouts). This mirrors the
# co-located -> DoE-resident two-rung shape used elsewhere for the split
# repo layout (coordinator_core.data_root, coordinator/bin/lib/
# coordinator_data_root.py) — no silent skip: a caller with neither rung
# resolved gets a hard, informative failure, not a quietly-skipped suite.
# ---------------------------------------------------------------------------

_MAKIMA_ROOT = Path(__file__).resolve().parents[2]
_COLOCATED_FIXTURES = _MAKIMA_ROOT / "coordinator" / "bin" / "fixtures" / "gen-settings-hooks"
_DOE_ROOT = Path(resolve_doe_root() or "/doe-root-unresolved")
_DOE_FIXTURES = _DOE_ROOT / "coordinator" / "bin" / "fixtures" / "gen-settings-hooks"

if (_COLOCATED_FIXTURES / "hooks.json").is_file():
    _ORACLE_FIXTURES = _COLOCATED_FIXTURES
elif (_DOE_FIXTURES / "hooks.json").is_file():
    _ORACLE_FIXTURES = _DOE_FIXTURES
else:
    raise RuntimeError(
        "test_gen_settings_hooks: oracle fixture not found. "
        f"Co-located (makima) tried: {_COLOCATED_FIXTURES}. "
        f"DoE-resident tried: {_DOE_FIXTURES}."
    )

_ORACLE_HOOKS_JSON = _ORACLE_FIXTURES / "hooks.json"
_ORACLE_EXPECTED = _ORACLE_FIXTURES / "expected-generated.json"


@pytest.fixture()
def coordinator_root(tmp_path: Path) -> Path:
    root = tmp_path / "coordinator"
    (root / "hooks").mkdir(parents=True)
    # 2026-07-28 polarity inversion: generation now defaults OFF absent the
    # positive per-machine marker (see gen_settings_hooks.ensure_positive_marker).
    # Every test in this module that exercises normal generation via the
    # `coordinator_root.parent / "settings-*.json"` out_path convention needs
    # this machine pre-"enabled" — the polarity-specific tests below construct
    # their own bare tmp dirs instead, so they don't inherit this.
    (root.parent / ".coordinator-hooks-enabled").touch()
    return root


def _enable(out_path: Path) -> None:
    """Touch the positive consent marker in `out_path`'s directory — the
    polarity-inversion equivalent of "this machine already opted in"."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    (out_path.parent / ".coordinator-hooks-enabled").touch()


def _iter_all_hooks(settings: dict):
    for groups in (settings.get("hooks") or {}).values():
        for group in groups:
            for hook in group.get("hooks", []) or []:
                yield hook


# ---------------------------------------------------------------------------
# Bake-normalization — C2 will change every generated python3/python hook
# command's interpreter token from the bare literal to a resolved-machine env
# var reference (`$COORDINATOR_PYTHON_BIN` POSIX, `$env:COORDINATOR_PYTHON_BIN`
# PowerShell), but ONLY when the generating machine's interpreter resolver
# (C1, `substrate.py`) actually returns a value — falling through to the bare
# token otherwise (plan AC3). That makes the emitted token legitimately
# machine-dependent, so `test_golden_output_matches_oracle_fixture` can no
# longer compare `actual["hooks"]` to the static oracle fixture byte-for-byte;
# both sides are normalized to a shared sentinel first instead of re-capturing
# the golden (`expected-generated.json` is also C4's multi-interpreter oracle
# and must stay byte-unchanged).
# ---------------------------------------------------------------------------

# python3 / python / both COORDINATOR_PYTHON_BIN reference spellings collapse
# to one sentinel; bash/node are deliberately excluded so they keep
# normalizing to themselves and stay distinguishable from the Python token
# and from each other (the oracle fixture contains both, and collapsing them
# would silently mask C2's non-Python-passthrough requirement, plan AC8).
_PYTHON_INTERPRETER_TOKENS = frozenset(
    {"python3", "python", "$COORDINATOR_PYTHON_BIN", "$env:COORDINATOR_PYTHON_BIN"}
)
_INTERPRETER_SENTINEL = "<NORMALIZED_PYTHON_INTERPRETER>"

_INTERPRETER_TOKEN_ALTS = "|".join(re.escape(tok) for tok in sorted(_PYTHON_INTERPRETER_TOKENS, key=len, reverse=True))

# Three command shapes an interpreter token can appear in — bare
# (``<token> <path>...``), guarded-POSIX (``... exec <token> "<path>"...``),
# and guarded-PowerShell (``... { <token> "<path>"...``). Each regex captures
# ONLY the token itself via zero-width lookaround, so substitution touches
# nothing else in the command — script paths, rest_args, and guard text pass
# through untouched, matching `_cmd_path`/`_split_hook_command`'s own
# understanding of where the interpreter word sits.
#
# The guarded-POSIX/PS lookbehinds each carry a SECOND alternative (C2, plan
# C3 chunk): the resolved-interpreter guard shape quotes the token
# (``exec "$COORDINATOR_PYTHON_BIN" "<path>"``) and, on PowerShell, invokes
# it via the call operator (``{ & $env:COORDINATOR_PYTHON_BIN "<path>"``) —
# shell-doc-ok: these quote the real bash/PowerShell guard shapes this
# regex matches; re-rendering without `$`/`>` would make the doc wrong.
# both put a character between the anchor word and the token that the
# original single-alternative lookbehind did not admit. Python's `re`
# requires fixed-width lookbehind, so this is spelled as an alternation of
# fixed-width branches, not a variable-width `?` — same anchoring discipline
# `_shared.py`'s guard regexes follow, just mirrored here for the token-only
# match this helper needs.
_BARE_INTERPRETER_RE = re.compile(rf"^(?P<interp>{_INTERPRETER_TOKEN_ALTS})(?= )")
_GUARDED_POSIX_INTERPRETER_RE = re.compile(rf'(?:(?<=exec )|(?<=exec "))(?P<interp>{_INTERPRETER_TOKEN_ALTS})(?=[ "])')
_GUARDED_PS_INTERPRETER_RE = re.compile(rf'(?:(?<=\{{ )|(?<=\{{ & ))(?P<interp>{_INTERPRETER_TOKEN_ALTS})(?=[ "])')

_ALL_INTERPRETER_TOKEN_RES = (
    _BARE_INTERPRETER_RE,
    _GUARDED_POSIX_INTERPRETER_RE,
    _GUARDED_PS_INTERPRETER_RE,
)

# The oracle fixture (`expected-generated.json`) was captured before C2 and
# encodes the CLASSIC (unresolved-interpreter) guard shape throughout. On a
# machine where C1's resolver actually resolves an interpreter, `generate()`
# now emits the WIDENED guard (an extra interpreter-existence test, plus a
# quoted/call-operator interpreter token — see `wrap_hook_command_guarded`'s
# `python_bin_resolved=True` branch) for every python3/python hook. That is
# real, correct, machine-dependent behavior (plan AC3), not a fixture
# regression — so it needs collapsing back to the classic shape here, same
# spirit as the token-sentinel swap below and NOT a substitute for it: this
# only touches the widening's structural residue (the extra bracket/Test-Path
# test, the quotes, the `&` call operator), never the interpreter token text
# itself, which `_normalize_command_interpreter` still sentinel-swaps next.
_INTERPRETER_EXISTENCE_TEST_POSIX_RE = re.compile(rf'\[ -x "(?:{_INTERPRETER_TOKEN_ALTS})" \] && ')
_INTERPRETER_EXISTENCE_TEST_PS_RE = re.compile(
    rf' -and \(Test-Path -LiteralPath "(?:{_INTERPRETER_TOKEN_ALTS})" -PathType Leaf\)'
)
_QUOTED_EXEC_INTERPRETER_RE = re.compile(rf'(?<=exec )"(?P<interp>{_INTERPRETER_TOKEN_ALTS})"(?=[ "])')
_CALL_OPERATOR_INTERPRETER_RE = re.compile(rf'\{{ & (?P<interp>{_INTERPRETER_TOKEN_ALTS})(?=[ "])')


def _strip_resolved_interpreter_widening(command: str) -> str:
    """Collapse a plan-C2 widened (resolved-interpreter) guard back to the
    classic guard's structural shape — strips the extra interpreter-existence
    test and un-quotes/un-`&`-prefixes the interpreter token — leaving the
    token itself untouched for `_normalize_command_interpreter` to sentinel-
    swap next. A command with no widening (bash/node, or an unresolved
    python3/python) passes through byte-identical.

    Review finding F4 (2026-08-03 code-reviewer): the PowerShell widened
    shape's interpreter-existence test (`-and (Test-Path ... PathType Leaf)`)
    and its `&` call operator before the interpreter token are ALWAYS emitted
    together by `wrap_hook_command_guarded` (plan C2) — the `&` is what
    stops a bare `$env:COORDINATOR_PYTHON_BIN` reference from merely being
    echoed (PowerShell's `$null`-exit-0 hole this whole plan exists to
    close). Silently stripping the existence test without also confirming
    `&` is present would let a regression that drops just that one character
    normalize byte-identical to a correct command, defeating the one test
    (`test_golden_output_matches_oracle_fixture`) built to catch shape
    drift. Fail LOUD here instead: if the existence-test widening is
    present, the call operator must be too."""
    if _INTERPRETER_EXISTENCE_TEST_PS_RE.search(command):
        assert _CALL_OPERATOR_INTERPRETER_RE.search(command), (
            "PowerShell interpreter-existence-test widening is present without "
            "the required `&` call operator before the interpreter token — this "
            "is the exact $null-exit-0 regression the widened guard exists to "
            f"prevent. command={command!r}"
        )
    command = _INTERPRETER_EXISTENCE_TEST_POSIX_RE.sub("", command)
    command = _INTERPRETER_EXISTENCE_TEST_PS_RE.sub("", command)
    command = _QUOTED_EXEC_INTERPRETER_RE.sub(lambda m: m.group("interp"), command)
    command = _CALL_OPERATOR_INTERPRETER_RE.sub(lambda m: "{ " + m.group("interp"), command)
    return command


def _normalize_command_interpreter(command: str) -> str:
    """Rewrite ONLY the interpreter token in a single hook `command` string
    to `_INTERPRETER_SENTINEL`, if that token is a Python-interpreter
    spelling. A command whose shape none of the three regexes recognize (or
    whose interpreter token isn't a Python spelling — `bash`, `node`) passes
    through byte-identical: the test must fail on a real shape regression,
    never on this normalizer misfiring."""
    command = _strip_resolved_interpreter_widening(command)
    for pattern in _ALL_INTERPRETER_TOKEN_RES:
        normalized, count = pattern.subn(
            lambda m: _INTERPRETER_SENTINEL if m.group("interp") in _PYTHON_INTERPRETER_TOKENS else m.group("interp"),
            command,
            count=1,
        )
        if count:
            return normalized
    return command


def _normalize_interpreter_tokens(hooks_obj: dict) -> dict:
    """Deep-copy a `settings["hooks"]` sub-object, normalizing every
    `command` string's interpreter token in place (see
    `_normalize_command_interpreter`). Leaves the input untouched."""
    normalized = copy.deepcopy(hooks_obj) or {}
    for groups in normalized.values():
        for group in groups:
            for hook in group.get("hooks", []) or []:
                if "command" in hook:
                    hook["command"] = _normalize_command_interpreter(hook["command"])
    return normalized


# ---------------------------------------------------------------------------
# Test (a) — type-filter fixture
# ---------------------------------------------------------------------------


def test_type_filter_against_oracle_fixture(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-a.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    hooks = list(_iter_all_hooks(settings))

    # No mcp_tool entries anywhere.
    assert not any(h.get("type") == "mcp_tool" for h in hooks), "mcp_tool entries must never be emitted"

    # No non-CPR command (platform-localize.sh).
    assert not any("platform-localize.sh" in h.get("command", "") for h in hooks)

    # PreToolUse Bash group (all mcp_tool) entirely absent.
    pretooluse = settings.get("hooks", {}).get("PreToolUse", [])
    assert not any(g.get("matcher") == "Bash" for g in pretooluse)

    # SubagentStop group (all non-CPR command) entirely absent — empty-after-filter path.
    assert "SubagentStop" not in settings.get("hooks", {})

    # Exactly 5 CPR command entries: session-guard.sh, bootstrap-substrate.py,
    # block-some-write.sh, plan-persistence-check.py, py-example-hook.py.
    command_hooks = [h for h in hooks if h.get("type") == "command"]
    assert len(command_hooks) == 5

    # Shape-P1 python3 CPR fixture emitted, rewritten to the portable form
    # (NOT to a coordinator_root-derived absolute path — the whole point of
    # the portability fix is that the fixture's tmp_path never appears here),
    # and exit-code-hygiene-guarded (2026-07-28) — the emitted command is no
    # longer the bare `<interpreter> <path>` form but the guarded wrapper
    # around it; assert via `_cmd_path` round-trip rather than a literal
    # string match so this test doesn't hardcode the guard's exact syntax.
    py_example = f"{_PORTABLE_EXPR}/hooks/scripts/py-example-hook.py"
    py_example_bare = f"python3 {py_example}"
    assert any(
        h.get("command", "") != py_example_bare and _cmd_path(h["command"]) == py_example
        for h in command_hooks
    )

    # Shape-P2 module-form command rejected (no CLAUDE_PLUGIN_ROOT substring).
    assert not any("coordinator_core.hooks.x" in h.get("command", "") for h in command_hooks)

    # No literal ${CLAUDE_PLUGIN_ROOT} remains anywhere.
    commands_dumped = json.dumps(command_hooks)
    assert "${CLAUDE_PLUGIN_ROOT}" not in commands_dumped

    # No trace of the fixture's own coordinator_root (a tmp_path) survives
    # into the emitted COMMANDS — that IS the portability invariant: a hook
    # command's text must not depend on where THIS machine's clone lives.
    # (The value legitimately DOES appear once, in `settings["env"]` — that
    # is the one-place-not-37-places design, checked separately below.)
    assert str(coordinator_root) not in commands_dumped
    assert settings["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == coordinator_root.as_posix()


# ---------------------------------------------------------------------------
# Test (b) — golden-file test against the oracle's own expected-generated.json
# ---------------------------------------------------------------------------


def test_golden_output_matches_oracle_fixture(coordinator_root: Path, monkeypatch: pytest.MonkeyPatch):
    # The committed oracle fixture (`expected-generated.json`) is POSIX-
    # shaped and host-independent by design -- force POSIX generation so
    # this pin holds on a native-Windows test runner too (see
    # `_OSNameProxy`).
    monkeypatch.setattr(_gsh_module, "os", _OSNameProxy("posix"))
    out_path = coordinator_root.parent / "settings-b.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    actual = json.loads(out_path.read_text())

    expected = json.loads(_ORACLE_EXPECTED.read_text())

    # Compare the `hooks` sub-object only — the fixture captures the HOOKS
    # shape (its whole purpose), and that shape no longer depends on
    # `coordinator_root` at all (unlike `actual["env"]`, which legitimately
    # carries this fixture's own tmp_path and so can't be golden-compared
    # against a static fixture file without per-run normalisation). Both
    # sides are ALSO bake-normalized (see `_normalize_interpreter_tokens`)
    # before comparing: once C2 lands, the Python interpreter token is
    # machine-dependent (resolved value or bare fallback), so a static golden
    # can only ever be correct up to that one token — never re-capture
    # `expected-generated.json` to chase it.
    assert _normalize_interpreter_tokens(actual["hooks"]) == _normalize_interpreter_tokens(expected["hooks"])
    assert actual["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == coordinator_root.as_posix()


def test_generated_hook_commands_are_machine_independent_in_resolved_case(coordinator_root: Path):
    """AC9 characterization: emitted hook command TEXT must not vary by
    generating machine, resolved or not — asserting that two generate() calls
    produce byte-identical `hooks` and neither bakes this fixture's own
    `coordinator_root` into a command. This does not itself force a resolved
    interpreter (see `test_generated_hook_commands_are_machine_independent_across_differing_resolved_interpreters`
    below for that measurement); it holds regardless of whether this run's
    `resolve_hook_python_bin()` resolves anything.
    """
    out_a = coordinator_root.parent / "settings-ac9-a.json"
    out_b = coordinator_root.parent / "settings-ac9-b.json"

    generate(
        out_path=str(out_a),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    generate(
        out_path=str(out_b),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    settings_a = json.loads(out_a.read_text())
    settings_b = json.loads(out_b.read_text())

    assert settings_a["hooks"] == settings_b["hooks"]

    commands = [h["command"] for h in _iter_all_hooks(settings_a) if h.get("type") == "command"]
    for command in commands:
        assert str(coordinator_root) not in command


def test_generated_hook_commands_are_machine_independent_across_differing_resolved_interpreters(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    """AC9 measurement (review finding F3, 2026-08-03 code-reviewer): the
    property the previous test only characterizes structurally — that
    emitted hook command TEXT must not vary by generating machine once an
    interpreter resolves, the whole point of C2's env-var indirection — is
    measured directly here across two DIFFERENT resolved interpreter paths,
    standing in for two different machines. Forces
    `resolve_hook_python_bin()` to two distinct fake values across two
    separate `generate()` calls and asserts `hooks` is still byte-identical;
    only `env` (which legitimately carries the per-machine value) may differ.
    """
    out_a = coordinator_root.parent / "settings-ac9-resolved-a.json"
    out_b = coordinator_root.parent / "settings-ac9-resolved-b.json"

    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/machine-a/venv/bin/python3")
    generate(
        out_path=str(out_a),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/machine-b/venv/bin/python3-DIFFERENT")
    generate(
        out_path=str(out_b),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    settings_a = json.loads(out_a.read_text())
    settings_b = json.loads(out_b.read_text())

    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings_a["env"]  # sanity: fourth-era shape actually emitted
    assert settings_a["hooks"] == settings_b["hooks"]
    assert settings_a["env"][COORDINATOR_PYTHON_BIN_ENV_KEY] != settings_b["env"][COORDINATOR_PYTHON_BIN_ENV_KEY]


# ---------------------------------------------------------------------------
# Test (c) — idempotency
# ---------------------------------------------------------------------------


def test_idempotent_rerun_is_byte_identical(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-c.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    pass1 = out_path.read_bytes()

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    pass2 = out_path.read_bytes()

    assert pass1 == pass2


# ---------------------------------------------------------------------------
# Test (d) — same-event collision: hand hook preserved alongside generated hooks
# ---------------------------------------------------------------------------


def test_hand_authored_hook_preserved_alongside_generated(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-d.json"
    out_path.write_text(
        json.dumps(
            {
                "env": {"TEST_KEY": "test_value"},
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ~/.claude/bin/touch-session-sentinel.sh",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "~/.claude/bin/portability-guard-hook",
                                    "timeout": 5000,
                                }
                            ],
                        }
                    ],
                },
            }
        )
    )

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    session_start_commands = [
        h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]
    ]
    assert "bash ~/.claude/bin/touch-session-sentinel.sh" in session_start_commands

    posttooluse_commands = [
        h["command"] for g in settings["hooks"]["PostToolUse"] for h in g["hooks"]
    ]
    assert "~/.claude/bin/portability-guard-hook" in posttooluse_commands

    # Generated SessionStart hooks also present (same-event coexistence).
    # Generated commands are now exit-code-hygiene-guarded (2026-07-28), so
    # they no longer start with the bare portable_dir prefix directly —
    # extract the underlying script path via `_cmd_path` (which understands
    # the guarded shape) and check THAT instead.
    portable_dir = _PORTABLE_EXPR + "/hooks/"
    assert any(
        _cmd_path(h["command"]).startswith(portable_dir)
        for h in [h for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    )

    # Non-hooks fields survive.
    assert settings["env"]["TEST_KEY"] == "test_value"

    # Second regenerate is a no-op diff.
    before = out_path.read_bytes()
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    assert out_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Test (e) — fail-loud on unresolvable coordinator root + no partial write
# ---------------------------------------------------------------------------


def test_fail_loud_on_unresolvable_coordinator_root(tmp_path: Path):
    out_path = tmp_path / "settings-e.json"
    out_path.write_text(json.dumps({"marker": "original"}))
    _enable(out_path)

    nonexistent = str(tmp_path / "nonexistent-coordinator")
    with pytest.raises(GenSettingsHooksError):
        generate(out_path=str(out_path), coordinator_root_override=nonexistent)

    assert json.loads(out_path.read_text())["marker"] == "original"


def test_cli_exit_code_nonzero_on_unresolvable_root(tmp_path: Path):
    out_path = tmp_path / "settings-e-cli.json"
    out_path.write_text(json.dumps({"marker": "original"}))
    _enable(out_path)
    nonexistent = str(tmp_path / "nonexistent-coordinator")

    rc = main(["--out", str(out_path), "--coordinator-root", nonexistent])

    assert rc == 1
    assert json.loads(out_path.read_text())["marker"] == "original"


# ---------------------------------------------------------------------------
# Test (f) — stray-check: hand hook under coordinator/hooks/ NOT in will-emit set
# ---------------------------------------------------------------------------


def test_stray_check_fails_loud_on_hand_hook_in_generated_dir(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-f.json"
    stray_command = f"bash {coordinator_root.as_posix()}/hooks/my-hand-authored-hook.sh"
    out_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": stray_command}],
                        }
                    ]
                }
            }
        )
    )

    with pytest.raises(GenSettingsHooksError) as excinfo:
        generate(
            out_path=str(out_path),
            hooks_json_override=str(_ORACLE_HOOKS_JSON),
            coordinator_root_override=str(coordinator_root),
        )

    msg = str(excinfo.value)
    assert "my-hand-authored-hook.sh" in msg
    assert "SessionStart" in msg

    # No partial write — settings.json unchanged.
    settings = json.loads(out_path.read_text())
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == stray_command


# ---------------------------------------------------------------------------
# Test (g) — operator kill-switch marker
# ---------------------------------------------------------------------------


def test_kill_switch_marker_is_noop(tmp_path: Path):
    killswitch_dir = tmp_path / "killswitch"
    killswitch_dir.mkdir()
    out_path = killswitch_dir / "settings.json"
    seed = json.dumps({"hooks": {}, "marker": "deliberately-stripped"})
    out_path.write_text(seed)
    (killswitch_dir / ".coordinator-hooks-disabled").write_text("")

    # generate() must return normally (not raise) and leave the file untouched.
    generate(out_path=str(out_path), coordinator_root_override="/should-not-be-resolved")

    assert out_path.read_text() == seed


def test_kill_switch_marker_cli_exit_zero(tmp_path: Path):
    killswitch_dir = tmp_path / "killswitch-cli"
    killswitch_dir.mkdir()
    out_path = killswitch_dir / "settings.json"
    seed = json.dumps({"hooks": {}, "marker": "deliberately-stripped"})
    out_path.write_text(seed)
    (killswitch_dir / ".coordinator-hooks-disabled").write_text("")

    rc = main(["--out", str(out_path), "--coordinator-root", "/should-not-be-resolved"])

    assert rc == 0
    assert out_path.read_text() == seed


# ---------------------------------------------------------------------------
# 2026-07-28 polarity inversion — the positive per-machine marker
# (.coordinator-hooks-enabled) is now REQUIRED for generation; its absence
# means "do nothing," never "start writing config." See
# `ensure_positive_marker` / `_has_local_generation_evidence`.
# ---------------------------------------------------------------------------


def test_no_positive_marker_and_no_evidence_is_untouched_noop(tmp_path: Path):
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    out_path = off_dir / "settings.json"
    seed = json.dumps({"hooks": {}, "marker": "should-not-change"})
    out_path.write_text(seed)

    status = generate(out_path=str(out_path), coordinator_root_override="/should-not-be-resolved")

    assert status == "skipped (no positive marker)"
    assert out_path.read_text() == seed
    assert not (off_dir / ".coordinator-hooks-enabled").exists()


def test_no_positive_marker_cli_exit_zero(tmp_path: Path):
    off_dir = tmp_path / "off-cli"
    off_dir.mkdir()
    out_path = off_dir / "settings.json"
    seed = json.dumps({"marker": "should-not-change"})
    out_path.write_text(seed)

    rc = main(["--out", str(out_path), "--coordinator-root", "/should-not-be-resolved"])

    assert rc == 0
    assert out_path.read_text() == seed


def test_check_only_never_creates_the_positive_marker(tmp_path: Path):
    """check-only must not mutate OR create markers — including the new
    positive one — even on a machine that would otherwise auto-migrate."""
    off_dir = tmp_path / "check-only"
    off_dir.mkdir()
    out_path = off_dir / "settings.json"
    out_path.write_text(json.dumps({"env": {COORDINATOR_CONTENT_ROOT_ENV_KEY: "/some/prior/root"}}))

    status = generate(out_path=str(out_path), check_only=True)

    assert status == "skipped (check-only)"
    assert not (off_dir / ".coordinator-hooks-enabled").exists()


def test_negative_marker_wins_even_with_positive_marker_present(tmp_path: Path):
    """Two independent ways to be OFF (negative marker, absent positive
    marker); only one way to be ON. The negative marker must win regardless
    of the positive marker's state."""
    d = tmp_path / "both-markers"
    d.mkdir()
    out_path = d / "settings.json"
    seed = json.dumps({"marker": "should-not-change"})
    out_path.write_text(seed)
    (d / ".coordinator-hooks-enabled").touch()
    (d / ".coordinator-hooks-disabled").touch()

    status = generate(out_path=str(out_path), coordinator_root_override="/should-not-be-resolved")

    assert status == "skipped (disabled by operator marker)"
    assert out_path.read_text() == seed


def test_positive_marker_present_enables_generation(coordinator_root: Path, tmp_path: Path):
    """coordinator_root fixture already touches the positive marker — this
    is the control case proving the marker actually gates generation (not
    just that the skip path works)."""
    out_path = coordinator_root.parent / "settings-enabled.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    assert status == "seeded"
    assert out_path.is_file()


def test_migration_auto_creates_marker_from_local_generation_evidence(tmp_path: Path, coordinator_root: Path):
    """The migration discriminator: a machine with NO positive marker but
    whose settings.json already carries `env.COORDINATOR_CONTENT_ROOT` from
    a prior run (this generator's own, first-party, untracked/gitignored
    output) is treated as already-consenting — the marker is auto-created
    and generation proceeds, so an existing healthy machine converts without
    a flag day."""
    migrate_dir = tmp_path / "migrate"
    migrate_dir.mkdir()
    out_path = migrate_dir / "settings.json"
    out_path.write_text(
        json.dumps({"env": {COORDINATOR_CONTENT_ROOT_ENV_KEY: "/some/prior/coordinator/root"}, "hooks": {}})
    )
    assert not (migrate_dir / ".coordinator-hooks-enabled").exists()

    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "seeded"
    assert (migrate_dir / ".coordinator-hooks-enabled").is_file()
    settings = json.loads(out_path.read_text())
    assert settings["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == coordinator_root.as_posix()


def test_migration_never_fires_from_a_bare_hooks_block_alone(tmp_path: Path, coordinator_root: Path):
    """A `hooks` block alone (no `env.COORDINATOR_CONTENT_ROOT`) is NOT
    treated as generation evidence — only the generator's own env key
    counts. This guards against inferring consent from something that COULD
    plausibly arrive over a git sync of a shared file (a `hooks` block
    shape is far less exclusively self-authored than the env key)."""
    d = tmp_path / "bare-hooks"
    d.mkdir()
    out_path = d / "settings.json"
    out_path.write_text(json.dumps({"hooks": {"SessionStart": []}}))

    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "skipped (no positive marker)"
    assert not (d / ".coordinator-hooks-enabled").exists()


def test_ensure_positive_marker_is_read_only_when_marker_already_exists(tmp_path: Path):
    d = tmp_path / "already-on"
    d.mkdir()
    out_path = d / "settings.json"
    marker = d / ".coordinator-hooks-enabled"
    marker.write_text("hand-authored")

    resolved_marker, is_enabled, migrated = ensure_positive_marker(str(out_path))

    assert resolved_marker == marker
    assert is_enabled is True
    assert migrated is False
    assert marker.read_text() == "hand-authored"  # untouched, not rewritten


def test_ensure_positive_marker_degrades_to_not_enabled_on_malformed_settings_json(tmp_path: Path):
    """Regression test for review finding F3 (2026-07-28 s2 review):
    `ensure_positive_marker` now runs BEFORE coordinator-root resolution, so
    a first-run machine with a malformed settings.json AND no marker yet
    must still degrade to "not enabled" (matching the pre-2026-07-28
    ordering's soft "skipped (clone absent)" forgiveness for this state),
    not raise `GenSettingsHooksError` on a settings.json parse this path
    doesn't actually need to succeed."""
    d = tmp_path / "malformed"
    d.mkdir()
    out_path = d / "settings.json"
    out_path.write_text("{not valid json")

    marker, is_enabled, migrated = ensure_positive_marker(str(out_path))

    assert marker == d / ".coordinator-hooks-enabled"
    assert is_enabled is False
    assert migrated is False
    assert not marker.exists()

    # generate() itself must not raise either, end to end, given the same
    # malformed-settings + no-marker-yet combination that Finding 3 flagged.
    status = generate(out_path=str(out_path), coordinator_root_override=str(tmp_path))
    assert status == "skipped (no positive marker)"


def test_positive_marker_path_matches_kill_switch_marker_convention(tmp_path: Path):
    out_path = tmp_path / "somewhere" / "settings.json"
    marker = positive_marker_path(str(out_path))
    assert marker == out_path.parent / ".coordinator-hooks-enabled"
    assert marker.parent == kill_switch_marker_path(str(out_path)).parent


# ---------------------------------------------------------------------------
# Addendum A5 — permission preservation on atomic rewrite (fresh-install /
# Family-I concern: settings.json may be operator-hardened to a restrictive
# mode; the atomic tempfile-swap must not silently reset it).
# ---------------------------------------------------------------------------


def test_atomic_write_preserves_prior_permission_bits(coordinator_root: Path):
    # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is always
    # 0o666 there regardless of the os.chmod() argument. Assert the real
    # invariant under test — mode preserved across the atomic rewrite — by
    # comparing against whatever mode chmod actually produced on this
    # platform, rather than asserting the literal POSIX octal.
    out_path = coordinator_root.parent / "settings-perm.json"
    out_path.write_text(json.dumps({}))
    os.chmod(out_path, 0o600)
    mode_before = stat.S_IMODE(os.stat(out_path).st_mode)

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    mode_after = stat.S_IMODE(os.stat(out_path).st_mode)
    assert mode_after == mode_before


# ---------------------------------------------------------------------------
# Family-I fresh-install smoke: no pre-existing settings.json at all — the
# generator must create the output directory and file from a cold start
# (mirrors a genuinely fresh ~/.claude with no settings.json yet).
# ---------------------------------------------------------------------------


def test_fresh_install_no_positive_marker_stays_off(coordinator_root: Path, tmp_path: Path):
    """2026-07-28 polarity inversion: a genuinely fresh machine — no
    settings.json, no positive marker, no local generation evidence — must
    stay OFF by default rather than silently seeding a fresh hooks block.
    This is the deliberate behavior change from the pre-inversion oracle
    (see `test_fresh_install_smoke_seeds_once_enabled` below for the
    opted-in fresh-install path)."""
    out_path = tmp_path / "fresh-home" / ".claude" / "settings.json"
    assert not out_path.parent.exists()

    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "skipped (no positive marker)"
    assert not out_path.exists()
    assert not out_path.parent.exists()


def test_fresh_install_smoke_seeds_once_enabled(coordinator_root: Path, tmp_path: Path):
    """Same fresh-machine shape as above, but the positive marker has been
    created (e.g. by the installer's own enable step) — generation proceeds
    exactly as the pre-inversion oracle expected."""
    out_path = tmp_path / "fresh-home" / ".claude" / "settings.json"
    assert not out_path.parent.exists()
    _enable(out_path)

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert out_path.is_file()
    settings = json.loads(out_path.read_text())
    assert "hooks" in settings
    assert len(list(_iter_all_hooks(settings))) == 5


# ---------------------------------------------------------------------------
# Windows portability (addendum A4): drive-letter backslash normalisation.
# ---------------------------------------------------------------------------


def test_windows_backslash_coordinator_root_is_normalised(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-win.json"
    backslashed = str(coordinator_root).replace("/", "\\")

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=backslashed,
    )

    settings = json.loads(out_path.read_text())
    command_hooks = [h for h in _iter_all_hooks(settings) if h.get("type") == "command"]
    # The portable form itself legitimately contains an escaped `\"` (JSON
    # quoting the shell double-quotes around `$HOME/...`) — assert no
    # WINDOWS-STYLE backslash PATH separator survives (the actual thing
    # this test guards), not a blanket "no backslash byte anywhere", which
    # the portable form's own quoting would now trip on unrelated grounds.
    for hook in command_hooks:
        assert not _DRIVE_LETTER_RE.search(hook["command"])
        assert backslashed not in hook["command"]

    # A backslashed --coordinator-root no longer needs to normalise into an
    # emitted PATH (the portable form never bakes coordinator_root at all),
    # but the backslash still had to normalise cleanly enough for the
    # generator to LOCATE and READ this machine's own hooks.json under that
    # root — assert the fixture's 5 CPR command hooks actually came through,
    # not just that no backslash survived (which would pass vacuously even
    # if hooks.json were never found and generate() failed silently short).
    assert len(list(_iter_all_hooks(settings))) == 5


# ---------------------------------------------------------------------------
# resolve_settings_out_path — HOME resolution (review: code-reviewer P3,
# 2026-07-28): os.environ.get("HOME", default) only applies `default` when
# HOME is ABSENT, not when it is empty-string, yielding a relative
# ".claude/settings.json" for a launcher that does `set "HOME="`.
# ---------------------------------------------------------------------------


def test_resolve_settings_out_path_explicit_out_path_wins(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    assert resolve_settings_out_path("/explicit/settings.json") == "/explicit/settings.json"


def test_resolve_settings_out_path_falls_back_to_path_home_when_unset(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    resolved = Path(resolve_settings_out_path())
    assert resolved.is_absolute()
    assert resolved == Path.home() / ".claude" / "settings.json"


def test_resolve_settings_out_path_falls_back_to_path_home_when_empty(monkeypatch):
    # The bug: os.environ.get("HOME", default) only applies `default` when
    # HOME is absent — an exported-empty HOME="" survived through unfixed,
    # yielding a cwd-relative ".claude/settings.json".
    monkeypatch.setenv("HOME", "")
    resolved = Path(resolve_settings_out_path())
    assert resolved.is_absolute()
    assert resolved == Path.home() / ".claude" / "settings.json"


def test_resolve_settings_out_path_uses_home_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = Path(resolve_settings_out_path())
    assert resolved == tmp_path / ".claude" / "settings.json"


# ---------------------------------------------------------------------------
# Portability regression suite (2026-07-28) — a POSIX host's settings.json
# was silently overwritten with a Windows peer's baked `X:/DoE-claude/...`
# hook-command paths by a cross-machine sync of the file, killing every
# coordinator hook there with no error surfaced anywhere. These tests assert
# the structural invariant that makes that class of failure impossible:
# generated hook commands never bake a machine- or clone-location-specific
# absolute path, so the emitted settings.json is identical no matter which
# machine (or which clone location on that machine) produced it.
# ---------------------------------------------------------------------------


def test_no_absolute_machine_path_or_drive_letter_in_generated_commands(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-portable.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())
    command_hooks = [h for h in _iter_all_hooks(settings) if h.get("type") == "command"]
    assert len(command_hooks) == 5  # sanity: the rewrite actually ran

    for hook in command_hooks:
        command = hook["command"]
        assert not _DRIVE_LETTER_RE.search(command), f"drive-letter path leaked: {command!r}"
        assert str(coordinator_root) not in command, f"fixture's own tmp_path leaked: {command!r}"
        assert "${CLAUDE_PLUGIN_ROOT}" not in command, f"unrewritten CPR survived: {command!r}"
        # Every rewritten command must anchor on the fixed, self-resolving
        # portable expression — never a bare filesystem path.
        assert _PORTABLE_EXPR in command


def test_generated_hook_commands_are_byte_identical_across_simulated_repo_roots(tmp_path: Path):
    """THE portability invariant: two machines (or two clone locations on
    the same machine) generating from otherwise-identical hooks.json must
    produce byte-identical HOOK COMMANDS. A baked-absolute-path generator
    fails this by construction (the fixture roots differ); this generator
    passes because coordinator_root never reaches an emitted command — it is
    written exactly once, into `env`, which is DELIBERATELY the one place
    still allowed (and expected) to differ between the two runs below. A
    prior revision of this fix asserted whole-file byte-identity; that
    invariant doesn't survive introducing `env` as the one-write location on
    purpose — the meaningful, still-true invariant is scoped to `hooks`."""
    root_a = tmp_path / "machine-a" / "some" / "clone" / "coordinator"
    root_b = tmp_path / "machine-b-drive-x" / "totally" / "different" / "clone" / "coordinator"
    (root_a / "hooks").mkdir(parents=True)
    (root_b / "hooks").mkdir(parents=True)

    out_a = tmp_path / "settings-a.json"
    out_b = tmp_path / "settings-b.json"
    _enable(out_a)
    _enable(out_b)

    generate(
        out_path=str(out_a),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(root_a),
    )
    generate(
        out_path=str(out_b),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(root_b),
    )

    settings_a = json.loads(out_a.read_text())
    settings_b = json.loads(out_b.read_text())

    # The invariant that matters: hook COMMAND TEXT is identical no matter
    # which clone location generated it.
    assert settings_a["hooks"] == settings_b["hooks"]

    # The one place that's SUPPOSED to differ — and it must actually reflect
    # each run's own root, not silently share one value or go missing.
    assert settings_a["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == root_a.as_posix()
    assert settings_b["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == root_b.as_posix()
    assert settings_a["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] != settings_b["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY]

    # No command anywhere carries either root's path text or a drive letter.
    commands = [h["command"] for h in _iter_all_hooks(settings_a) if h.get("type") == "command"]
    commands += [h["command"] for h in _iter_all_hooks(settings_b) if h.get("type") == "command"]
    for command in commands:
        assert str(root_a) not in command
        assert str(root_b) not in command
        assert not _DRIVE_LETTER_RE.search(command)


def test_assert_portable_command_fails_loud_on_residual_cpr():
    with pytest.raises(GenSettingsHooksError, match=r"unrewritten"):
        _assert_portable_command(
            "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/x.py", event="SessionStart"
        )


def test_assert_portable_command_fails_loud_on_drive_letter():
    with pytest.raises(GenSettingsHooksError, match=r"[Dd]rive-letter"):
        _assert_portable_command(
            "python3 X:/DoE-claude/coordinator/hooks/scripts/x.py", event="SessionStart"
        )


def test_assert_portable_command_accepts_the_portable_form():
    # Must not raise.
    _assert_portable_command(
        f"python3 {_PORTABLE_EXPR}/hooks/scripts/x.py", event="SessionStart"
    )


def test_stray_check_detects_portable_form_stray(coordinator_root: Path):
    """Mirrors test (f) but for a hand-authored hook that ALREADY uses the
    portable-form prefix instead of a legacy baked-absolute path — such a
    hook would also be silently clobbered on regeneration if the stray
    check only recognised the legacy prefix."""
    out_path = coordinator_root.parent / "settings-f-portable.json"
    stray_command = f"bash {_PORTABLE_EXPR}/hooks/my-hand-authored-hook.sh"
    out_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [{"type": "command", "command": stray_command}],
                        }
                    ]
                }
            }
        )
    )

    with pytest.raises(GenSettingsHooksError) as excinfo:
        generate(
            out_path=str(out_path),
            hooks_json_override=str(_ORACLE_HOOKS_JSON),
            coordinator_root_override=str(coordinator_root),
        )

    msg = str(excinfo.value)
    assert "my-hand-authored-hook.sh" in msg
    assert "SessionStart" in msg


# ---------------------------------------------------------------------------
# `env` block — the one-write-location design (replaces the earlier
# command-substitution revision; see module docstring history).
# ---------------------------------------------------------------------------


def test_env_block_carries_coordinator_content_root(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-env.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())
    assert settings["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == coordinator_root.as_posix()


def test_env_block_preserves_other_keys_and_overwrites_only_its_own(coordinator_root: Path):
    out_path = coordinator_root.parent / "settings-env-preserve.json"
    out_path.write_text(
        json.dumps(
            {
                "env": {
                    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                    COORDINATOR_CONTENT_ROOT_ENV_KEY: "/some/stale/prior/value",
                },
                "hooks": {},
            }
        )
    )

    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    # Operator-set key untouched.
    assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    # Generator-owned key overwritten to THIS run's value, not left stale.
    assert settings["env"][COORDINATOR_CONTENT_ROOT_ENV_KEY] == coordinator_root.as_posix()


def test_hook_root_env_expr_uses_env_prefix_on_windows_and_bare_var_on_posix():
    assert hook_root_env_expr(windows=False) == f"${COORDINATOR_CONTENT_ROOT_ENV_KEY}"
    assert hook_root_env_expr(windows=True) == f"$env:{COORDINATOR_CONTENT_ROOT_ENV_KEY}"
    # No `${VAR:-default}` bash-parameter-expansion shape and no `$(...)`
    # command substitution in either form — both would be a regression
    # (the first is a PowerShell syntax error, the second re-introduces the
    # per-hook-fire extra process spawn this design exists to eliminate).
    for expr in (hook_root_env_expr(windows=False), hook_root_env_expr(windows=True)):
        assert ":-" not in expr
        assert "$(" not in expr


# ---------------------------------------------------------------------------
# Double-fire refusal (2026-07-29): generate() must refuse to emit a
# settings.json hooks block when plugin-side delivery is VERIFIED live and
# resolvable on this machine -- and must fall through to the pre-existing
# marker-gated generation path on ANY absence of that positive evidence.
# See gen_settings_hooks.py module docstring, "Double-fire refusal".
# ---------------------------------------------------------------------------


def _write_plugin_side_hooks_json(content_root: Path, *, script_exists: bool) -> None:
    """Build a minimal plugin-side ``hooks/hooks.json`` under `content_root`
    with exactly one CPR command hook, whose declared script either does or
    does not exist on disk -- the two knobs the real
    ``detect_hook_delivery_duplication`` reachability check keys off."""
    hooks_dir = content_root / "hooks"
    scripts_dir = hooks_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    if script_exists:
        (scripts_dir / "session-guard.sh").write_text("#!/bin/sh\nexit 0\n")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session-guard.sh",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )


def test_skips_when_plugin_delivery_is_live_and_resolvable(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    plugin_root = tmp_path / "live-plugin-root"
    plugin_root.mkdir()
    _write_plugin_side_hooks_json(plugin_root, script_exists=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    out_path = coordinator_root.parent / "settings-live.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "skipped (plugin delivery already live)"
    assert not out_path.exists()


def test_generates_when_plugin_hooks_json_absent(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    plugin_root = tmp_path / "no-hooks-json-plugin-root"
    plugin_root.mkdir()
    # No hooks/hooks.json created under plugin_root at all.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    out_path = coordinator_root.parent / "settings-absent.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "seeded"
    assert out_path.is_file()


def test_generates_when_plugin_script_paths_do_not_resolve(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    plugin_root = tmp_path / "unresolvable-script-plugin-root"
    plugin_root.mkdir()
    _write_plugin_side_hooks_json(plugin_root, script_exists=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    out_path = coordinator_root.parent / "settings-unresolvable.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "seeded"
    assert out_path.is_file()


def test_generates_when_content_root_cannot_be_resolved_at_all(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    # No CLAUDE_PLUGIN_ROOT, no COORDINATOR_ROOT -- and the autouse
    # `_quarantine_real_home` fixture already points HOME at an empty
    # quarantine dir, so the registry/`.doe-root`-pointer rungs of
    # `resolve_content_root()` cannot find anything either. This is the
    # "content root cannot be resolved at all" case.
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)

    out_path = coordinator_root.parent / "settings-no-root.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "seeded"
    assert out_path.is_file()


def test_skip_announcement_fires_and_names_the_reason(
    coordinator_root: Path, tmp_path: Path, monkeypatch, capsys
):
    plugin_root = tmp_path / "announce-plugin-root"
    plugin_root.mkdir()
    _write_plugin_side_hooks_json(plugin_root, script_exists=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    out_path = coordinator_root.parent / "settings-announce.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    stderr = capsys.readouterr().err
    assert "plugin-side hook delivery" in stderr
    assert "already live" in stderr
    assert "deliberate" in stderr


def test_existing_settings_hooks_block_left_byte_identical_when_skipping(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    out_path = coordinator_root.parent / "settings-preexisting.json"
    pre_existing = json.dumps({"hooks": {"SessionStart": []}, "env": {}}, indent=2) + "\n"
    out_path.write_text(pre_existing)

    live_report = HookDeliveryReport(
        plugin_present=True,
        plugin_resolvable=True,
        plugin_entry_count=1,
        settings_present=False,
        settings_resolvable=False,
        settings_entry_count=0,
        duplicated_scripts=[],
    )
    monkeypatch.setattr(
        _gsh_module, "detect_hook_delivery_duplication", lambda **kw: live_report
    )

    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "skipped (plugin delivery already live)"
    assert out_path.read_text() == pre_existing


def test_double_fire_refusal_reuses_the_detector_no_second_resolver():
    from coordinator_core.ops.session import guard_settings_integrity as gsi

    # Import-identity proof: gen_settings_hooks does not define its own
    # hook-path-resolution/duplication-detection logic -- it holds a
    # reference to the SAME function object guard_settings_integrity
    # exports (and that the SessionStart double-fire banner also calls).
    assert _gsh_module.detect_hook_delivery_duplication is gsi.detect_hook_delivery_duplication


def test_double_fire_refusal_calls_the_reused_detector(
    coordinator_root: Path, tmp_path: Path, monkeypatch
):
    calls = []
    real = _gsh_module.detect_hook_delivery_duplication

    def _spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(_gsh_module, "detect_hook_delivery_duplication", _spy)

    out_path = coordinator_root.parent / "settings-spy.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert len(calls) == 1
    assert calls[0]["config_dir"] == out_path.parent


# ---------------------------------------------------------------------------
# C4 dispatch, point 8 — AC4 regeneration idempotence over the widened
# (env-var-referenced) guard shape: a settings.json already carrying that
# shape (i.e., this run's own prior output, on a machine where
# `resolve_hook_python_bin()` resolves something) must not duplicate a
# single group on a second run. bb88f375e was a SILENT duplication failure,
# so this asserts the COUNT, not merely that generate() didn't raise.
# ---------------------------------------------------------------------------


def test_ac4_regenerate_over_env_var_referenced_shape_group_count_stable(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    # Review finding F1 (2026-08-03 code-reviewer): force
    # `resolve_hook_python_bin()` to a known value so this test deterministically
    # exercises the widened (env-var-referenced) fourth-era guard shape, rather
    # than silently degrading to the pre-existing three-era path on any box
    # that doesn't happen to resolve a pinned interpreter.
    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/fake/venv/bin/python3")

    out_path = coordinator_root.parent / "settings-ac4.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings_first = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings_first["env"]  # sanity: fourth-era shape actually emitted
    group_count_first = sum(len(groups) for groups in settings_first["hooks"].values())
    hook_count_first = len(list(_iter_all_hooks(settings_first)))
    assert hook_count_first == 5  # sanity: the oracle fixture's known CPR-command count

    # Regenerate over this run's OWN output -- now deterministically carrying
    # the widened, env-var-referenced guard shape this AC is about -- the
    # group/hook count must not move.
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings_second = json.loads(out_path.read_text())
    group_count_second = sum(len(groups) for groups in settings_second["hooks"].values())
    hook_count_second = len(list(_iter_all_hooks(settings_second)))

    assert group_count_second == group_count_first
    assert hook_count_second == hook_count_first


# ---------------------------------------------------------------------------
# C4 dispatch, point 9 — AC7 zero-strays-on-differing-bake: `_stray_check`
# compares emitted COMMAND TEXT (built fresh from hooks.json + THIS run's own
# `python_bin_resolved` flag), never against a stale `env` value -- so a
# settings.json whose `env.COORDINATOR_PYTHON_BIN` names a DIFFERENT
# machine's interpreter path must never be mistaken for a stray hand-authored
# hook. Distinct from AC4 above: that guards duplication, this guards a
# false-positive stray-check failure (`generate()` raising when it must not).
# ---------------------------------------------------------------------------


def test_ac7_stray_check_zero_strays_when_env_bakes_differing_python_bin(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    # Review finding F1/F2 (2026-08-03 code-reviewer): force
    # `resolve_hook_python_bin()` to a known value so this test actually
    # reaches the fourth-era guard shape `_cmd_path`/`_stray_check` must
    # classify -- without this, the test would pass identically whether or
    # not C3's `_cmd_path` fix for that shape exists, giving it zero power
    # over the regression it's named for.
    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/fake/venv/bin/python3")

    out_path = coordinator_root.parent / "settings-ac7.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings["env"]  # sanity: fourth-era shape actually emitted
    group_count_before = sum(len(groups) for groups in settings["hooks"].values())
    settings["env"][COORDINATOR_PYTHON_BIN_ENV_KEY] = "/some/other/machines/venv/bin/python3-DIFFERENT"
    out_path.write_text(json.dumps(settings, indent=2) + "\n")

    # A stray-check false-positive raises GenSettingsHooksError -- must NOT.
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    assert status == "seeded"

    # Review finding F2: independently confirm `_cmd_path`/`_group_is_generated`
    # correctly classified the mutated-env fourth-era shape as a real generated
    # hook (not an unrecognized shape `_stray_check` silently `continue`s past)
    # by asserting the group count is unchanged -- a fourth-era classification
    # regression must surface here as a duplicated group or a raised error,
    # never slip through an unrelated `continue`.
    settings_after = json.loads(out_path.read_text())
    group_count_after = sum(len(groups) for groups in settings_after["hooks"].values())
    assert group_count_after == group_count_before


# ---------------------------------------------------------------------------
# Break-class fix (2026-08-03, docs/plans/2026-08-03-hooks-baked-interpreter-
# resolution.md AC3/AC7/C1-F7): `_stray_check` must survive an interpreter
# RESOLUTION transition across regenerations, not just a differing baked
# VALUE within the same resolved shape (AC7 above). A resolved -> unresolved
# transition (e.g. a venv rebuild) changes the emitted command TEXT itself
# (fourth-era guarded shape -> bare `python3` shape, per `_cmd_path`'s own
# docstring) -- a text-equality stray-check would misclassify every one of
# its own prior-era hooks as hand-authored and abort `generate()` on exactly
# the repair path (regeneration) that scenario needs. Fixed by comparing
# `_cmd_path` identity instead of raw command text.
# ---------------------------------------------------------------------------


def test_regen_survives_resolved_to_unresolved_interpreter_transition(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/fake/venv/bin/python3")
    out_path = coordinator_root.parent / "settings-regen-resolved-to-unresolved.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings_first = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings_first["env"]  # sanity: resolved shape actually emitted

    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "")
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    assert status == "seeded"  # must NOT raise GenSettingsHooksError

    settings_second = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY not in settings_second["env"]  # bare shape dropped the env key
    hooks_text = json.dumps(settings_second["hooks"])
    assert "COORDINATOR_PYTHON_BIN" not in hooks_text  # emitted commands are the bare-token shape


def test_regen_survives_unresolved_to_resolved_interpreter_transition(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "")
    out_path = coordinator_root.parent / "settings-regen-unresolved-to-resolved.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings_first = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY not in settings_first["env"]  # sanity: bare shape actually emitted

    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/fake/venv/bin/python3")
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    assert status == "seeded"  # must NOT raise GenSettingsHooksError

    settings_second = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings_second["env"]  # resolved shape now emitted


# ---------------------------------------------------------------------------
# C4 dispatch, point 10 — cross-surface pin: `detect_foreign_platform_paths`
# (coordinator_core.ops.session.guard_foreign_platform_paths, located by
# grep under that exact name -- present at HEAD, no substitute needed) must
# report NO finding over a generated settings.json, checked for BOTH
# `host_is_windows` polarities.
#
# The POSIX-generated form is checked against ITS OWN platform
# (`host_is_windows=False`) via a real `generate()` call. The Windows-shaped
# form is built directly via `wrap_hook_command_guarded(..., windows=True)`
# rather than through `generate()`, which derives its own `windows` flag from
# `os.name` internally and cannot be forced to the Windows branch from a
# POSIX test runner -- this is still the REAL production shape (the exact
# string `wrap_hook_command_guarded` emits), not a hand-approximated one.
# ---------------------------------------------------------------------------


def test_cross_surface_pin_detect_foreign_platform_paths_clean_for_posix_shape(
    monkeypatch: pytest.MonkeyPatch, coordinator_root: Path
):
    # Review finding F1 (2026-08-03 code-reviewer): force a resolved
    # interpreter so this pin actually covers the fourth-era (env-var-
    # referenced) POSIX guard shape, not just whatever the pre-existing
    # three-era path happens to be on this box.
    monkeypatch.setattr(_gsh_module, "resolve_hook_python_bin", lambda: "/fake/venv/bin/python3")
    # Force POSIX generation regardless of the test runner's real host --
    # see `_OSNameProxy` and this test's own "checked against ITS OWN
    # platform ... via a real generate() call" contract above. A real
    # `tmp_path`-derived `coordinator_root` is unavoidably drive-lettered on
    # a real-Windows filesystem, so the env value it produces would trip
    # `detect_foreign_platform_paths`'s drive-letter check regardless of
    # command shape -- swap in a synthetic POSIX root instead (via
    # `_OSPosixPathOverrideProxy`'s faked `isdir`) so this test isolates the
    # thing it actually pins: command-shape cleanliness, not this runner's
    # filesystem drive letter.
    monkeypatch.setattr(_gsh_module, "os", _OSPosixPathOverrideProxy())
    posix_root = "/fake/posix/coordinator"

    out_path = coordinator_root.parent / "settings-ac-crosssurface-posix.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=posix_root,
    )
    settings = json.loads(out_path.read_text())
    assert COORDINATOR_PYTHON_BIN_ENV_KEY in settings["env"]  # sanity: fourth-era shape actually emitted
    findings = detect_foreign_platform_paths(settings, host_is_windows=False)
    assert findings == []


def test_cross_surface_pin_detect_foreign_platform_paths_clean_for_windows_shape():
    # Illustrative Windows-shaped placeholder paths for a detector fixture,
    # never a real host -- see the module-level comment above.
    windows_root = "C:/Users/Jane/DoE-claude/coordinator"  # abs-path-ok: illustrative placeholder, not a real host
    windows_python = "C:/Users/Jane/.venv/Scripts/python.exe"  # abs-path-ok: illustrative placeholder, not a real host
    windows_settings = {
        "env": {
            COORDINATOR_CONTENT_ROOT_ENV_KEY: windows_root,
            COORDINATOR_PYTHON_BIN_ENV_KEY: windows_python,
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": wrap_hook_command_guarded(
                                "python3 $env:COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py",
                                windows=True,
                                python_bin_resolved=True,
                            ),
                        }
                    ],
                }
            ]
        },
    }
    findings = detect_foreign_platform_paths(windows_settings, host_is_windows=True)
    assert findings == []


# ---------------------------------------------------------------------------
# code-reviewer F2 (2026-08-03) — `_merge_env` must POP a stale
# COORDINATOR_PYTHON_BIN, not merely leave it unwritten, on a
# resolved-then-unresolved transition across two runs: the key is fully
# generator-owned (mirroring COORDINATOR_CONTENT_ROOT), so a regression
# in resolution must be reflected in `env`, not silently left stale.
# ---------------------------------------------------------------------------


def test_merge_env_pops_stale_python_bin_on_resolution_regression():
    _merge_env = _gsh_module._merge_env
    current_settings = {
        "env": {
            COORDINATOR_CONTENT_ROOT_ENV_KEY: "/old/coordinator/root",
            COORDINATOR_PYTHON_BIN_ENV_KEY: "/some/venv/bin/python3",
            "SOME_OTHER_KEY": "preserved",
        }
    }
    merged = _merge_env(current_settings, "/new/coordinator/root", python_bin="")
    assert COORDINATOR_PYTHON_BIN_ENV_KEY not in merged
    assert merged[COORDINATOR_CONTENT_ROOT_ENV_KEY] == "/new/coordinator/root"
    assert merged["SOME_OTHER_KEY"] == "preserved"


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — clause 0, the sole SHAPED clause
# (`_HOOKS_MERGE_CLAUSE_INDEX`, the `hooks.<event>` structured-file-key merge).
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("gen-settings-hooks", {}).get(_gsh_module._HOOKS_MERGE_CLAUSE_INDEX)


def test_journal_records_merged_event_keys(coordinator_root: Path, _journal_env):
    out_path = coordinator_root.parent / "settings-journal.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {f"hooks.{event}" for event in settings["hooks"]}
    for entry in resolution.entries:
        assert entry.kind == "structured-file-key"
        assert entry.path == str(out_path)


def test_journal_empty_entries_on_kill_switch_marker(tmp_path, _journal_env):
    """The operator kill-switch marker is a genuine 'resolved to nothing'
    decision for this run, journaled as an empty tuple — not a phantom
    write, but also not 'never got there'."""
    killswitch_dir = tmp_path / "killswitch"
    killswitch_dir.mkdir()
    out_path = killswitch_dir / "settings.json"
    out_path.write_text(json.dumps({"hooks": {}, "marker": "deliberately-stripped"}))
    (killswitch_dir / ".coordinator-hooks-disabled").write_text("")

    generate(out_path=str(out_path), coordinator_root_override="/should-not-be-resolved")

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_empty_entries_on_no_positive_marker(tmp_path, _journal_env):
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    out_path = off_dir / "settings.json"

    status = generate(out_path=str(out_path), coordinator_root_override="/should-not-be-resolved")

    assert status == "skipped (no positive marker)"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_unreported_on_check_only(coordinator_root: Path, _journal_env):
    """check-only never resolves or mutates anything (module contract) —
    genuinely 'never got there' for this clause, distinct from the
    empty-tuple 'resolved to nothing' cases above."""
    out_path = coordinator_root.parent / "settings-check-only.json"
    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
        check_only=True,
    )
    assert status == "skipped (check-only)"
    assert _resolved(_journal_env) is None


def test_journal_omits_entry_for_unresolvable_coordinator_root(tmp_path, _journal_env, monkeypatch):
    """Clause resolves to nothing (discovery came up empty), not
    'never got there' — the kill-switch/no-marker branches above are
    reached earlier in `generate()` and are unreported here only because
    they never fire in this scenario; this one exercises the
    coordinator-root-discovery-empty branch specifically. `resolve_coordinator_root`
    is monkeypatched to fail deterministically rather than relying on this
    machine's own registry state coming up empty."""
    d = tmp_path / "clone-absent"
    d.mkdir()
    out_path = d / "settings.json"
    (d / ".coordinator-hooks-enabled").touch()

    def _unresolvable():
        raise RuntimeError("no coordinator root resolvable")

    monkeypatch.setattr(_gsh_module, "resolve_coordinator_root", _unresolvable)

    status = generate(out_path=str(out_path))

    assert status == "skipped (clone absent)"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_omits_entry_when_mutation_disabled(coordinator_root: Path, _journal_env, monkeypatch):
    """`gen_settings_hooks.py` does not itself gate the settings.json write
    on `COORDINATOR_DISABLE_MACHINE_MUTATION` — only the journal append
    does, via `resolution_journal.record_resolution`'s own guard. Setting
    the kill switch refuses only the journal row: the write still lands on
    disk, but this clause is left UNREPORTED for this run rather than
    journaled with an entry no uninstall pass can trust came from the
    journal's own machine-mutation-gated append."""
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    out_path = coordinator_root.parent / "settings-mutation-disabled.json"

    status = generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )

    assert status == "seeded"
    assert out_path.is_file()
    assert _resolved(_journal_env) is None


# ---------------------------------------------------------------------------
# Per-hook `timeout` is bounded, not forwarded verbatim
#
# The field is the HARNESS's kill ceiling for a hook on the session/commit hot
# path, and its value comes from a plugin-side hooks.json this repo does not
# contain — so until 2026-08-21 an off-repo number set how long a wedged hook
# could hold a session, with nothing in this suite able to see it. The oracle
# fixture's `bootstrap-substrate.py` SessionStart entry carries a 120 that
# proves the shape was reachable.
# ---------------------------------------------------------------------------


def test_oracle_fixture_timeouts_are_all_bounded_by_the_ceiling(coordinator_root: Path):
    """End-to-end, through `generate()`: no emitted hook carries a timeout
    above the ceiling, INCLUDING the fixture's 120s SessionStart entry."""
    out_path = coordinator_root.parent / "settings-timeout-ceiling.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    emitted = [h.get("timeout") for h in _iter_all_hooks(settings) if "timeout" in h]
    assert emitted, "fixture carries timeouts; an empty set would make this vacuous"
    assert max(emitted) <= HOOK_TIMEOUT_CEILING_SECS

    source = json.loads(_ORACLE_HOOKS_JSON.read_text())
    over = [
        h["timeout"]
        for groups in source["hooks"].values()
        for g in groups
        for h in g.get("hooks", [])
        if h.get("timeout", 0) > HOOK_TIMEOUT_CEILING_SECS
    ]
    assert over, "the input fixture must keep at least one over-ceiling value or this proves nothing"


def test_over_ceiling_timeout_is_reported_naming_event_and_original(
    coordinator_root: Path, capsys: pytest.CaptureFixture
):
    """The clamp is announced, not silent — an operator who wrote 120 in the
    sibling repo learns their number did not survive, and where to change it."""
    out_path = coordinator_root.parent / "settings-timeout-report.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    err = capsys.readouterr().err

    assert f"over the {HOOK_TIMEOUT_CEILING_SECS}s ceiling" in err
    assert "event=SessionStart" in err
    assert "timeout=120" in err
    assert str(_ORACLE_HOOKS_JSON) in err
    assert "Lower it" in err


def test_under_ceiling_timeout_is_forwarded_untouched(coordinator_root: Path):
    """The clamp bounds; it does not normalize. A hook already inside the
    ceiling keeps its own number, so this is not a flat rewrite of the field."""
    out_path = coordinator_root.parent / "settings-timeout-passthrough.json"
    generate(
        out_path=str(out_path),
        hooks_json_override=str(_ORACLE_HOOKS_JSON),
        coordinator_root_override=str(coordinator_root),
    )
    settings = json.loads(out_path.read_text())

    session_guard = [
        h
        for h in _iter_all_hooks(settings)
        if "session-guard.sh" in h.get("command", "")
    ]
    assert len(session_guard) == 1
    assert session_guard[0]["timeout"] == 5


@pytest.mark.parametrize(
    "value",
    [None, "30", True, False, 0, -1, [], {}],
    ids=["absent", "string", "true", "false", "zero", "negative", "list", "dict"],
)
def test_non_numeric_or_non_positive_timeout_is_left_alone(value):
    """`_clamp_hook_timeout` bounds a number the harness will act on; it is
    not a schema validator for the sibling repo's file, so anything that is
    not a positive real number passes through exactly as it arrived.

    `True`/`False` are in the corpus deliberately: `bool` is an `int`
    subclass, so a naive isinstance check would read `"timeout": true` as a
    1-second ceiling rather than a malformed field."""
    hook = {"command": "x"} if value is None else {"command": "x", "timeout": value}
    before = dict(hook)
    clamped: list = []

    _clamp_hook_timeout(hook, "SessionStart", clamped)

    assert hook == before
    assert clamped == []


def test_clamp_fires_exactly_at_the_boundary():
    """The ceiling itself is allowed; one over it is not."""
    at = {"command": "x", "timeout": HOOK_TIMEOUT_CEILING_SECS}
    _clamp_hook_timeout(at, "SessionStart", None)
    assert at["timeout"] == HOOK_TIMEOUT_CEILING_SECS

    over = {"command": "x", "timeout": HOOK_TIMEOUT_CEILING_SECS + 0.5}
    rows: list = []
    _clamp_hook_timeout(over, "PreToolUse", rows)
    assert over["timeout"] == HOOK_TIMEOUT_CEILING_SECS
    assert rows == [("PreToolUse", "x", HOOK_TIMEOUT_CEILING_SECS + 0.5)]


def test_ceiling_stays_a_backstop_not_a_budget():
    """A ratchet on the number itself. CLAUDE.md forbids any process past 2s
    and puts the fix bar at 200ms; a kill ceiling must sit above the target
    it guards, but a hook allowed to run for a minute is one the harness
    never reclaims. Raising this past 10 is a doctrine change, not an edit."""
    assert 2 < HOOK_TIMEOUT_CEILING_SECS <= 10
