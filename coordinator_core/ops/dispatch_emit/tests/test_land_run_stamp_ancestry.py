"""dispatch.land_run default path: the live stamp source is merged first, or the land refuses typed."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git.run import run_git as real_run_git
from coordinator_core.ops.dispatch_emit import land_stamp_ancestry
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401  (repo is a fixture)
    _author,
    _declare,
    _git,
    _land,
    _record_dir,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _sha(root: Path, rev: str = "HEAD") -> str:
    return _git(["rev-parse", rev], root).strip()


def _is_ancestor(root: Path, ancestor: str) -> bool:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, "HEAD"], cwd=str(root), **no_console_creationflags()
    ).returncode == 0


def _stamp_commit(root: Path, rel: str, text: str) -> str:
    """A commit on a side branch off HEAD, so the working branch lacks it; returns its sha."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root).strip()
    _git(["checkout", "-q", "-b", "stamp-side"], root)
    _write(root, rel, text)
    _git(["add", rel], root)
    _git(["commit", "-q", "-m", "stamp source"], root)
    stamp = _sha(root)
    _git(["checkout", "-q", branch], root)
    return stamp


def _advance_branch(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    _git(["add", rel], root)
    _git(["commit", "-q", "-m", "branch work"], root)


def _pin_stamp(monkeypatch: pytest.MonkeyPatch, sha) -> None:
    monkeypatch.setattr(land_stamp_ancestry, "live_stamp_sha", lambda: sha)


def _ready(root: Path) -> None:
    _author(root, "src/a.py", "a = 1\n")
    _declare(root, "src/a.py")
    _record_dir(root)


def test_behind_and_clean_merges_the_stamp_first_and_reports_it(repo, monkeypatch) -> None:
    stamp = _stamp_commit(repo, "stamp.txt", "from the stamp\n")
    _advance_branch(repo, "branch.txt", "branch only\n")
    _pin_stamp(monkeypatch, stamp)
    _ready(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert _is_ancestor(repo, stamp)
    merge = out["stamp_merge"]
    assert merge["merged"] is True and merge["sha"] == stamp
    assert merge["merge_commit"] == _sha(repo, "HEAD~1")
    assert "src/a.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert (repo / "stamp.txt").read_text(encoding="utf-8") == "from the stamp\n"
    assert not out.get("advisories")


def test_conflict_refuses_typed_and_leaves_head_worktree_and_index_alone(repo, monkeypatch) -> None:
    stamp = _stamp_commit(repo, "README.md", "stamp side\n")
    _advance_branch(repo, "README.md", "branch side\n")
    _pin_stamp(monkeypatch, stamp)
    _ready(repo)
    head = _sha(repo)
    readme = (repo / "README.md").read_bytes()
    index = _git(["ls-files", "-s"], repo)

    out = _land(repo)

    assert out["committed"] is False and out["refused"] == "stamp-merge-conflict", out
    assert out["stamp_sha"] == stamp and out["remedy"] == f"git merge {stamp}"
    assert stamp in out["error"] and f"git merge {stamp}" in out["error"]
    assert _sha(repo) == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "README.md").read_bytes() == readme
    assert _git(["ls-files", "-s"], repo) == index
    assert "stamp_merge" not in out


def test_current_branch_spends_one_gate_spawn_and_reports_nothing(repo, monkeypatch) -> None:
    _pin_stamp(monkeypatch, _sha(repo))
    calls: list = []

    def counting(args, **kw):
        calls.append(list(args))
        return real_run_git(args, **kw)

    monkeypatch.setattr(land_stamp_ancestry, "run_git", counting)
    _ready(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert len(calls) == 1 and "--is-ancestor" in calls[0], calls
    assert "stamp_merge" not in out and not out.get("advisories")


@pytest.mark.parametrize("stamp", ["f" * 40, None])
def test_an_unresolvable_or_foreign_stamp_lands_with_the_advisory(repo, monkeypatch, stamp) -> None:
    _pin_stamp(monkeypatch, stamp)
    _ready(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert any(a.startswith("stamp-ancestry-unknown") for a in out["advisories"]), out.get("advisories")
    assert "stamp_merge" not in out


def test_a_refusal_after_the_merge_still_reports_the_merge(repo, monkeypatch) -> None:
    stamp = _stamp_commit(repo, "stamp.txt", "from the stamp\n")
    _advance_branch(repo, "branch.txt", "branch only\n")
    _pin_stamp(monkeypatch, stamp)
    _ready(repo)
    _write(repo, "src/a.py", "a = 2  # edited after the session's last recorded write\n")

    out = _land(repo)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert out["stamp_merge"]["merged"] is True and out["stamp_merge"]["sha"] == stamp
    assert _is_ancestor(repo, stamp)
