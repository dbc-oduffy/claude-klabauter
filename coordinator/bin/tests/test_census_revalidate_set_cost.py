"""coordinator/bin/tests/test_census_revalidate_set_cost.py -- the process cost of `revalidate_set`.

Subject: the per-call parse, screen, key and gate rules of `revalidate_set` in
`coordinator/bin/mise-census-revalidate.py`. Repos are built under tmp_path with `git init`; spawns
are counted at `_spawn`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.roadmap.census_inproc import evaluate_many as _REAL_EVALUATE_MANY
from coordinator_core.roadmap.census_store import CensusStore, store_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_set_cost", TOOL)
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
    lines += ["next_key: x", "---", "", "body", ""]
    path = root / "docs" / "plans" / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    time.sleep(0.02)
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
    """The arms in this file pin the spawn path, so in-process evaluation declines."""
    monkeypatch.setattr(
        mcr._inproc(), "evaluate_many",
        lambda commands, files, store, read_budget_s, force_first=True: {c: None for c in commands},
    )


def _far() -> float:
    return time.monotonic() + 600


def _run(mcr, plans, repo, store, **kw):
    return mcr.revalidate_set(plans, repo, 30, deadline=_far(), head="H1", store=store, **kw)


def _states(reply: dict) -> dict:
    return {p: [e["state"] for e in r["entries"]] for p, r in reply["plans"].items()}


def test_a_plans_census_block_is_parsed_once_per_call_and_not_at_all_when_unchanged(
    mcr, repo, monkeypatch
):
    plans = [
        _plan(repo, "a", [("grep -c alpha f.txt", "2"), ("grep -c alpha g.txt", "1"),
                          ("grep -c needle d/a.txt", "1")]),
        _plan(repo, "b", [("grep -c alpha f.txt", "2")]),
    ]
    store = _store(repo)
    loads: list[int] = []
    real = yaml.safe_load

    def counting(stream, *a, **k):
        loads.append(1)
        return real(stream, *a, **k)

    monkeypatch.setattr(yaml, "safe_load", counting)
    _run(mcr, plans, repo, store)
    assert len(loads) == 2
    loads.clear()
    _run(mcr, plans, repo, store)
    assert loads == []


def test_an_edited_plan_is_parsed_again_and_judged_on_its_new_census(mcr, repo, spawns):
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2")])
    store = _store(repo)
    assert _states(_run(mcr, [plan], repo, store)) == {"docs/plans/a.md": ["MATCH"]}
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "9")])
    assert _states(_run(mcr, [plan], repo, store)) == {"docs/plans/a.md": ["DRIFT"]}


def test_census_block_slice_equals_the_whole_frontmatter_parse(mcr):
    texts = [
        "status: x\ncensus:\n  - question: q\n    command: 'ls'\n    result: 'a: b'\nz: 1\n",
        "census:\n- question: q\n  command: ls\n  result: |\n    two\n    lines\n# tail\nk: v\n",
        "a: 1\n# c\ncensus: # note\n  - {question: q, command: ls, result: '1'}\n",
    ]
    for fm in texts:
        assert mcr._census_block(fm, yaml) == yaml.safe_load(fm)["census"]


@pytest.mark.parametrize("fm", [
    "census: []\n",
    "census: [{command: ls}]\n",
    "census:\ncensus:\n  - {command: ls}\n",
    "status: x\n",
    "census:\n  - question: [unclosed\n",
])
def test_census_block_defers_to_the_whole_parse_for_any_other_shape(mcr, fm):
    assert mcr._census_block(fm, yaml) is None


def test_a_command_shared_by_two_plans_is_screened_and_keyed_once_per_call(
    mcr, repo, spawns, monkeypatch
):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")]),
             _plan(repo, "b", [("grep -c alpha f.txt", "2")])]
    screened: list[str] = []
    keyed: list[str] = []
    real_screen, real_key = mcr.screen, mcr.input_key
    monkeypatch.setattr(mcr, "screen", lambda c, r=None: (screened.append(c), real_screen(c, r))[1])
    monkeypatch.setattr(
        mcr, "input_key", lambda e, *a, **k: (keyed.append(e["command"]), real_key(e, *a, **k))[1]
    )
    _run(mcr, plans, repo, _store(repo))
    assert screened == ["grep -c alpha f.txt"]
    assert keyed == ["grep -c alpha f.txt"]
    assert spawns == ["grep -c alpha f.txt"]


def test_an_entry_stored_direct_under_a_cold_key_cost_is_a_zero_spawn_hit_by_the_third_call(
    mcr, repo, spawns
):
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2")])
    store = _store(repo)
    store.put_run("grep -c alpha f.txt", None, (0, "2\n", ""), 5, 5000)
    _run(mcr, [plan], repo, store)
    spawns.clear()
    third = _run(mcr, [plan], repo, store)
    assert spawns == []
    assert third["memo_hits"] == 1 and third["direct_runs"] == 0
    assert _states(third) == {"docs/plans/a.md": ["MATCH"]}


def test_an_entry_whose_unchanged_tree_key_costs_more_than_its_spawn_stays_direct(
    mcr, repo, spawns, monkeypatch
):
    plan = _plan(repo, "a", [("grep -c alpha f.txt", "2")])

    def slow_key(entry, repo_root, *, head, view=None, stage_cache=None):
        time.sleep(0.1)
        return "k1"

    monkeypatch.setattr(mcr, "input_key", slow_key)
    store = _store(repo)
    _run(mcr, [plan], repo, store)
    spawns.clear()
    second = _run(mcr, [plan], repo, store)
    assert spawns == ["grep -c alpha f.txt"]
    assert second["direct_runs"] == 1 and second["memo_hits"] == 0
    assert store.run("grep -c alpha f.txt")[0] == mcr._DIRECT_KEY


def test_an_untracked_file_under_a_directory_operand_neither_moves_the_key_nor_respawns(
    mcr, repo, spawns
):
    plan = _plan(repo, "a", [("grep -rn needle d", "2")])
    store = _store(repo)
    _run(mcr, [plan], repo, store)
    (repo / "d" / "untracked.txt").write_text("needle\n", encoding="utf-8")
    spawns.clear()
    again = _run(mcr, [plan], repo, store)
    assert spawns == [] and again["memo_hits"] == 1


def test_the_spawn_process_ceiling_defers_a_spawn_and_still_judges_a_hit(
    mcr, repo, spawns, monkeypatch
):
    plans = [_plan(repo, "hit", [("grep -c alpha f.txt", "2")]),
             _plan(repo, "new", [("grep -c alpha g.txt", "1")]),
             _plan(repo, "newer", [("grep -c needle d/a.txt", "1")])]
    store = _store(repo)
    _run(mcr, [plans[0]], repo, store)
    spawns.clear()
    reply = _run(mcr, plans, repo, store, process_budget_ms=1)
    assert len(spawns) == 1, "the progress floor runs exactly one unit"
    states = _states(reply)
    assert states["docs/plans/hit.md"] == ["MATCH"]
    rest = sorted(tuple(s) for p, s in states.items() if p != "docs/plans/hit.md")
    assert rest == [(), ("MATCH",)], "one cold plan parsed and run, the other left unparsed"
    assert len(reply["pending"]) == 1
    assert (reply["covered"], reply["census_plans"]) == (2, 3)
    assert reply["progressed"] >= 1


def test_the_verdicts_of_a_fixture_set_are_those_of_the_per_plan_pass(mcr, repo):
    plans = [
        _plan(repo, "m", [("grep -c alpha f.txt", "2")]),
        _plan(repo, "x", [("grep -c alpha f.txt", "7")]),
        _plan(repo, "u", [("grep -rn needle d", "present in d")]),
    ]
    reply = _run(mcr, plans, repo, _store(repo))
    for plan in plans:
        single = mcr.revalidate(plan, repo, 30, require_stamp=False, decidable_only=True)
        assert [e["state"] for e in single["entries"]] == _states(reply)[
            f"docs/plans/{plan.name}"
        ]
    assert _states(reply) == {
        "docs/plans/m.md": ["MATCH"],
        "docs/plans/x.md": ["DRIFT"],
        "docs/plans/u.md": ["UNDECIDABLE"],
    }


def test_children_cpu_counts_against_the_spawn_bar_and_stops_the_next_batch(
    mcr, repo, monkeypatch
):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")]),
             _plan(repo, "b", [("grep -c alpha g.txt", "1")])]
    monkeypatch.setattr(mcr, "_UNMEASURED_SHARE", 1)
    monkeypatch.setattr(
        mcr._LegCost, "_children_s", lambda self: 10.0 if self.spawns else 0.0
    )
    reply = _run(mcr, plans, repo, _store(repo))
    assert reply["spawns"] == 1
    assert reply["children_process_ms"] == 10_000
    assert len(reply["pending"]) == 1
    assert sorted(s[0] for s in _states(reply).values()) == ["MATCH", "UNRUNNABLE"]


def test_the_reply_carries_children_spawns_and_inproc_counts(mcr, repo, monkeypatch):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2"), ("grep -c alpha g.txt", "1")])]
    reply = _run(mcr, plans, repo, _store(repo))
    assert reply["spawns"] == 1
    assert isinstance(reply["children_process_ms"], int) and reply["children_process_ms"] >= 0
    assert reply["inproc_runs"] == 0
    assert _states(reply) == {"docs/plans/a.md": ["MATCH", "MATCH"]}
    monkeypatch.setattr(mcr._inproc(), "evaluate_many", _REAL_EVALUATE_MANY)
    other = [_plan(repo, "c", [("grep -c alpha f.txt", "2"), ("grep -c needle d/a.txt", "1")])]
    fresh = _run(mcr, other, repo, _store(repo))
    assert fresh["inproc_runs"] >= 1
    assert fresh["spawns"] == 1, "one cat-file batch reads every blob; no command is spawned"


def test_process_budget_ms_is_accepted_and_a_smaller_one_stops_the_next_batch(
    mcr, repo, monkeypatch
):
    plans = [_plan(repo, "a", [("grep -c alpha f.txt", "2")]),
             _plan(repo, "b", [("grep -c alpha g.txt", "1")])]
    monkeypatch.setattr(mcr, "_UNMEASURED_SHARE", 1)
    monkeypatch.setattr(
        mcr._LegCost, "_children_s", lambda self: 0.4 if self.spawns else 0.0
    )
    wide = _run(mcr, plans, repo, _store(repo), process_budget_ms=0)
    assert wide["spawns"] == 1 and len(wide["pending"]) == 1
    assert _run(mcr, plans, repo, _store(repo), process_budget_ms=2000)["pending"] == []
