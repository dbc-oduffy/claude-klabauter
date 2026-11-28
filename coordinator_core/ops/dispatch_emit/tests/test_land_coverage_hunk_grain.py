"""`_land_coverage` reports a measured path the commit carries only some hunks of as partially landed."""

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.tests._fixtures import init_repo, run_git

PLAN = "docs/plans/2026-11-20-x-68c477.md"
BASE = "".join(f"line{i}\n" for i in range(1, 11))
BOTH = BASE.replace("line2\n", "two\n").replace("line9\n", "nine\n")
FIRST_ONLY = BASE.replace("line2\n", "two\n")


def _blob(repo, text):
    got = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=text.encode(), capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return got.stdout.decode().strip()


def _commit(repo, rel, text):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8", newline="")
    run_git(repo, "add", rel)
    run_git(repo, "commit", "-m", f"c {rel}")
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _repo(tmp_path, landed_text):
    repo = tmp_path / "r"
    init_repo(repo)
    _commit(repo, "src/a.py", BASE)
    base_blob = _blob(repo, BASE)
    measured = {"paths": ["src/a.py"], "blobs": {"src/a.py": _blob(repo, BOTH)}}
    sha = _commit(repo, "src/a.py", landed_text)
    return repo, measured, sha, base_blob


def _forbid_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git spawned on the coverage path")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("coordinator_core.git.run.run_git", boom)


def test_commit_with_one_of_two_hunks_is_partial_and_counts_the_left_hunk(tmp_path, monkeypatch):
    repo, measured, sha, base_blob = _repo(tmp_path, FIRST_ONLY)
    _forbid_spawn(monkeypatch)
    split = land_run._partial_hunks(repo, measured, ["src/a.py"], sha, {"src/a.py": base_blob})
    assert split["src/a.py"] == {"landed": 1, "left": 1, "left_hunks": ["9,1"]}
    out = land_run._land_coverage(["src/a.py"], {"src/a.py"}, {}, PLAN, "r1", partial=split)
    assert out["land_outcome"] == "partial"
    assert out["partial_hunks"] == {"src/a.py": {"landed": 1, "left": 1, "left_hunks": ["9,1"]}}
    assert "1 of 2 hunk(s) left out (9,1)" in out["withheld_reasons"]["src/a.py"]
    assert "ok" not in out


def test_base_falls_back_to_the_commit_parent_without_a_baseline(tmp_path, monkeypatch):
    repo, measured, sha, _base = _repo(tmp_path, FIRST_ONLY)
    _forbid_spawn(monkeypatch)
    split = land_run._partial_hunks(repo, measured, ["src/a.py"], sha, None)
    assert split["src/a.py"]["left"] == 1


def test_committed_blob_equal_to_measured_blob_is_absent_from_partial_hunks(tmp_path, monkeypatch):
    repo, measured, sha, base_blob = _repo(tmp_path, BOTH)
    _forbid_spawn(monkeypatch)
    split = land_run._partial_hunks(repo, measured, ["src/a.py"], sha, {"src/a.py": base_blob})
    assert split == {}
    assert land_run._land_coverage(["src/a.py"], {"src/a.py"}, {}, PLAN, "r1", partial=split) is None


def test_fully_carried_split_reports_nothing_and_missing_record_skips():
    full = {"src/a.py": {"landed": 2, "left": 0, "left_hunks": []}}
    assert land_run._land_coverage(["src/a.py"], {"src/a.py"}, {}, PLAN, "r1", partial=full) is None
    assert land_run._partial_hunks(None, None, ["src/a.py"], "x" * 40, {}) == {}


def test_a_partial_path_does_not_make_the_commit_landed_no_code():
    split = {"src/a.py": {"landed": 1, "left": 1, "left_hunks": ["9,1"]}}
    out = land_run._land_coverage(["src/a.py"], {"src/a.py"}, {}, PLAN, "r1", partial=split)
    assert out["land_outcome"] == "partial" and "ok" not in out


def test_a_partial_path_beside_a_fully_withheld_one_names_both():
    split = {"src/a.py": {"landed": 1, "left": 2, "left_hunks": ["3,1", "9,2"]}}
    out = land_run._land_coverage(
        ["src/a.py", "src/b.py"], {"src/a.py"}, {"src/b.py": "why"}, PLAN, "r1", dirty={"src/b.py"}, partial=split,
    )
    assert out["withheld_reasons"]["src/b.py"] == "why"
    assert "2 of 3 hunk(s) left out (3,1, 9,2)" in out["withheld_reasons"]["src/a.py"]
    assert list(out["partial_hunks"]) == ["src/a.py"]


@pytest.mark.parametrize("path", ["src/other.py", PLAN])
def test_a_split_for_a_path_outside_the_measured_code_is_ignored(path):
    split = {path: {"landed": 0, "left": 1, "left_hunks": ["1,1"]}}
    assert land_run._land_coverage(["src/a.py"], {"src/a.py", path}, {}, PLAN, "r1", partial=split) is None
