"""claude-md-grant-cli wrapper: local help/usage answers, argv forwarded to
claude_md_grant.main, and the engine-root transport failure exit."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claude_md_grant as cmg
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "claude_md_grant_cli", str(_BIN_DIR / "claude-md-grant-cli.py")
    )
    spec = importlib.util.spec_from_loader("claude_md_grant_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


@pytest.fixture()
def engine_in_process(monkeypatch):
    monkeypatch.setattr(_cli, "_import_module", lambda: cmg)


def _make_repo(tmp_path):
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_help_exits_zero_and_names_the_wrapper(flag, capsys):
    assert _cli.main([flag]) == 0
    out = capsys.readouterr().out
    assert cmg.GRANT_CLI_NAME in out
    assert "grant pm" in out and "read" in out and "check" in out
    assert "run from the repo whose CLAUDE.md the write targets" in out


def test_no_args_exits_two(capsys):
    assert _cli.main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_unknown_subcommand_exits_two(engine_in_process):
    assert _cli.main(["bogus"]) == 2


def test_grant_em_exits_two(engine_in_process, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "s-cli")
    assert _cli.main(["grant", "em", "note"]) == 2


def test_grant_pm_writes_the_record_verbatim(engine_in_process, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "s-cli")
    note = 'approve the "one-line" edit'
    assert _cli.main(["grant", "pm", note]) == 0
    grant = repo / ".git" / "coordinator-sessions" / "s-cli" / "claude-md-write-grant.json"
    record = json.loads(grant.read_text(encoding="utf-8"))
    assert record["granted_by"] == "pm"
    assert record["note"] == note


@pytest.mark.parametrize("launcher", ["claude-md-grant-cli.cmd", "claude-md-grant-cli.ps1"])
def test_launcher_targets_an_existing_entrypoint(launcher):
    text = (_BIN_DIR / launcher).read_text(encoding="utf-8")
    assert "claude-md-grant-cli.py" in text
    assert (_BIN_DIR / "claude-md-grant-cli.py").is_file()


def test_engine_root_failure_exits_transport_fail(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("no engine root")

    monkeypatch.setattr(_cli, "_import_module", _boom)
    assert _cli.main(["check"]) == 3
    assert "engine-root resolution failed" in capsys.readouterr().err
