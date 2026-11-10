"""A worktree freeze over hundreds of paths never builds a git argv past the Windows command-line cap.

4bfba2's review prep froze 646 paths in one `git diff` argv; CreateProcess refused it and git
"exited -1" with no stderr, twice, halting the run unreviewed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import coordinator_core.ops.review_freeze_diff as review_freeze_diff
from coordinator_core.ops.review_freeze_diff import _pathspec_chunks, freeze_diff

_WINDOWS_CMDLINE_CAP = 32767


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_chunks_cover_every_spec_in_order_and_stay_under_budget() -> None:
    specs = [f":(literal)coordinator_core/ops/dispatch_emit/tests/test_{i:04d}_a_long_name.py" for i in range(700)]
    chunks = _pathspec_chunks(specs)
    assert len(chunks) > 1
    assert [s for c in chunks for s in c] == specs
    assert all(sum(len(s) + 3 for s in c) <= review_freeze_diff._ARGV_CHAR_BUDGET for c in chunks)


def test_worktree_freeze_over_many_paths_splits_the_diff_argv(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    deep = "a_fairly_long_directory_name/" * 3
    paths = [f"{deep}file_{i:04d}_with_a_long_name.txt" for i in range(400)]
    for p in paths:
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text("v1\n", encoding="utf-8", newline="\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "base"], repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    for p in paths:
        (repo / p).write_text("v2\n", encoding="utf-8", newline="\n")

    real = review_freeze_diff._git
    lengths: list[int] = []

    def capped(argv, **kw):
        lengths.append(sum(len(a) + 1 for a in argv))
        return real(argv, **kw)

    monkeypatch.setattr(review_freeze_diff, "_git", capped)
    out = freeze_diff(repo, base, "many", paths=paths, worktree=True)
    assert out.get("error") is None, out
    assert len(lengths) > 1 and max(lengths) < _WINDOWS_CMDLINE_CAP
    diff = Path(out["diff_path"]).read_text(encoding="utf-8")
    assert all(f"b/{p}" in diff for p in paths)
