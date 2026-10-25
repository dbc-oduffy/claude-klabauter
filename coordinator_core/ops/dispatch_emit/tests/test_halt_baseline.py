"""since_halt_base precedence; the real-git cases live in test_halt_baseline_spawns."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import emit, halt_baseline

PLAN = "docs/plans/p.md"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def test_precedence_table(tmp_path):
    state = emit.ResumeLanded(frozenset(), None, run_halts={"r": (frozenset(), B)}, run_first_halts={"r": A})
    base = lambda resume, head=None, st=state: halt_baseline.since_halt_base(tmp_path, resume, PLAN, head, st)
    assert base({"run_id": "r", "since_sha": C, "halt_sha": B}, "h") == (C, "param")
    assert base({"run_id": "r", "halt_sha": B}, "h") == (A, "resolved")
    assert base({"run_id": "zz", "halt_sha": B}, "h") == (B, "param")
    assert base({"run_id": "zz"}, "h") == ("h", "stage-head")
    assert base({"run_id": "zz", "since_sha": "short"}, None) == (None, None)


def test_param_present_spawns_no_git(tmp_path, monkeypatch):
    import coordinator_core.git.run as git_run

    calls: list = []
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a))

    def _no_walk(*a, **k):
        raise AssertionError("a since_sha param must not trigger resume_landed_state")

    monkeypatch.setattr(emit, "resume_landed_state", _no_walk)
    assert halt_baseline.since_halt_base(tmp_path, {"run_id": "r", "since_sha": A}, PLAN, None) == (A, "param")
    assert calls == []
