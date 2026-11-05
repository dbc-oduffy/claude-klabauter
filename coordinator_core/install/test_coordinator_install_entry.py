from __future__ import annotations

import json
import os
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


def test_usage_lists_host():
    assert "--host" in cie._USAGE


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
