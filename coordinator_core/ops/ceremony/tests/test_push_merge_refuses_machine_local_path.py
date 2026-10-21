"""The push-ladder merge declines a merged tree that adds machine-local paths."""
from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import push as push_mod
from coordinator_core.ops.ceremony.tests.test_push_reject_landed_by_peer import (
    _git,
    _init_repo_with_upstream,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SHARE = ".coordinator-local/subagent-share"


def _remote_adds(tmp_path, rel: str) -> None:
    other = tmp_path / "other-share"
    _git(["clone", "-q", str(tmp_path / "origin.git"), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    target = other / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("telemetry", encoding="utf-8")
    _git(["add", "-f", "--", rel], other)
    _git(["commit", "-q", "-m", "remote adds machine-local"], other)
    _git(["push", "-q", "origin", "work/x"], other)


def _local_commit(repo) -> None:
    (repo / "ours.txt").write_text("unpublished", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours"], repo)


def test_merge_adding_a_machine_local_path_declines_touching_nothing(tmp_path):
    repo = _init_repo_with_upstream(tmp_path)
    _remote_adds(tmp_path, f"{_SHARE}/s/report.md")
    _local_commit(repo)
    _git(["fetch", "-q", "origin"], repo)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    index_before = _git(["ls-files", "-s"], repo)
    refs_before = _git(["for-each-ref"], repo)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 1
    assert reason.startswith("merge recovery declined, nothing touched: ")
    assert f"{_SHARE}/s/report.md" in reason
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert _git(["ls-files", "-s"], repo) == index_before
    assert _git(["for-each-ref"], repo) == refs_before
    assert not (repo / _SHARE).exists()


def test_merge_without_machine_local_paths_still_lands(tmp_path):
    repo = _init_repo_with_upstream(tmp_path)
    _remote_adds(tmp_path, "docs/note.md")
    _local_commit(repo)
    _git(["fetch", "-q", "origin"], repo)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 0, reason
    assert (repo / "docs/note.md").exists()
