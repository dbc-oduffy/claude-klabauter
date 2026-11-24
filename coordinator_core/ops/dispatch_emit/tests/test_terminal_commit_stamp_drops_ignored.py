"""_stamp_coded_commit drops gitignored untracked paths from its commit_v2 call instead of refusing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import terminal_commit_stamp as tcs

SHA = "deadbeef" * 5


def _stub_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ref_cas_retry, "head_sha", lambda root: "h1")
    monkeypatch.setattr(ref_cas_retry, "paths_unchanged_between", lambda *a: True)


def test_commit_params_ask_commit_v2_to_drop_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_cas(monkeypatch)
    (tmp_path / "plan.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("y\n", encoding="utf-8")
    calls: list = []

    def commit_v2(params, repo_root):
        calls.append(params)
        return {"committed": True, "sha": "c0ffee", "dropped_ignored": ["note.txt"]}

    out = tcs._stamp_coded_commit(
        commit_v2, tmp_path, tmp_path, {}, SHA, None, also_commit=("plan.md", "note.txt"),
    )
    assert calls[0]["drop_ignored"] is True
    assert out["_committed"] == ["plan.md"]
    assert "coded_stamp_error" not in out


def test_unreported_drop_keeps_every_path_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_cas(monkeypatch)
    (tmp_path / "plan.md").write_text("x\n", encoding="utf-8")

    def commit_v2(params, repo_root):
        return {"committed": True, "sha": "c0ffee"}

    out = tcs._stamp_coded_commit(commit_v2, tmp_path, tmp_path, {}, SHA, None, also_commit=("plan.md",))
    assert out["_committed"] == ["plan.md"]


def test_real_commit_v2_lands_stamp_and_leaves_ignored_untracked(tmp_path: Path) -> None:
    from coordinator_core.ops.ceremony.commit_v2 import _handler

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()

    git("init", "-q", "-b", "feature")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (tmp_path / "plan.md").write_text("one\n", encoding="utf-8")
    git("add", ".gitignore", "plan.md")
    git("commit", "-q", "-m", "init")
    (tmp_path / "plan.md").write_text("two\n", encoding="utf-8")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "review-targets.txt").write_text("t\n", encoding="utf-8")

    def commit_v2(params, repo_root):
        return _handler(params, repo_root=tmp_path / ".git")

    out = tcs._stamp_coded_commit(
        commit_v2, tmp_path, tmp_path, {}, SHA, None,
        also_commit=("plan.md", "scratch/review-targets.txt"),
    )
    assert "coded_stamp_error" not in out, out
    assert out["coded_sha"]
    assert out["_committed"] == ["plan.md"]
    assert git("ls-files").split() == [".gitignore", "plan.md"]
    assert git("show", "HEAD:plan.md") == "two"
