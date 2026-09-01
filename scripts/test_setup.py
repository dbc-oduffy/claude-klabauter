"""scripts/test_setup.py — unit tests for scripts/setup.py's pure-logic paths.

Covers the pieces that need no subprocess/network/venv to exercise:
`parse_args` error paths, `derive_deps` against a fixture pyproject.toml, the
`--skip-dep-check`/`--accept-missing-deps-risk` exit-93 flag-pair gate,
`resolve_python`'s branch behavior with a monkeypatched `sys.version_info`,
and the `--claude-klabauter-live-root`/`--coordinator-root` flag -> env -> default
resolution ladders.

Review: code-reviewer 2026-07-21 Finding 8 (P2) — this 689-line installer
landed with zero test coverage; this file closes that gap for the
straightforwardly-unit-testable subset (subprocess-touching paths like pip
installs and machine-local registration are out of scope here — they need
integration-level fixtures, not unit mocks).

Run: python3 -m pytest scripts/test_setup.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SETUP_PY_PATH = Path(__file__).resolve().parent / "setup.py"


def _load_setup_module():
    """Load scripts/setup.py as a module named `_scripts_setup_under_test`
    (not `setup`, which could collide with an installed `setup` package on
    sys.path) via `importlib.util.spec_from_file_location`."""
    spec = importlib.util.spec_from_file_location(
        "_scripts_setup_under_test", _SETUP_PY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules before exec: `setup.py`'s `@dataclass`
    # (added 2026-08-08 for CoordSourceResolution) resolves its string
    # annotations (`from __future__ import annotations`) via
    # `sys.modules[cls.__module__]` at class-definition time -- without this
    # registration the module isn't found there yet and dataclass() raises
    # `AttributeError: 'NoneType' object has no attribute '__dict__'` on
    # Python 3.13. Standard `importlib` idiom; harmless no-op for every
    # module that doesn't need it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def setup_mod():
    return _load_setup_module()


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults(setup_mod):
    args = setup_mod.parse_args([])
    assert args.agent_mode is False
    assert args.skip_dep_check is False
    assert args.accept_risk is False
    assert args.allow_venv_fallback is False
    assert args.register_only is False
    assert args.check is False
    assert args.help is False
    assert args.claude_klabauter_root == ""
    assert args.coordinator_root == ""


def test_parse_args_all_flags(setup_mod):
    args = setup_mod.parse_args(
        [
            "--i-am-agent",
            "--skip-dep-check",
            "--accept-missing-deps-risk",
            "--allow-venv-fallback",
            "--register-only",
            "--claude-klabauter-live-root",
            "/tmp/claude-klabauter",
            "--coordinator-root",
            "/tmp/coord",
        ]
    )
    assert args.agent_mode is True
    assert args.skip_dep_check is True
    assert args.accept_risk is True
    assert args.allow_venv_fallback is True
    assert args.register_only is True
    assert args.claude_klabauter_root == "/tmp/claude-klabauter"
    assert args.coordinator_root == "/tmp/coord"


def test_parse_args_break_system_packages_is_an_unknown_flag(setup_mod):
    """`--break-system-packages` is retired entirely (machine-first-install-
    surface plan, C2) -- it must not silently parse as a no-op; it is an
    unrecognized flag like any other."""
    with pytest.raises(setup_mod.ArgError, match="unknown flag"):
        setup_mod.parse_args(["--break-system-packages"])


def test_parse_args_unknown_flag_raises(setup_mod):
    with pytest.raises(setup_mod.ArgError, match="unknown flag"):
        setup_mod.parse_args(["--not-a-real-flag"])


def test_parse_args_claude_klabauter_root_missing_value_raises(setup_mod):
    with pytest.raises(setup_mod.ArgError, match="--claude-klabauter-live-root requires a path argument"):
        setup_mod.parse_args(["--claude-klabauter-live-root"])


def test_parse_args_claude_klabauter_root_value_looks_like_flag_raises(setup_mod):
    with pytest.raises(setup_mod.ArgError, match="--claude-klabauter-live-root requires a path argument"):
        setup_mod.parse_args(["--claude-klabauter-live-root", "--check"])


def test_parse_args_coordinator_root_missing_value_raises(setup_mod):
    with pytest.raises(setup_mod.ArgError, match="--coordinator-root requires a path argument"):
        setup_mod.parse_args(["--coordinator-root"])


# ---------------------------------------------------------------------------
# derive_deps
# ---------------------------------------------------------------------------


def test_derive_deps_reads_specs_and_import_names(setup_mod, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "fixture"\n'
        'dependencies = ["pydantic>=2.0", "PyYAML", "some-thing==1.2.3"]\n'
    )
    specs, import_names = setup_mod.derive_deps(pyproject)
    assert specs == ["pydantic>=2.0", "PyYAML", "some-thing==1.2.3"]
    # PyYAML is the explicit IMPORT_NAME_OVERRIDES exception; the rest fall
    # back to a normalized (lowercased, hyphen->underscore) form.
    assert import_names == ["pydantic", "yaml", "some_thing"]


def test_derive_deps_malformed_entry_exits(setup_mod, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "fixture"\n'
        'dependencies = ["!!!not-a-valid-dep-name"]\n'
    )
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.derive_deps(pyproject)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# provision_deps — machine-first, no override flag, ever
# (2026-08-17, docs/plans/2026-08-17-machine-first-install-surface.md § C2,
# superseding DR-307's healthy-venv prior-consent branch and retiring
# --break-system-packages entirely)
# ---------------------------------------------------------------------------


def _fixture_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "fixture"\n'
        'dependencies = ["PyYAML"]\n'
    )
    return pyproject


def _stub_settings_home(setup_mod, monkeypatch, tmp_path):
    """Redirect settings_home() at a tmp dir and return (settings_home_dir,
    venv_dir, venv_py) — the venv is NOT built, only its path is resolved,
    so tests that never expect a fallback never touch disk for it."""
    from coordinator_core import _settings_home as settings_home_mod
    from coordinator_core.install import ensure_venv as ensure_venv_mod

    settings_home_dir = tmp_path / "settings-home"
    settings_home_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(settings_home_mod, "settings_home", lambda: settings_home_dir)

    venv_dir = settings_home_dir / ".coordinator-venv"
    venv_py = ensure_venv_mod.venv_python_path(venv_dir)
    return settings_home_dir, venv_dir, venv_py


def _stub_candidates(setup_mod, monkeypatch, py, *extra):
    candidates = [
        setup_mod.InterpreterCandidate(
            label="installer interpreter", path=py, consumers=("test consumer",)
        ),
        *extra,
    ]
    monkeypatch.setattr(setup_mod, "enumerate_provisioning_candidates", lambda installer_py: candidates)
    return candidates


def test_provision_deps_machine_first_installs_editable_package(setup_mod, monkeypatch, tmp_path):
    """Machine-first: an unguarded, not-yet-provisioned candidate gets a
    single `pip install <deps> -e <claude_klabauter_root>` -- the editable install is
    what materializes [project.scripts] console entrypoints (C3's dep)."""
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: False)

    state = {"installed": False}
    monkeypatch.setattr(setup_mod, "_engine_installed", lambda interpreter, import_names: state["installed"])

    calls = []

    def _fake_run_pip(argv):
        calls.append(argv)
        state["installed"] = True
        return setup_mod.subprocess.CompletedProcess(argv, 0, stdout="Successfully installed")

    monkeypatch.setattr(setup_mod, "_run_pip", _fake_run_pip)

    engine_py, import_names = setup_mod.provision_deps(pyproject_dir, sys.executable, False)

    assert engine_py == sys.executable
    assert len(calls) == 1
    assert "-e" in calls[0]
    assert str(pyproject_dir) in calls[0]


def test_provision_deps_idempotent_noop_when_already_installed(setup_mod, monkeypatch, tmp_path):
    """`_engine_installed` True (deps importable AND coordinator_core
    pip-installed) -- no pip call at all."""
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: False)
    monkeypatch.setattr(setup_mod, "_engine_installed", lambda interpreter, import_names: True)

    calls = []
    monkeypatch.setattr(setup_mod, "_run_pip", lambda argv: calls.append(argv))

    engine_py, import_names = setup_mod.provision_deps(pyproject_dir, sys.executable, False)

    assert engine_py == sys.executable
    assert calls == []


def test_provision_deps_guarded_interpreter_exits_96_no_fallback_no_override(setup_mod, monkeypatch, tmp_path, capsys):
    """A PEP-668 externally-managed candidate is a DESIGNED REFUSAL --
    EXIT_INTERPRETER_UNSUPPORTED (96), unconditional: even with
    --allow-venv-fallback passed, no pip install is ever attempted and the
    venv is never reached (anti-scope: swap the interpreter, never pass an
    override flag or narrow the blast radius)."""
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: True)
    monkeypatch.setattr(setup_mod, "_offer_homebrew_removal", lambda candidate, settings_home_path: False)

    calls = []
    monkeypatch.setattr(setup_mod, "_run_pip", lambda argv: calls.append(argv))

    with pytest.raises(SystemExit) as exc_info:
        setup_mod.provision_deps(pyproject_dir, sys.executable, True)

    assert exc_info.value.code == setup_mod.EXIT_INTERPRETER_UNSUPPORTED
    assert calls == []
    stderr = capsys.readouterr().err
    assert "PEP-668" in stderr or "PEP 668" in stderr
    assert "does not honour it" in stderr


def test_provision_deps_nonpep668_failure_no_flag_exits_1(setup_mod, monkeypatch, tmp_path, capsys):
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: False)
    monkeypatch.setattr(setup_mod, "_engine_installed", lambda interpreter, import_names: False)
    monkeypatch.setattr(
        setup_mod, "_run_pip",
        lambda argv: setup_mod.subprocess.CompletedProcess(argv, 1, stdout="permission denied"),
    )

    with pytest.raises(SystemExit) as exc_info:
        setup_mod.provision_deps(pyproject_dir, sys.executable, False)

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "--allow-venv-fallback" in stderr


def test_provision_deps_nonpep668_failure_flag_falls_back_to_venv_deps_only(setup_mod, monkeypatch, tmp_path):
    """--allow-venv-fallback survives as explicit break-glass for a genuine
    (non-PEP-668) failure -- and the venv fallback installs ONLY the
    third-party deps, never `-e .` (purpose (c) stays deps-only per
    docs/reference/shared-fleet-venv-contract.md)."""
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    settings_home_dir, venv_dir, venv_py = _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: False)
    monkeypatch.setattr(setup_mod, "_engine_installed", lambda interpreter, import_names: False)

    calls = []

    def _fake_run_pip(argv):
        calls.append(argv)
        if str(venv_py) in argv:
            return setup_mod.subprocess.CompletedProcess(argv, 0, stdout="Successfully installed PyYAML")
        return setup_mod.subprocess.CompletedProcess(argv, 1, stdout="permission denied")

    monkeypatch.setattr(setup_mod, "_run_pip", _fake_run_pip)
    monkeypatch.setattr(setup_mod, "deps_importable", lambda interpreter, import_names: interpreter == str(venv_py))

    from coordinator_core.install import ensure_venv as ensure_venv_mod

    def _fake_ensure_coordinator_venv(*a, **k):
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("")
        return "provisioned"

    monkeypatch.setattr(ensure_venv_mod, "ensure_coordinator_venv", _fake_ensure_coordinator_venv)

    engine_py, import_names = setup_mod.provision_deps(pyproject_dir, sys.executable, True)

    assert engine_py == str(venv_py)
    venv_call = next(c for c in calls if str(venv_py) in c)
    assert "-e" not in venv_call


def test_provision_deps_mid_install_pep668_refusal_exits_96(setup_mod, monkeypatch, tmp_path):
    """The marker-file probe can miss a guard (probe error -> fails open);
    pip's own refusal string is still the ultimate authority and still
    exits 96, never falls back even with the flag passed."""
    pyproject_dir = tmp_path / "root"
    pyproject_dir.mkdir()
    _fixture_pyproject(pyproject_dir)
    _stub_settings_home(setup_mod, monkeypatch, tmp_path)
    _stub_candidates(setup_mod, monkeypatch, sys.executable)
    monkeypatch.setattr(setup_mod, "_is_externally_managed", lambda interpreter: False)
    monkeypatch.setattr(setup_mod, "_engine_installed", lambda interpreter, import_names: False)
    monkeypatch.setattr(
        setup_mod, "_run_pip",
        lambda argv: setup_mod.subprocess.CompletedProcess(argv, 1, stdout="error: externally-managed-environment"),
    )

    with pytest.raises(SystemExit) as exc_info:
        setup_mod.provision_deps(pyproject_dir, sys.executable, True)

    assert exc_info.value.code == setup_mod.EXIT_INTERPRETER_UNSUPPORTED


