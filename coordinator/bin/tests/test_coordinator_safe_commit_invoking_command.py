"""test_coordinator_safe_commit_invoking_command -- `--invoking-command` grants
`--blanket` exactly what `CLAUDE_INVOKING_COMMAND` grants, no more.

The flag exists because the warm door forwards no `CLAUDE_INVOKING_COMMAND`
(`coordinator_core/warm/env_forwarding.py::FORWARDING_SET`); argv always
crosses the door.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit_invoking", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit_invoking", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.delenv("CLAUDE_INVOKING_COMMAND", raising=False)
    return _load_cli_module()


def test_parse_args_reads_the_flag(mod):
    args = mod.parse_args(["--blanket", "--invoking-command", "workstream-start", "sweep commit"])
    assert args.mode == "blanket"
    assert args.invoking_command == "workstream-start"
    assert args.subject == "sweep commit"


def test_parse_args_rejects_a_missing_flag_value(mod):
    with pytest.raises(mod.UsageError):
        mod.parse_args(["--blanket", "sweep commit", "--invoking-command"])


def test_flag_allowed(mod):
    assert mod._blanket_invoking_command_allowed("workstream-start") is True


def test_flag_refused(mod):
    assert mod._blanket_invoking_command_allowed("not-a-ceremony") is False


def test_env_allowed_without_the_flag(mod, monkeypatch):
    monkeypatch.setenv("CLAUDE_INVOKING_COMMAND", "workstream-start")
    assert mod._blanket_invoking_command_allowed() is True


def test_a_refused_flag_does_not_mask_an_allowed_env(mod, monkeypatch):
    monkeypatch.setenv("CLAUDE_INVOKING_COMMAND", "update-docs")
    assert mod._blanket_invoking_command_allowed("not-a-ceremony") is True


@pytest.mark.parametrize(
    "value",
    sorted(
        _load_cli_module().BLANKET_ALLOWED_COMMANDS
        | {"/workstream-start.md", "bogus", "", "workstream-start-extra"}
    ),
)
def test_flag_and_env_grant_the_same_set(mod, monkeypatch, value):
    flag_verdict = mod._blanket_invoking_command_allowed(value)
    monkeypatch.setenv("CLAUDE_INVOKING_COMMAND", value)
    assert flag_verdict == mod._blanket_invoking_command_allowed()


def test_refusal_text_names_the_flag_form(mod, capsys):
    class _Args:
        invoking_command = ""

    with pytest.raises(SystemExit):
        mod.do_blanket("sid", _Args(), None, None, None)
    err = capsys.readouterr().err
    assert "--invoking-command workstream-start" in err
    assert "CLAUDE_INVOKING_COMMAND=" not in err
