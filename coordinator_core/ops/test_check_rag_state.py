"""
coordinator_core.ops.test_check_rag_state — parity tests for the naked-Python
port of the DoE-owned bash script.

Port of: check-rag-state.sh (DoE b5a4192c, 2026-07-20)

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


@pytest.fixture(autouse=True)
def _drop_settings_home_override(monkeypatch):
    """Neutralise ``COORDINATOR_SETTINGS_HOME`` for every test in this module.

    The module docstring's "no test touches the real $HOME" claim rests on the
    CLAUDE_HOME monkeypatch below, which only holds while the `.doe-root`
    pointer resolves off CLAUDE_HOME. ``doe_root_pointer`` tries the DURABLE
    rung first — ``_settings_home.settings_home()/machine-local/.doe-root`` —
    and that resolver prefers ``COORDINATOR_SETTINGS_HOME`` over CLAUDE_HOME.
    The suite-root home quarantine (``coordinator_core/conftest.py::
    _quarantine_real_home``) does not clear that override, so on a box where an
    operator exports it every case here reads the operator's real `.doe-root`
    and never exercises the seeded one.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


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


@pytest.fixture
def flat_mirror_home(tmp_path, monkeypatch):
    """A CLAUDE_HOME whose .doe-root points at a FLAT published mirror.

    The mirror's repo root IS the coordinator content root, gated by its own
    `.claude-plugin/plugin.json` — the layout a cloud container registers.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    doe_root = tmp_path / "flat-mirror"
    (doe_root / ".claude-plugin").mkdir(parents=True)
    (doe_root / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (home / ".claude" / ".doe-root").write_text(str(doe_root) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("RAG_STATE", raising=False)
    monkeypatch.delenv("CLAUDE_RAG_STATE_FILE", raising=False)
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    return home, doe_root


def test_flat_mirror_satisfies_doe_root_precondition(flat_mirror_home, monkeypatch):
    monkeypatch.setenv("RAG_STATE", "fresh")
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("fresh", 0)


def test_flat_mirror_plugin_root_is_the_mirror_root(flat_mirror_home, monkeypatch):
    _home, doe_root = flat_mirror_home
    marker = doe_root / "tasks" / ".rag-state"
    marker.parent.mkdir(parents=True)
    marker.write_text("stale\n", encoding="utf-8")
    text, rc = subject.check_rag_state()
    assert (text, rc) == ("stale", 0)


def test_flat_mirror_main_prints_token(flat_mirror_home, monkeypatch, capsys):
    monkeypatch.setenv("RAG_STATE", "absent")
    rc = subject.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "absent"


def test_bare_directory_is_not_a_content_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    bare = tmp_path / "bare"
    bare.mkdir()
    (home / ".claude" / ".doe-root").write_text(str(bare) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("RAG_STATE", "fresh")
    assert subject.check_rag_state() == ("", 1)
    assert subject.main([]) == 1
