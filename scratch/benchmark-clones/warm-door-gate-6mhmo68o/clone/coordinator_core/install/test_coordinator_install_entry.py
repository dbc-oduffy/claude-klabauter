"""Tests for coordinator_core.install.coordinator_install_entry — the
settings-home `coordinator-install` forwarder. See the module's own
docstring for what it dispatches and why.
"""
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


#: The manifest leg `coordinator_install_entry` will actually read on THIS host --
#: production picks it as `"windows" if os.name == "nt" else "posix"`. Every fixture
#: below declares this key rather than a hardcoded "posix": a posix-only fixture
#: makes each of these cases green on POSIX and red on Windows, where the entry
#: point correctly refuses a platform with no declared installer. The real
#: `docs/install/agent-install-manifest.json` declares both legs, so a fixture that
#: declares one was never modelling the shipped manifest either.
_PLATFORM_KEY = "windows" if os.name == "nt" else "posix"


def _stub_claude_klabauter_root(monkeypatch, repo_root: Path) -> None:
    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root_with_class",
        lambda: (str(repo_root), "stub"),
    )


# ---------------------------------------------------------------------------
# `--` / `--help` precedence (Finding 1 regression)
# ---------------------------------------------------------------------------


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
    """Regression for Finding 1: `coordinator-install -- --help` must reach
    the dispatched installer verbatim, not be swallowed by this wrapper's
    own usage text."""
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


# ---------------------------------------------------------------------------
# _declared_installer resolution failures (Finding 2b)
# ---------------------------------------------------------------------------


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
    """The module's stated reason: a wrong guess turns a probe into a
    mutation, so an undeclared check_only_flag must refuse, not assume a
    spelling like `--check-only`."""
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


# ---------------------------------------------------------------------------
# Exit-code forwarding, especially the reserved refusal code 96 (Finding 2d)
# ---------------------------------------------------------------------------


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
    # `**kwargs`, not a bare `cmd`: on Windows the entry point passes
    # `creationflags` (the no-console guard), so a POSIX-shaped one-arg stub
    # raises TypeError there and never exercises the forwarding this asserts.
    monkeypatch.setattr(cie.subprocess, "call", lambda cmd, **kwargs: exit_code)

    rc = main([])

    assert rc == exit_code, "exit 96 (designed refusal) must never be remapped onto 1 or any other code"