# ---------------------------------------------------------------------------
# enumerate_provisioning_candidates
# ---------------------------------------------------------------------------


def test_enumerate_provisioning_candidates_dedupes_same_realpath(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: sys.executable)
    candidates = setup_mod.enumerate_provisioning_candidates(sys.executable)
    assert len(candidates) == 1
    assert candidates[0].path == sys.executable


def test_enumerate_provisioning_candidates_includes_bare_python3_when_distinct(setup_mod, monkeypatch, tmp_path):
    fake_python3 = tmp_path / "python3"
    fake_python3.write_text("")
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: str(fake_python3))
    candidates = setup_mod.enumerate_provisioning_candidates("/some/other/python")
    assert len(candidates) == 2
    assert candidates[1].label == "bare python3 on PATH"
    assert any("hooks.json" in c for c in candidates[1].consumers)


def test_enumerate_provisioning_candidates_no_bare_python3_on_path(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    candidates = setup_mod.enumerate_provisioning_candidates(sys.executable)
    assert len(candidates) == 1


def test_enumerate_provisioning_candidates_dedup_unions_consumers_not_first_wins(setup_mod, monkeypatch):
    """Review (team-lead, 2026-08-17): the realpath dedup must UNION both
    roles' consumer text into the one surviving candidate, not silently
    keep only the first-added role's -- a first-wins merge drops the
    hooks.json/dialect-guard consumer exactly when it shares a realpath
    with the installer's own interpreter, which is the box this plan
    exists for."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: sys.executable)
    candidates = setup_mod.enumerate_provisioning_candidates(sys.executable)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.path == sys.executable
    assert len(candidate.consumers) == 2
    assert any("resolve_python()" in c for c in candidate.consumers)
    assert any("hooks.json" in c for c in candidate.consumers)
    assert "installer interpreter" in candidate.label
    assert "bare python3 on PATH" in candidate.label


# ---------------------------------------------------------------------------
# _is_externally_managed — PEP 668 marker-file probe
# ---------------------------------------------------------------------------


def test_is_externally_managed_true_when_marker_present(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: setup_mod.subprocess.CompletedProcess(argv, 0, stdout="1\n"),
    )
    assert setup_mod._is_externally_managed(sys.executable) is True


def test_is_externally_managed_false_when_marker_absent(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: setup_mod.subprocess.CompletedProcess(argv, 0, stdout="0\n"),
    )
    assert setup_mod._is_externally_managed(sys.executable) is False


def test_is_externally_managed_fails_open_on_probe_error(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: (_ for _ in ()).throw(OSError("no such interpreter")),
    )
    assert setup_mod._is_externally_managed("/nonexistent") is False


# ---------------------------------------------------------------------------
# Homebrew Python detection + consented, recorded removal
# ---------------------------------------------------------------------------


def test_is_homebrew_python_detects_cellar_path(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.os.path, "realpath",
        lambda p: "/opt/homebrew/Cellar/python@3.14/3.14.0/bin/python3.14",
    )
    assert setup_mod._is_homebrew_python("/opt/homebrew/bin/python3") is True


def test_is_homebrew_python_false_for_python_org(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.os.path, "realpath",
        lambda p: "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
    )
    assert setup_mod._is_homebrew_python("/usr/local/bin/python3") is False


def test_homebrew_python_formula_parses_cellar_dirname(setup_mod, monkeypatch):
    monkeypatch.setattr(
        setup_mod.os.path, "realpath",
        lambda p: "/opt/homebrew/Cellar/python@3.14/3.14.0/bin/python3.14",
    )
    assert setup_mod._homebrew_python_formula("/opt/homebrew/bin/python3") == "python@3.14"


def test_offer_homebrew_removal_declines_on_eof(setup_mod, monkeypatch, tmp_path):
    """--i-am-agent's typically-closed stdin: EOFError -> declines, no brew call."""
    monkeypatch.setattr(setup_mod, "_is_homebrew_python", lambda interpreter: True)
    monkeypatch.setattr(setup_mod, "_homebrew_python_formula", lambda interpreter: "python@3.14")
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))
    calls = []
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda *a, **k: calls.append(a) or setup_mod.subprocess.CompletedProcess(a, 0),
    )

    candidate = setup_mod.InterpreterCandidate(
        label="bare python3 on PATH", path="/opt/homebrew/bin/python3", consumers=("hooks.json",)
    )
    assert setup_mod._offer_homebrew_removal(candidate, tmp_path) is False
    assert calls == []


def test_offer_homebrew_removal_declines_on_non_affirmative(setup_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(setup_mod, "_is_homebrew_python", lambda interpreter: True)
    monkeypatch.setattr(setup_mod, "_homebrew_python_formula", lambda interpreter: "python@3.14")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    calls = []
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda *a, **k: calls.append(a) or setup_mod.subprocess.CompletedProcess(a, 0),
    )

    candidate = setup_mod.InterpreterCandidate(
        label="bare python3 on PATH", path="/opt/homebrew/bin/python3", consumers=("hooks.json",)
    )
    assert setup_mod._offer_homebrew_removal(candidate, tmp_path) is False
    assert calls == []


def test_offer_homebrew_removal_accepts_and_records(setup_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(setup_mod, "_is_homebrew_python", lambda interpreter: True)
    monkeypatch.setattr(setup_mod, "_homebrew_python_formula", lambda interpreter: "python@3.14")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return setup_mod.subprocess.CompletedProcess(argv, 0, stdout="Uninstalling python@3.14")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    settings_home_dir = tmp_path / "settings-home"
    candidate = setup_mod.InterpreterCandidate(
        label="bare python3 on PATH", path="/opt/homebrew/bin/python3", consumers=("hooks.json (test)",)
    )
    assert setup_mod._offer_homebrew_removal(candidate, settings_home_dir) is True
    assert calls == [["brew", "uninstall", "python@3.14"]]

    record_path = settings_home_dir / setup_mod._HOMEBREW_REMOVALS_FILENAME
    assert record_path.is_file()
    record = json.loads(record_path.read_text())
    assert record[0]["formula"] == "python@3.14"
    assert record[0]["interpreter"] == "/opt/homebrew/bin/python3"
    assert record[0]["consumers"] == ["hooks.json (test)"]


def test_offer_homebrew_removal_returns_false_for_non_homebrew_interpreter(setup_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(setup_mod, "_is_homebrew_python", lambda interpreter: False)
    candidate = setup_mod.InterpreterCandidate(
        label="installer interpreter", path="/usr/local/bin/python3", consumers=("test",)
    )
    assert setup_mod._offer_homebrew_removal(candidate, tmp_path) is False


# ---------------------------------------------------------------------------
# --skip-dep-check / --accept-missing-deps-risk exit-93 flag-pair gate
# ---------------------------------------------------------------------------


def test_flag_pair_violation_skip_only(setup_mod):
    assert setup_mod.main(["--skip-dep-check"]) == setup_mod.EXIT_FLAG_PAIR_VIOLATION


def test_flag_pair_violation_accept_only(setup_mod):
    assert setup_mod.main(["--accept-missing-deps-risk"]) == setup_mod.EXIT_FLAG_PAIR_VIOLATION


def test_check_mode_exits_zero_before_flag_pair_gate(setup_mod):
    # --check short-circuits before the flag-pair gate is even reached.
    assert setup_mod.main(["--check"]) == 0


def test_help_mode_exits_zero(setup_mod):
    assert setup_mod.main(["--help"]) == 0


# ---------------------------------------------------------------------------
# resolve_python — branch behavior with a monkeypatched sys.version_info
# ---------------------------------------------------------------------------


def test_resolve_python_returns_sys_executable_when_already_311_plus(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "version_info", (3, 12, 0))
    assert setup_mod.resolve_python() == setup_mod.sys.executable


def test_resolve_python_reexecs_when_candidate_found_below_311(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "version_info", (3, 9, 0))
    monkeypatch.setattr(setup_mod, "_python_version_ok", lambda candidate: candidate == "python3")

    calls = {}

    def fake_execvp(file, args):
        calls["file"] = file
        calls["args"] = args
        raise _StopExecvp()

    class _StopExecvp(Exception):
        pass

    monkeypatch.setattr(setup_mod.os, "execvp", fake_execvp)
    with pytest.raises(_StopExecvp):
        setup_mod.resolve_python()
    assert calls["file"] == "python3"
    assert calls["args"][0] == "python3"
    assert calls["args"][1] == str(_SETUP_PY_PATH)


def test_resolve_python_exits_1_when_no_candidate_found(setup_mod, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "version_info", (3, 9, 0))
    monkeypatch.setattr(setup_mod, "_python_version_ok", lambda candidate: False)
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.resolve_python()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLAUDE_KLABAUTER_ROOT / coordinator-claude root resolution ladders
# ---------------------------------------------------------------------------


def test_resolve_claude_klabauter_root_flag_wins(setup_mod, monkeypatch):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    args = setup_mod.Args()
    args.claude_klabauter_root = "/flag/path"
    root, source = setup_mod.resolve_claude_klabauter_root(Path("/repo"), args)
    assert root == Path("/flag/path")
    assert source == "--claude-klabauter-live-root flag"


def test_resolve_claude_klabauter_root_env_fallback(setup_mod, monkeypatch):
    """The RETIRED name still answers here, and says so in its source label.

    C23 kept this rung deliberately: an installer runs against un-migrated
    boxes, which are exactly the population still exporting the old spelling.
    The label carries `(RETIRED)` so an operator reading setup's own output
    learns which name answered.
    """
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/env/path")
    args = setup_mod.Args()
    root, source = setup_mod.resolve_claude_klabauter_root(Path("/repo"), args)
    assert root == Path("/env/path")
    assert source == "CLAUDE_KLABAUTER_ROOT env var (RETIRED)"


def test_resolve_claude_klabauter_root_prefers_the_current_name(setup_mod, monkeypatch):
    """C23 REGRESSION PIN. This rung previously read the retired name and
    nothing else, so a box exporting only COORDINATOR_ENGINE_ROOT got no
    env rung at all and fell silently through to git-root auto-discovery --
    the installer then provisioned against whichever tree it happened to sit
    in, ignoring the operator's explicit pin."""
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/current/path")
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    args = setup_mod.Args()
    root, source = setup_mod.resolve_claude_klabauter_root(Path("/repo"), args)
    assert root == Path("/current/path")
    assert source == "COORDINATOR_ENGINE_ROOT env var"


def test_resolve_claude_klabauter_root_current_name_outranks_retired(setup_mod, monkeypatch):
    """Both set: the current name wins, mirroring the accessor's precedence."""
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "/current/path")
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/stale/path")
    args = setup_mod.Args()
    root, source = setup_mod.resolve_claude_klabauter_root(Path("/repo"), args)
    assert root == Path("/current/path")


def test_resolve_claude_klabauter_root_repo_root_default(setup_mod, monkeypatch):
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    args = setup_mod.Args()
    root, source = setup_mod.resolve_claude_klabauter_root(Path("/repo"), args)
    assert root == Path("/repo")
    assert source == "git-root auto-discovery"


def test_resolve_coordinator_claude_root_flag_wins(setup_mod, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    args = setup_mod.Args()
    args.coordinator_root = "/flag/coord"
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo"), args)
    assert root == Path("/flag/coord")
    assert source.rung is setup_mod.CoordSourceRung.FLAG
    assert source.display == "--coordinator-root flag"


def test_resolve_coordinator_claude_root_sibling_default(setup_mod, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    # The pointer, registry, and settings-home rungs all sit above sibling-dir
    # and all read REAL machine state (the shared `.doe-root` pointer, the
    # registered `engine.working_repos.doe_claude` key, and
    # `<settings-home>/machine-local/.doe-root` respectively). Stub all three
    # so this case exercises the bottom rung rather than whatever this
    # particular box happens to have recorded -- without this the assertion
    # below passes or fails depending on the developer's machine, which is
    # not a property a unit test may have.
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_settings_home", lambda: None)
    args = setup_mod.Args()
    # /repo/coordinator-claude does not exist on any box running this test --
    # exercises the honesty-gated branch (C1Cc) alongside the rung-selection
    # itself, so the suffix is asserted rather than a bare source string.
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == Path("/repo/coordinator-claude")
    assert source.rung is setup_mod.CoordSourceRung.SIBLING_DIR_DEFAULT
    assert source.is_unresolved is True
    assert source.display == "sibling-dir default [UNRESOLVED -- PATH DOES NOT EXIST]"


def test_resolve_coordinator_claude_root_sibling_default_verified_exists(setup_mod, monkeypatch, tmp_path):
    """When the sibling-dir guess DOES exist on disk, no unresolved suffix is
    added -- the honesty gate (C1Cc) only flags the fabrication, it does not
    penalize a guess that happens to be correct."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_settings_home", lambda: None)
    repo_root = tmp_path / "claude-klabauter"
    repo_root.mkdir()
    (tmp_path / "coordinator-claude").mkdir()
    args = setup_mod.Args()
    root, source = setup_mod._resolve_coordinator_claude_root(repo_root, args)
    assert root == tmp_path / "coordinator-claude"
    assert source.rung is setup_mod.CoordSourceRung.SIBLING_DIR_DEFAULT
    assert source.is_unresolved is False
    assert source.display == "sibling-dir default"


# Review: staff-eng 2026-08-08 MAJOR-2 — the two tests above only assert on
# `_resolve_coordinator_claude_root`'s RETURN VALUE; neither drives
# `check_coordinator_claude_dep`, the only consumer of the honesty suffix and
# the only thing a fresh-OSS-box stranger ever sees. That gap is why MAJOR-1
# (the `==` vs decorated-string equality break) landed green.
def test_check_coordinator_claude_dep_unresolved_sibling_default_prints_git_clone(
    setup_mod, monkeypatch, capsys
):
    """The exact fresh-OSS-box path: no override, no pointer/registry/
    settings-home rung resolves, sibling guess does not exist on disk ->
    `check_coordinator_claude_dep` must still print the `git clone`
    remediation and must NOT claim a --coordinator-root/COORDINATOR_CLAUDE_ROOT
    location the stranger never provided."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_settings_home", lambda: None)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: False,
    )
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_coordinator_claude_dep(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "git clone" in stderr
    assert "the provided --coordinator-root" not in stderr


# Defect class fix 2026-08-08 (state/debt-backlog/2026-08-08-a-decorated-
# string-is-used-as-a-control-91d15e71174a.yaml): the instance (MAJOR-1) was
# fixed by widening an `==` to `.startswith`, which is STILL a string-shape
# dependency and remains breakable by any future annotation appended to the
# display text. This test proves the class is closed, not merely the
# instance: it decorates the display string with an arbitrary NEW suffix no
# rung currently produces and asserts the git-clone remediation still fires
# -- against the OLD (pre-fix) shape, where `check_coordinator_claude_dep`
# branched on `coord_source.startswith("sibling-dir default")`, this would
# still have passed (a decoration is a suffix), so it does not by itself
# discriminate old vs new shape; the discriminating property is that ANY
# possible decoration of `.display` -- prepended, inserted, or replacing the
# text outright -- cannot affect the branch, because the branch never reads
# `.display` at all. Asserting that mutating `.display` in place has zero
# effect on the exit path is what a purely string-shape branch could not
# have survived.
def test_check_coordinator_claude_dep_routes_on_rung_not_decorated_display(
    setup_mod, monkeypatch, capsys
):
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_settings_home", lambda: None)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: False,
    )

    real_resolver = setup_mod._resolve_coordinator_claude_root

    def _decorated_resolver(repo_root, args):
        candidate, source = real_resolver(repo_root, args)
        decorated = setup_mod.CoordSourceResolution(
            rung=source.rung,
            display=f"{source.display} [SOME FUTURE ANNOTATION NO CODE TODAY EXPECTS]",
            is_publish_mirror_rejected=source.is_publish_mirror_rejected,
            is_unresolved=source.is_unresolved,
        )
        return candidate, decorated

    monkeypatch.setattr(setup_mod, "_resolve_coordinator_claude_root", _decorated_resolver)

    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_coordinator_claude_dep(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "git clone" in stderr
    assert "the provided --coordinator-root" not in stderr


def test_resolve_coordinator_claude_root_prefers_settings_home_over_sibling(
    setup_mod, monkeypatch, tmp_path
):
    """The settings-home rung outranks the sibling-dir guess.

    Regression guard for the exit-90 outage: `.doe-root` recorded the real
    checkout the whole time, the ladder never read it, and the resolver fell
    through to a sibling path that does not exist on this machine.
    """
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    recorded = tmp_path / "DoE-clone"
    recorded.mkdir()
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_settings_home", lambda: recorded
    )
    args = setup_mod.Args()
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == recorded
    assert source.rung is setup_mod.CoordSourceRung.SETTINGS_HOME
    assert source.display == "settings-home .doe-root sentinel"


def test_explicit_override_still_outranks_settings_home(setup_mod, monkeypatch, tmp_path):
    """An operator's explicit --coordinator-root is never overridden by the
    sentinel — the new rung was inserted BELOW the flag and env rungs."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: None)
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_settings_home", lambda: tmp_path / "sentinel"
    )
    args = setup_mod.Args()
    args.coordinator_root = str(tmp_path / "explicit")
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == tmp_path / "explicit"
    assert source.rung is setup_mod.CoordSourceRung.FLAG
    assert source.display == "--coordinator-root flag"


def test_resolve_coordinator_claude_root_registry_rung(setup_mod, monkeypatch, tmp_path):
    """The registry rung (`engine.working_repos.doe_claude`, via
    `_coordinator_root_from_registry`) outranks BOTH the settings-home
    sentinel and the sibling-dir default -- EM ruling: better evidence
    (a cross-fleet identity assertion) outranks weaker evidence (a local
    breadcrumb / a directory that merely sits next door)."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: None)
    registered = tmp_path / "registered-checkout"
    registered.mkdir()
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_registry", lambda: registered)
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_settings_home", lambda: tmp_path / "sentinel"
    )
    args = setup_mod.Args()
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == registered
    assert source.rung is setup_mod.CoordSourceRung.REGISTRY
    assert source.display == "engine.working_repos.doe_claude registry key"


