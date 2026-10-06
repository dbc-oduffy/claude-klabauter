"""push.outstanding resolves its default ceiling per repo and streams above the default."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
import coordinator_core.ops.push_outstanding as po
from coordinator_core.ops.ceremony import push_ceiling
from coordinator_core.ops.ceremony.push import PUSH_RETRY_BUDGET_SECS, PushOutcome

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo_ahead(tmp_path: Path) -> Path:
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/x"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(["add", "--", "b.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    return repo


@pytest.fixture
def captured(monkeypatch, tmp_path):
    empty_global = tmp_path / "global-gitconfig"
    empty_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    push_ceiling._GLOBAL_MEMO.clear()
    seen: dict = {}

    def _fake(root, **kwargs):
        seen.update(kwargs)
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(po, "push_with_retry", _fake)
    return seen


def test_default_repo_keeps_default_ceiling_and_is_not_streamed(captured, tmp_path):
    repo = _repo_ahead(tmp_path)
    po.push_outstanding(repo)
    assert captured["budget_secs"] == PUSH_RETRY_BUDGET_SECS == 18.0
    assert "use_streamed_push" not in captured


def test_lfs_required_repo_gets_60s_and_is_streamed(captured, tmp_path):
    repo = _repo_ahead(tmp_path)
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", encoding="utf-8")
    _git(["config", "filter.lfs.required", "true"], repo)
    po.push_outstanding(repo)
    assert captured["budget_secs"] == 60.0
    assert captured["use_streamed_push"] is True


def test_explicit_budget_is_used_as_given(captured, tmp_path):
    repo = _repo_ahead(tmp_path)
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    _git(["config", "filter.lfs.required", "true"], repo)
    po.push_outstanding(repo, budget_secs=5.0)
    assert captured["budget_secs"] == 5.0
    assert "use_streamed_push" not in captured


def test_dispatch_timeout_exceeds_the_ceiling_maximum():
    assert ipc._timeout_for("push.outstanding") > po.PUSH_CEILING_MAX_SECS
