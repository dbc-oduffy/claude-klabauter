from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.install import coordinator_install_entry as cie
from coordinator_core.install.coordinator_install_entry import (
    InstallEntryError,
    _declared_installer,
    main,
)


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    manifest_dir = tmp_path / "docs" / "install"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / "agent-install-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _base_manifest(**overrides) -> dict:
    manifest = {
        "agent_install_contract_version": 3,
        "repo_id": "claude-klabauter",
        "direct_deps": [],
    }
    manifest.update(overrides)
    return manifest


_PLATFORM_KEY = "windows" if os.name == "nt" else "posix"


def _stub_claude_klabauter_root(monkeypatch, repo_root: Path) -> None:
    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root_with_class",
        lambda: (str(repo_root), "stub"),
    )


def test_wrapper_help_still_prints_usage_and_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: coordinator-install" in out


def test_wrapper_short_help_still_prints_usage_and_exits_zero(capsys):
    rc = main(["-h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: coordinator-install" in out


def test_help_after_double_dash_is_forwarded_not_intercepted(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: "scripts/setup.py"}),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)

    captured_cmd = {}

    def _fake_call(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return 0

    monkeypatch.setattr(cie.subprocess, "call", _fake_call)

    rc = main(["--", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: coordinator-install" not in out
    assert captured_cmd["cmd"][-1] == "--help"


def test_declared_installer_raises_when_standalone_setup_script_absent(tmp_path):
    repo_root = tmp_path / "repo"
    _write_manifest(repo_root, _base_manifest())

    with pytest.raises(InstallEntryError, match="declares no standalone_setup_script"):
        _declared_installer(repo_root)


def test_declared_installer_raises_when_platform_leg_is_non_string(tmp_path):
    repo_root = tmp_path / "repo"
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: {"nested": "object"}}),
    )

    with pytest.raises(InstallEntryError, match="not a path string"):
        _declared_installer(repo_root)


def test_declared_installer_raises_when_declared_path_does_not_resolve(tmp_path):
    repo_root = tmp_path / "repo"
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: "scripts/gone.py"}),
    )

    with pytest.raises(InstallEntryError, match="does not resolve"):
        _declared_installer(repo_root)


def test_declared_installer_resolves_a_present_script(tmp_path):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(
            standalone_setup_script={
                _PLATFORM_KEY: "scripts/setup.py",
                "entry_point_contract": {"check_only_flag": "--check"},
            }
        ),
    )

    resolved, contract = _declared_installer(repo_root)

    assert resolved == script.resolve()
    assert contract == {"check_only_flag": "--check"}


# ---------------------------------------------------------------------------
# --check-only uses the DECLARED flag, never an assumed spelling (Finding 2c)
# ---------------------------------------------------------------------------