def test_resolve_coordinator_claude_root_doe_root_pointer_rung(setup_mod, monkeypatch, tmp_path):
    """The shared `.doe-root` pointer rung (C1Cc) outranks BOTH the registry
    rung and the settings-home sentinel -- setup.py is the installer, so it
    must resolve before any registry is necessarily populated on a fresh
    box. Does not reverse `da7cd333a`'s ordering between the registry rung
    and the settings-home sentinel, only sits ahead of both."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    pointed = tmp_path / "pointed-checkout"
    pointed.mkdir()
    monkeypatch.setattr(setup_mod, "_coordinator_root_from_doe_root_pointer", lambda: pointed)
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_registry", lambda: tmp_path / "registered"
    )
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_settings_home", lambda: tmp_path / "sentinel"
    )
    args = setup_mod.Args()
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == pointed
    assert source.rung is setup_mod.CoordSourceRung.DOE_ROOT_POINTER
    assert source.display == "shared .doe-root pointer"


# ---------------------------------------------------------------------------
# `_coordinator_root_from_doe_root_pointer` direct coverage (C1Cc) — exercised
# against a REDIRECTED settings-home (never the real one), per brief's two
# required cases: (a) a planted `.doe-root` sentinel resolves, (b) no
# pointer + no sibling dir does NOT return a fabricated path.
# ---------------------------------------------------------------------------


def test_doe_root_pointer_resolves_planted_sentinel(setup_mod, monkeypatch, tmp_path):
    """Case (a): no registry, a `.doe-root` pointer planted under a
    redirected settings-home -> resolves to the pointed-to path."""
    settings_home_dir = tmp_path / "redirected-settings-home"
    machine_local_dir = settings_home_dir / "machine-local"
    machine_local_dir.mkdir(parents=True)
    checkout = tmp_path / "doe-checkout"
    _add_source_evidence(checkout)
    (machine_local_dir / ".doe-root").write_text(str(checkout), encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    root = setup_mod._coordinator_root_from_doe_root_pointer()
    assert root == checkout


def test_doe_root_pointer_no_pointer_no_sibling_returns_none(setup_mod, monkeypatch, tmp_path):
    """Case (b): no registry, no pointer file anywhere, no sibling dir on
    disk -> the pointer rung returns None (never fabricates a path)."""
    settings_home_dir = tmp_path / "redirected-settings-home-empty"
    settings_home_dir.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    legacy_home = tmp_path / "unrelated-home-no-legacy-pointer"
    legacy_home.mkdir()
    monkeypatch.setenv("HOME", str(legacy_home))
    root = setup_mod._coordinator_root_from_doe_root_pointer()
    assert root is None


# Review: staff-eng 2026-08-08 MAJOR-3 — C1F's own commit message makes the
# case: "a test that only runs on this dev box passes either way, since
# `coordinator/lib` exists here." This repo's own `coordinator/lib` always
# exists, so the flat-`lib/` fallback branch below was dead code as far as
# this suite was concerned -- the one call site (this file) that actually
# ships. Payload-shaped fixture: a temp tree with ONLY `lib/`, no
# `coordinator/` at all.
def test_doe_root_pointer_resolves_via_flat_payload_lib_fallback(setup_mod, monkeypatch, tmp_path):
    # Order-dependence guard: `_coordinator_root_from_doe_root_pointer` does a
    # bare `from read_doe_root_pointer import ...`, which binds through
    # `sys.modules` -- once ANY earlier test has imported that name (e.g. via
    # this repo's own `coordinator/lib`, which always exists on a dev box),
    # this test would silently reuse the cached module and never exercise the
    # flat-`lib/` fallback at all, whatever this fixture builds on disk. Force
    # a real re-import so the payload-shaped tree below is what actually gets
    # exercised, in isolation or in file order alike.
    monkeypatch.delitem(sys.modules, "read_doe_root_pointer", raising=False)
    monkeypatch.delitem(sys.modules, "settings_home", raising=False)

    payload_root = tmp_path / "payload-repo"
    scripts_dir = payload_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "setup.py").write_bytes(_SETUP_PY_PATH.read_bytes())

    real_coordinator_lib = Path(__file__).resolve().parent.parent / "coordinator" / "lib"
    flat_lib_dir = payload_root / "lib"
    flat_lib_dir.mkdir()
    # Mirror the real publish row's shape (coordinator/bin/tests/
    # test_published_lib_layout.py): the flattened `lib/` ships
    # `read_doe_root_pointer.py` AND its sibling `settings_home.py` side by
    # side, not the pointer helper alone -- `coordinator_read_doe_root_pointer`
    # imports `settings_home` internally, and a fixture missing it fails
    # closed (empty settings-home) for a reason that has nothing to do with
    # the fallback branch under test.
    (flat_lib_dir / "read_doe_root_pointer.py").write_bytes(
        (real_coordinator_lib / "read_doe_root_pointer.py").read_bytes()
    )
    (flat_lib_dir / "settings_home.py").write_bytes(
        (real_coordinator_lib / "settings_home.py").read_bytes()
    )
    # negative-spec: deliberately no `payload_root / "coordinator"` at all --
    # the payload flattens it away, and the probe must fall through to `lib/`.
    assert not (payload_root / "coordinator").exists()

    spec = importlib.util.spec_from_file_location(
        "_scripts_setup_under_test_payload_shaped", scripts_dir / "setup.py"
    )
    payload_setup_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = payload_setup_mod  # see _load_setup_module's comment
    spec.loader.exec_module(payload_setup_mod)

    settings_home_dir = tmp_path / "redirected-settings-home"
    machine_local_dir = settings_home_dir / "machine-local"
    machine_local_dir.mkdir(parents=True)
    checkout = tmp_path / "doe-checkout"
    _add_source_evidence(checkout)
    (machine_local_dir / ".doe-root").write_text(str(checkout), encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))

    root = payload_setup_mod._coordinator_root_from_doe_root_pointer()
    assert root == checkout


def test_explicit_override_still_outranks_registry(setup_mod, monkeypatch, tmp_path):
    """An operator's explicit --coordinator-root is never overridden by the
    registry rung either."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(
        setup_mod, "_coordinator_root_from_registry", lambda: tmp_path / "registered"
    )
    args = setup_mod.Args()
    args.coordinator_root = str(tmp_path / "explicit")
    root, source = setup_mod._resolve_coordinator_claude_root(Path("/repo/claude-klabauter"), args)
    assert root == tmp_path / "explicit"
    assert source.rung is setup_mod.CoordSourceRung.FLAG
    assert source.display == "--coordinator-root flag"


