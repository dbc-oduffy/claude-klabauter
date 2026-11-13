"""commit_scoped folds atlas freshness: the covering page's `files:` count lands in the same commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography.atlas_freshness import ACTED_LEDGER_RELPATH
from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags
from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PAGE = "docs/architecture/systems/cartography.md"
_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# File index

## Directory → system

### cartography — atlas tooling

`coordinator_core/cartography/`
"""
_PAGE_TEXT = "---\nsystem: cartography\nfiles: 7\nlines: 10\n---\n\n# cartography\n"
_NEW = "coordinator_core/cartography/new_mod.py"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _msg(tmp_path: Path) -> Path:
    msg = tmp_path / "msg.txt"
    msg.write_text("add module\n", encoding="utf-8")
    return msg


def _repo_with_atlas(tmp_path: Path) -> Path:
    repo = real_git_repo(tmp_path)
    _write(repo, "docs/architecture/file-index.md", _FILE_INDEX)
    _write(repo, _PAGE, _PAGE_TEXT)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "atlas"], repo)
    return repo


def test_committed_tree_carries_repaired_page_and_ledger_row(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    _write(repo, _NEW, "x = 1\n")
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert "files: 8" in _git(["show", f"HEAD:{_PAGE}"], repo).stdout
    assert _NEW in _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo).stdout
    rows = [json.loads(l) for l in (repo / ACTED_LEDGER_RELPATH).read_text(encoding="utf-8").splitlines()]
    assert any(r["page"] == _PAGE and r["kind"] == "repaired" for r in rows)


def test_repo_without_atlas_commits_only_its_paths(tmp_path):
    repo = real_git_repo(tmp_path)
    _write(repo, _NEW, "x = 1\n")
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo).stdout.split() == [_NEW]
    assert not (repo / ACTED_LEDGER_RELPATH).exists()


def test_failed_commit_restores_the_page(tmp_path, monkeypatch):
    repo = _repo_with_atlas(tmp_path)
    _write(repo, _NEW, "x = 1\n")
    failed = git_native.GitResult(returncode=-1, stdout="", stderr="boom")
    monkeypatch.setattr(git_native, "_commit_scoped_unfolded", lambda *a, **k: (
        # the page was already rewritten by the fold when the commit ran
        _assert_page_rewritten(repo) or failed
    ))
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert not result.ok
    assert (repo / _PAGE).read_text(encoding="utf-8") == _PAGE_TEXT
    assert not (repo / ACTED_LEDGER_RELPATH).exists()


def _assert_page_rewritten(repo: Path) -> None:
    assert "files: 8" in (repo / _PAGE).read_text(encoding="utf-8")


def test_fold_error_commits_original_paths(tmp_path, monkeypatch):
    repo = _repo_with_atlas(tmp_path)
    _write(repo, _NEW, "x = 1\n")
    from coordinator_core.cartography import atlas_freshness

    def _boom(*a, **k):
        raise RuntimeError("fold broke")

    monkeypatch.setattr(atlas_freshness, "plan_repair", _boom)
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo).stdout.split() == [_NEW]
    rows = [json.loads(l) for l in (repo / ACTED_LEDGER_RELPATH).read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows] == ["fold_error"]


def test_dirty_page_is_not_swept_into_the_commit(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    peer_text = _PAGE_TEXT + "peer edit\n"
    _write(repo, _PAGE, peer_text)
    _write(repo, _NEW, "x = 1\n")
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo).stdout.split() == [_NEW]
    assert (repo / _PAGE).read_text(encoding="utf-8") == peer_text


def test_unparseable_head_sha_records_unknown_not_empty(tmp_path, monkeypatch):
    repo = _repo_with_atlas(tmp_path)
    _write(repo, _NEW, "x = 1\n")
    real = git_native._commit_scoped_unfolded

    def _odd_stdout(*a, **k):
        res = real(*a, **k)
        return git_native.GitResult(returncode=res.returncode, stdout="committed", stderr=res.stderr)

    monkeypatch.setattr(git_native, "_commit_scoped_unfolded", _odd_stdout)
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    rows = [json.loads(l) for l in (repo / ACTED_LEDGER_RELPATH).read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["sha"] == "unknown" for r in rows)


def _seed_pending(repo: Path, pending: dict) -> Path:
    path = repo / ".coordinator-local/freshness/atlas-pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending), encoding="utf-8")
    return path


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def test_pending_delta_lands_in_next_commit_and_is_cleared(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    pending = _seed_pending(repo, {_PAGE: 1})
    _write(repo, _NEW, "x = 1\n")
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert "files: 9" in _git(["show", f"HEAD:{_PAGE}"], repo).stdout
    assert json.loads(pending.read_text(encoding="utf-8")) == {}


def test_pending_page_untouched_by_paths_is_repaired_by_the_fold(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    _seed_pending(repo, {_PAGE: 2})
    _write(repo, "notes.txt", "n\n")
    result = git_native.commit_scoped(["notes.txt"], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert "files: 9" in _git(["show", f"HEAD:{_PAGE}"], repo).stdout


def test_new_sha_is_recorded_as_folded_and_watermark_advances(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    parent = _head(repo)
    wm = repo / ".coordinator-local/freshness/atlas-watermark.json"
    wm.parent.mkdir(parents=True, exist_ok=True)
    wm.write_text(json.dumps({"sha": parent, "folded": []}), encoding="utf-8")
    _write(repo, _NEW, "x = 1\n")
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    data = json.loads(wm.read_text(encoding="utf-8"))
    assert data["folded"] == [_head(repo)]
    assert data["sha"] == _head(repo)


def test_failed_commit_keeps_the_pending_entry(tmp_path, monkeypatch):
    repo = _repo_with_atlas(tmp_path)
    pending = _seed_pending(repo, {_PAGE: 1})
    _write(repo, _NEW, "x = 1\n")
    failed = git_native.GitResult(returncode=-1, stdout="", stderr="boom")
    monkeypatch.setattr(git_native, "_commit_scoped_unfolded", lambda *a, **k: failed)
    result = git_native.commit_scoped([_NEW], _msg(tmp_path), repo)
    assert not result.ok
    assert json.loads(pending.read_text(encoding="utf-8")) == {_PAGE: 1}
    assert (repo / _PAGE).read_text(encoding="utf-8") == _PAGE_TEXT


def test_dirty_page_keeps_its_pending_entry(tmp_path):
    repo = _repo_with_atlas(tmp_path)
    pending = _seed_pending(repo, {_PAGE: 1})
    _write(repo, _PAGE, _PAGE_TEXT + "peer edit\n")
    _write(repo, "notes.txt", "n\n")
    result = git_native.commit_scoped(["notes.txt"], _msg(tmp_path), repo)
    assert result.ok, result.stderr
    assert json.loads(pending.read_text(encoding="utf-8")) == {_PAGE: 1}
