"""Tests for the out-of-harness doctor command.

The doctor exists because five occurrences in two days of the same incident class left the
agent unable to repair the break, since the break removed Write, Edit and Bash. Its value is
therefore entirely in being *right* about a broken machine while a human reads it in a plain
terminal — a doctor that reports OK on a broken layer is worse than no doctor, because it
converts "something is wrong" into "something is wrong and the tool says it isn't".

These drive the real CLI as a subprocess rather than calling the op directly. Invocation is
part of what is under test: the trampoline resolves its own claude-klabauter root and the whole point is
that it runs with no Claude Code process involved.

Fixtures point `REPO_DOE_CLAUDE` at a throwaway tree. Never at the live one — a health check
tested against live shared config is the thing it is meant to catch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_DOCTOR = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin" / "doctor.py"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _write_hooks(doe_root: Path, command: str) -> None:
    hooks_dir = doe_root / "coordinator" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command", "command": command}]}]}},
            indent=2,
        )
    )


_LOADER = (
    "import os,sys;b=sys.argv.pop();exec(open(b,encoding='utf-8').read()) if os.path.isfile(b) "
    "else sys.stderr.write(\"COORDINATOR HOOK SEAM: bootstrap missing, hooks fail OPEN: \"+b)"
)


def _write_hooks_exec_form(doe_root: Path, script_token: str) -> None:
    """The exec-form registration `fail_open_launcher.wrap_command_exec` emits and
    every live hooks.json entry uses: bare interpreter in `command`, real argv in
    `args`, no shell in the path. The doctor's original extractor only understood
    the legacy single-string form, so against this shape every entry parsed as
    "shape not understood" and the missing-on-disk stat never ran."""
    hooks_dir = doe_root / "coordinator" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{
                "type": "command",
                "command": "python3",
                "timeout": 15,
                "args": [
                    "-c", _LOADER,
                    script_token,
                    "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/_hook_venv_inject.py",
                    "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/_hook_boot.py",
                ],
            }]}]}},
            indent=2,
        )
    )


def _run_doctor(doe_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_DOCTOR), *args],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
        # Both roots pinned explicitly. Otherwise the sibling-resolution layer reports
        # BROKEN under pytest (the machine-local registry is not reachable there) and every
        # assertion about the hook layer ends up hostage to an unrelated one.
        #
        # COORDINATOR_ENGINE_ROOT, not CLAUDE_KLABAUTER_ROOT. C14 (`fb1421af2`) stopped `engine_root`
        # honouring the old name -- it is read now only to emit the "no longer honoured"
        # advisory -- so the pin above silently stopped landing and the hostage situation
        # this comment describes came back, as four reds that read as unrelated. This file
        # is the same population as
        # state/bug-backlog/2026-08-25-fixture-env-dicts-still-pin-the-retired-claude-klabauter-live-root,
        # missed by that sweep because a fixture WRITES the var and never reads it.
        env=dict(
            os.environ,
            REPO_DOE_CLAUDE=str(doe_root),
            REPO_CLAUDE_KLABAUTER=str(_CLAUDE_KLABAUTER_ROOT),
            COORDINATOR_ENGINE_ROOT=str(_CLAUDE_KLABAUTER_ROOT),
        ),
    )


@pytest.fixture
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "DoE-claude"
    (root / "coordinator" / "hooks" / "scripts").mkdir(parents=True)
    return root


def test_registration_pointing_at_a_missing_script_is_reported_broken(doe_root: Path):
    """The exact 2026-07-29 incident: a registration outliving the script it names. Every
    on-disk consistency check passed while this was true, which is why the doctor has to be
    the thing that catches it."""
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/deleted-by-a-peer.py")

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "BROKEN" in result.stdout
    assert "deleted-by-a-peer.py" in result.stdout, "the report must name the missing script"
    assert "Hook registration" in result.stdout


def test_a_healthy_registration_is_quiet(doe_root: Path):
    """Quiet on clean. A check that fires on benign states is muted within a week, and this
    guard family already has members that went inert exactly that way."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    result = _run_doctor(doe_root)

    assert "BROKEN" not in result.stdout, result.stdout
    assert "registered script missing" not in result.stdout


