"""review.prep spawn count is constant in the dirty-file count; slices are cut from the frozen whole diff."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-budget"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _seed(root: Path, dirty: int, committed: bool) -> tuple:
    """A repo with `dirty` declared files (half modified, half new); returns (repo, base, head, paths)."""
    _git(root, "init", "-q")
    plan = root / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-budget-1\n---\n\n# p\n", encoding="utf-8")
    tracked = [f"src/d{i % 20}/m{i}.py" for i in range(dirty // 2)]
    fresh = [f"src/d{i % 20}/u{i}.py" for i in range(dirty - dirty // 2)]
    for rel in tracked:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("v = 0\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    for rel in [*tracked, *fresh]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("v = 1\n" * 30, encoding="utf-8")
    paths = [*tracked, *fresh]
    head = None
    if committed:
        _git(root, "add", "--", *paths)
        _git(root, "commit", "-q", "-m", "head")
        head = _git(root, "rev-parse", "HEAD").strip()
    return root, base, head, paths


def _spawns(monkeypatch) -> list:
    seen: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, args, *a, **kw):
            seen.append(args)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return seen


def _run(tmp_path: Path, monkeypatch, dirty: int, committed: bool) -> tuple:
    repo, base, head, paths = _seed(tmp_path, dirty, committed)
    seen = _spawns(monkeypatch)
    reply = rp.review_prep(
        repo, base=base, paths=paths, slice_prefix="budget-prep", plan_path="docs/plans/p.md",
        session_id=SESSION, head=head,
    )
    monkeypatch.undo()
    return reply, len(seen), paths


@pytest.mark.parametrize("committed", [False, True], ids=["worktree", "range"])
def test_spawn_count_is_the_same_for_10_and_500_dirty_files(tmp_path, monkeypatch, committed):
    for name in ("small", "large"):
        (tmp_path / name).mkdir()
    small_reply, small, _ = _run(tmp_path / "small", monkeypatch, 10, committed)
    large_reply, large, _ = _run(tmp_path / "large", monkeypatch, 500, committed)
    assert small_reply["error"] is None and large_reply["error"] is None
    assert large_reply["verdict"] == "PARTITION-MANDATORY" and len(large_reply["slices"]) > 1
    assert large == small


def test_each_slice_diff_holds_exactly_its_own_files_of_the_whole_diff(tmp_path, monkeypatch):
    reply, _, paths = _run(tmp_path, monkeypatch, 60, False)
    assert reply["error"] is None and len(reply["slices"]) > 1
    header = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)
    whole = header.findall(Path(reply["whole_diff_path"]).read_text(encoding="utf-8"))
    cut: list = []
    for s in reply["slices"]:
        files = header.findall(Path(s["diff_path"]).read_text(encoding="utf-8"))
        assert sorted(files) == sorted(s["files"])
        cut += files
    assert sorted(cut) == sorted(whole) == sorted(paths)