def test_resolve_plugin_root_for_machine_local_oss_shape(setup_mod, tmp_path):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    assert setup_mod._resolve_plugin_root_for_machine_local(tmp_path) == tmp_path


# ---------------------------------------------------------------------------
# Publish-mirror rejection (Defect fix 2026-08-07) — a registered
# publish.mirrors.*.path entry is never accepted as the coordinator-claude
# WORKING root, and a candidate lacking plugin-manifest evidence is rejected
# by check_coordinator_claude_dep even when it happens not to be a mirror.
# ---------------------------------------------------------------------------


def _add_source_evidence(path: Path) -> None:
    (path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (path / ".claude-plugin" / "plugin.json").write_text("{}")
    (path / "commands").mkdir(parents=True, exist_ok=True)


def test_resolve_coordinator_claude_root_flags_registered_publish_mirror(setup_mod, monkeypatch, tmp_path):
    mirror = tmp_path / "coordinator-claude-mirror"
    mirror.mkdir()
    _add_source_evidence(mirror)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: str(Path(target_root).resolve()) == str(mirror.resolve()),
    )
    args = setup_mod.Args()
    args.coordinator_root = str(mirror)
    root, source = setup_mod._resolve_coordinator_claude_root(tmp_path / "repo", args)
    assert root == mirror
    assert source.is_publish_mirror_rejected is True
    assert "[PUBLISH MIRROR -- REJECTED]" in source.display


def test_check_coordinator_claude_dep_exits_hard_on_publish_mirror(setup_mod, monkeypatch, tmp_path):
    mirror = tmp_path / "coordinator-claude-mirror"
    mirror.mkdir()
    _add_source_evidence(mirror)  # even WITH source evidence -- mirror rejection wins
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: str(Path(target_root).resolve()) == str(mirror.resolve()),
    )
    args = setup_mod.Args()
    args.coordinator_root = str(mirror)
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_coordinator_claude_dep(tmp_path / "repo", args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING


def test_check_coordinator_claude_dep_rejects_bare_directory_no_evidence(setup_mod, monkeypatch, tmp_path):
    bare = tmp_path / "just-a-directory"
    bare.mkdir()
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: False,
    )
    args = setup_mod.Args()
    args.coordinator_root = str(bare)
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_coordinator_claude_dep(tmp_path / "repo", args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING


def test_check_coordinator_claude_dep_passes_real_source_checkout_no_mirrors_registered(setup_mod, monkeypatch, tmp_path):
    # OSS case -- no publish mirrors registered anywhere -- must resolve
    # normally, never a new failure mode.
    real_source = tmp_path / "coordinator-claude"
    real_source.mkdir()
    _add_source_evidence(real_source)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: False,
    )
    args = setup_mod.Args()
    args.coordinator_root = str(real_source)
    setup_mod.check_coordinator_claude_dep(tmp_path / "repo", args)  # must not raise


def test_resolve_plugin_root_for_machine_local_doe_clone_shape(setup_mod, tmp_path):
    """A dev clone resolves on the artifact the caller actually needs.

    The fixture builds `templates/bin/_machine_local.py` because that is what
    `resolve_machine_local_cli` consumes downstream. It used to build
    `coordinator/CLAUDE.md` instead — see the sibling regression test below for
    why that proxy stopped working.
    """
    (tmp_path / "coordinator" / "templates" / "bin").mkdir(parents=True)
    (tmp_path / "coordinator" / "templates" / "bin" / "_machine_local.py").write_text("")
    assert setup_mod._resolve_plugin_root_for_machine_local(tmp_path) == tmp_path / "coordinator"


def test_retired_claude_md_alone_does_not_resolve_a_plugin_root(setup_mod, tmp_path):
    """Regression guard: `coordinator/CLAUDE.md` is NOT evidence of a plugin root.

    DoE retired that file (`e8f9051db`). While this probe still keyed on it, the
    real dev clone resolved to None and `install_bin_forwarders` skipped with a
    "no templates/ dir" advisory — so `scripts/setup.py` exited 0 having installed
    no forwarders at all. Resolving on a doctrine file that a sibling repo is free
    to retire is the defect; this test pins that it no longer does.
    """
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "CLAUDE.md").write_text("# doc")
    assert setup_mod._resolve_plugin_root_for_machine_local(tmp_path) is None


def test_resolve_plugin_root_for_machine_local_no_match(setup_mod, tmp_path):
    assert setup_mod._resolve_plugin_root_for_machine_local(tmp_path) is None


# ---------------------------------------------------------------------------
# ensure_percolate_identity — idempotent generator, never overwrites
# ---------------------------------------------------------------------------


def test_ensure_percolate_identity_creates_when_absent(setup_mod, tmp_path):
    settings_home = tmp_path / "settings-home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    path, status = setup_mod.ensure_percolate_identity(settings_home, repo_root)

    assert status == "created"
    assert path == settings_home / ".percolate-identity"
    assert path.is_file()
    content = path.read_text()
    assert "PERSONAL_EXPECTED_PATTERNS=(" in content
    assert "PERSONAL_REVIEW_PATTERNS=(" in content
    assert "PERSONAL_ALLOW_TOKENS=(" in content


def test_ensure_percolate_identity_idempotent_does_not_overwrite(setup_mod, tmp_path):
    settings_home = tmp_path / "settings-home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    settings_home.mkdir()
    sentinel = settings_home / ".percolate-identity"
    sentinel.write_text("# hand-edited, reviewed identity config\n")

    path, status = setup_mod.ensure_percolate_identity(settings_home, repo_root)

    assert status == "exists"
    assert path == sentinel
    assert path.read_text() == "# hand-edited, reviewed identity config\n"


def test_ensure_percolate_identity_second_call_is_noop(setup_mod, tmp_path):
    settings_home = tmp_path / "settings-home"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    setup_mod.ensure_percolate_identity(settings_home, repo_root)
    first_content = (settings_home / ".percolate-identity").read_text()

    path, status = setup_mod.ensure_percolate_identity(settings_home, repo_root)

    assert status == "exists"
    assert path.read_text() == first_content


def test_derive_identity_hints_falls_back_to_placeholders_without_git(setup_mod, tmp_path, monkeypatch):
    # A repo_root with no .git directory and no reachable git binary must
    # still yield placeholder-shaped defaults, never an empty string.
    monkeypatch.setattr(
        setup_mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("git not found")),
    )
    hints = setup_mod._derive_identity_hints(tmp_path)
    assert hints["author_name"] == "YOUR_NAME_HERE"
    assert hints["org_slug"] == "your-github-org"
    assert hints["hostname_slug"]  # hostname probe still runs; never empty


# ---------------------------------------------------------------------------
# install_machine_identity -- install-time coordinator.machine_slug /
# coordinator.contributor_slug registry population (de-bash spawn-
# amplification hardening, 2026-08-05: "pay the find-out-where-we-are tax
# once, at install"). machine_resolver.compute_*_live and
# coordinator_core.install._shared.resolve_machine_local_cli are monkeypatched
# directly on their owning modules (install_machine_identity imports both
# names function-local, so patching the module attribute before the call is
# picked up at call time) -- subprocess.run is faked on setup.py's own module
# global, same idiom as test_derive_identity_hints above.
# ---------------------------------------------------------------------------


def test_install_machine_identity_writes_resolved_values(setup_mod, tmp_path, monkeypatch):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "testmachine")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "testcontrib")
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return setup_mod.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)

    assert calls == [
        ["machine-local", "set", "coordinator.machine_slug", "testmachine"],
        ["machine-local", "set", "coordinator.contributor_slug", "testcontrib"],
    ]


def test_install_machine_identity_skips_unresolved_values(setup_mod, tmp_path, monkeypatch):
    # Neither value resolvable live (no git identity / hostname) -- must
    # write NOTHING rather than persisting "unknown" as if it were real.
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "unknown")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "unknown")
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: calls.append(argv) or setup_mod.subprocess.CompletedProcess(argv, 0),
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)

    assert calls == []


def test_install_machine_identity_advisory_when_machine_local_absent(setup_mod, tmp_path, monkeypatch):
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)

    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)

    assert called["n"] is False  # advisory return, no subprocess attempted


def test_install_machine_identity_idempotent_against_real_cli(setup_mod, tmp_path_factory, tmp_path, monkeypatch):
    """Finding 3 (P2, code-reviewer) — the mocked idempotency test above only
    proves `install_machine_identity` issues the same argv twice; it cannot
    tell whether `machine-local set` itself appends a duplicate key or
    updates in place, which is the commit message's actual idempotency
    claim. This test runs the REAL `machine-local set` CLI (resolved via
    `resolve_machine_local_cli`, unmocked) against an isolated scratch
    registry — `coordinator_core.testing.registry_sandbox.sandbox_registry_dir`
    points `MACHINE_LOCAL_REGISTRY_DIR` at a per-test tmp dir seeded from the
    live registry (same idiom as `coordinator/tests/test_new_project_scaffold.py`),
    so the write is real but contained; the live registry is never touched.
    Only `compute_*_live` are stubbed (git-identity resolution is exercised
    elsewhere); `subprocess.run` is NOT mocked here.

    Review: code-reviewer (F3, P2).
    """
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install._shared import resolve_machine_local_cli
    from coordinator_core.testing.registry_sandbox import sandbox_registry_dir

    #: Unlike every other test in this module, this one drives the REAL CLI
    #: rather than a mocked `subprocess.run`, so it inherits that CLI's
    #: resolution ladder as a precondition -- and its last rung is
    #: `machine-local` on PATH, which a bare CI image or a fresh sandbox need
    #: not have. Skip loudly rather than fail: an unresolvable CLI means the
    #: environment cannot host this test, which is a different fact from the
    #: idempotency property under test being false. Same graceful-skip
    #: precedent as `coordinator/tests/test_new_project_scaffold.py`, whose
    #: sandbox idiom this test already borrows.
    if not resolve_machine_local_cli(None):
        pytest.skip("machine-local CLI not resolvable in this environment")

    registry_dir = sandbox_registry_dir(monkeypatch, tmp_path_factory.mktemp("machine-local-registry"))

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "realclitest-machine")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "realclitest-contrib")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)
    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)

    registry_path = registry_dir / "registry.local.toml"
    contents = registry_path.read_text(encoding="utf-8")

    for key, value in (
        ("coordinator.machine_slug", "realclitest-machine"),
        ("coordinator.contributor_slug", "realclitest-contrib"),
    ):
        needle = f'"{key}"'
        occurrences = contents.count(needle)
        assert occurrences == 1, (
            f"expected exactly one '{key}' entry after two install runs, "
            f"found {occurrences} in:\n{contents}"
        )
        assert value in contents


def test_install_machine_identity_idempotent_across_two_runs(setup_mod, tmp_path, monkeypatch):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "testmachine")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "testcontrib")
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: calls.append(argv) or setup_mod.subprocess.CompletedProcess(argv, 0),
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)
    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)

    # Two runs -> the SAME two `set` calls each time, no accumulation/duplication.
    assert calls == [
        ["machine-local", "set", "coordinator.machine_slug", "testmachine"],
        ["machine-local", "set", "coordinator.contributor_slug", "testcontrib"],
    ] * 2


def test_install_machine_identity_continues_past_nonzero_returncode(setup_mod, tmp_path, monkeypatch):
    """Finding 4 (P2, code-reviewer) — pin the non-fatal shape (mirrors
    `install_percolate_identity`): a nonzero `returncode` from `machine-local
    set` for the first key must not raise, must print the advisory to
    stderr, and must not stop the loop before the second key is attempted.
    """
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "testmachine")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "testcontrib")
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        returncode = 1 if argv[-1] == "testmachine" else 0
        return setup_mod.subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)  # must not raise

    assert calls == [
        ["machine-local", "set", "coordinator.machine_slug", "testmachine"],
        ["machine-local", "set", "coordinator.contributor_slug", "testcontrib"],
    ]


