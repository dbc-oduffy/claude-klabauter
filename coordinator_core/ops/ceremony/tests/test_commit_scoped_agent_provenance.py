"""commit_scoped stamps engine-computed Agent-* trailers for non-Claude sessions, none for Claude.

Drives the default `commit_scoped(paths, msg_file, cwd)` call on real tmp_path repos, on both the
agree and the diverged (private-index) branches.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.agent_provenance import ADMISSION_MARKER, TRAILER_KEYS
from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import make_agree_path, make_diverged_path, real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION_ID = "abcdef12-3456-7890-abcd-ef1234567890"
_DETECTION_ENV = (
    "CODEX_THREAD_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "CLAUDECODE",
    "COORDINATOR_AGENT_VENDOR",
    "COORDINATOR_AGENT_MODEL",
    "COORDINATOR_AGENT_SESSION",
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _msg(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "msg.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _agent_lines(repo: Path) -> list[str]:
    body = _git(["log", "-1", "--format=%B"], repo)
    return [ln for ln in body.splitlines() if ln.split(":", 1)[0] in TRAILER_KEYS]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    from coordinator_core.git import agent_provenance
    monkeypatch.setattr(agent_provenance, "WRITER_READINESS_ACKS", {"project-rag": "test-ack", "example-cockpit-repo": "test-ack"})
    for name in _DETECTION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)


def _commit_agree(tmp_path: Path, msg_text: str):
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "a.txt", "a\n")
    return repo, git_native.commit_scoped(["a.txt"], _msg(tmp_path, msg_text), repo)


def test_codex_session_is_stamped_and_forged_claude_vendor_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-1")
    repo, result = _commit_agree(tmp_path, "subject\n\nAgent-Vendor: claude\nAgent-Tier: frontier\n")
    assert result.ok, result.stderr
    assert _agent_lines(repo) == [
        "Agent-Vendor: codex",
        "Agent-Model: unknown",
        "Agent-Tier: unknown",
        "Agent-Session-Id: thr-1",
    ]


def test_codex_session_is_stamped_when_the_message_omits_them(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-2")
    monkeypatch.setenv("COORDINATOR_AGENT_MODEL", "gpt-5.1")
    repo, result = _commit_agree(tmp_path, "subject\n")
    assert result.ok, result.stderr
    lines = _agent_lines(repo)
    assert "Agent-Vendor: codex" in lines
    assert "Agent-Model: gpt-5.1" in lines
    assert "Agent-Tier: frontier" in lines
    assert "Agent-Session-Id: thr-2" in lines


def test_diverged_branch_is_stamped_too(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-9")
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "d.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    result = git_native.commit_scoped(["d.txt"], _msg(tmp_path, "diverged\n"), repo)
    assert result.ok, result.stderr
    lines = _agent_lines(repo)
    assert lines[0] == "Agent-Vendor: agy"
    assert lines[-1] == "Agent-Session-Id: conv-9"
    assert len(lines) == 4


def test_claude_session_carries_none_and_a_forged_agent_line_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    repo, result = _commit_agree(tmp_path, "subject\n\nAgent-Vendor: codex\n")
    assert result.ok, result.stderr
    assert _agent_lines(repo) == []
    body = _git(["log", "-1", "--format=%B"], repo)
    assert f"Session-Id: {_SESSION_ID}" in body


def test_claude_commit_message_is_byte_identical_without_agent_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    msg = _msg(tmp_path, "subject\n\nbody line\n")
    before = msg.read_bytes()
    assert git_native._with_agent_provenance(msg, ["--trailer", "X-Y: z"]) == ["--trailer", "X-Y: z"]
    assert msg.read_bytes() == before


def test_admission_advisory_on_closed_marker_never_alters_the_commit(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-3")
    repo = real_git_repo(tmp_path)
    marker = repo / ADMISSION_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"closed_to_non_claude": true}\n', encoding="utf-8")
    make_agree_path(repo, "a.txt", "a\n")
    result = git_native.commit_scoped(["a.txt"], _msg(tmp_path, "subject\n"), repo)
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" in capfd.readouterr().err
    assert "Agent-Vendor: codex" in _agent_lines(repo)


def test_no_advisory_without_marker_or_for_claude(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-4")
    _, result = _commit_agree(tmp_path, "subject\n")
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" not in capfd.readouterr().err

    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    sub = tmp_path / "second"
    sub.mkdir()
    repo = real_git_repo(sub)
    marker = repo / ADMISSION_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"closed_to_non_claude": true}\n', encoding="utf-8")
    make_agree_path(repo, "a.txt", "a\n")
    result = git_native.commit_scoped(["a.txt"], _msg(sub, "subject\n"), repo)
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" not in capfd.readouterr().err


def test_claude_klabauter_marker_is_closed():
    import json

    root = Path(__file__).resolve().parents[4]
    data = json.loads((root / ADMISSION_MARKER).read_text(encoding="utf-8"))
    assert data == {"closed_to_non_claude": True}


def test_admission_advisory_fires_on_authored_content_and_new_file_routes(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-5")
    repo = real_git_repo(tmp_path)
    marker = repo / ADMISSION_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text('{"closed_to_non_claude": true}\n', encoding="utf-8")
    result = git_native.commit_authored_new_file("n.txt", "one\n", _msg(tmp_path, "subject\n"), repo)
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" in capfd.readouterr().err
    result = git_native.commit_authored_content("n.txt", "two\n", _msg(tmp_path, "subject\n"), repo)
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" in capfd.readouterr().err

    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    result = git_native.commit_authored_content("n.txt", "three\n", _msg(tmp_path, "subject\n"), repo)
    assert result.ok, result.stderr
    assert "closed to non-Claude agents" not in capfd.readouterr().err