def test_check_only_uses_declared_flag_spelling(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(
            standalone_setup_script={
                _PLATFORM_KEY: "scripts/setup.py",
                "entry_point_contract": {"check_only_flag": "--probe-only"},
            }
        ),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)

    captured_cmd = {}

    def _fake_call(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return 0

    monkeypatch.setattr(cie.subprocess, "call", _fake_call)

    rc = main(["--check-only"])

    assert rc == 0
    assert captured_cmd["cmd"][-1] == "--probe-only"
    assert "--check-only" not in captured_cmd["cmd"]


def test_check_only_refuses_to_guess_when_no_flag_declared(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: "scripts/setup.py"}),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)

    def _boom(cmd, **kwargs):
        raise AssertionError("subprocess.call must not be reached when the flag is undeclared")

    monkeypatch.setattr(cie.subprocess, "call", _boom)

    rc = main(["--check-only"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "declares no check_only_flag" in err
    assert "Refusing to guess" in err


def _host_fixture(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: "scripts/setup.py"}),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)
    captured = {}

    def _fake_call(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(cie.subprocess, "call", _fake_call)
    return captured


@pytest.mark.parametrize("argv", [["--host", "codex,agy"], ["--host=codex,agy"]])
def test_host_is_forwarded_to_the_installer_without_double_dash(tmp_path, monkeypatch, argv):
    captured = _host_fixture(tmp_path, monkeypatch)

    assert main(argv) == 0

    cmd = captured["cmd"]
    i = cmd.index("--host")
    assert cmd[i + 1] == "codex,agy"
    assert cmd.count("--host") == 1


def test_host_runs_as_a_second_call_after_the_full_install(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    cmds = []

    def _call(cmd, **kwargs):
        cmds.append(cmd)
        return 0

    monkeypatch.setattr(cie.subprocess, "call", _call)

    assert main(["--host", "codex,agy"]) == 0

    assert len(cmds) == 2
    assert "--host" not in cmds[0]
    assert cmds[1][cmds[1].index("--host") + 1] == "codex,agy"


def test_host_step_is_skipped_when_the_full_install_fails(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    cmds = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: cmds.append(cmd) or 90)

    assert main(["--host", "codex"]) == 90
    assert len(cmds) == 1 and "--host" not in cmds[0]


def test_omitting_host_forwards_no_host_flag(tmp_path, monkeypatch):
    captured = _host_fixture(tmp_path, monkeypatch)

    assert main([]) == 0

    assert "--host" not in captured["cmd"]


def test_host_composes_with_check_only_and_double_dash(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(
            standalone_setup_script={
                _PLATFORM_KEY: "scripts/setup.py",
                "entry_point_contract": {"check_only_flag": "--check"},
            }
        ),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)
    captured = {}
    monkeypatch.setattr(
        cie.subprocess, "call", lambda cmd, **kw: captured.setdefault("cmd", cmd) and 0
    )

    main(["--check-only", "--host", "agy", "--", "--verbose"])

    cmd = captured["cmd"]
    assert "--check" in cmd
    assert cmd[cmd.index("--host") + 1] == "agy"
    assert cmd[-1] == "--verbose"


def test_host_without_value_is_refused_before_dispatch(tmp_path, monkeypatch, capsys):
    _host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cie.subprocess, "call", lambda cmd, **kw: pytest.fail("must not dispatch")
    )

    assert main(["--host"]) == 2
    assert "--host requires a value" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--bogus"],
        ["--host", "codex", "--bogus"],
        ["stray-positional"],
    ],
)
def test_unsupported_options_exit_2_before_any_subprocess_or_write(
    tmp_path, monkeypatch, capsys, argv
):
    _host_fixture(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: spawned.append(cmd) or 0)
    monkeypatch.setattr(
        cie.subprocess, "run", lambda cmd, **kw: pytest.fail("must not spawn")
    )
    monkeypatch.setattr(
        cie.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn")
    )
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    mtimes = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    assert main(argv) == 2

    err = capsys.readouterr().err
    assert "unsupported option" in err
    offending = next(a for a in argv if a in ("--bogus", "stray-positional"))
    assert offending in err
    assert spawned == []
    assert before == sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert mtimes == {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}


def test_supported_options_are_still_accepted(tmp_path, monkeypatch):
    captured = _host_fixture(tmp_path, monkeypatch)

    assert main(["--host", "codex"]) == 0
    assert "cmd" in captured


def test_usage_lists_host():
    assert "--host" in cie._USAGE


