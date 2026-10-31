"""A two-entry `asks` submission (~2KB `em_context` each) through the default hook entry
`submit_engine_ask.evaluate` and the real fan-out + `dispatch.emit`: warm process time under the
500ms bar and a pinned spawn bound. The figures print so a run reports the measured cost.
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BAR_S = 0.5
SPAWN_BOUND = 0  # measured 0 warm; any new spawn on this path is a regression
SESSION = "sess-real-emit"
repo = real.repo


def _two_asks() -> list[dict]:
    context = ("EM context line: constraint, live checkout, land files atomically. " * 30)[:2048]
    return [
        {"ask": f"two-ask job {i}", "label": f"job-{i}", "writes": [f"pkg/m{i}.py"], "em_context": context}
        for i in range(2)
    ]


def _seed_turn(repo, monkeypatch) -> None:
    from coordinator_core.ops import baton_pm_turns as PT
    from coordinator_core.session_baton import store

    (repo.parent / "hub" / SESSION).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: repo.parent / "hub" / s)
    assert PT.append_turn("do these two separable jobs", session_id=SESSION)["ok"]


def _measure(repo, monkeypatch):
    _seed_turn(repo, monkeypatch)
    cold = time.process_time()
    mod.evaluate(real._payload(repo, {"asks": _two_asks()}))  # warm the imports; the bar is the steady state
    cold = time.process_time() - cold
    payload = real._payload(repo, {"asks": _two_asks()})
    with _count_spawns_attributed(monkeypatch) as spawns:
        start = time.process_time()
        env = mod.evaluate(payload)
        spent = time.process_time() - start
    real._assert_rewrite(env, launcher=True)
    print(f"two-ask submission: {spent * 1000:.0f}ms process time warm ({cold * 1000:.0f}ms cold), {len(spawns)} spawns")
    return spent, spawns


def test_two_ask_submission_spawns_stay_within_the_pinned_bound(repo, monkeypatch):
    _, spawns = _measure(repo, monkeypatch)
    assert len(spawns) <= SPAWN_BOUND, [s.argv for s in spawns]
    real._all_git_run(spawns)


def test_two_ask_submission_process_time_is_under_the_bar(repo, monkeypatch):
    spent, _ = _measure(repo, monkeypatch)
    assert spent < BAR_S, f"two-ask submission took {spent:.3f}s process time (bar {BAR_S}s)"
