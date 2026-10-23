"""review.prep in worktree mode re-freezes when a declared path moves after a checkpoint commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-ckpt"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _call(repo: Path, base: str, paths: list[str]) -> dict:
    return rp.review_prep(
        repo, base=base, paths=paths, slice_prefix=f"p-run1-{base[:12]}-prep", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )


def _checkpoint(repo: Path, rel: str, text: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"checkpoint {rel}")


def test_a_checkpoint_commit_after_an_empty_freeze_is_frozen_on_the_next_call(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    first = _call(repo, base, ["a/f.py"])
    assert first["product_files"] == 0

    _checkpoint(repo, "a/f.py", "v = 1\n" * 3)
    second = _call(repo, base, ["a/f.py"])
    assert second["error"] is None
    assert second["product_files"] >= 1
    assert [s["files"] for s in second["slices"]] == [["a/f.py"]]


def test_a_declared_path_written_later_is_picked_up_beside_an_already_frozen_one(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _checkpoint(repo, "a/one.py", "v = 1\n" * 3)
    first = _call(repo, base, ["a/one.py", "b/two.py"])
    assert first["product_files"] == 1

    _checkpoint(repo, "b/two.py", "v = 1\n" * 3)
    second = _call(repo, base, ["a/one.py", "b/two.py"])
    assert second["product_files"] == 2
    assert sorted(f for s in second["slices"] for f in s["files"]) == ["a/one.py", "b/two.py"]
