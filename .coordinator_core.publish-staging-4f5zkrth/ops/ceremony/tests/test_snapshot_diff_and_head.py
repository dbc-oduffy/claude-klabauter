"""
coordinator_core.ops.ceremony.tests.test_snapshot_diff_and_head

Tests for the `review.snapshot_diff_and_head` op (snapshot_diff_and_head.py) —
the read-only diff+HEAD snapshot replacement for the `parallel-code-review`
fence (`coordinator/skills/parallel-code-review/SKILL.md:155`).

Coverage:
  - a genuine snapshot freezes diff.patch + head.sha under a SHA-keyed
    directory (not a wall-clock timestamp).
  - a second, identical invocation is a safe no-op (AC7 idempotency) — same
    ts_dir/diff_path/head_sha, file contents unchanged.
  - two DISTINCT head commits produce two DISTINCT ts_dir slots (never
    collapsed onto the same directory).
  - required-param validation (missing findings_dir, missing repo_root).
  - a ref that fails to resolve surfaces a structured error, not a raise.

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo. No mutating git command is issued by
this test file itself beyond throwaway-repo setup (init/add/commit/branch).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.ceremony import snapshot_diff_and_head

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _rev_parse(repo: Path, ref: str) -> str:
    return _git(["rev-parse", ref], repo).stdout.strip()


def _call(params: dict, repo_root) -> dict:
    return snapshot_diff_and_head._handler(params, repo_root=repo_root)


def test_snapshot_freezes_diff_and_head_under_sha_keyed_dir(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_sha = _rev_parse(repo, "main")

    _seed_file(repo, "README.md", "seed\nchanged\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "change"], repo)
    head_sha = _rev_parse(repo, "HEAD")

    findings_dir = tmp_path / "findings"
    result = _call(
        {"findings_dir": str(findings_dir), "base_ref": base_sha, "head_ref": "HEAD"},
        repo_root=repo,
    )

    assert "error" not in result
    assert result["head_sha"] == head_sha
    ts_dir = Path(result["ts_dir"])
    diff_path = Path(result["diff_path"])
    assert result["diff_path"] == str(ts_dir / "diff.patch")
    assert ts_dir.is_dir()
    assert diff_path.is_file()
    assert "changed" in diff_path.read_text(encoding="utf-8")
    assert (ts_dir / "head.sha").read_text(encoding="utf-8").strip() == head_sha
    # SHA-keyed, not wall-clock: directory name derives from the two SHAs.
    assert base_sha[:12] in ts_dir.name
    assert head_sha[:12] in ts_dir.name


def test_second_identical_invocation_is_safe_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_sha = _rev_parse(repo, "main")

    _seed_file(repo, "README.md", "seed\nchanged\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "change"], repo)

    findings_dir = tmp_path / "findings"
    params = {"findings_dir": str(findings_dir), "base_ref": base_sha, "head_ref": "HEAD"}

    first = _call(params, repo_root=repo)
    assert "error" not in first

    diff_path = Path(first["diff_path"])
    head_sha_path = Path(first["ts_dir"]) / "head.sha"
    first_diff_mtime = diff_path.stat().st_mtime_ns
    first_diff_bytes = diff_path.read_bytes()

    second = _call(params, repo_root=repo)

    assert second == first
    # Idempotency short-circuit: content untouched, not merely equal-by-luck.
    assert diff_path.read_bytes() == first_diff_bytes
    assert diff_path.stat().st_mtime_ns == first_diff_mtime
    assert head_sha_path.read_text(encoding="utf-8").strip() == first["head_sha"]


def test_distinct_head_commits_produce_distinct_snapshot_slots(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_sha = _rev_parse(repo, "main")

    findings_dir = tmp_path / "findings"
    params = {"findings_dir": str(findings_dir), "base_ref": base_sha, "head_ref": "HEAD"}

    first = _call(params, repo_root=repo)

    _seed_file(repo, "README.md", "seed\nmore\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "more"], repo)

    second = _call(params, repo_root=repo)

    assert first["head_sha"] != second["head_sha"]
    assert first["ts_dir"] != second["ts_dir"]
    assert Path(first["ts_dir"]).is_dir()
    assert Path(second["ts_dir"]).is_dir()


def test_missing_repo_root_is_a_structured_error(tmp_path):
    result = snapshot_diff_and_head._handler(
        {"findings_dir": str(tmp_path / "findings")}, repo_root=None
    )
    assert result["error"]
    assert result["ts_dir"] is None
    assert result["diff_path"] is None
    assert result["head_sha"] is None


def test_missing_findings_dir_is_a_structured_error(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    result = _call({}, repo_root=repo)
    assert result["error"]
    assert result["ts_dir"] is None


def test_unresolvable_ref_is_a_structured_error_not_a_raise(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed\n")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    result = _call(
        {
            "findings_dir": str(tmp_path / "findings"),
            "base_ref": "does-not-exist-ref",
            "head_ref": "HEAD",
        },
        repo_root=repo,
    )
    assert result["error"]
    assert "does-not-exist-ref" in result["error"]