def test_install_machine_identity_continues_past_launch_failure(setup_mod, tmp_path, monkeypatch, capsys):
    """Finding 4 (P2, code-reviewer) — pin the non-fatal shape for the other
    uncovered branch: `subprocess.run` raising `OSError`/`TimeoutExpired`
    while launching `machine-local set` for the first key must not raise out
    of `install_machine_identity`, must print the advisory to stderr, and
    must still attempt the second key.
    """
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    monkeypatch.setattr(mr, "compute_machine_live", lambda: "testmachine")
    monkeypatch.setattr(mr, "compute_contributor_live", lambda: "testcontrib")
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "testmachine":
            raise OSError("no such file or directory: machine-local")
        return setup_mod.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod.install_machine_identity(repo_root, claude_klabauter_root, args)  # must not raise

    assert calls == [
        ["machine-local", "set", "coordinator.machine_slug", "testmachine"],
        ["machine-local", "set", "coordinator.contributor_slug", "testcontrib"],
    ]

    stderr = capsys.readouterr().err
    assert "registry write failed to launch" in stderr


# ---------------------------------------------------------------------------
# run_health_probe -- hard-severity failure detection (state/audits/
# 2026-08-05 registry-key mismatch task: "a required probe that cannot fail
# is not a probe"). subprocess.run is faked (same idiom as
# test_derive_identity_hints_falls_back_to_placeholders_without_git above)
# so these exercise the parsing/return-value contract without a real
# claude-klabauter-doctor-probe.py subprocess.
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_probe_run(ndjson_lines, returncode):
    joined = "\n".join(ndjson_lines) + "\n"
    return lambda *a, **k: _FakeCompletedProcess(joined, returncode)


def test_run_health_probe_returns_false_when_all_probes_pass(setup_mod, tmp_path, monkeypatch, capsys):
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            ['{"name": "claude-klabauter.root.resolve", "status": "pass", "severity": "hard", "detail": "", "remediation": "—"}'],
            0,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)
    assert result is False
    assert "ERROR" not in capsys.readouterr().err


def test_run_health_probe_returns_false_on_advisory_only_failure(setup_mod, tmp_path, monkeypatch, capsys):
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            ['{"name": "claude-klabauter.schema.vendor_drift", "status": "fail", "severity": "advisory", "detail": "", "remediation": "—"}'],
            1,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)
    assert result is False
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "ERROR" not in err


def test_run_health_probe_returns_true_on_hard_failure(setup_mod, tmp_path, monkeypatch, capsys):
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            ['{"name": "claude-klabauter.root.resolve", "status": "fail", "severity": "hard", "detail": "boom", "remediation": "fix it"}'],
            1,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)
    assert result is True
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "HARD-severity" in err


def test_run_health_probe_hard_failure_detected_in_agent_mode_too(setup_mod, tmp_path, monkeypatch, capsys):
    # agent_mode skips the human-readable per-line loop entirely -- hard-
    # failure detection must not depend on that loop having run.
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            ['{"name": "claude-klabauter.root.resolve", "status": "fail", "severity": "hard", "detail": "boom", "remediation": "fix it"}'],
            1,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=True)
    assert result is True


def test_run_health_probe_missing_probe_file_returns_false(setup_mod, tmp_path):
    # tmp_path has no bin/claude-klabauter-doctor-probe.py at all.
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)
    assert result is False


# ---------------------------------------------------------------------------
# resolve_repo_identity — this script is BOTH claude-klabauter's AND
# claude-klabauter's standalone installer; register_claude_klabauter_root must know
# which tree it is running from before deciding which machine-local key(s)
# to write (dispatch context: scripts/setup.py is not only claude-klabauter's
# installer but claude-klabauter's too — docs/plans/2026-07-31-claude-
# klabauter-oss-release.md § 150/626).
# ---------------------------------------------------------------------------


def _write_manifest(root: Path, repo_id: str) -> None:
    manifest_dir = root / "docs" / "install"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "agent-install-manifest.json").write_text(
        json.dumps({"repo_id": repo_id})
    )


def test_resolve_repo_identity_claude_klabauter_manifest(setup_mod, tmp_path):
    _write_manifest(tmp_path, "claude-klabauter")
    assert setup_mod.resolve_repo_identity(tmp_path) == "claude-klabauter"


def test_resolve_repo_identity_klabauter_agents_md(setup_mod, tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# claude-klabauter — Agent Entry Point\n\nSome body text.\n"
    )
    assert setup_mod.resolve_repo_identity(tmp_path) == "claude-klabauter"


def test_resolve_repo_identity_klabauter_wins_over_claude_klabauter_manifest(setup_mod, tmp_path):
    # Both markers present (not a realistic tree today, but the klabauter
    # AGENTS.md check runs first and must not be starved by an also-present
    # claude-klabauter manifest).
    (tmp_path / "AGENTS.md").write_text("# claude-klabauter — Agent Entry Point\n")
    _write_manifest(tmp_path, "claude-klabauter")
    assert setup_mod.resolve_repo_identity(tmp_path) == "claude-klabauter"


def test_resolve_repo_identity_unrelated_agents_md_falls_through(setup_mod, tmp_path):
    # An AGENTS.md that isn't klabauter's must not false-positive — falls
    # through to the claude-klabauter manifest check.
    (tmp_path / "AGENTS.md").write_text("# some other project\n")
    _write_manifest(tmp_path, "claude-klabauter")
    assert setup_mod.resolve_repo_identity(tmp_path) == "claude-klabauter"


def test_resolve_repo_identity_wrong_repo_id_is_not_claude_klabauter(setup_mod, tmp_path):
    _write_manifest(tmp_path, "some-other-repo")
    assert setup_mod.resolve_repo_identity(tmp_path) is None


def test_resolve_repo_identity_neither_marker_present(setup_mod, tmp_path):
    assert setup_mod.resolve_repo_identity(tmp_path) is None


def test_resolve_repo_identity_empty_agents_md_falls_through(setup_mod, tmp_path):
    (tmp_path / "AGENTS.md").write_text("")
    _write_manifest(tmp_path, "claude-klabauter")
    assert setup_mod.resolve_repo_identity(tmp_path) == "claude-klabauter"


def test_resolve_repo_identity_malformed_manifest_json_is_unresolved(setup_mod, tmp_path):
    manifest_dir = tmp_path / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "agent-install-manifest.json").write_text("{not valid json")
    assert setup_mod.resolve_repo_identity(tmp_path) is None


# ---------------------------------------------------------------------------
# register_claude_klabauter_root — identity-branched registration. Exercised via the
# machine-local-absent + override-pair-accepted degrade path (advisory, no
# subprocess), matching this file's own convention (see
# test_install_machine_identity_advisory_when_machine_local_absent) of not
# needing a real machine-local binary to prove which keys a caller intends
# to write.
# ---------------------------------------------------------------------------


def _override_args(setup_mod) -> "setup_mod.Args":
    args = setup_mod.Args()
    args.skip_dep_check = True
    args.accept_risk = True
    return args


def test_register_claude_klabauter_root_claude_klabauter_identity_writes_both_keys(setup_mod, tmp_path, monkeypatch, capsys):
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, "claude-klabauter")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False  # degrade path, no subprocess attempted
    out = capsys.readouterr().out
    assert "repos.claude_klabauter" in out
    assert "engine.working_repos.claude_klabauter" in out


def test_register_claude_klabauter_root_klabauter_identity_writes_only_klabauter_key(setup_mod, tmp_path, monkeypatch, capsys):
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "AGENTS.md").write_text("# claude-klabauter — Agent Entry Point\n")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False  # degrade path, no subprocess attempted
    out = capsys.readouterr().out
    assert "repos.claude_klabauter" in out
    assert "repos.claude_klabauter" not in out
    assert "engine.working_repos.claude_klabauter" not in out


def test_register_claude_klabauter_root_unresolved_identity_fails_loud(setup_mod, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()  # no AGENTS.md, no docs/install manifest
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, setup_mod.Args())

    assert exc_info.value.code == setup_mod.EXIT_REPO_IDENTITY_UNRESOLVED


def test_register_claude_klabauter_root_unresolved_identity_not_overridable(setup_mod, tmp_path):
    # Unlike the machine-local-absent guard, identity resolution is NOT
    # degradable via --skip-dep-check/--accept-missing-deps-risk -- a wrong
    # guess here poisons the working-repo discriminant, so the override pair
    # must not bypass it.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert exc_info.value.code == setup_mod.EXIT_REPO_IDENTITY_UNRESOLVED


# ---------------------------------------------------------------------------
# register_claude_klabauter_root / _discover_klabauter_root — dual-boot auto-arm.
# docs/plans/2026-08-12-auto-arm-the-dual-boot-for-claude-klabauter-instal.md C1,
# AC1-AC4 plus the discovery ladder's order and rejection guarantees that
# back "AC2 over AC1" (never write a guessed or wrong path).
# ---------------------------------------------------------------------------


def test_register_claude_klabauter_root_claude_klabauter_seeds_klabauter_when_discoverable(
    setup_mod, tmp_path, monkeypatch, capsys
):
    # AC1: a discoverable klabauter checkout is auto-armed with the
    # DISCOVERED path, not the claude-klabauter root being installed.
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, "claude-klabauter")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    discovered = tmp_path / "discovered-klabauter"
    discovered.mkdir()
    monkeypatch.setattr(
        setup_mod, "_discover_klabauter_root", lambda repo_root, plugin_root: str(discovered)
    )

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False  # degrade path, no subprocess attempted
    out = capsys.readouterr().out
    assert f"repos.claude_klabauter {discovered}" in out
    assert f"repos.claude_klabauter {claude_klabauter_root}" not in out


def test_register_claude_klabauter_root_claude_klabauter_no_klabauter_key_when_undiscoverable(
    setup_mod, tmp_path, monkeypatch, capsys
):
    # AC2: no discoverable klabauter checkout -> install completes normally
    # and the key is ABSENT entirely, never written empty or guessed.
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, "claude-klabauter")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    monkeypatch.setattr(
        setup_mod, "_discover_klabauter_root", lambda repo_root, plugin_root: None
    )

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False
    out = capsys.readouterr().out
    assert "repos.claude_klabauter" not in out
    assert "repos.claude_klabauter" in out
    assert "engine.working_repos.claude_klabauter" in out


def test_register_claude_klabauter_root_klabauter_identity_never_calls_discover(
    setup_mod, tmp_path, monkeypatch, capsys
):
    # AC3: the claude-klabauter branch is byte-identical -- exactly one key,
    # neither claude_klabauter key, and _discover_klabauter_root is NEVER
    # invoked on this path (the PM ruling that this user must never
    # encounter the dual-boot concept, asserted structurally).
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    discover_called = {"n": False}

    def _spy_discover(repo_root, plugin_root):
        discover_called["n"] = True
        return None

    monkeypatch.setattr(setup_mod, "_discover_klabauter_root", _spy_discover)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "AGENTS.md").write_text("# claude-klabauter — Agent Entry Point\n")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False
    assert discover_called["n"] is False
    out = capsys.readouterr().out
    # Finding 5 (staff-eng C8 review): `engine.target` now precedes
    # `repos.claude_klabauter` so a mid-loop failure leaves the safe
    # target-without-mirror residue, not the false-positive
    # mirror-without-target one.
    assert "--- Registration (claude-klabauter): engine.target + repos.claude_klabauter ---" in out
    assert "repos.claude_klabauter" not in out
    assert "engine.working_repos.claude_klabauter" not in out


def test_register_claude_klabauter_root_preserves_existing_valid_klabauter_value(
    setup_mod, tmp_path, monkeypatch, capsys
):
    # AC4: an existing, valid repos.claude_klabauter value is preserved --
    # never replaced with a different (also valid) discovered path.
    from coordinator_core.install import _shared

    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)
    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, "claude-klabauter")
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()

    existing = tmp_path / "existing-klabauter"
    existing.mkdir()
    (existing / "coordinator_core").mkdir()

    other_sibling = repo_root.parent / "claude-klabauter"
    other_sibling.mkdir()
    (other_sibling / "coordinator_core").mkdir()

    def _fake_ml_get(key, *, plugin_root=None, registry_dir=None):
        if key == "repos.claude_klabauter":
            return str(existing)
        return ""

    monkeypatch.setattr(_shared, "ml_get", _fake_ml_get)

    setup_mod.register_claude_klabauter_root(claude_klabauter_root, "test-source", repo_root, _override_args(setup_mod))

    assert called["n"] is False
    out = capsys.readouterr().out
    assert f"repos.claude_klabauter {existing}" in out
    assert str(other_sibling) not in out


