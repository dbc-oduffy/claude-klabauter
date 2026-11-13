"""Unit tests for atlas_catchup: watermark, one git log spawn, pending repairs, ACTED facts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography import atlas_catchup as ac
from coordinator_core.cartography.atlas_freshness import read_acted
from coordinator_core.ops import check_atlas_watch_drift

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# File index

## Directory → system

### cartography — atlas tooling

`coordinator_core/cartography/`
"""
_PAGE = "docs/architecture/systems/cartography.md"
_PAGE_TEXT = "---\nsystem: cartography\nfiles: 7\nlines: 10\n---\n\n# cartography\n"
_NEW = "coordinator_core/cartography/new_mod.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_NO_WINDOW
    ).stdout.strip()


def _commit(repo: Path, rel: str, text: str = "x = 1\n") -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"touch {rel}")
    return _git(repo, "rev-parse", "HEAD")


def _remove(repo: Path, rel: str) -> str:
    _git(repo, "rm", "-q", rel)
    _git(repo, "commit", "-q", "-m", f"rm {rel}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "docs/architecture/systems").mkdir(parents=True)
    (root / "docs/architecture/file-index.md").write_text(_FILE_INDEX, encoding="utf-8")
    (root / _PAGE).write_text(_PAGE_TEXT, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "atlas")
    return root


@pytest.fixture
def spawns(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = ac.run_git

    def _counting(args, *a, **k):
        calls.append(["git", *args])
        return real(args, *a, **k)

    monkeypatch.setattr(ac, "run_git", _counting)
    return calls


@pytest.fixture
def no_spawn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(ac, "run_git", _boom)


def _seed(repo: Path, folded: list[str] | None = None, sha: str | None = None) -> None:
    """Write a watermark with file reads only, so it can run under `no_spawn`."""
    ac._write_watermark(repo, sha or ac._read_head(repo), folded or [])


def test_no_atlas_writes_nothing_and_spawns_nothing(tmp_path, no_spawn):
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    (bare / ".git/HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    res = ac.catch_up(bare)
    assert (res.ran, res.spawns) == (False, 0)
    assert not (bare / ".coordinator-local").exists()


def test_no_atlas_settle_commit_writes_nothing(tmp_path):
    ac.settle_commit(tmp_path, [_PAGE], "a" * 40, "b" * 40)
    assert not (tmp_path / ".coordinator-local").exists()


def test_first_run_seeds_the_watermark_at_head_without_a_spawn(repo, no_spawn):
    head = (repo / ".git/refs/heads/main").read_text(encoding="utf-8").strip()
    res = ac.catch_up(repo)
    assert (res.ran, res.spawns, res.reason) == (False, 0, "seeded")
    assert json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["sha"] == head
    assert not (repo / ac.PENDING_RELPATH).exists()


def test_steady_state_spawns_nothing(repo, no_spawn):
    _seed(repo)
    res = ac.catch_up(repo)
    assert (res.ran, res.spawns, res.reason) == (False, 0, "up-to-date")


def test_moved_head_runs_exactly_one_spawn_and_queues_the_repair(repo, spawns):
    _seed(repo)
    head = _commit(repo, _NEW)
    spawns.clear()
    res = ac.catch_up(repo)
    assert len(spawns) == 1 and spawns[0][:4] == ["git", "-c", "core.quotepath=off", "log"]
    assert (res.ran, res.spawns, res.pending) == (True, 1, 1)
    assert ac.read_pending(repo) == {_PAGE: 1}
    fact = read_acted(repo)[_PAGE]
    assert (fact.kind, fact.sha) == ("repair_pending", head)
    assert json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["sha"] == head
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_second_catch_up_after_a_fold_is_a_no_op(repo, monkeypatch):
    _seed(repo)
    head = _commit(repo, _NEW)
    ac.catch_up(repo)
    monkeypatch.setattr(ac, "run_git", lambda *a, **k: pytest.fail("spawned"))
    res = ac.catch_up(repo)
    assert (res.ran, res.spawns) == (False, 0)
    assert json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["sha"] == head


def test_a_folded_commit_is_skipped(repo):
    _seed(repo)
    head = _commit(repo, _NEW)
    _seed(repo, folded=[head], sha=_git(repo, "rev-parse", "HEAD~1"))
    res = ac.catch_up(repo)
    assert res.ran and res.pending == 0
    assert ac.read_pending(repo) == {}
    assert read_acted(repo) == {}


def test_pending_sums_across_runs(repo):
    _seed(repo)
    _commit(repo, _NEW)
    ac.catch_up(repo)
    _commit(repo, "coordinator_core/cartography/second_mod.py")
    ac.catch_up(repo)
    assert ac.read_pending(repo) == {_PAGE: 2}


def test_add_then_delete_in_one_range_nets_to_nothing(repo):
    _seed(repo)
    _commit(repo, _NEW)
    _remove(repo, _NEW)
    res = ac.catch_up(repo)
    assert res.ran and res.pending == 0
    assert ac.read_pending(repo) == {}


def test_delete_of_an_existing_covered_file_is_minus_one(repo):
    old = "coordinator_core/cartography/old_mod.py"
    _commit(repo, old)
    _seed(repo)
    _remove(repo, old)
    ac.catch_up(repo)
    assert ac.read_pending(repo) == {_PAGE: -1}


def test_modification_is_a_content_drift_fact_with_no_pending(repo):
    existing = "coordinator_core/cartography/mod.py"
    _commit(repo, existing)
    _seed(repo)
    _commit(repo, existing, "x = 2\n")
    ac.catch_up(repo)
    assert ac.read_pending(repo) == {}
    assert read_acted(repo)[_PAGE].kind == "content_drift"


def test_unknown_watermark_reseeds_and_records_a_fact(repo, spawns):
    _seed(repo, sha="f" * 40)
    head = _commit(repo, _NEW)
    res = ac.catch_up(repo)
    assert (res.ran, res.reason) == (False, "reseeded")
    assert read_acted(repo)["docs/architecture/file-index.md"].kind == "catch_up_reseeded"
    assert json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["sha"] == head
    assert ac.read_pending(repo) == {}


def test_range_cap_records_one_capped_fact_and_advances(repo, monkeypatch):
    monkeypatch.setattr(ac, "CATCHUP_RANGE_CAP", 2)
    _seed(repo)
    for i in range(3):
        head = _commit(repo, f"coordinator_core/cartography/m{i}.py")
    ac.catch_up(repo)
    assert read_acted(repo)["docs/architecture/file-index.md"].kind == "catch_up_capped"
    assert ac.read_pending(repo) == {_PAGE: 2}
    assert json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["sha"] == head


def test_check_atlas_watch_drift_prints_acted_for_the_page(repo, monkeypatch, capsys):
    _seed(repo)
    _commit(repo, _NEW)
    ac.catch_up(repo)
    monkeypatch.chdir(repo)
    assert check_atlas_watch_drift.main([]) == 0
    assert "ACTED cartography " in capsys.readouterr().out


def test_head_resolves_from_packed_refs(repo):
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "pack-refs", "--all")
    assert not (repo / ".git/refs/heads/main").exists()
    assert ac._read_head(repo) == head


def test_head_resolves_through_a_worktree_gitfile(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert (wt / ".git").is_file()
    assert ac._read_head(wt) == _git(wt, "rev-parse", "HEAD")


def test_detached_head_resolves(repo):
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach")
    assert ac._read_head(repo) == head


def test_settle_commit_drops_applied_pages_and_advances(repo):
    parent = _git(repo, "rev-parse", "HEAD")
    _seed(repo)
    ac._write_json(repo / ac.PENDING_RELPATH, {_PAGE: 1, "other.md": 2})
    sha = "c" * 40
    ac.settle_commit(repo, [_PAGE], sha, parent)
    assert ac.read_pending(repo) == {"other.md": 2}
    wm = json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))
    assert wm == {"sha": sha, "folded": [sha]}


def test_settle_commit_keeps_the_watermark_when_parent_differs(repo):
    _seed(repo)
    before = _git(repo, "rev-parse", "HEAD")
    ac.settle_commit(repo, [], "c" * 40, "d" * 40)
    wm = json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))
    assert wm == {"sha": before, "folded": ["c" * 40]}