@pytest.mark.parametrize("argv", [["--host", "codex"], ["--host=codex"], []])
@pytest.mark.parametrize("flag", [None, "--i-am-agent"])
def test_validated_runtime_consent_runs_strict_leg_once(tmp_path, monkeypatch, argv, flag):
    _host_fixture(tmp_path, monkeypatch)
    _write_manifest(tmp_path / "repo", _base_manifest(standalone_setup_script={
        _PLATFORM_KEY: "scripts/setup.py", "entry_point_contract": {"non_interactive_flag": flag}}))
    monkeypatch.setattr(cie, "_runtime_selection", lambda *a: True)
    monkeypatch.setattr(cie, "_enrollment_roots", lambda *a: (tmp_path, tmp_path / "plugin"))
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    assert main([*argv, "--", "--verbose"]) == 0
    assert len(calls) == 2
    assert "--verbose" in calls[0][0]
    assert calls[1][0][-3:] == ["--host", "codex", "--enroll-runtime"]
    assert calls[1][0] == [sys.executable, str(tmp_path / "repo" / "scripts" / "setup.py"),
                            *([flag] if flag else []), "--host", "codex", "--enroll-runtime"]
    assert "--verbose" not in calls[1][0]
    assert calls[1][1]["stdin"] == cie.subprocess.DEVNULL
    assert calls[1][1]["env"]["CODEX_HOME"] == str(tmp_path / "codex")
    assert calls[1][1]["env"]["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "settings")
    assert calls[1][1]["env"]["CLAUDE_PLUGIN_ROOT"] == str(tmp_path / "plugin")


@pytest.mark.parametrize("value", [None, False, "true", 1])
def test_runtime_selection_requires_literal_true_consent(monkeypatch, value):
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="consent.codex_runtime"):
        cie._runtime_selection(Path(__file__).resolve().parents[2], json.dumps({
            "codex_runtime": "enrol", "consent": {"codex_runtime": value}}))


@pytest.mark.parametrize("choice", ["enrol"])
@pytest.mark.parametrize("consent", [None, {}, {"codex_runtime": False}, {"codex_runtime": None},
                                     {"codex_runtime": 0}, {"codex_runtime": 1},
                                     {"codex_runtime": "true"}, "true", []])
def test_wrapper_argv_rejects_consent_before_base(tmp_path, monkeypatch, choice, consent):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("must not write"))
    answers = {"codex_runtime": choice}
    if consent is not None:
        answers["consent"] = consent
    denied = consent is None or consent == {} or isinstance(consent, dict) and consent.get("codex_runtime") is False
    assert main(["--answers", json.dumps(answers)]) == (1 if denied else 2)


@pytest.mark.parametrize("choice", ["enrol"])
@pytest.mark.parametrize("base_rc,enroll_rc", [(0, 0), (90, 0), (0, 7)])
def test_wrapper_real_answers_strict_argv(tmp_path, monkeypatch, choice, base_rc, enroll_rc):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    monkeypatch.setattr(cie, "_enrollment_roots", lambda *a: (tmp_path / "source", tmp_path / "plugin"))
    calls = []
    def call(cmd, **kw):
        calls.append((cmd, kw))
        return base_rc if len(calls) == 1 else enroll_rc
    monkeypatch.setattr(cie.subprocess, "call", call)
    assert main(["--answers", json.dumps({"codex_runtime": choice, "consent": {"codex_runtime": True}})]) == (base_rc or enroll_rc)
    assert calls[0][0][-2:] == ["--coordinator-root", str(tmp_path / "source")]
    assert len(calls) == (1 if base_rc else 2)
    if not base_rc:
        assert calls[1][0][-3:] == ["--host", "codex", "--enroll-runtime"]
        assert calls[1][1]["env"]["CLAUDE_PLUGIN_ROOT"] == str(tmp_path / "plugin")


@pytest.mark.parametrize("consent", [{}, {"codex_runtime": False}])
def test_printed_runtime_consent_matches_apply(tmp_path, monkeypatch, capsys, consent):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)

    assert main(["--host", "codex", "--plan"]) == 0
    guidance = json.loads(capsys.readouterr().out)["answers_shape"]["consent"]["codex_runtime"]
    assert "literal true" in guidance["enrol"] and "before setup writes" in guidance["enrol"]
    assert "Omits Codex runtime enrollment" in guidance["skip"]
    answers = {"codex_runtime": "enrol"}
    if consent:
        answers["consent"] = consent
    assert main(["--host", "codex", "--answers", json.dumps(answers)]) == 1
    assert "codex_runtime FAILED: consent.codex_runtime" in capsys.readouterr().err
    assert not calls
    answers["codex_runtime"] = "skip"
    assert main(["--host", "codex", "--answers", json.dumps(answers)]) == 0
    assert len(calls) == 1 and "--host" not in calls[0] and "--enroll-runtime" not in calls[0]