def test_discover_klabauter_root_registry_value_wins_over_sibling(
    setup_mod, tmp_path, monkeypatch
):
    # Discovery ladder ORDER: step 1 (registry value) and step 3 (sibling
    # layout) both resolve, to DIFFERENT paths -- step 1 must win.
    from coordinator_core.install import _shared

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    registry_hit = tmp_path / "registry-hit"
    registry_hit.mkdir()
    (registry_hit / "coordinator_core").mkdir()

    sibling = repo_root.parent / "claude-klabauter"
    sibling.mkdir()
    (sibling / "coordinator_core").mkdir()

    def _fake_ml_get(key, *, plugin_root=None, registry_dir=None):
        if key == "repos.claude_klabauter":
            return str(registry_hit)
        return ""

    monkeypatch.setattr(_shared, "ml_get", _fake_ml_get)

    result = setup_mod._discover_klabauter_root(repo_root, None)

    assert result == str(registry_hit)


def test_discover_klabauter_root_rejects_candidate_without_coordinator_core(
    setup_mod, tmp_path, monkeypatch
):
    # Discovery REJECTION: a candidate that exists on disk but lacks
    # coordinator_core/ must be rejected and the ladder must continue past
    # it -- the guard against writing a bogus path (AC2-beats-AC1).
    from coordinator_core.install import _shared

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    bogus_registry_hit = tmp_path / "bogus-registry-hit"
    bogus_registry_hit.mkdir()  # exists, but no coordinator_core/ -- must be rejected

    sibling = repo_root.parent / "claude-klabauter"
    sibling.mkdir()
    (sibling / "coordinator_core").mkdir()

    def _fake_ml_get(key, *, plugin_root=None, registry_dir=None):
        if key == "repos.claude_klabauter":
            return str(bogus_registry_hit)
        return ""

    monkeypatch.setattr(_shared, "ml_get", _fake_ml_get)

    result = setup_mod._discover_klabauter_root(repo_root, None)

    assert result == str(sibling)


# ---------------------------------------------------------------------------
# install_claude_doe_launcher_chain — the interactive `claude-doe` launch
# chain (.doe-root pointer -> wrapper -> launcher -> rc/profile shim). Root-
# cause: scripts/setup.py never called any of the four coordinator/bin/
# *claude-doe* generators, so a fresh clean install left coordinator SILENTLY
# absent from every session (sizing dlv-claude-doe-launcher-generators-are-
# absen-bb685e). These tests are the artifact that discharges "the operator
# remembers is not one" -- a dropped generator, or a reordering that breaks
# the root-pointer-before-wrapper-before-shim dependency, fails here.
# ---------------------------------------------------------------------------

_EXPECTED_CLAUDE_DOE_CHAIN_ORDER = (
    "gen-doe-root-pointer.py",
    "install-claude-doe-wrapper.py",
    "gen-claude-doe-launcher.py",
    "gen-claude-doe-shim.py",
)


def test_claude_doe_chain_names_all_four_generators_in_dependency_order(setup_mod):
    names = tuple(name for _, name, _ in setup_mod._CLAUDE_DOE_CHAIN_STEPS)
    assert names == _EXPECTED_CLAUDE_DOE_CHAIN_ORDER


def test_claude_doe_chain_generators_exist_on_disk(setup_mod):
    # Regression guard: if a generator is ever renamed/removed from
    # coordinator/bin/ without updating the chain (or vice versa), this
    # fails loudly instead of silently install-chain-skipping it.
    repo_root = _SETUP_PY_PATH.parent.parent
    for _, name, _ in setup_mod._CLAUDE_DOE_CHAIN_STEPS:
        assert (repo_root / "coordinator" / "bin" / name).is_file(), (
            f"{name} is declared in _CLAUDE_DOE_CHAIN_STEPS but missing from "
            "coordinator/bin/ — the chain and the generator surface have drifted."
        )


def test_claude_doe_chain_is_wired_into_main(setup_mod):
    # A defined-but-never-called step reproduces the exact silent-absence
    # defect this fix closes -- assert the function is both DEFINED and
    # INVOKED (def + call site), not merely present in the module. AST-walked
    # (not a raw substring count) so commenting out the call site while
    # leaving the text intact -- which would keep a `.count(...) >= 2` check
    # green -- is caught: a `Call` node whose `func` resolves to the name
    # must actually exist in `main`'s body.
    source = _SETUP_PY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SETUP_PY_PATH))

    defined = any(
        isinstance(node, ast.FunctionDef) and node.name == "install_claude_doe_launcher_chain"
        for node in ast.walk(tree)
    )
    assert defined, "install_claude_doe_launcher_chain is not defined in scripts/setup.py"

    main_func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "install_claude_doe_launcher_chain"
        for node in ast.walk(main_func)
    )
    assert called, "install_claude_doe_launcher_chain is defined but never called from main()"


def test_claude_doe_chain_manifest_declares_the_step():
    manifest_path = _SETUP_PY_PATH.parent.parent / "docs" / "install" / "agent-install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = {entry.get("id") for entry in manifest.get("system_prerequisites", [])}
    assert "claude_doe_launcher_chain" in ids


def test_install_claude_doe_launcher_chain_missing_generators_is_loud_advisory(
    setup_mod, tmp_path, monkeypatch, capsys
):
    repo_root = tmp_path / "repo"
    (repo_root / "coordinator" / "bin").mkdir(parents=True)  # empty -- all four missing

    called = {"n": 0}
    monkeypatch.setattr(
        setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )

    args = setup_mod.Args()
    setup_mod.install_claude_doe_launcher_chain(repo_root, sys.executable, tmp_path, args)

    assert called["n"] == 0  # never even attempted a subprocess for a missing file
    err = capsys.readouterr().err
    assert err.count("[ADVISORY]") >= len(setup_mod._CLAUDE_DOE_CHAIN_STEPS)
    assert "claude-doe launcher chain incomplete" in err
    assert "will NOT load" in err


def test_install_claude_doe_launcher_chain_continues_past_a_mid_chain_failure(
    setup_mod, tmp_path, monkeypatch, capsys
):
    repo_root = tmp_path / "repo"
    bin_dir = repo_root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    for _, name, _ in setup_mod._CLAUDE_DOE_CHAIN_STEPS:
        (bin_dir / name).write_text("# stub\n")

    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if "install-claude-doe-wrapper.py" in argv[1]:
            return setup_mod.subprocess.CompletedProcess(argv, 1, stdout="boom", stderr="")
        return setup_mod.subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    args = setup_mod.Args()
    setup_mod.install_claude_doe_launcher_chain(repo_root, sys.executable, tmp_path, args)

    # All four steps attempted despite the mid-chain failure -- a failure in
    # one generator must not skip the others (only runtime resolution is
    # order-dependent, not generation).
    assert len(calls) == len(setup_mod._CLAUDE_DOE_CHAIN_STEPS)
    out_err = capsys.readouterr()
    assert "PASS [claude-doe-chain]" in out_err.out
    assert "[ADVISORY]" in out_err.err
    assert "claude-doe launcher chain incomplete" in out_err.err


def test_install_claude_doe_launcher_chain_doe_root_skip_is_not_pass(
    setup_mod, tmp_path, monkeypatch, capsys
):
    # Review: coordinatorcode-reviewer-7ca32c22 — `gen-doe-root-pointer.py
    # --graceful-skip-unresolved` exits 0 without writing the pointer when
    # repos.doe_claude isn't resolved yet (fresh-clean-install shape). This
    # must never read as PASS, and the skip explanation must survive
    # agent_mode (the mode a scripted/CI install runs under).
    repo_root = tmp_path / "repo"
    bin_dir = repo_root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    for _, name, _ in setup_mod._CLAUDE_DOE_CHAIN_STEPS:
        (bin_dir / name).write_text("# stub\n")

    def _fake_run(argv, **kwargs):
        if "gen-doe-root-pointer.py" in argv[1]:
            return setup_mod.subprocess.CompletedProcess(
                argv, 0,
                stdout="doe_root_pointer: skipped (repos.doe_claude not resolved — complete step 3.5a first)",
                stderr="",
            )
        return setup_mod.subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    args = setup_mod.Args()
    args.agent_mode = True
    setup_mod.install_claude_doe_launcher_chain(repo_root, sys.executable, tmp_path, args)

    out_err = capsys.readouterr()
    assert "PASS [claude-doe-chain] doe-root pointer" not in out_err.out
    assert "doe_root_pointer: skipped" in out_err.err  # survives agent_mode
    assert "[ADVISORY]" in out_err.err
    # The other three steps still ran and still PASS -- only the skipped
    # step's own outcome downgrades.
    assert out_err.out.count("PASS [claude-doe-chain]") == len(setup_mod._CLAUDE_DOE_CHAIN_STEPS) - 1


def test_install_claude_doe_launcher_chain_all_pass_prints_no_incomplete_summary(
    setup_mod, tmp_path, monkeypatch, capsys
):
    repo_root = tmp_path / "repo"
    bin_dir = repo_root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    for _, name, _ in setup_mod._CLAUDE_DOE_CHAIN_STEPS:
        (bin_dir / name).write_text("# stub\n")

    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: setup_mod.subprocess.CompletedProcess(argv, 0, stdout="ok", stderr=""),
    )

    args = setup_mod.Args()
    setup_mod.install_claude_doe_launcher_chain(repo_root, sys.executable, tmp_path, args)

    out_err = capsys.readouterr()
    assert out_err.out.count("PASS [claude-doe-chain]") == len(setup_mod._CLAUDE_DOE_CHAIN_STEPS)
    assert "incomplete" not in out_err.err
    assert "[ADVISORY]" not in out_err.err


# ---------------------------------------------------------------------------
# _seed_fleet_env_root_from_klabauter — C4: seeds fleet_env.root from the
# already-registered repos.claude_klabauter so the fleet environment lands
# at the contract's documented location (<klabauter-root>/.fleet-env)
# without a hand-run `machine-local set`. Same monkeypatch idiom as
# test_install_machine_identity_* above: coordinator_core.machine_resolver
# .registry_get and coordinator_core.install._shared.resolve_machine_local_cli
# are imported function-local, so patching the module attribute before the
# call is picked up at call time; subprocess.run is faked on setup.py's own
# module global.
# ---------------------------------------------------------------------------


def test_seed_fleet_env_root_writes_from_klabauter_when_absent(setup_mod, tmp_path, monkeypatch):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    values = {"fleet_env.root": None, "repos.claude_klabauter": str(tmp_path / "klabauter")}
    monkeypatch.setattr(mr, "registry_get", lambda key: values.get(key))
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        lambda argv, **k: calls.append(argv) or setup_mod.subprocess.CompletedProcess(argv, 0),
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod._seed_fleet_env_root_from_klabauter(repo_root, claude_klabauter_root, args)

    expected = str(tmp_path / "klabauter" / ".fleet-env")
    assert calls == [["machine-local", "set", "fleet_env.root", expected]]


def test_seed_fleet_env_root_never_overwrites_existing_value(setup_mod, tmp_path, monkeypatch):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    values = {
        "fleet_env.root": "/operator/chosen/.fleet-env",
        "repos.claude_klabauter": str(tmp_path / "klabauter"),
    }
    monkeypatch.setattr(mr, "registry_get", lambda key: values.get(key))
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: calls.append(a) or None)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod._seed_fleet_env_root_from_klabauter(repo_root, claude_klabauter_root, args)

    assert calls == []  # an operator-set (or previously-seeded) key is never re-written


def test_seed_fleet_env_root_noop_when_klabauter_unregistered(setup_mod, tmp_path, monkeypatch):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    values = {"fleet_env.root": None, "repos.claude_klabauter": None}
    monkeypatch.setattr(mr, "registry_get", lambda key: values.get(key))
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: ["machine-local"])

    calls = []
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: calls.append(a) or None)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod._seed_fleet_env_root_from_klabauter(repo_root, claude_klabauter_root, args)

    assert calls == []  # nothing discoverable to seed from -- C5's ladder still resolves at read time


