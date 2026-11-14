"""Doctor layer "Staged paths behind HEAD": counts, per registered repo, index entries sitting at an
older committed blob. Throwaway repos under `tmp_path`, ceilinged so no git call reaches an outer repo."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import doctor, doctor_staged_behind as sb

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def _commit(repo: Path, files: dict[str, bytes], msg: str) -> None:
    for rel, data in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", msg)


def _stage_old_blob(repo: Path, rel: str, rev: str) -> None:
    sha = _git(repo, "rev-parse", f"{rev}:{rel}")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}")


def _seed_behind(repo: Path) -> None:
    """HEAD~1 holds a/f.py=v1, HEAD holds v2; the index is put back at v1 (an index-only landing's leftover)."""
    _commit(repo, {"a/f.py": b"v1\n", "keep.py": b"k\n"}, "one")
    _commit(repo, {"a/f.py": b"v2\n"}, "two")
    _stage_old_blob(repo, "a/f.py", "HEAD~1")


def test_clean_repo_reports_nothing(tmp_path):
    repo = _repo(tmp_path, "clean")
    _commit(repo, {"a/f.py": b"v1\n"}, "one")
    assert sb.staged_behind(repo) == []


def test_staged_at_previous_committed_blob_is_behind(tmp_path):
    repo = _repo(tmp_path, "r")
    _seed_behind(repo)
    assert sb.staged_behind(repo) == ["a/f.py"]


def test_staged_at_a_never_committed_blob_is_not_behind(tmp_path):
    repo = _repo(tmp_path, "r")
    _commit(repo, {"a/f.py": b"v1\n"}, "one")
    (repo / "a" / "f.py").write_bytes(b"brand new work\n")
    _git(repo, "add", "--", "a/f.py")
    assert sb.staged_behind(repo) == []


def test_path_deleted_in_head_but_still_staged_at_its_old_blob_is_behind(tmp_path):
    repo = _repo(tmp_path, "r")
    _commit(repo, {"gone.py": b"g\n", "stay.py": b"s\n"}, "one")
    _git(repo, "rm", "-q", "--", "gone.py")
    _git(repo, "commit", "-q", "-m", "delete")
    _stage_old_blob(repo, "gone.py", "HEAD~1")
    assert sb.staged_behind(repo) == ["gone.py"]


def test_ancestor_search_is_bounded(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "r")
    _commit(repo, {"f.py": b"v1\n"}, "one")
    _commit(repo, {"f.py": b"v2\n"}, "two")
    _commit(repo, {"f.py": b"v3\n"}, "three")
    _stage_old_blob(repo, "f.py", "HEAD~2")
    monkeypatch.setattr(sb, "ANCESTOR_DEPTH", 2)
    assert sb.staged_behind(repo) == ["f.py"]
    monkeypatch.setattr(sb, "ANCESTOR_DEPTH", 1)
    assert sb.staged_behind(repo) == []


def test_candidate_cap_makes_the_count_a_floor(tmp_path):
    repo = _repo(tmp_path, "r")
    names = [f"f{i:02d}.py" for i in range(sb.CANDIDATE_CAP + 4)]
    _commit(repo, {n: b"v1\n" for n in names}, "one")
    _commit(repo, {n: b"v2\n" for n in names}, "two")
    for n in names:
        _stage_old_blob(repo, n, "HEAD~1")
    assert len(sb.staged_behind(repo)) == sb.CANDIDATE_CAP


def test_layer_lists_every_registered_repo_and_never_fails(tmp_path):
    first, second, clean = _repo(tmp_path, "first"), _repo(tmp_path, "second"), _repo(tmp_path, "clean")
    _seed_behind(first)
    _seed_behind(second)
    _commit(clean, {"x.py": b"x\n"}, "one")
    layer = sb.staged_behind_layer({"first": first, "second": second, "clean": clean})
    assert layer.name == sb.LAYER_NAME
    assert layer.status == "ok"
    assert [f.severity for f in layer.findings] == ["info", "info"]
    assert layer.findings[0].message.startswith("first: 1 staged path(s) behind HEAD (e.g. a/f.py)")
    assert layer.findings[1].message.startswith("second: 1 staged path(s) behind HEAD (e.g. a/f.py)")
    assert "reset -q -- a/f.py" in layer.findings[0].message


def test_unreadable_repo_is_skipped(tmp_path):
    bare = tmp_path / "notgit"
    bare.mkdir()
    layer = sb.staged_behind_layer({"notgit": bare})
    assert layer.status == "ok" and layer.findings == []


def test_cache_tree_root_matches_head_tree_for_a_fresh_commit(tmp_path):
    repo = _repo(tmp_path, "r")
    _commit(repo, {"a/f.py": b"v1\n", "b.py": b"b\n"}, "one")
    _git(repo, "write-tree")
    raw = (repo / ".git" / "index").read_bytes()
    assert sb._cache_tree_root(raw) == bytes.fromhex(_git(repo, "rev-parse", "HEAD^{tree}"))
    _stage_old_blob(repo, "b.py", "HEAD")
    _commit(repo, {"b.py": b"b2\n"}, "two")
    _stage_old_blob(repo, "b.py", "HEAD~1")
    assert sb._cache_tree_root((repo / ".git" / "index").read_bytes()) != bytes.fromhex(
        _git(repo, "rev-parse", "HEAD^{tree}")
    )


def test_run_doctor_carries_the_layer_for_the_registry(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "r")
    _seed_behind(repo)
    from coordinator_core.plugin_health import claude_md_handoff_convention as conv

    monkeypatch.setattr(conv, "fleet_repos", lambda raw=None: {"r": repo})
    report, _ = doctor.run_doctor()
    layer = next(l for l in report.layers if l.name == sb.LAYER_NAME)
    assert layer.status == "ok"
    assert len(layer.findings) == 1 and "r: 1 staged path(s) behind HEAD" in layer.findings[0].message
