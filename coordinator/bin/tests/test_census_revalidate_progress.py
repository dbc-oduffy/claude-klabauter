"""coordinator/bin/tests/test_census_revalidate_progress.py -- the progress contract of `revalidate_set`.

Subject: the budget-share gate, the progress floor, the cursor and the over-bar classification of
`revalidate_set` in `coordinator/bin/mise-census-revalidate.py`. A call whose budget is spent
before it starts is forced with a patched `_LegCost.total_s`; repos are built under tmp_path with
`git init`.
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
SPENT = 99.0


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_progress", TOOL)
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
    (root / "docs" / "plans").mkdir(parents=True)
    _git(["add", "f.txt", "g.txt", "d/a.txt"], root)
    _git(["commit", "-qm", "seed"], root)
    return root


def _plan(root: Path, name: str, entries: list[tuple[str, str]]) -> Path:
    lines = ["---", "status: approved", "census:"]
    for command, result in entries:
        lines += ["  - question: q", f"    command: {command!r}", f"    result: {result!r}"]
    lines += ["---", "", "body", ""]
    path = root / "docs" / "plans" / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    time.sleep(0.02)
    return path


def _run(mcr, plans, repo, store, **kw):
    return mcr.revalidate_set(
        plans, repo, 30, deadline=time.monotonic() + 600, head="H1", store=store, **kw
    )


def _states(reply: dict) -> dict:
    return {p: [e["state"] for e in r["entries"]] for p, r in reply["plans"].items()}


def _decline_all(monkeypatch, mcr):
    monkeypatch.setattr(
        mcr._inproc(), "evaluate_many",
        lambda commands, files, store, read_budget_s, force_first=True: {c: None for c in commands},
    )


@pytest.fixture
def spawns(mcr, monkeypatch):
    calls: list[str] = []
    real = mcr._spawn

    def counting(command, repo_root, timeout):
        calls.append(command)
        return real(command, repo_root, timeout)

    monkeypatch.setattr(mcr, "_spawn", counting)
    return calls


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


def _spent_clock(monkeypatch, mcr):
    monkeypatch.setattr(mcr._LegCost, "total_s", lambda self: SPENT)


def test_a_call_whose_phase_one_is_over_the_bar_still_completes_and_persists_one_unit(
    mcr, repo, monkeypatch
):
    _spent_clock(monkeypatch, mcr)
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2")])
    store = CensusStore.load(repo)
    reply = _run(mcr, [plan], repo, store)
    assert _states(reply) == {"docs/plans/a.md": ["MATCH"]}
    assert reply["progressed"] >= 1
    from coordinator_core.roadmap import census_store

    census_store._SEEN.clear()
    assert CensusStore.load(repo).run("grep -c alpha f.txt") is not None, "the unit was saved"


def test_a_spent_budget_runs_exactly_one_spawned_unit_and_persists_it(
    mcr, repo, spawns, monkeypatch
):
    _spent_clock(monkeypatch, mcr)
    _decline_all(monkeypatch, mcr)
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2"), ("grep -c alpha g.txt", "1")])
    store = CensusStore.load(repo)
    reply = _run(mcr, [plan], repo, store)
    assert len(spawns) == 1
    assert sorted(_states(reply)["docs/plans/a.md"]) == ["MATCH", "UNRUNNABLE"]
    assert reply["pending"] == ["docs/plans/a.md"] and reply["progressed"] >= 1
    from coordinator_core.roadmap import census_store

    census_store._SEEN.clear()
    assert CensusStore.load(repo).run(spawns[0]) is not None


def test_k_calls_over_a_cold_store_raise_covered_strictly_until_full(mcr, repo, monkeypatch):
    _spent_clock(monkeypatch, mcr)
    plans = [
        _plan(repo, "a", [("grep -c alpha f.txt", "2")]),
        _plan(repo, "b", [("grep -c alpha g.txt", "1")]),
        _plan(repo, "c", [("grep -c needle d/a.txt", "1")]),
        _plan(repo, "d", [("wc -l f.txt", "2")]),
    ]
    store = CensusStore.load(repo)
    covered: list[int] = []
    for _ in range(len(plans)):
        reply = _run(mcr, plans, repo, store)
        covered.append(reply["covered"])
        assert reply["census_plans"] == len(plans)
    assert covered == [1, 2, 3, 4]
    assert _run(mcr, plans, repo, store)["pending"] == []


def test_the_cursor_moves_and_a_second_call_starts_where_the_first_stopped(
    mcr, repo, spawns, monkeypatch
):
    _spent_clock(monkeypatch, mcr)
    _decline_all(monkeypatch, mcr)
    commands = ["grep -c alpha f.txt", "grep -c alpha g.txt", "grep -c needle d/a.txt"]
    plan = _plan(repo, "a", [(commands[0], "2"), (commands[1], "1"), (commands[2], "1")])
    store = CensusStore.load(repo)
    moved = []
    for _ in commands:
        moved.append(_run(mcr, [plan], repo, store)["cursor_moved"])
    assert spawns == commands, "each call resumed after the command the last one finished"
    assert moved[0] is True
    assert store.cursor() is None
    final = _run(mcr, [plan], repo, store)
    assert _states(final) == {"docs/plans/a.md": ["MATCH"] * 3} and final["cursor_moved"] is False


def test_an_over_bar_stored_command_is_never_spawned_and_is_listed(
    mcr, repo, popens, monkeypatch
):
    _decline_all(monkeypatch, mcr)
    command = "grep -c alpha f.txt"
    plan = _plan(repo, "a", [(command, "2")])
    store = CensusStore.load(repo)
    store.put_run(command, None, (0, "2\n", ""), 5000, None)
    reply = _run(mcr, [plan], repo, store)
    assert popens == []
    assert reply["over_bar"] == [command]
    row = reply["plans"]["docs/plans/a.md"]["entries"][0]
    assert row["state"] == mcr.UNRUNNABLE and row["basis"] == mcr._OVER_BAR
    assert reply["pending"] == ["docs/plans/a.md"]


def test_a_never_run_sleeper_is_killed_at_the_budget_marked_and_not_rerun(
    mcr, repo, popens, monkeypatch
):
    _decline_all(monkeypatch, mcr)
    monkeypatch.setattr(mcr, "screen", lambda c, r=None, t=None: None)
    plan = _plan(repo, "a", [("sleep 5", "1")])
    store = CensusStore.load(repo)
    began = time.monotonic()
    first = _run(mcr, [plan], repo, store, process_budget_ms=400)
    assert time.monotonic() - began < 4
    assert first["over_bar"] == ["sleep 5"]
    assert store.over_bar("sleep 5") is not None
    row = first["plans"]["docs/plans/a.md"]["entries"][0]
    assert row["state"] == mcr.UNRUNNABLE and row["basis"] == mcr._OVER_BAR
    popens.clear()
    second = _run(mcr, [plan], repo, store, process_budget_ms=400)
    assert popens == [] and second["over_bar"] == ["sleep 5"]


def test_a_stored_run_clears_an_over_bar_marker(mcr, repo, monkeypatch):
    store = CensusStore()
    store.put_over_bar("c", 300)
    assert store.over_bar("c") == 300
    store.put_run("c", "k", (0, "1\n", ""), 10, 1)
    assert store.over_bar("c") is None


def test_a_gate_that_is_open_runs_every_fitting_command_in_one_call(mcr, repo, spawns, monkeypatch):
    _decline_all(monkeypatch, mcr)
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2"), ("grep -c alpha g.txt", "1")])
    reply = _run(mcr, [plan], repo, CensusStore.load(repo))
    assert sorted(spawns) == ["grep -c alpha f.txt", "grep -c alpha g.txt"]
    assert reply["spawns"] == 2 and reply["pending"] == []
    assert _states(reply) == {"docs/plans/a.md": ["MATCH", "MATCH"]}


def test_next_batch_skips_a_command_that_does_not_fit_unless_the_floor_takes_it(mcr):
    known = {"dear": (500, True), "a": (10, True)}

    def estimate(command):
        return known.get(command, (1, False))

    assert mcr._next_batch(["dear", "a"], 0, 100.0, estimate, floor=False) == ["a"]
    assert mcr._next_batch(["dear", "a"], 0, 100.0, estimate) == ["dear"]
