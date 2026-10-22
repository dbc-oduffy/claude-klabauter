"""coordinator/bin/tests/test_census_revalidate_set.py -- `revalidate_set`, the store-backed set pass.

Subject: `revalidate_set` and the store-backed `_TreeView` in `coordinator/bin/mise-census-revalidate.py`.
Repos are built under tmp_path with `git init`; spawns are counted at `_spawn`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.roadmap.census_inproc import evaluate as _REAL_EVALUATE
from coordinator_core.roadmap.census_store import CensusStore, store_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_set", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "f.txt").write_text("alpha\nalpha\n", encoding="utf-8")
    (root / "g.txt").write_text("alpha\n", encoding="utf-8")
    (root / "d").mkdir()
    (root / "d" / "a.txt").write_text("needle\n", encoding="utf-8")
    (root / "d" / "b.txt").write_text("needle\n", encoding="utf-8")
    _git(["add", "f.txt", "g.txt", "d/a.txt", "d/b.txt"], root)
    _git(["commit", "-qm", "seed"], root)
    return root


def _plan(root: Path, name: str, entries: list[tuple[str, str]]) -> Path:
    lines = ["---", "census:"]
    for command, result in entries:
        lines += [f"  - question: q", f"    command: {command!r}", f"    result: {result!r}"]
    lines += ["---", "", "body", ""]
    path = root / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _store(repo: Path) -> CensusStore:
    store = CensusStore.load(repo)
    assert str(repo.parent) in str(store_path(repo)), "the store must live under tmp_path"
    return store


@pytest.fixture
def spawns(mcr, monkeypatch):
    calls: list[str] = []
    real = mcr._spawn

    def counting(command, repo_root, timeout):
        calls.append(command)
        return real(command, repo_root, timeout)

    monkeypatch.setattr(mcr, "_spawn", counting)
    return calls


@pytest.fixture(autouse=True)
def _spawn_path(mcr, monkeypatch):
    """The arms in this file pin the spawn and memo path, so in-process evaluation declines."""
    monkeypatch.setattr(mcr._inproc(), "evaluate", lambda command, files, store: None)


def test_inproc_runs_counts_the_in_process_entries(mcr, repo, monkeypatch):
    monkeypatch.setattr(mcr._inproc(), "evaluate", _REAL_EVALUATE)
    monkeypatch.setattr(mcr, "_spawn", lambda *a: pytest.fail("spawned"))
    plans = [_plan(repo, "p1", [("grep -c alpha f.txt", "2"), ("grep -c needle d/a.txt", "1")]),
             _plan(repo, "p2", [("grep -c alpha f.txt", "2")])]
    reply = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=_store(repo))
    assert reply["inproc_runs"] == 3
    assert reply["spawns"] == 0
    assert all(s == ["MATCH"] * len(s) for s in _states(reply).values())


def _far() -> float:
    return time.monotonic() + 600


def _states(reply: dict) -> dict:
    return {p: [e["state"] for e in r["entries"]] for p, r in reply["plans"].items()}


def test_second_call_over_unchanged_tree_spawns_nothing(mcr, repo, spawns):
    plans = [_plan(repo, "p1", [("grep -c alpha f.txt", "2"), ("grep -c needle d/a.txt", "1")]),
             _plan(repo, "p2", [("grep -c alpha g.txt", "1")])]
    store = _store(repo)
    first = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    assert len(spawns) == 3
    spawns.clear()
    second = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    assert spawns == []
    assert _states(second) == _states(first)
    assert second["memo_hits"] == 3
    assert second["covered"] == second["census_plans"] == 2


def test_reloaded_store_gives_a_zero_spawn_second_call(mcr, repo, spawns):
    plans = [_plan(repo, "p1", [("grep -c alpha f.txt", "2"), ("grep -rn needle d", "2")])]
    mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=_store(repo))
    spawns.clear()
    again = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=_store(repo))
    assert spawns == []
    assert again["memo_hits"] == 2


def test_operand_edit_respawns_only_the_entries_reading_it(mcr, repo, spawns):
    plans = [_plan(repo, "p1", [("grep -c alpha f.txt", "2"), ("grep -c alpha g.txt", "1")])]
    store = _store(repo)
    mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    spawns.clear()
    (repo / "g.txt").write_text("alpha\nalpha\n", encoding="utf-8")
    reply = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    assert spawns == ["grep -c alpha g.txt"]
    assert [e["observed"] for e in reply["plans"]["p1.md"]["entries"]] == ["2", "2"]


def test_cheap_plan_is_covered_and_dear_plan_is_not_whatever_the_input_order(
    mcr, repo, spawns, monkeypatch
):
    store = CensusStore(repo / ".coordinator-local" / "cache" / "census-store.json")
    store.put_run("grep -c alpha f.txt", None, (0, "2\n", ""), 5000, None)
    store.put_run("grep -c alpha g.txt", None, (0, "1\n", ""), 5, None)
    dear = _plan(repo, "dear", [("grep -c alpha f.txt", "2")])
    cheap = _plan(repo, "cheap", [("grep -c alpha g.txt", "1")])
    budget = iter([1])
    monkeypatch.setattr(
        mcr, "_remaining_timeout", lambda timeout, deadline: next(budget, None)
    )
    reply = mcr.revalidate_set([dear, cheap], repo, 30, deadline=_far(), head="H1", store=store)
    assert spawns == ["grep -c alpha g.txt"]
    assert _states(reply) == {"dear.md": ["UNRUNNABLE"], "cheap.md": ["MATCH"]}
    row = reply["plans"]["dear.md"]["entries"][0]
    assert row["detail"] == "not run: set census budget spent"
    assert reply["pending"] == ["dear.md"]
    assert (reply["covered"], reply["census_plans"]) == (1, 2)
    assert reply["order"] == "measured"
    assert store.run("grep -c alpha f.txt")[2] == 5000


def test_store_hits_are_judged_after_the_budget_is_spent(mcr, repo, spawns):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")]),
             _plan(repo, "b", [("grep -c alpha g.txt", "1")])]
    store = _store(repo)
    mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    spawns.clear()
    (repo / "f.txt").write_text("alpha\nalpha\nalpha\n", encoding="utf-8")
    reply = mcr.revalidate_set(plans, repo, 30, deadline=time.monotonic() - 5, head="H1",
                               store=store)
    assert spawns == []
    assert _states(reply) == {"a.md": ["UNRUNNABLE"], "b.md": ["MATCH"]}


def test_directory_operand_is_walked_once_per_call(mcr, repo, monkeypatch):
    plans = [_plan(repo, "a", [("grep -rln needle d", "2")]),
             _plan(repo, "b", [("grep -rn needle d", "2")])]
    walked: list[str] = []
    real = os.scandir

    def counting(path):
        walked.append(str(path))
        return real(path)

    monkeypatch.setattr(mcr.os, "scandir", counting)
    mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=_store(repo))
    assert [w for w in walked if w.endswith("d")] == [str(repo / "d")]


def test_unchanged_stat_is_not_rehashed_and_a_touched_file_is(mcr, repo):
    import coordinator_core.git.commit as commit

    plans = [_plan(repo, "a", [("grep -c alpha g.txt", "1")])]
    store = _store(repo)
    hashed: list[str] = []
    real = commit.checkin_blob_sha

    def counting(root, rel):
        hashed.append(rel)
        return real(root, rel)

    commit.checkin_blob_sha = counting
    try:
        # Make g.txt stat-stale against the index so the hash path is taken.
        st = (repo / "g.txt").stat()
        os.utime(repo / "g.txt", ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))
        mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
        assert hashed == ["g.txt"]
        hashed.clear()
        mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
        assert hashed == []
        st = (repo / "g.txt").stat()
        os.utime(repo / "g.txt", ns=(st.st_atime_ns, st.st_mtime_ns - 3_000_000_000))
        mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
        assert hashed == ["g.txt"]
    finally:
        commit.checkin_blob_sha = real


def test_timeout_and_deadline_rows_are_never_stored(mcr, repo, monkeypatch):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")])]
    store = _store(repo)
    monkeypatch.setattr(
        mcr, "_spawn", lambda command, root, timeout: {"state": mcr.UNRUNNABLE, "detail": "t"}
    )
    reply = mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store)
    assert _states(reply) == {"a.md": ["UNRUNNABLE"]}
    assert store.run("grep -c alpha f.txt") is None
    spent = mcr.revalidate_set(plans, repo, 30, deadline=time.monotonic() - 5, head="H1",
                               store=store)
    assert _states(spent) == {"a.md": ["UNRUNNABLE"]}
    assert store.run("grep -c alpha f.txt") is None


def test_order_reads_input_over_an_empty_store_and_measured_once_costs_exist(mcr, repo):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")])]
    store = _store(repo)
    assert mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1",
                              store=store)["order"] == "input"
    (repo / "f.txt").write_text("alpha\n", encoding="utf-8")
    assert mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1",
                              store=store)["order"] == "measured"
