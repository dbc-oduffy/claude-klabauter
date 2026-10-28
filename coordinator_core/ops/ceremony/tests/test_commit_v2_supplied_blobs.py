"""`ceremony.commit_v2` threads `supplied_blobs` to `commit_paths`: the run's own blob lands on a
co-declared path while the worktree keeps the peer's hunk; a blob absent from the store refuses;
a params key the op does not know refuses `unknown-param` rather than being dropped.
Throwaway repos under `tmp_path`, ceilinged so no git call can reach an outer repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        input=stdin, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / "shared.py").write_bytes(b"base\n")
    _git(repo, "add", "--", "shared.py")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _blob(repo: Path, text: str) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", stdin=text).strip()


def _call(repo: Path, **params) -> dict:
    params.setdefault("message", _SID)
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_supplied_blob_lands_and_worktree_keeps_peer_hunk(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    mine = _blob(repo, "base\nmine\n")
    peer_text = b"base\nmine\npeer\n"
    (repo / "shared.py").write_bytes(peer_text)
    reply = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": mine})
    assert reply["committed"] is True
    assert _git(repo, "rev-parse", "HEAD:shared.py").strip() == mine
    assert (repo / "shared.py").read_bytes() == peer_text


def _index_entry(repo: Path, path: str) -> str:
    return _git(repo, "ls-files", "-s", "--", path)


def test_union_of_dirty_staged_and_clean_paths_commits_supplied_blobs(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    for name in ("b.py", "c.py", "d.py"):
        (repo / name).write_bytes(b"base\n")
        _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", "seed2")
    supplied = {
        "shared.py": _blob(repo, "S-shared\n"),
        "b.py": _blob(repo, "S-b\n"),
        "c.py": _blob(repo, "S-c\n"),
    }
    (repo / "shared.py").write_bytes(b"W-shared\n")  # dirty, unstaged
    (repo / "b.py").write_bytes(b"W-b\n")  # dirty and staged
    _git(repo, "add", "--", "b.py")
    (repo / "b.py").write_bytes(b"W-b2\n")
    # c.py clean in the worktree; d.py dirty and not supplied
    (repo / "d.py").write_bytes(b"W-d\n")
    reply = _call(repo, paths=["shared.py", "b.py", "c.py", "d.py"], supplied_blobs=supplied)
    assert reply["committed"] is True
    for path, sha in supplied.items():
        assert _git(repo, "rev-parse", f"HEAD:{path}").strip() == sha
    assert _git(repo, "show", "HEAD:d.py") == "W-d\n"
    assert (repo / "shared.py").read_bytes() == b"W-shared\n"
    assert (repo / "b.py").read_bytes() == b"W-b2\n"
    assert (repo / "c.py").read_bytes() == b"base\n"
    # The index follows HEAD for a committed path; it never holds the worktree bytes.
    for path, sha in supplied.items():
        assert _index_entry(repo, path).split()[1] == sha


def test_supplied_blob_for_clean_path_lands(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    mine = _blob(repo, "base\nmine\n")
    reply = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": mine})
    assert reply["committed"] is True
    assert _git(repo, "rev-parse", "HEAD:shared.py").strip() == mine
    assert (repo / "shared.py").read_bytes() == b"base\n"
    assert _index_entry(repo, "shared.py").split()[1] == mine


def test_supplied_blob_absent_from_store_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "shared.py").write_bytes(b"base\npeer\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    reply = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": "a" * 40})
    assert reply["committed"] is False
    assert reply["refused"] == "supplied-blob-missing"
    assert _git(repo, "rev-parse", "HEAD").strip() == before
    assert (repo / "shared.py").read_bytes() == b"base\npeer\n"


def test_unknown_param_refuses_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "shared.py").write_bytes(b"base\npeer\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    reply = _call(repo, paths=["shared.py"], suplied_blobs={"shared.py": "a" * 40})
    assert reply["committed"] is False
    assert reply["refused"] == "unknown-param"
    assert "suplied_blobs" in reply["error"]
    assert _git(repo, "rev-parse", "HEAD").strip() == before


def test_supplied_blobs_key_outside_every_path_list_refuses_unknown_param(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "shared.py").write_bytes(b"base\npeer\n")
    mine = _blob(repo, "base\nmine\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    reply = _call(
        repo, paths=["shared.py"],
        supplied_blobs={"shared.py": mine, "typo.py": mine},
    )
    assert reply["committed"] is False
    assert reply["refused"] == "unknown-param"
    assert "typo.py" in reply["error"]
    assert _git(repo, "rev-parse", "HEAD").strip() == before


def test_supplied_blobs_key_named_by_prefer_staged_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    mine = _blob(repo, "base\nmine\n")
    reply = _call(repo, prefer_staged=["shared.py"], supplied_blobs={"shared.py": mine})
    assert reply.get("refused") != "unknown-param"


def test_known_params_are_not_refused_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "shared.py").write_bytes(b"base\npeer\n")
    reply = _call(repo, paths=["shared.py"], allow_empty=False, skip_missing=False)
    assert reply["committed"] is True


def test_supplied_blob_malformed_sha_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "shared.py").write_bytes(b"x\n")
    reply = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": "nothex"})
    assert reply["committed"] is False
    assert reply["refused"] == "supplied-blob-missing"


def test_blob_written_after_a_refusal_is_seen_on_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    sha = _git(repo, "hash-object", "--stdin", stdin="base\nlater\n").strip()  # computed, not stored
    first = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": sha})
    assert first["refused"] == "supplied-blob-missing"
    assert _blob(repo, "base\nlater\n") == sha
    retry = _call(repo, paths=["shared.py"], supplied_blobs={"shared.py": sha})
    assert retry["committed"] is True
    assert _git(repo, "rev-parse", "HEAD:shared.py").strip() == sha


#: Every params key a caller in coordinator_core/ or coordinator/bin sends to ceremony.commit_v2.
_CALLER_KEYS = [
    "paths", "deleted_paths", "untracked_paths", "message", "prefer_staged", "allow_empty",
    "finish_merge", "session_id", "declared_reverts", "expected_content_sha256", "supplied_blobs",
    "ignored_untracked", "drop_ignored", "full_reply", "carry_own_left_behind", "carry_stopped_runs",
    "repo_root", "skip_missing", "gone_tracked_as_deleted", "on_main_reason", "force_ignored",
    "prefer_deliberate_stage", "expected_blobs",
]


@pytest.mark.parametrize("key", _CALLER_KEYS)
def test_every_caller_key_passes_the_unknown_param_gate(key):
    reply = commit_v2._parse_params({key: None}, None)
    assert isinstance(reply, dict) and reply.get("refused") != "unknown-param"


def test_without_supplied_blobs_whole_worktree_lands(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"mine\nforeign\n")
    reply = _call(repo, paths=["a.py"])
    assert reply["committed"] is True, reply
    assert _git(repo, "show", "HEAD:a.py") == "mine\nforeign\n"


@pytest.mark.parametrize("bad", ["x", ["a.py"], {"a.py": 1}, {1: "x"}])
def test_type_errors_name_the_param(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"mine\n")
    reply = _call(repo, paths=["a.py"], supplied_blobs=bad)
    assert reply["committed"] is False
    assert "params.supplied_blobs" in reply["error"]
