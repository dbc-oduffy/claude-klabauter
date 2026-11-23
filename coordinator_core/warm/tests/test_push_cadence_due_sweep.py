"""The 15s due arm of the push cadence: due repos, the work/* owned-remote gate, tail legs."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import push_cadence as pc


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch):
    pc.reset_cadence_for_test()
    monkeypatch.setattr(pc, "machine_profile", lambda: "author")
    yield
    pc.reset_cadence_for_test()


def _tick(now: list, calls: list):
    def fake_sweep(repos, **kwargs):
        calls.append((list(repos), kwargs))

    return pc.on_idle_tick(
        served_repos=lambda: ["served"],
        clock=lambda: now[0],
        interval_secs=600.0,
        sweep_fn=fake_sweep,
    )


def test_due_repo_is_swept_at_next_tick_with_due_only(tmp_path: Path) -> None:
    now, calls = [0.0], []
    _tick(now, calls)  # seeds the 600s clock
    pc.note_commit(tmp_path)
    now[0] = 5.0
    assert _tick(now, calls) is False
    assert [c[0] for c in calls] == [[tmp_path]]
    assert calls[0][1]["due_only"] is True


def test_swept_repo_waits_the_min_gap_before_the_due_arm_takes_it_again(tmp_path: Path) -> None:
    now = [0.0]
    pc.note_commit(tmp_path)
    assert pc.take_due_repos(lambda: now[0]) == [tmp_path]
    pc._due_swept_at[tmp_path] = 100.0
    now[0] = 100.0 + pc.DUE_SWEEP_MIN_GAP_SECS - 0.1
    assert pc.take_due_repos(lambda: now[0]) == []
    now[0] = 100.0 + pc.DUE_SWEEP_MIN_GAP_SECS
    assert pc.take_due_repos(lambda: now[0]) == [tmp_path]


def test_no_due_repo_means_no_sweep_call() -> None:
    now, calls = [0.0], []
    _tick(now, calls)
    now[0] = 20.0
    _tick(now, calls)
    assert calls == []


def test_full_interval_still_fires_the_full_sweep() -> None:
    now, calls = [0.0], []
    _tick(now, calls)
    now[0] = 601.0
    assert _tick(now, calls) is True
    assert calls[0][0] == ["served"] and "due_only" not in calls[0][1]


@pytest.mark.parametrize(
    "branch,remote",
    [
        ("main", ("origin", None)),
        ("feature/x", ("origin", None)),
        (None, ("origin", None)),
        ("work/a/b", (None, "remote origin is non-owned")),
    ],
)
def test_scope_gate_skips_everything_but_owned_work_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch, remote
) -> None:
    monkeypatch.setattr(pc, "_declined_as_held_clone", lambda r: False)
    monkeypatch.setattr(pc, "_declined_by_compile_hold", lambda r: False)
    monkeypatch.setattr(pc, "head_branch", lambda r: branch)
    monkeypatch.setattr(pc, "owned_push_remote", lambda r, b: remote)
    pushed = []
    monkeypatch.setattr(pc, "push_outstanding", lambda r, **kw: pushed.append(r))
    assert pc._sweep_one(tmp_path, scope_gate=True) == "skipped"
    assert pushed == []


def test_scope_gate_admits_an_owned_work_branch_and_full_sweep_keeps_wider_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Ok:
        failed = False
        unconfirmed = False
        acted = ["push"]

    monkeypatch.setattr(pc, "_declined_as_held_clone", lambda r: False)
    monkeypatch.setattr(pc, "_declined_by_compile_hold", lambda r: False)
    monkeypatch.setattr(pc, "_declined_by_branch_hold", lambda r, b: False)
    monkeypatch.setattr(pc, "head_sha", lambda r: "a" * 40)
    monkeypatch.setattr(pc, "no_upstream_and_no_new_commits", lambda r, b, s: False)
    monkeypatch.setattr(pc, "_acquire_sweep_lock", lambda r: True)
    monkeypatch.setattr(pc, "_release_sweep_lock", lambda r: None)
    pushed = []
    monkeypatch.setattr(pc, "push_outstanding", lambda r, **kw: pushed.append(r) or _Ok())
    monkeypatch.setattr(pc, "owned_push_remote", lambda r, b: ("origin", None))

    monkeypatch.setattr(pc, "head_branch", lambda r: "work/a/b")
    assert pc._sweep_one(tmp_path, scope_gate=True) == "pushed"

    monkeypatch.setattr(pc, "head_branch", lambda r: "feature/x")
    assert pc._sweep_one(tmp_path, scope_gate=False) == "pushed"
    assert pushed == [tmp_path, tmp_path]


@pytest.mark.parametrize("hold", ["compile", "branch"])
def test_due_arm_skips_a_repo_under_a_push_hold_on_an_owned_work_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    monkeypatch.setattr(pc, "_declined_as_held_clone", lambda r: False)
    monkeypatch.setattr(pc, "_declined_by_compile_hold", lambda r: hold == "compile")
    monkeypatch.setattr(pc, "_declined_by_branch_hold", lambda r, b: hold == "branch")
    monkeypatch.setattr(pc, "head_branch", lambda r: "work/a/b")
    monkeypatch.setattr(pc, "owned_push_remote", lambda r, b: ("origin", None))
    pushed = []
    monkeypatch.setattr(pc, "push_outstanding", lambda r, **kw: pushed.append(r))
    assert pc._sweep_one(tmp_path, scope_gate=True) == "skipped"
    assert pushed == []


def test_due_only_sweep_skips_tail_legs_and_records_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tail = []
    monkeypatch.setattr(pc, "_sweep_foreign", lambda **kw: tail.append("foreign"))
    monkeypatch.setattr(pc, "_sweep_memo_push_pending", lambda *a, **kw: tail.append("memo"))
    monkeypatch.setattr(pc, "_sweep_publish_retire", lambda *a, **kw: tail.append("retire"))
    monkeypatch.setattr(pc, "is_ahead", lambda r: True)
    monkeypatch.setattr(pc, "resolve_push_ceiling", lambda r, default_secs: default_secs)
    gates = []
    monkeypatch.setattr(pc, "_sweep_one", lambda r, scope_gate=False: gates.append(scope_gate) or "pushed")
    monkeypatch.setattr(pc.telemetry, "record_sweep", lambda *a, **kw: None)
    pc.note_commit(tmp_path)
    pc.sweep_repos([tmp_path], due_only=True, clock=lambda: 50.0)
    assert tail == [] and gates == [True]
    assert pc._due_swept_at[tmp_path] == 50.0 and tmp_path not in pc._due_repos

    pc.sweep_repos([tmp_path], clock=lambda: 60.0)
    assert tail == ["foreign", "memo", "retire"] and gates == [True, False]