def test_enrollment_flag_cannot_bypass_wrapper_consent(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("must not spawn"))
    assert main(["--", "--host", "codex", "--enroll-runtime"]) == 2


def test_host_failure_is_forwarded_without_retry(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cie, "_runtime_selection", lambda *a: True)
    monkeypatch.setattr(cie, "_enrollment_roots", lambda *a: (tmp_path, tmp_path / "plugin"))
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: (calls.append(cmd), 7 if len(calls) == 2 else 0)[1])
    assert main([]) == 7
    assert len(calls) == 2


def test_check_only_probes_once_without_collecting_consent(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    root = tmp_path / "repo"
    _write_manifest(root, _base_manifest(standalone_setup_script={
        _PLATFORM_KEY: "scripts/setup.py", "entry_point_contract": {"check_only_flag": "--check"}}))
    monkeypatch.setattr(cie, "_runtime_selection", lambda *a: pytest.fail("must not prompt"))
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
    assert main(["--check-only", "--host=codex"]) == 0
    assert len(calls) == 1 and calls[0][-3:] == ["--check", "--host", "codex"]
    assert "--enroll-runtime" not in calls[0]


def test_plan_never_dispatches_setup_or_collects_consent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    _host_fixture(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(cie, "_planner", lambda *a: SimpleNamespace(main=lambda argv: seen.append(argv) or 0))
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("must not spawn"))
    assert main(["--host", "codex", "--plan"]) == 0
    assert seen == [["--plan"]]


def _byte_snapshot(root: Path) -> "dict[str, tuple]":
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in root.rglob("*")}