def test_a_bare_registration_is_flagged_as_not_fail_open(doe_root: Path):
    """A present script that is nonetheless registered bare is a latent instance of the same
    incident — it works until the day the path stops resolving."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    result = _run_doctor(doe_root)

    assert "bare" in result.stdout.lower(), result.stdout


def test_exit_code_distinguishes_broken_from_healthy(doe_root: Path):
    """A caller gating on this must be able to tell the two apart without parsing prose."""
    script = doe_root / "coordinator" / "hooks" / "scripts" / "real.py"
    script.write_text("import sys\nsys.exit(0)\n")
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")
    healthy = _run_doctor(doe_root).returncode

    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/gone.py")
    broken = _run_doctor(doe_root).returncode

    assert broken == 1
    assert healthy != broken


def test_exec_form_registration_pointing_at_a_missing_script_is_reported_broken(doe_root: Path):
    """The vacuous-pass regression (doe-claude-em memo, 2026-08-17). Under the
    single-string-only extractor this case reported `[OK]` while emitting a
    `broken` finding for every entry — the true pass and the vacuous pass were
    the same green."""
    _write_hooks_exec_form(
        doe_root, "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/deleted-by-a-peer.py"
    )

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "BROKEN" in result.stdout, result.stdout
    assert "deleted-by-a-peer.py" in result.stdout, "the report must name the missing script"
    assert "shape not understood" not in result.stdout


def test_a_healthy_exec_form_registration_is_quiet_and_reads_as_wrapped(doe_root: Path):
    """Polarity check. The seam marker lives in `args`, not `command`, so testing
    `command` alone reported every exec-form hook as bare — and, inverted, would
    have reported a genuinely unwrapped hook as wrapped, suppressing the warning
    that exists because that state bricks every tool call."""
    scripts = doe_root / "coordinator" / "hooks" / "scripts"
    for name in ("real.py", "_hook_venv_inject.py", "_hook_boot.py"):
        (scripts / name).write_text("import sys\nsys.exit(0)\n")
    _write_hooks_exec_form(doe_root, "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")

    result = _run_doctor(doe_root)

    assert "BROKEN" not in result.stdout, result.stdout
    assert "registered script missing" not in result.stdout
    assert "bare" not in result.stdout.lower(), "exec form IS fail-open-wrapped"


def test_a_finding_the_layer_calls_broken_can_never_report_ok(doe_root: Path):
    """Defect B standalone, independent of any one encoding: status is derived
    from the findings, not from the missing-script counter. An unparseable
    registration is the general case — any future shape migration lands here."""
    hooks_dir = doe_root / "coordinator" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command", "command": "node", "args": ["--some-future-shape"]}]}]}},
            indent=2,
        )
    )

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "BROKEN" in result.stdout, result.stdout


def test_a_hook_with_no_parseable_command_is_reported_broken_not_ok(doe_root: Path):
    """The same "OK on zero parsed registrations" pathology the status-derivation
    fix (above) closes, one call frame earlier: `_hook_argv` used to return
    `None` silently for a missing/non-string/empty `command`, so
    `_iter_hook_commands` never yielded the entry, `total` stayed 0, and the
    doc took the early `return "ok", [], True` branch — a doc where every hook
    is unparseable read as clean. `_iter_hook_commands` now yields the entry
    with `argv=None` so it counts toward `total` and becomes a `broken`
    finding instead of vanishing."""
    hooks_dir = doe_root / "coordinator" / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [
                {"type": "command"}]}]}},
            indent=2,
        )
    )

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "BROKEN" in result.stdout, result.stdout
    assert "shape not understood" in result.stdout, result.stdout


def _install_real_launcher(doe_root: Path) -> None:
    """Copy the LIVE `fail_open_launcher.py` into the throwaway tree.

    The seam this exercises is a cross-repo API boundary — a hand-written stub
    would keep passing through exactly the rename that broke it (`wrap_command`
    -> `wrap_command_exec`), which is the defect, not the test. Skips rather
    than fabricating when DoE-claude is not reachable."""
    # `coordinator_doe_root()` is deliberately quarantined to a throwaway stub
    # under pytest, so the real checkout comes from conftest's collection-time
    # capture instead — the same escape hatch the manifest read uses.
    from coordinator_core.conftest import _REAL_DOE_ROOT

    src = (
        Path(_REAL_DOE_ROOT) / "coordinator" / "hooks" / "fail_open_launcher.py"
        if _REAL_DOE_ROOT
        else None
    )
    if not src or not src.is_file():
        pytest.skip("DoE-claude fail_open_launcher not reachable — nothing real to bind against")
    (doe_root / "coordinator" / "hooks").mkdir(parents=True, exist_ok=True)
    (doe_root / "coordinator" / "hooks" / "fail_open_launcher.py").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_fix_does_not_crash_or_rewrite_already_exec_wrapped_registrations(doe_root: Path):
    """Defect C: `--fix` called `fail_open_launcher.wrap_command`, removed in
    favour of `wrap_command_exec`. Since the bareness probe read only the
    `command` string (`python3`), every live hook looked unwrapped, so `--fix`
    reached the missing attribute — an uncaught AttributeError on a repair path
    whose entire premise is being usable on a broken machine."""
    scripts = doe_root / "coordinator" / "hooks" / "scripts"
    for name in ("real.py", "_hook_venv_inject.py", "_hook_boot.py"):
        (scripts / name).write_text("import sys\nsys.exit(0)\n")
    hooks_json = doe_root / "coordinator" / "hooks" / "hooks.json"
    _write_hooks_exec_form(doe_root, "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")
    _install_real_launcher(doe_root)
    before = hooks_json.read_text()

    result = _run_doctor(doe_root, "--fix")

    assert "Traceback" not in result.stderr, result.stderr
    assert "unimportable" not in result.stdout, "the launcher must actually have been bound"
    assert hooks_json.read_text() == before, "already-wrapped registrations must be left alone"


def test_fix_wraps_a_bare_registration_into_the_exec_form(doe_root: Path):
    """The repair path's positive case, bound against the real launcher: a bare
    hook comes back as an exec-form registration, and the doctor then reads its
    own repair as healthy — the round trip, not just the write."""
    scripts = doe_root / "coordinator" / "hooks" / "scripts"
    for name in ("real.py", "_hook_venv_inject.py", "_hook_boot.py"):
        (scripts / name).write_text("import sys\nsys.exit(0)\n")
    hooks_json = doe_root / "coordinator" / "hooks" / "hooks.json"
    _write_hooks(doe_root, "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py")
    _install_real_launcher(doe_root)

    result = _run_doctor(doe_root, "--fix")

    assert "Traceback" not in result.stderr, result.stderr
    hook = json.loads(hooks_json.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["command"] == "python3"
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/real.py" in hook["args"]

    after = _run_doctor(doe_root)
    assert "BROKEN" not in after.stdout, after.stdout
    assert "bare" not in after.stdout.lower(), "the doctor must read back its own repair as wrapped"


def test_legacy_form_survives_windows_path_separators(monkeypatch):
    """Windows is first-class here, so the legacy single-string extractor may
    not depend on POSIX escape semantics. Under `shlex.split`'s POSIX mode every
    backslash in a native path is eaten, the resulting path stats absent, and
    the layer reports a registration broken that is in fact fine — a false RED,
    the mirror of the false GREEN this whole family exists to prevent.

    Driven in-process rather than through the CLI: the defect is keyed on
    `os.name`, and the point is to exercise the `nt` branch from a POSIX box.
    """
    from coordinator_core.ops import doctor

    native = "<drive>:\\hooks\\scripts\\sessionstart-dispatch.py"
    hook = {"type": "command", "command": f"python3 {native}"}

    monkeypatch.setattr(doctor.os, "name", "nt")
    argv = doctor._hook_argv(hook)

    assert argv == ["python3", native], argv
    assert doctor._extract_script_path(argv) == native


def test_legacy_form_strips_quotes_from_a_path_containing_spaces(monkeypatch):
    """`posix=False` is what preserves the separators, but it also hands back
    the surrounding quotes — so the quote-stripping is load-bearing, not
    cosmetic: an unstripped token stats as a path beginning with `\"`."""
    from coordinator_core.ops import doctor

    native = "<drive>:\\Program Files\\hooks\\dispatch.py"
    hook = {"type": "command", "command": f'python3 "{native}"'}

    monkeypatch.setattr(doctor.os, "name", "nt")
    argv = doctor._hook_argv(hook)

    assert doctor._extract_script_path(argv) == native


@pytest.mark.parametrize(
    "interpreter",
    [
        "<drive>:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python313\\python3.EXE",
        "<drive>:/Users/u/AppData/Local/Programs/Python/Python313/python.exe",
        "/usr/bin/python3",
        "python3",
    ],
    ids=["windows-backslash-EXE", "windows-forward-exe", "posix-absolute", "bare"],
)
def test_an_absolute_interpreter_path_is_understood(interpreter):
    """A registration is recognised by the interpreter's BASENAME, not by the
    whole argv[0] token.

    Observed 2026-08-26 on a healthy box: a live example-game-repo hook registered as
    `<...>\\Python313\\python3.EXE <script>.py` — firing on every tool call —
    was reported `broken: command shape not understood`, because the gate
    compared argv[0] against the bare names only. Windows is first-class here
    and an absolute interpreter path is its ORDINARY shape, so the bare-name
    case is the exception, not the rule.

    The cost of getting this wrong is not one wrong line: a checker that calls
    a working registration broken is the finding a reader learns to scroll
    past, and the true finding then arrives into an audience that has stopped
    reading it.
    """
    from coordinator_core.ops import doctor

    script = "/plugins/x/hooks/scripts/nudge.py"
    assert doctor._extract_script_path([interpreter, script]) == script


def test_an_http_hook_is_not_a_broken_command(doe_root: Path):
    """A `type: "http"` registration has no script path BY CONSTRUCTION, so the
    command layer has nothing to say about it and must stay quiet.

    Observed 2026-08-26: the live PreToolUse fan-in is an http entry pointing at
    the warm engine, and this layer reported it `broken: command shape not
    understood` — the warm entrypoint, working, called broken on a healthy box.
    The layer still SAYS it audited nothing rather than returning a bare `ok`;
    "nothing to check" and "everything checks out" must not print the same.
    """
    from coordinator_core.ops import doctor

    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "http",
                                    "url": "http://127.0.0.1:47623/hook",
                                    "timeout": 15,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    status, findings, present = doctor._check_one_hooks_doc(
        hooks_dir / "hooks.json", str(doe_root), "hooks.json"
    )

    assert present is True
    assert status == "ok", [f.message for f in findings]
    assert not [f for f in findings if f.severity == "broken"]
    assert any("non-command" in f.message for f in findings), [f.message for f in findings]


def test_an_http_hook_does_not_swallow_a_broken_sibling_command(doe_root: Path):
    """The `continue` that skips a non-command entry lives mid-loop, right
    next to the `total`/`bare` counters — the exact shape that silently
    swallows a sibling finding if a future edit reorders those counters. A
    matcher block carrying one `type: "http"` entry AND one genuinely broken
    `command` entry must still report `broken` with the broken finding
    present; the http entry must not short-circuit or miscount its sibling.

    Review: coordinator:code-reviewer Finding 4.
    """
    from coordinator_core.ops import doctor

    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "http",
                                    "url": "http://127.0.0.1:47623/hook",
                                    "timeout": 15,
                                },
                                {
                                    "type": "command",
                                    "command": "python3 /plugins/x/hooks/scripts/missing.py",
                                },
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    status, findings, present = doctor._check_one_hooks_doc(
        hooks_dir / "hooks.json", str(doe_root), "hooks.json"
    )

    assert present is True
    assert status == "broken", [f.message for f in findings]
    broken = [f for f in findings if f.severity == "broken"]
    assert broken, [f.message for f in findings]
    assert any("missing on disk" in f.message for f in broken), [f.message for f in findings]


def test_a_typo_type_with_zero_real_commands_is_broken_not_ok(doe_root: Path):
    """The `total == 0` branch, hit with a broken finding already sitting in
    `findings` and no counted command registrations at all. A doc whose ONLY
    entry is a `command`-shaped hook with a misspelled `type` (e.g.
    `"Command"`) never increments `total` — it falls into the unrecognized-type
    branch above the `total` counter — so this is the one case that reaches
    `total == 0` carrying a `broken` finding with zero non-command entries
    either. Must still report `broken`, never the bare `ok` the `total == 0`
    early-return would otherwise take.

    Review: coordinator:code-reviewer coverage-gap finding (test_doctor.py).
    """
    from coordinator_core.ops import doctor

    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "Command",
                                    "command": "python3 /plugins/x/hooks/scripts/nudge.py",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    status, findings, present = doctor._check_one_hooks_doc(
        hooks_dir / "hooks.json", str(doe_root), "hooks.json"
    )

    assert present is True
    assert status == "broken", [f.message for f in findings]
    broken = [f for f in findings if f.severity == "broken"]
    assert broken, [f.message for f in findings]
    assert any("unrecognized hook type" in f.message for f in broken), [f.message for f in findings]


def test_a_non_python_interpreter_is_still_not_understood():
    """Negative half, so the basename match does not become "accept anything":
    only python/python3 reduce to a script path. A node or sh registration is
    genuinely a shape this layer does not read, and must keep saying so."""
    from coordinator_core.ops import doctor

    assert doctor._extract_script_path(["/usr/bin/node", "hook.py"]) is None
    assert doctor._extract_script_path(["<drive>:\\tools\\pythonish.exe", "hook.py"]) is None


def test_reports_rather_than_raises_on_an_unreadable_hooks_document(doe_root: Path):
    """A layer it cannot evaluate must say so. Absence of a check must never be
    indistinguishable from the check passing — that is the pathology the whole plan is
    about."""
    hooks_dir = doe_root / "coordinator" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text("{ not valid json")

    result = _run_doctor(doe_root)

    assert result.returncode == 1, result.stdout
    assert "Traceback" not in result.stderr, "must report, not crash: " + result.stderr
