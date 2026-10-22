"""coordinator/bin/tests/test_census_revalidate_inproc.py -- `revalidate_set` answers GRAMMAR
commands in-process (`census_inproc.evaluate_many` over `_TreeView`) before it batches the rest.

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


def _decline_all(monkeypatch, mcr):
    monkeypatch.setattr(
        mcr._inproc(), "evaluate_many",
        lambda commands, files, store, read_budget_s, force_first=True: {c: None for c in commands},
    )


def test_all_grammar_set_spawns_no_command_and_matches_the_declined_states(
    mcr, repo, popens, monkeypatch
):
    plans = [_plan(repo, "p1", GRAMMAR_ENTRIES)]
    reply = _run(mcr, repo, plans)
    assert [a[0][-2:] for a in popens] == [["cat-file", "--batch"]], "one blob batch, no command"
    assert reply["spawns"] == 1
    assert reply["inproc_runs"] == 3
    _decline_all(monkeypatch, mcr)
    spawned = _run(mcr, repo, plans)
    assert spawned["inproc_runs"] == 0
    assert spawned["spawns"] >= 1
    assert _states(reply) == _states(spawned)
    assert all(s == "MATCH" for s in _states(reply)["p1.md"])


def test_in_process_answers_are_stored_under_the_in_process_run_marker(mcr, repo):
    store = CensusStore()
    first = _run(mcr, repo, [_plan(repo, "p1", GRAMMAR_ENTRIES)], store)
    marker = mcr._inproc_run_ms()
    for command, _ in GRAMMAR_ENTRIES:
        stored = store.run(command)
        assert stored is not None and stored[2] == marker and stored[0] is not None
    assert store.median_run_ms() is None, "an in-process run is not a spawn cost"
    again = _run(mcr, repo, [_plan(repo, "p1", GRAMMAR_ENTRIES)], store)
    assert again["memo_hits"] == 3 and again["inproc_runs"] == 0 and again["spawns"] == 0
    assert _states(again) == _states(first)


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


def test_second_call_over_unchanged_index_reads_no_blob_and_a_restaged_file_reads_one(
    mcr, repo, monkeypatch
):
    plans = [_plan(repo, "p1", GRAMMAR_ENTRIES)]
    store = CensusStore()
    first = _run(mcr, repo, plans, store)
    fetched: list[list[str]] = []
    real = mcr._TreeView.read_blobs

    def counting(self, oids):
        fetched.append(list(oids))
        return real(self, oids)

    monkeypatch.setattr(mcr._TreeView, "read_blobs", counting)
    assert first["inproc_runs"] == 3
    _run(mcr, repo, plans, store)
    assert fetched == []
    (repo / "d" / "b.txt").write_text("needle\nneedle\n", encoding="utf-8")
    _git(["add", "d/b.txt"], repo)
    _run(mcr, repo, plans, store)
    assert sum(len(f) for f in fetched) <= 3, "only the restaged blob is read, for its patterns"
    assert fetched


def test_tree_view_lists_tracked_paths_with_modes_and_never_a_worktree_file(mcr, repo):
    (repo / "d" / "link").symlink_to("a.txt")
    (repo / "d" / "untracked.txt").write_text("needle\n", encoding="utf-8")
    _git(["add", "d/link"], repo)
    view = mcr._TreeView(repo)
    listed = {p: mode for p, _oid, mode in view.files("d")}
    assert listed == {"d/a.txt": "100644", "d/b.txt": "100644", "d/link": "120000"}
    assert view.files("nowhere") == []
    assert view.files("f.txt")[0][0] == "f.txt"
    assert view.files("d") is view.files("d"), "one listing per directory per call"