def test_public_plan_with_the_real_planner_changes_no_byte_under_the_home(tmp_path, monkeypatch, capsys):
    """lo-89: `coordinator-install --host codex --plan` once ran base setup (registry writes, forwarders,
    receipt rewrite) and exited 0. The real planner runs here; only a setup spawn is forbidden."""
    _host_fixture(tmp_path, monkeypatch)
    home = tmp_path / "home"
    settings = home / "settings-home"
    seeded = {
        settings / "machine-local" / "registry.toml": b"coordinator.guard_level = 'strict'\n",
        settings / "machine-local" / "registry.local.toml": b"repos.claude_klabauter = 'x'\n",
        settings / "bin" / "coordinator-invoke.py": b"# forwarder\n",
        settings / "bin" / "machine-local.cmd": b"@rem forwarder\r\n",
        home / ".claude" / "settings.json": b'{"env": {}}',
        home / ".claude" / "state" / "coordinator.yaml": b"setup_concluded_at: 2026-01-01T00:00:00Z\nsetup_receipt: {}\n",
        home / ".codex" / "config.toml": b"model = 'x'\n",
    }
    for path, data in seeded.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for var in ("HOME", "USERPROFILE", "CLAUDE_HOME"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("COORDINATOR_PERMISSION_MODE", raising=False)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("--plan reached setup"))
    before = _byte_snapshot(tmp_path)

    assert main(["--host", "codex", "--plan"]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert "codex_runtime" in {d["id"] for d in plan["decisions"]} and "engine_channel" in plan
    assert _byte_snapshot(tmp_path) == before


@pytest.mark.parametrize("head,expected", [("ref: refs/heads/channel", "channel"), ("b" * 40, "detached@bbbbbbb")])
def test_default_wrapper_plan_reports_enrollment_and_channel_without_writes(tmp_path, monkeypatch, capsys, head, expected):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda *a: planner)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(planner, "_git_user", lambda: "Ada")
    registered = tmp_path / "registered"
    selected = tmp_path / "selected"
    for root, value in [(registered, head), (selected, "a" * 40)]:
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(value + "\n")
    monkeypatch.setattr(planner, "_registry_flat", lambda: {"repos.claude_klabauter": str(registered), "engine.target": "main"})
    monkeypatch.setattr(planner, "_engine_root", lambda: (True, str(selected)))
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("must not install"))
    monkeypatch.setattr(cie.subprocess, "run", lambda *a, **kw: pytest.fail("must not spawn"))
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    assert main(["--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    decision = next(d for d in plan["decisions"] if d["id"] == "codex_runtime")
    assert decision["options"] == ["enrol", "skip"]
    assert decision["default"] == "skip"
    assert set(plan["engine_channel"]) == {"checkout", "checkout_branch", "engine_target", "governs", "mismatch", "note"}
    assert plan["engine_channel"]["checkout"] == str(registered)
    assert plan["engine_channel"]["checkout_branch"] == expected
    assert plan["engine_channel"]["engine_target"] == "main"
    assert plan["engine_channel"]["governs"] == "engine.target"
    assert plan["engine_channel"]["mismatch"] is True
    assert plan["environment"]["selected_engine_root"] == str(selected)
    assert plan["environment"]["checkout_branch"] == "detached:" + "a" * 40
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before


@pytest.mark.parametrize("prefix", [[], ["--host", "codex"], ["--plan"], ["--check-only"]])
def test_unknown_switch_refused_before_engine_discovery(prefix, monkeypatch, capsys):
    from coordinator_core import engine_root

    monkeypatch.setattr(engine_root, "coordinator_engine_root_with_class",
                        lambda: pytest.fail("unexpected engine discovery"))
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup"))
    assert main([*prefix, "--invalid-option"]) == 2
    assert "--invalid-option" in capsys.readouterr().err


def test_host_missing_answers_refused_before_setup(tmp_path, monkeypatch):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup"))
    assert main(["--host", "codex", "--answers", str(tmp_path / "absent.json")]) == 2


def test_interactive_wrapper_without_answers_never_prompts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    _host_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cie, "_planner", lambda root: SimpleNamespace(
        _load_answers=lambda spec: {},
        _stdin_can_answer=lambda: True,
        ask_codex_runtime=lambda *a: pytest.fail("wrapper must not prompt"),
        codex_runtime_requested=lambda raw: False,
        plugin_root=lambda: tmp_path / "plugin"))
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)

    assert main([]) == 0
    assert len(calls) == 1
    assert "--host" not in calls[0]


@pytest.mark.parametrize("argv,remaining", [
    (["--host", "codex"], None),
    (["--host=codex"], None),
    (["--host", "codex,agy"], "agy"),
    (["--host=agy"], "agy"),
])
def test_explicit_skip_suppresses_codex_host_writes(tmp_path, monkeypatch, argv, remaining):
    _host_fixture(tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[2]
    planner = cie._planner(root)
    monkeypatch.setattr(cie, "_planner", lambda selected: planner)
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)

    assert main([*argv, "--answers", '{"codex_runtime":"skip"}']) == 0
    assert len(calls) == (2 if remaining else 1)
    assert "--host" not in calls[0]
    assert all("--enroll-runtime" not in cmd for cmd in calls)
    if remaining:
        assert calls[1][-2:] == ["--host", remaining]


@pytest.mark.parametrize("forwarded", [
    ["--host", "codex"],
    ["--host", "agy, codex"],
    ["--host", "grok", "--host", "codex"],
])
def test_skip_rejects_conflicting_forwarded_host_before_setup(tmp_path, monkeypatch, forwarded):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda selected: planner)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup"))
    assert main(["--answers", '{"codex_runtime":"skip"}', "--", *forwarded]) == 2


@pytest.mark.parametrize("answers,host", [
    ([], "codex"),
    (["--answers", '{"codex_runtime":"skip"}'], "agy,grok"),
])
def test_nonconflicting_forwarded_hosts_reach_setup(tmp_path, monkeypatch, answers, host):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda selected: planner)
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
    assert main([*answers, "--", "--host", host]) == 0
    assert len(calls) == 1
    assert calls[0][-2:] == ["--host", host]


