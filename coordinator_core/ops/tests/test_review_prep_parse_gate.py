"""review.prep refuses a declared .py path that fails ast.parse, before any git spawn, lock or sidecar."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
BROKEN = "rung = 1 if rung not in RUNGS:\n    pass\n"


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


def _call(repo: Path, paths: list[str], **kw) -> dict:
    base = _git(repo, "rev-parse", "HEAD").strip()
    return rp.review_prep(
        repo, base=base, paths=paths, slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, **kw,
    )


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("process spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def test_unparseable_py_is_refused_naming_path_and_line_with_no_spawn(repo, monkeypatch):
    (repo / "bad.py").write_text(BROKEN, encoding="utf-8")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _no_spawn(monkeypatch)
    reply = rp.review_prep(
        repo, base=base, paths=["bad.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION,
    )
    assert reply["error"].startswith("review.prep refused: py-unparseable:")
    assert "bad.py" in reply["error"] and "line 1" in reply["error"]
    assert reply["slices"] == []
    assert not list(repo.rglob("*.review-prep.*"))


def _commit_file(repo: Path, rel: str, text: str) -> str:
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"add {rel}")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_range_mode_refuses_a_broken_head_blob_over_a_clean_worktree(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    head = _commit_file(repo, "m.py", BROKEN)
    (repo / "m.py").write_text("v = 1\n", encoding="utf-8")
    reply = rp.review_prep(
        repo, base=base, paths=["m.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, head=head,
    )
    assert reply["error"].startswith("review.prep refused: py-unparseable:")
    assert "m.py" in reply["error"] and "line 1" in reply["error"]
    assert not list(repo.rglob("*.review-prep.*"))


def test_range_mode_passes_a_clean_head_blob_over_a_broken_worktree(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    head = _commit_file(repo, "m.py", "v = 1\n")
    (repo / "m.py").write_text(BROKEN, encoding="utf-8")
    reply = rp.review_prep(
        repo, base=base, paths=["m.py", "absent.py"], slice_prefix="prep-t", plan_path="docs/plans/p.md",
        session_id=SESSION, head=head,
    )
    assert not (reply["error"] or "").startswith("review.prep refused: py-unparseable")


def test_clean_py_md_and_deleted_paths_pass(repo):
    (repo / "ok.py").write_text("v = 1\n", encoding="utf-8")
    (repo / "notes.md").write_text("rung = 1 if (\n", encoding="utf-8")
    reply = _call(repo, ["ok.py", "notes.md", "gone.py"])
    assert not (reply["error"] or "").startswith("review.prep refused: py-unparseable")