def test_seed_fleet_env_root_advisory_when_machine_local_absent(setup_mod, tmp_path, monkeypatch, capsys):
    import coordinator_core.machine_resolver as mr
    from coordinator_core.install import _shared

    values = {"fleet_env.root": None, "repos.claude_klabauter": str(tmp_path / "klabauter")}
    monkeypatch.setattr(mr, "registry_get", lambda key: values.get(key))
    monkeypatch.setattr(_shared, "resolve_machine_local_cli", lambda plugin_root: None)

    called = {"n": False}
    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("n", True))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    args = setup_mod.Args()

    setup_mod._seed_fleet_env_root_from_klabauter(repo_root, claude_klabauter_root, args)

    assert called["n"] is False  # advisory return, no subprocess attempted
    assert "[ADVISORY]" in capsys.readouterr().out


def test_run_health_probe_hard_inconclusive_does_not_gate_the_install(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """A hard probe that could not MEASURE is not a failed install.

    `claude-klabauter.warm.residency`'s reachability primitive is Windows-only, so on
    every POSIX box running a warm server it is inconclusive by construction,
    with remediation "—". Treating that as a hard failure returned exit 94
    from a correct macOS install, permanently, for a reading nobody can make
    conclusive and nobody can act on.
    """
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            ['{"name": "claude-klabauter.warm.residency", "status": "inconclusive", "severity": "hard",'
             ' "detail": "reachability primitive is Windows-only", "remediation": "\\u2014"}'],
            1,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)

    assert result is False
    err = capsys.readouterr().err
    assert "claude-klabauter.warm.residency" in err
    assert "could not measure" in err
    assert "HARD-severity failure" not in err


def test_run_health_probe_hard_fail_still_gates_alongside_an_inconclusive(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """Narrowing to "fail" must not swallow a real hard failure sitting
    beside an inconclusive one."""
    probe = tmp_path / "bin" / "claude-klabauter-doctor-probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("# fake probe\n")
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_probe_run(
            [
                '{"name": "claude-klabauter.warm.residency", "status": "inconclusive", "severity": "hard",'
                ' "detail": "cannot tell", "remediation": "\\u2014"}',
                '{"name": "claude-klabauter.root.resolve", "status": "fail", "severity": "hard",'
                ' "detail": "boom", "remediation": "fix it"}',
            ],
            1,
        ),
    )
    result = setup_mod.run_health_probe(tmp_path, sys.executable, agent_mode=False)

    assert result is True
    assert "HARD-severity failure" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# start_warm_engine — C2 (dispatch brief 2026-08-22-warm-engine-and-door-
# install-from-published-root). `resolve_engine_root_for_install`,
# `spawn_detached`/`_child_env`, and `SERVER_ENTRY_SCRIPT` are all imported
# function-local in `start_warm_engine`, so patching each module's attribute
# before the call is picked up at call time (same idiom as the
# `_seed_fleet_env_root_from_klabauter` tests above). The verification
# child's own `subprocess.run` is faked on setup.py's own module global and
# returns canned stdout JSON, standing in for the real child process
# (eng-director F1's PYTHONPATH-rooted poll) without actually spawning one.
# ---------------------------------------------------------------------------


class _FakeInstallEngineRoot:
    def __init__(self, kind, root=None, remediation=None):
        self.kind = kind
        self.root = root
        self.remediation = remediation


def _patch_warm_engine_seams(monkeypatch, *, resolved, spawn_exc=None):
    """Patch the three function-local imports `start_warm_engine` makes,
    plus `subprocess.run` on setup.py's own module global for the
    verification child. Returns a dict of call-recording lists the test
    body can assert against."""
    import coordinator_core.install.engine_root_for_install as engine_root_for_install_mod
    import coordinator_core.ops.ceremony.detached_spawn as detached_spawn_mod
    import coordinator_core.warm.client as warm_client_mod

    calls = {"spawn": []}

    def _fake_spawn_detached(repo_root, script_path, args=None):
        calls["spawn"].append((repo_root, script_path))
        if spawn_exc is not None:
            raise spawn_exc
        return True

    monkeypatch.setattr(
        engine_root_for_install_mod, "resolve_engine_root_for_install", lambda: resolved
    )
    monkeypatch.setattr(detached_spawn_mod, "spawn_detached", _fake_spawn_detached)
    monkeypatch.setattr(detached_spawn_mod, "_child_env", lambda repo_root: {"PYTHONPATH": repo_root})
    monkeypatch.setattr(warm_client_mod, "SERVER_ENTRY_SCRIPT", "coordinator_core/warm/server.py")
    return calls


def _fake_verification_run(stdout_json: dict, *, returncode: int = 0):
    def _run(argv, **kwargs):
        return type("_CP", (), {"returncode": returncode, "stdout": json.dumps(stdout_json) + "\n", "stderr": ""})()
    return _run


def test_start_warm_engine_advisory_on_no_published_root(setup_mod, tmp_path, monkeypatch, capsys):
    resolved = _FakeInstallEngineRoot("none", root=None, remediation="run the publish step")
    calls = _patch_warm_engine_seams(monkeypatch, resolved=resolved)

    setup_mod.start_warm_engine(tmp_path)

    assert calls["spawn"] == []  # no published root -> never even attempts a spawn
    err = capsys.readouterr().err
    assert "[ADVISORY]" in err
    assert "run the publish step" in err


def test_start_warm_engine_advisory_on_spawn_failure(setup_mod, tmp_path, monkeypatch, capsys):
    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_warm_engine_seams(monkeypatch, resolved=resolved, spawn_exc=RuntimeError("boom"))

    setup_mod.start_warm_engine(tmp_path)

    err = capsys.readouterr().err
    assert "[ADVISORY]" in err
    assert "spawn failed" in err
    # `SERVER_ENTRY_SCRIPT` is repo-relative and resolves against the PUBLISHED
    # root, never the operator's cwd -- and the same relative path exists in a
    # source clone, where a server started from it is ineligible to serve
    # (DR-315 s2 / DR-331). A rootless advisory therefore reads as a working
    # instruction and starts the wrong thing.
    assert str(published) in err


def test_start_warm_engine_advisory_on_unserved_ping(setup_mod, tmp_path, monkeypatch, capsys):
    """AC5: a forced fall-through (the verification child never observes a
    served ping) must FAIL the check — never a fabricated PASS."""
    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_warm_engine_seams(monkeypatch, resolved=resolved)
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_verification_run({"served": False, "coordinator_core_file": None}),
    )

    setup_mod.start_warm_engine(published)

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "did not serve a ping" in err
    # `SERVER_ENTRY_SCRIPT` is repo-relative and resolves against the PUBLISHED
    # root, never the operator's cwd -- and the same relative path exists in a
    # source clone, where a server started from it is ineligible to serve
    # (DR-315 s2 / DR-331). A rootless advisory therefore reads as a working
    # instruction and starts the wrong thing.
    assert str(published) in err


def test_start_warm_engine_advisory_when_resolved_file_outside_published_root(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """A served ping whose resolved coordinator_core.__file__ is NOT under
    the published root must also FAIL — a served ping alone is exactly the
    trap AC3/F1 exist to close (a cold path answers a ping identically)."""
    published = tmp_path / "published"
    published.mkdir()
    elsewhere = tmp_path / "elsewhere" / "coordinator_core" / "__init__.py"
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_warm_engine_seams(monkeypatch, resolved=resolved)
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_verification_run({"served": True, "coordinator_core_file": str(elsewhere)}),
    )

    setup_mod.start_warm_engine(published)

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "did not resolve" in err
    # `SERVER_ENTRY_SCRIPT` is repo-relative and resolves against the PUBLISHED
    # root, never the operator's cwd -- and the same relative path exists in a
    # source clone, where a server started from it is ineligible to serve
    # (DR-315 s2 / DR-331). A rootless advisory therefore reads as a working
    # instruction and starts the wrong thing.
    assert str(published) in err


def test_start_warm_engine_pass_on_positive_assertion(setup_mod, tmp_path, monkeypatch, capsys):
    """The positive assertion AC3 requires: a served ping AND a resolved
    `coordinator_core.__file__` confirmed under the published root."""
    published = tmp_path / "published"
    (published / "coordinator_core").mkdir(parents=True)
    resolved_file = published / "coordinator_core" / "__init__.py"
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_warm_engine_seams(monkeypatch, resolved=resolved)
    monkeypatch.setattr(
        setup_mod.subprocess, "run",
        _fake_verification_run({"served": True, "coordinator_core_file": str(resolved_file)}),
    )

    setup_mod.start_warm_engine(published)

    out = capsys.readouterr().out
    assert "PASS [warm engine]" in out
    assert str(resolved_file) in out


def test_verification_child_program_names_the_artifact(setup_mod):
    """The child program's source must itself dispatch through
    `try_warm_dispatch` and report `coordinator_core.__file__` — the
    positive-assertion contract AC3 requires, not merely `served: true`."""
    program = setup_mod._verification_child_program()
    assert "try_warm_dispatch" in program
    assert "coordinator_core.__file__" in program
    assert "coordinator_core_file" in program


# ---------------------------------------------------------------------------
# install_warm_door (C6: wire the door step into the installer, verified
# through the door). Spec backlink: state/dispatch-briefs/2026-08-22-warm-
# engine-and-door-install-from-published-root/C6.md
# ---------------------------------------------------------------------------


class _FakeDoorRouteResult:
    def __init__(self, route, entry=None):
        self.route = route
        self.entry = entry


class _FakeDoorBuildResult:
    def __init__(self, built, output=None, advisory=None):
        self.built = built
        self.output = output
        self.advisory = advisory


def _patch_door_seams(
    monkeypatch,
    *,
    resolved,
    settings_home_dir,
    socket_path_exc=None,
    posix_build=None,
    door_route=None,
    control_route=None,
):
    """Patches every function-local import `install_warm_door` makes, on
    the REAL module objects those imports resolve to (mirroring
    `_patch_warm_engine_seams`'s shape) — never on `setup_mod` itself,
    since these are deferred imports inside the function body."""
    import coordinator_core._settings_home as settings_home_mod
    import coordinator_core.install.door_install as door_install_mod
    import coordinator_core.install.door_install_posix_build as door_posix_mod
    import coordinator_core.install.door_route_signal as door_route_signal_mod
    import coordinator_core.install.engine_root_for_install as engine_root_for_install_mod
    import coordinator_core.warm.election as election_mod
    import coordinator_core.warm.skew as skew_mod

    monkeypatch.setattr(
        engine_root_for_install_mod, "resolve_engine_root_for_install", lambda: resolved
    )
    monkeypatch.setattr(settings_home_mod, "settings_home", lambda: settings_home_dir)
    monkeypatch.setattr(skew_mod, "compute_client_token", lambda repo_root: "deadbeef")

    def _fake_socket_path(token, *, engine_clone=None):
        if socket_path_exc is not None:
            raise socket_path_exc
        return Path("/tmp/fake.sock")

    monkeypatch.setattr(election_mod, "socket_path", _fake_socket_path)

    monkeypatch.setattr(door_install_mod, "install_door", lambda bin_dst, engine_root: bin_dst / "coordinator-invoke")

    if posix_build is None:
        posix_build = _FakeDoorBuildResult(built=True, output=settings_home_dir / "bin" / "coordinator-invoke")
    monkeypatch.setattr(door_posix_mod, "build_or_advise", lambda engine_root, output=None: posix_build)

    if door_route is None:
        door_route = _FakeDoorRouteResult(door_route_signal_mod.WARM_SERVER)
    monkeypatch.setattr(door_route_signal_mod, "read_door_route", lambda door_path, op, *, repo_root: door_route)

    if control_route is None:
        control_route = _FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS)
    monkeypatch.setattr(
        door_route_signal_mod, "run_cold_control_invocation",
        lambda op, *, repo_root, params=None: control_route,
    )


def test_install_warm_door_advisory_on_no_published_root(setup_mod, tmp_path, monkeypatch, capsys):
    resolved = _FakeInstallEngineRoot("none", root=None, remediation="run the publish step")
    _patch_door_seams(monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    err = capsys.readouterr().err
    assert "[ADVISORY]" in err
    assert "run the publish step" in err


def test_install_warm_door_advisory_on_sun_path_budget(setup_mod, tmp_path, monkeypatch, capsys):
    """AC12: a too-long socket path is reported as its own named ADVISORY,
    never folded into a fall-through/unresolved read."""
    import coordinator_core.warm.election as election_mod

    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        socket_path_exc=election_mod.SocketPathTooLongError("socket path is 140 bytes, over the 100-byte sun_path budget"),
    )

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "sun_path budget" in err
    assert "140 bytes" in err