@pytest.mark.parametrize("exit_code", [0, 1, 90, cie.EXIT_INTERPRETER_UNSUPPORTED])
def test_dispatched_exit_code_forwarded_verbatim(tmp_path, monkeypatch, exit_code):
    repo_root = tmp_path / "repo"
    script = repo_root / "scripts" / "setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_manifest(
        repo_root,
        _base_manifest(standalone_setup_script={_PLATFORM_KEY: "scripts/setup.py"}),
    )
    _stub_claude_klabauter_root(monkeypatch, repo_root)
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kwargs: exit_code)

    rc = main([])

    assert rc == exit_code, "exit 96 (designed refusal) must never be remapped onto 1 or any other code"


@pytest.mark.parametrize("argv", [
    ["--host", "codex", "--answers="],
    ["--answers", ""],
    ["--answers=   ", "--answers={}"],
    ["--host", ","],
    ["--host="],
    ["--host", ""],
    ["--host= , ", "--host=codex"],
])
def test_empty_option_value_stops_before_discovery(argv, monkeypatch):
    monkeypatch.setattr("coordinator_core.engine_root.coordinator_engine_root_with_class",
                        lambda: pytest.fail("unexpected engine discovery"))
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup"))
    assert main(argv) == 2


@pytest.mark.parametrize("hosts", ["codex", " codex ", "agy, codex"])
@pytest.mark.parametrize("layout", ["nested", "flat"])
@pytest.mark.parametrize("choice", ["enrol"])
def test_consented_wrapper_binds_strict_leg_to_base_root(tmp_path, monkeypatch, layout, hosts, choice):
    engine = Path(__file__).resolve().parents[2]
    _stub_claude_klabauter_root(monkeypatch, engine)
    source = tmp_path / "doctrine"
    plugin = source / "coordinator" if layout == "nested" else source
    marker = plugin / "templates/bin/_machine_local.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    foreign = tmp_path / "other-plugin"
    foreign.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(foreign))
    calls = []
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    answers = json.dumps({"codex_runtime": choice, "consent": {"codex_runtime": True}})
    assert main(["--host", hosts, "--answers", answers, "--", "--coordinator-root", str(source)]) == 0
    base, *legacy, strict = calls
    assert [cmd[cmd.index("--host") + 1] for cmd, _ in legacy] == (["agy"] if "agy" in hosts else [])
    assert base[0][-2:] == ["--coordinator-root", str(source)]
    assert strict[0][-3:] == ["--host", "codex", "--enroll-runtime"]
    assert strict[1]["env"]["CLAUDE_PLUGIN_ROOT"] == str(plugin.resolve())


@pytest.mark.parametrize("setup_mode", [
    ["--help"], ["--check"], ["--preflight"], ["--uninstall-pruner"], ["--deps-only"],
    ["--register-only"], ["--repair", "forwarders"], ["--host", "agy"],
])
@pytest.mark.parametrize("choice", ["enrol"])
def test_consented_enrollment_requires_full_base_before_setup(monkeypatch, capsys, setup_mode, choice):
    _stub_claude_klabauter_root(monkeypatch, Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup call"))
    answers = json.dumps({"codex_runtime": choice, "consent": {"codex_runtime": True}})
    assert main(["--host", "codex", "--answers", answers, "--", *setup_mode]) == 2
    assert "requires a full base install" in capsys.readouterr().err


def test_retired_configure_answer_refused_before_setup(tmp_path, monkeypatch, capsys):
    _host_fixture(tmp_path, monkeypatch)
    planner = cie._planner(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(cie, "_planner", lambda root: planner)
    monkeypatch.setattr(cie.subprocess, "call", lambda *a, **kw: pytest.fail("unexpected setup"))
    answers = {"codex_runtime": "configure", "consent": {"codex_runtime": True}}
    assert main(["--host", "codex", "--answers", json.dumps(answers)]) == 2
    error = capsys.readouterr().err
    assert "codex_runtime" in error and "configure" in error and "enrol" in error
