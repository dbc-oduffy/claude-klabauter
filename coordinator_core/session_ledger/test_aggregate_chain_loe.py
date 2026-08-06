"""
coordinator_core.session_ledger.test_aggregate_chain_loe — CLI-entry-point
tests for aggregate_chain_loe.main(), the in-process entry point consumed by
the example-doctrine-repo-side CLI trampoline (coordinator/bin/aggregate-chain-loe.py).

The chain-walk/aggregate/format logic itself (aggregate(), parse_session_ledgers(),
resolve_handoff_path(), format_yaml_frontmatter/format_json) is covered
byte-for-byte against the retired bash oracle by the example-doctrine-repo-side test suite
(14 cases, run via the trampoline in-process). This file covers only
main()'s own CLI-parsing / exit-code / help-text surface, added for the
DOE-PORT trampoline.

Spec backlink: docs/plans/2026-06-29-handoff-lineage-dag-fan-in-fan-out.md § C2
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.session_ledger.aggregate_chain_loe import main, resolve_state_root


def _write_handoff(path: Path, created: str = "2026-05-05", predecessor: str = "null") -> None:
    path.write_text(
        f"""---
created: {created}
predecessor: {predecessor}
---

# Handoff

## Session Ledger

| Field | Value |
|-------|-------|
| session_id | sid-{path.stem} |
| agent_dispatches | 3 |
| opus_dispatches | 1 |
| em_tokens | 1000 |
""",
        encoding="utf-8",
    )


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    (tmp_path / "coordinator" / "lib").mkdir(parents=True)
    return tmp_path


def test_help_exits_zero_and_prints_usage(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage: aggregate-chain-loe.sh --terminal-handoff <path>" in out
    assert "Exit codes:" in out


def test_missing_terminal_handoff_exits_one(capsys):
    rc = main([])
    assert rc == 1
    assert "Error: --terminal-handoff is required" in capsys.readouterr().err


def test_unknown_argument_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    assert "Error: unknown argument: --bogus" in capsys.readouterr().err


def test_not_inside_git_repo_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/x.md"])
    assert rc == 1
    assert "not inside a git repo" in capsys.readouterr().err


def test_terminal_handoff_not_found_exits_one(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/missing.md"])
    assert rc == 1
    assert "terminal handoff not found" in capsys.readouterr().err


def test_single_session_chain_yaml_output(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chain_loe:" in out
    assert "sessions: 1" in out
    assert "agent_dispatches: 3" in out


def test_single_session_chain_json_output(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md", "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"chain_loe"' in out
    assert '"sessions": 1' in out


def test_unknown_format_exits_one_after_walk(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md", "--format", "xml"])
    assert rc == 1
    assert "unknown format 'xml'" in capsys.readouterr().err


def test_main_defaults_to_sys_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["aggregate-chain-loe", "--help"])
    rc = main()
    assert rc == 0
    assert "Usage:" in capsys.readouterr().out


def test_resolve_state_root_is_scoped_to_passed_cwd_not_ambient_cwd(tmp_path, monkeypatch):
    """Review: code-reviewer (F1) — resolve_state_root(coordinator_root, cwd)
    must resolve against *cwd*, not the process's ambient os.getcwd(). Two
    distinct (non-meta) repos: chdir the process into repo_a, then resolve
    against repo_b explicitly — the result must be scoped to repo_b."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_a, check=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_b, check=True)

    monkeypatch.chdir(repo_a)
    result = resolve_state_root(Path("unused"), repo_b)

    assert result == repo_b.resolve() / "state"
    assert result != repo_a.resolve() / "state"
