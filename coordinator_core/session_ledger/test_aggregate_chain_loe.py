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

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.session import harness_registry as hr
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


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md) —
# main()'s wiring of coordinator_core.pickup_assemble.compute_repo_identity_gate.
# AC6: a REAL anchor/root divergence, constructed via CLAUDE_CONFIG_DIR +
# CLAUDE_PID overrides and a real registry file on disk — never by
# monkeypatching compute_repo_identity_gate's own return value. Reuses the
# fixture-construction pattern from
# coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py (the one
# leg monkeypatched there, too, is `_resolve_claude_pid_from_env`'s
# psutil-name-match check — inherently OS-process-identity bound and
# unconstructible as a real fixture inside a test process not named
# "claude"; every other input is real files on disk).
# ---------------------------------------------------------------------------


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def test_main_refuses_on_real_repo_identity_mismatch(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    foreign_root = tmp_path / "foreign-repo"
    foreign_root.mkdir(parents=True)
    (foreign_root / ".git").mkdir()

    config_dir = tmp_path / "claude-config"
    sessions_dir = config_dir / "sessions"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-mismatch")
    # The registry's own real record names foreign_root as the session's
    # anchor cwd — a real divergence from repo_root, the ceremony's
    # `--terminal-handoff`-resolved root below.
    _write_registry_record(sessions_dir, "4242.json", "sess-mismatch", 4242, foreign_root)
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: ((4242, 0.0), "env-hit"),
    )
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "repo-identity" in err
    assert "MISMATCH" in err
    assert "sess-mismatch" in err


def test_main_does_not_refuse_on_repo_identity_match(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    config_dir = tmp_path / "claude-config"
    sessions_dir = config_dir / "sessions"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_PID", "5252")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-match")
    # The registry's real record anchors the session inside repo_root itself
    # — a genuine, on-disk MATCH.
    _write_registry_record(sessions_dir, "5252.json", "sess-match", 5252, repo_root)
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: ((5252, 0.0), "env-hit"),
    )
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gates:" in out
    assert 'repo_identity: "MATCH"' in out


def test_main_no_registry_record_is_unresolved_never_refuses(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-unresolved")
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: (None, "env-miss:absent"),
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'repo_identity: "UNRESOLVED"' in out