def test_settle_commit_caps_the_folded_list(repo):
    _seed(repo, folded=[f"{i:040x}" for i in range(ac.FOLDED_CAP)])
    ac.settle_commit(repo, [], "e" * 40, "d" * 40)
    folded = json.loads((repo / ac.WATERMARK_RELPATH).read_text(encoding="utf-8"))["folded"]
    assert len(folded) == ac.FOLDED_CAP and folded[-1] == "e" * 40


def test_read_pending_tolerates_garbage(repo):
    path = repo / ac.PENDING_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert ac.read_pending(repo) == {}


def test_idle_tick_throttles_per_repo(repo, monkeypatch):
    ac._next_tick.pop(repo.resolve(), None)
    clock = [1000.0]
    monkeypatch.setattr(ac.time, "monotonic", lambda: clock[0])
    _seed(repo)
    _commit(repo, _NEW)
    assert ac.on_idle_tick([repo]) is True
    _commit(repo, "coordinator_core/cartography/again.py")
    assert ac.on_idle_tick([repo]) is False
    clock[0] += ac.CATCHUP_INTERVAL_SECS + 1
    assert ac.on_idle_tick([repo]) is True
    assert ac.read_pending(repo) == {_PAGE: 2}


def test_idle_tick_records_fold_error_and_never_raises(repo, monkeypatch):
    ac._next_tick.pop(repo.resolve(), None)
    monkeypatch.setattr(ac, "catch_up", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ac.on_idle_tick([repo]) is False
    assert read_acted(repo)["docs/architecture/file-index.md"].kind == "fold_error"
