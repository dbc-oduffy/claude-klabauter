"""
coordinator_core.ops.test_check_rag_state — parity tests for the naked-Python
port of the coordinator-claude-owned bash script.

Port of: check-rag-state.sh (coordinator-claude b5a4192c, 2026-07-20)

Golden oracle captured by running the bash script directly (positive: RAG_STATE
env fast-path for each valid token + each marker-file token; negative: garbage
env token, garbage marker content, absent marker with no env override, and a
missing/invalid `.doe-root`). Every case here reproduces the exact stdout
token + exit code observed from the bash oracle.

No test touches the real $HOME — CLAUDE_HOME is monkeypatched to a tmp_path
fixture for every case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import check_rag_state as subject  # noqa: E402


@pytest.fixture
def doe_home(tmp_path, monkeypatch):
    """A CLAUDE_HOME whose .doe-root points at a directory with a coordinator/ subdir."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    doe_root = tmp_path / "doe-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    (home / ".claude" / ".doe-root").write_text(str(doe_root) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("RAG_STATE", raising=False)
    monkeypatch.delenv("CLAUDE_RAG_STATE_FILE", raising=False)
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    return home, doe_root


@pytest.mark.parametrize("token", ["fresh", "stale", "absent"])
def test_env_fastpath_valid_tokens(doe_home, monkeypatch, token):
    monkeypatch.setenv("RAG_STATE", token)
    text, rc = subject.check_rag_state()
    assert (text, rc) == (token, 0)


def test_env_fastpath_unknown_token(doe_home, monkeypatch):
    monkeypatch.setenv("RAG_STATE", "unknown")
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("unknown", 1)


def test_env_fastpath_garbage_falls_through_to_unknown(doe_home, monkeypatch):
    monkeypatch.setenv("RAG_STATE", "garbage")
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("unknown", 1)


@pytest.mark.parametrize("token", ["fresh", "stale", "absent"])
def test_marker_file_valid_tokens(doe_home, tmp_path, monkeypatch, token):
    marker = tmp_path / "marker"
    marker.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_RAG_STATE_FILE", str(marker))
    text, rc = subject.check_rag_state()
    assert (text, rc) == (token, 0)


def test_marker_file_unknown_token(doe_home, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    marker.write_text("unknown\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_RAG_STATE_FILE", str(marker))
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("unknown", 1)


def test_marker_file_garbage_content_falls_through_to_unknown(doe_home, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    marker.write_text("bogus\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_RAG_STATE_FILE", str(marker))
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("unknown", 1)


def test_no_marker_no_env_falls_back_to_unknown(doe_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_STATE_FILE", str(tmp_path / "does-not-exist"))
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("unknown", 1)


def test_bad_doe_root_is_fail_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-such-home"))
    monkeypatch.delenv("RAG_STATE", raising=False)
    rc = subject.main([])
    assert rc == 1


def test_bad_doe_root_check_rag_state_returns_empty_and_1(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-such-home"))
    monkeypatch.delenv("RAG_STATE", raising=False)
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("", 1)


def test_main_prints_token_and_returns_rc(doe_home, monkeypatch, capsys):
    monkeypatch.setenv("RAG_STATE", "fresh")
    rc = subject.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "fresh"


def test_untrusted_plugin_root_is_fail_loud(doe_home, monkeypatch):
    home, _doe_root = doe_home
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/some/untrusted/path")
    rc = subject.main([])
    assert rc == 1


def test_trusted_root_opt_out_overrides_untrusted_path(doe_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/some/untrusted/path")
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")
    monkeypatch.setenv("RAG_STATE", "fresh")
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("fresh", 0)
