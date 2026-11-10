"""stale_base_reverted_paths: a scratch index built before a peer commit is flagged; a current one is not."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from coordinator_core import deny_registry
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._dc_git_probe import _run_git
from coordinator_core.bash_guards._stale_index_base import (
    _RECENT_COMMITS,
    stale_base_reverted_paths,
)

_IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str, index: Path | None = None) -> str:
    env = dict(os.environ, **_IDENT)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    out = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", msg)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "p.txt", "p0\n", "base p")
    _commit(repo, "q.txt", "q0\n", "base q")
    return repo


def _temp_index(repo: Path, tmp_path: Path) -> Path:
    idx = tmp_path / "scratch-index"
    _git(repo, "read-tree", "HEAD", index=idx)
    return idx


def _stage_own_hunk(repo: Path, idx: Path) -> None:
    (repo / "q.txt").write_text("q-mine\n", encoding="utf-8")
    _git(repo, "add", "--", "q.txt", index=idx)


def _probe(repo: Path, idx: Path):
    return stale_base_reverted_paths(str(idx), str(repo), _run_git)


def test_race_peer_commit_after_index_build_is_named(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _commit(repo, "p.txt", "p-peer\n", "peer lands p")
    _stage_own_hunk(repo, idx)
    assert _probe(repo, idx) == ["p.txt"]


def test_no_race_control_is_empty(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _stage_own_hunk(repo, idx)
    assert _probe(repo, idx) == []


def test_index_older_than_two_peer_commits_to_one_path_is_still_named(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _commit(repo, "p.txt", "p-peer1\n", "peer 1")
    _commit(repo, "p.txt", "p-peer2\n", "peer 2")
    _stage_own_hunk(repo, idx)
    assert _probe(repo, idx) == ["p.txt"]


def test_peer_added_file_missing_from_stale_index_is_named(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _commit(repo, "new.txt", "peer new\n", "peer adds")
    _stage_own_hunk(repo, idx)
    assert _probe(repo, idx) == ["new.txt"]


def test_ordinary_staged_change_is_not_a_revert(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "p-peer\n", "peer lands p")
    idx = _temp_index(repo, tmp_path)
    (repo / "p.txt").write_text("p-brand-new\n", encoding="utf-8")
    _git(repo, "add", "--", "p.txt", index=idx)
    assert _probe(repo, idx) == []


def test_commit_older_than_window_is_not_flagged(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _commit(repo, "p.txt", "p-peer\n", "peer lands p")
    for i in range(_RECENT_COMMITS):
        _commit(repo, "q.txt", "q%d\n" % (i + 1), "churn %d" % i)
    _stage_own_hunk(repo, idx)
    assert _probe(repo, idx) == []


def test_probe_failure_fails_open(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    assert stale_base_reverted_paths(str(idx), str(repo), lambda *a, **k: (1, "")) is None


def test_clean_index_spawns_once(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    calls: list = []

    def counting(args, cwd, **kw):
        calls.append(args[0])
        return _run_git(args, cwd, **kw)

    assert stale_base_reverted_paths(str(idx), str(repo), counting) == []
    assert calls == ["diff-index"]


def _dispatch(repo: Path, command: str):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _stale_deny_text(out) -> str | None:
    """The deny text of `stale-temp-index-base` in a dispatch result, else None."""
    text = json.dumps(out, default=str)
    return text if "predates HEAD" in text else None


def _hand_commit(idx: Path) -> str:
    return 'GIT_INDEX_FILE=%s git commit -m "mine"' % str(idx).replace("\\", "/")


def test_dispatch_denies_a_hand_commit_of_a_stale_index_naming_the_path(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _commit(repo, "p.txt", "p-peer\n", "peer lands p")
    _stage_own_hunk(repo, idx)
    text = _stale_deny_text(_dispatch(repo, _hand_commit(idx)))
    assert text is not None and "p.txt" in text and "deny" in text


def test_dispatch_is_silent_on_a_current_index(tmp_path):
    repo = _repo(tmp_path)
    idx = _temp_index(repo, tmp_path)
    _stage_own_hunk(repo, idx)
    assert _stale_deny_text(_dispatch(repo, _hand_commit(idx))) is None


def test_dispatch_without_git_index_file_spawns_no_stale_probe(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "p-peer\n", "peer lands p")
    calls: list = []
    monkeypatch.setattr(
        "coordinator_core.bash_guards._stale_index_base.stale_base_reverted_paths",
        lambda *a, **k: calls.append(a) or [],
    )
    assert _stale_deny_text(_dispatch(repo, 'git commit -m "x" -- q.txt')) is None
    assert calls == []


def test_stale_temp_index_base_is_disruptive_class():
    assert deny_registry.GUARD_DENY_CLASS["stale-temp-index-base"] == deny_registry.DISRUPTIVE