def test_install_warm_door_advisory_on_posix_build_miss(setup_mod, tmp_path, monkeypatch, capsys):
    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        posix_build=_FakeDoorBuildResult(built=False, advisory="no C compiler found on PATH"),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "no C compiler found on PATH" in err


def test_install_warm_door_pass_on_warm_server_route(setup_mod, tmp_path, monkeypatch, capsys):
    """AC5's PASS arm: a genuine warm_server-routed row reports PASS."""
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        door_route=_FakeDoorRouteResult(door_route_signal_mod.WARM_SERVER),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    out = capsys.readouterr().out
    assert "PASS [door]" in out
    assert "warm_server" in out


def test_install_warm_door_advisory_on_forced_fall_through(setup_mod, tmp_path, monkeypatch, capsys):
    """AC5's FAIL arm: a forced fall-through (route=in_process) with a
    genuine (in_process-proving) control invocation reports an ADVISORY,
    never a fabricated PASS."""
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        door_route=_FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS),
        control_route=_FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "fell through" in err


def test_install_warm_door_advisory_on_discriminator_unavailable(setup_mod, tmp_path, monkeypatch, capsys):
    """The discriminator-inert trap (C5 module docstring): an unresolved
    door read whose control invocation ALSO comes back unresolved reports
    `discriminator_unavailable` explicitly — never folded into a
    fall-through or a PASS."""
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        door_route=_FakeDoorRouteResult(door_route_signal_mod.UNRESOLVED),
        control_route=_FakeDoorRouteResult(door_route_signal_mod.UNRESOLVED),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    out, err = capsys.readouterr()
    assert "PASS" not in out
    assert "[ADVISORY]" in err
    assert "discriminator_unavailable" in err


def test_install_warm_door_control_invocation_anchored_to_repo_root(setup_mod, tmp_path, monkeypatch, capsys):
    """eng-director F5 (C6 half): the control invocation must be anchored
    explicitly to THIS function's `repo_root` parameter, never the
    executing process's ambient `Path.cwd()`."""
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    claude_klabauter_checkout = tmp_path / "claude-klabauter-checkout"
    claude_klabauter_checkout.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        door_route=_FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS),
        control_route=_FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    seen_repo_roots = []
    monkeypatch.setattr(
        door_route_signal_mod, "run_cold_control_invocation",
        lambda op, *, repo_root, params=None: (
            seen_repo_roots.append(repo_root) or _FakeDoorRouteResult(door_route_signal_mod.IN_PROCESS)
        ),
    )

    setup_mod.install_warm_door(claude_klabauter_checkout, claude_klabauter_checkout, setup_mod.Args())

    assert seen_repo_roots == [claude_klabauter_checkout]


def test_install_warm_door_read_anchored_to_repo_root_not_engine_root(setup_mod, tmp_path, monkeypatch, capsys):
    """The door's telemetry row is envelope-anchored to the CALLER's git
    common dir (`op_latency._write_entry` resolves `repo_key` from the
    dispatch envelope's origin worktree), never to the engine root the
    dispatched op runs from. `read_door_route`'s `repo_root` kwarg must
    therefore be THIS function's `repo_root` parameter (the claude-klabauter
    checkout) — never `engine_root` (the resolved, published mirror) —
    or the read lands in the wrong sink and a genuine PASS reports as
    `discriminator_unavailable`."""
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    claude_klabauter_checkout = tmp_path / "claude-klabauter-checkout"
    claude_klabauter_checkout.mkdir()
    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=tmp_path / "settings-home",
        door_route=_FakeDoorRouteResult(door_route_signal_mod.WARM_SERVER),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    seen_repo_roots = []
    monkeypatch.setattr(
        door_route_signal_mod, "read_door_route",
        lambda door_path, op, *, repo_root: (
            seen_repo_roots.append(repo_root) or _FakeDoorRouteResult(door_route_signal_mod.WARM_SERVER)
        ),
    )

    setup_mod.install_warm_door(claude_klabauter_checkout, claude_klabauter_checkout, setup_mod.Args())

    assert seen_repo_roots == [claude_klabauter_checkout]
    assert seen_repo_roots != [published]
    out = capsys.readouterr().out
    assert "PASS [door]" in out


def test_install_warm_door_posix_branch_claims_the_bare_name(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """The POSIX branch must strip the shadowing `.ps1` sibling.

    Regression guard, and it is the REAL path this time. `install_door()`
    removes the sibling for itself, but `install_warm_door` only calls
    `install_door()` under `sys.platform == "win32"` -- POSIX goes through
    `door_install_posix_build.build_or_advise`. A removal reachable only
    from `install_door()` is therefore dead code on every Mac and Linux
    box, which is exactly what shipped: a full `scripts/setup.py
    --i-am-agent` run landed the door and left `coordinator-invoke.ps1` in
    place, while the unit tests calling `install_door()` directly all
    passed. Assert against `install_warm_door` on a darwin platform, not
    against `install_door`.
    """
    import coordinator_core.install.door_route_signal as door_route_signal_mod

    published = tmp_path / "published"
    published.mkdir()
    settings_home_dir = tmp_path / "settings-home"
    bin_dst = settings_home_dir / "bin"
    bin_dst.mkdir(parents=True)
    shadowing = bin_dst / "coordinator-invoke.ps1"
    shadowing.write_text("# forwarder that would outrank the door in PowerShell\n")

    resolved = _FakeInstallEngineRoot("published", root=published)
    _patch_door_seams(
        monkeypatch, resolved=resolved, settings_home_dir=settings_home_dir,
        door_route=_FakeDoorRouteResult(door_route_signal_mod.WARM_SERVER),
    )
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")

    setup_mod.install_warm_door(tmp_path, tmp_path, setup_mod.Args())

    assert not shadowing.exists(), (
        "the POSIX branch left coordinator-invoke.ps1 in place -- on Windows "
        "PowerShell would resolve the bare name to it and never reach the door"
    )
    assert "PASS [door]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# install_lfs_pre_push_gate — hooks-dir resolution
# ---------------------------------------------------------------------------


def test_install_lfs_pre_push_gate_honours_a_non_default_hooks_path(
    setup_mod, tmp_path, monkeypatch
):
    """Review: code-reviewer P2 — a repo with `core.hooksPath` set must have
    the gate written where git actually reads it, not hardcoded
    `.git/hooks`. Simulates `git rev-parse --git-path hooks` resolving to a
    non-default directory and asserts the gate lands there, not at the
    default `.git/hooks/pre-push`."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    custom_hooks = tmp_path / "custom-hooks-dir"
    custom_hooks.mkdir()

    real_run = setup_mod.subprocess.run

    def _fake_run(argv, **kwargs):
        if argv[:3] == ["git", "rev-parse", "--git-path"]:
            return setup_mod.subprocess.CompletedProcess(
                argv, 0, stdout=str(custom_hooks) + "\n", stderr=""
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    args = setup_mod.Args()
    setup_mod.install_lfs_pre_push_gate(repo_root, args)

    installed = custom_hooks / "pre-push"
    default_installed = repo_root / ".git" / "hooks" / "pre-push"
    assert installed.is_file(), "the gate must land at the git-resolved hooks directory"
    assert not default_installed.exists(), (
        "the gate must NOT fall back to .git/hooks when core.hooksPath resolves elsewhere"
    )


def test_install_lfs_pre_push_gate_falls_back_on_git_resolution_failure(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """A failed `git rev-parse --git-path hooks` must fall back to
    `repo_root/.git/hooks` and say so on stderr, never silently no-op."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    real_run = setup_mod.subprocess.run

    def _fake_run(argv, **kwargs):
        if argv[:3] == ["git", "rev-parse", "--git-path"]:
            return setup_mod.subprocess.CompletedProcess(argv, 1, stdout="", stderr="not a git repo")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)

    args = setup_mod.Args()
    setup_mod.install_lfs_pre_push_gate(repo_root, args)

    installed = repo_root / ".git" / "hooks" / "pre-push"
    assert installed.is_file()
    assert "falling back" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# check_governed_authoring_surfaces_manifest — HARD dep (PM ruling 2026-08-29)
# ---------------------------------------------------------------------------
#
# `guard-doctrine-surface-bash-write` reads `<plugin_root>/governed-
# authoring-surfaces.json` fresh on every Bash call; a miss (absent,
# unreadable, bad JSON, wrong shape) degrades that guard to a silent DECLINE.
# Install time is the only place a broken manifest is catchable, so this
# check is the whole safety net for that failure mode. Each helper below
# stubs `_resolve_coordinator_claude_root` (never exercised by this check
# beyond producing SOME coord_path) and `_resolve_plugin_root_for_machine_
# local` directly, mirroring the decorated-resolver monkeypatch idiom used
# by test_check_coordinator_claude_dep_routes_on_rung_not_decorated_display
# above, rather than re-deriving a real coordinator-claude checkout on disk
# for every case.


def _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root):
    monkeypatch.setattr(
        setup_mod,
        "_resolve_coordinator_claude_root",
        lambda repo_root, args: (Path("/fake/coordinator-claude"), None),
    )
    monkeypatch.setattr(
        setup_mod, "_resolve_plugin_root_for_machine_local", lambda coord_path: plugin_root
    )


def test_check_governed_authoring_surfaces_manifest_well_formed_passes_and_names_count(
    setup_mod, monkeypatch, tmp_path, capsys
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "governed-authoring-surfaces.json").write_text(
        json.dumps(["surface/one.md", "surface/two.md", "surface/three.md"])
    )
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)  # must not raise
    out = capsys.readouterr().out
    assert "PASS [hard] governed-authoring-surfaces manifest — 3 surface(s)" in out


def test_check_governed_authoring_surfaces_manifest_empty_list_is_a_real_answer_and_passes(
    setup_mod, monkeypatch, tmp_path, capsys
):
    """An empty list means the install governs no surfaces -- a real answer,
    not a miss. The manifest-shape check (`isinstance(data, list) and all(...)`)
    is vacuously true for `[]`, so this must PASS. If it did not, that would
    be a finding against the implementation, not a case to encode as a
    failure here."""
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "governed-authoring-surfaces.json").write_text("[]")
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)  # must not raise
    out = capsys.readouterr().out
    assert "PASS [hard] governed-authoring-surfaces manifest — 0 surface(s)" in out


def test_check_governed_authoring_surfaces_manifest_absent_exits_hard(
    setup_mod, monkeypatch, tmp_path, capsys
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "absent at" in stderr
    assert "unreadable" not in stderr
    assert "not a" not in stderr


def test_check_governed_authoring_surfaces_manifest_unreadable_bad_json_exits_hard(
    setup_mod, monkeypatch, tmp_path, capsys
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "governed-authoring-surfaces.json").write_text("{not valid json")
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "unreadable at" in stderr
    assert "absent at" not in stderr
    assert "valid JSON but not a" not in stderr


def test_check_governed_authoring_surfaces_manifest_valid_json_not_a_list_exits_hard(
    setup_mod, monkeypatch, tmp_path, capsys
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "governed-authoring-surfaces.json").write_text(json.dumps({"a": "dict, not a list"}))
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "valid JSON but not a" in stderr
    assert "absent at" not in stderr
    assert "unreadable at" not in stderr


def test_check_governed_authoring_surfaces_manifest_list_with_non_string_entry_exits_hard(
    setup_mod, monkeypatch, tmp_path, capsys
):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "governed-authoring-surfaces.json").write_text(json.dumps(["surface/one.md", 42]))
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, plugin_root)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "valid JSON but not a" in stderr
    assert "absent at" not in stderr
    assert "unreadable at" not in stderr


def test_check_governed_authoring_surfaces_manifest_unresolvable_plugin_root_exits_hard(
    setup_mod, monkeypatch, capsys
):
    _stub_coord_and_plugin_root(setup_mod, monkeypatch, None)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_governed_authoring_surfaces_manifest(Path("/repo/claude-klabauter"), args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    stderr = capsys.readouterr().err
    assert "could not resolve a plugin root" in stderr
    assert "absent at" not in stderr
    assert "unreadable at" not in stderr
    assert "valid JSON but not a" not in stderr
