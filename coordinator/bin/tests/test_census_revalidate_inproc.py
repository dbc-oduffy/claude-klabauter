"""coordinator/bin/tests/test_census_revalidate_inproc.py -- `revalidate_set` answers GRAMMAR
commands in-process (`census_inproc.evaluate` over `_TreeView`) before it batches the rest.

Repos are built under tmp_path with `git init`; spawns are counted at `subprocess.Popen`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.roadmap.census_store import CensusStore
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_inproc", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    (root / "d").mkdir()
    (root / "d" / "a.txt").write_text("needle\nhay\n", encoding="utf-8")
    (root / "d" / "b.txt").write_text("needle\n", encoding="utf-8")
    (root / "f.txt").write_text("alpha\nalpha\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-qm", "seed"], root)
    return root


def _plan(root: Path, name: str, entries: list[tuple[str, str]]) -> Path:
    lines = ["---", "census:"]
    for command, result in entries:
        lines += ["  - question: q", f"    command: {command!r}", f"    result: {result!r}"]
    lines += ["---", "", "body", ""]
    path = root / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _states(reply: dict) -> dict:
    return {p: [e["state"] for e in r["entries"]] for p, r in reply["plans"].items()}


def _run(mcr, repo, plans, store=None):
    return mcr.revalidate_set(
        plans, repo, 30, deadline=time.monotonic() + 600, head="H1",
        store=store if store is not None else CensusStore(),
    )


@pytest.fixture
def popens(monkeypatch):
    calls: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return calls


GRAMMAR_ENTRIES = [
    ("grep -rl needle d | wc -l", "2"),
    ("grep -c alpha f.txt", "2"),
    ("find d -name '*.txt' | wc -l", "2"),
]


def test_all_grammar_set_spawns_nothing_and_matches_the_declined_states(
    mcr, repo, popens, monkeypatch
):
    plans = [_plan(repo, "p1", GRAMMAR_ENTRIES)]
    reply = _run(mcr, repo, plans)
    assert popens == []
    assert reply["spawns"] == 0
    assert reply["inproc_runs"] == 3
    monkeypatch.setattr(mcr._inproc(), "evaluate", lambda command, files, store: None)
    spawned = _run(mcr, repo, plans)
    assert spawned["inproc_runs"] == 0
    assert spawned["spawns"] >= 1
    assert _states(reply) == _states(spawned)
    assert all(s == "MATCH" for s in _states(reply)["p1.md"])


def test_in_process_answers_are_never_stored_as_runs(mcr, repo):
    store = CensusStore()
    _run(mcr, repo, [_plan(repo, "p1", GRAMMAR_ENTRIES)], store)
    assert all(store.run(c) is None for c, _ in GRAMMAR_ENTRIES)


def test_mixed_set_spawns_one_batch_for_the_declined_commands(mcr, repo, monkeypatch):
    batches: list[list[str]] = []
    real = mcr._spawn_many

    def counting(commands, repo_root, timeout, cost):
        batches.append(list(commands))
        return real(commands, repo_root, timeout, cost)

    monkeypatch.setattr(mcr, "_spawn_many", counting)
    declined = ["cat f.txt | wc -l", "cat d/b.txt | wc -l"]
    plans = [_plan(repo, "p1", [GRAMMAR_ENTRIES[0], (declined[0], "2")]),
             _plan(repo, "p2", [GRAMMAR_ENTRIES[1], (declined[1], "1")])]
    reply = _run(mcr, repo, plans)
    assert batches == [declined]
    assert reply["inproc_runs"] == 2
    assert reply["covered"] == reply["census_plans"] == 2


def test_second_call_over_unchanged_tree_reads_no_file_bodies(mcr, repo, monkeypatch):
    plans = [_plan(repo, "p1", GRAMMAR_ENTRIES)]
    store = CensusStore()
    first = _run(mcr, repo, plans, store)
    reads: list[str] = []
    real = mcr._TreeView.read

    def counting(self, rel):
        reads.append(rel)
        return real(self, rel)

    monkeypatch.setattr(mcr._TreeView, "read", counting)
    assert first["inproc_runs"] == 3
    time.sleep(0.01)
    _run(mcr, repo, plans, store)
    # the racy-clean rule distrusts records stamped within the file's mtime tick; a rewrite of
    # the store after the tree settled makes them trusted
    reads.clear()
    _run(mcr, repo, plans, store)
    assert reads == []
    (repo / "d" / "b.txt").write_text("needle\nneedle\n", encoding="utf-8")
    _run(mcr, repo, plans, store)
    assert set(reads) <= {"d/b.txt"}


def test_tree_view_walk_skips_git_and_symlinks_and_shares_the_listing(mcr, repo, monkeypatch):
    (repo / "d" / "link").symlink_to("a.txt")
    view = mcr._TreeView(repo)
    assert sorted(rel for rel, _ in view.walk("d")) == ["d/a.txt", "d/b.txt"]
    assert [rel for rel, _ in view.walk("f.txt")] == ["f.txt"]
    assert view.walk("nowhere") is None
    assert all(not rel.startswith(".git") for rel, _ in view.walk("."))
    scans: list = []
    real = mcr.os.scandir
    monkeypatch.setattr(mcr.os, "scandir", lambda p: scans.append(p) or real(p))
    assert view.dir_id("d") is not None
    view.walk("d")
    assert scans == []
