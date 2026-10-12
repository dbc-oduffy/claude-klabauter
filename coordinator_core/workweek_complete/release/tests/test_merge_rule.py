"""Tests for the merge-evidence rule: real git under tmp_path, a fake review dimension."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import gate_validate_invocable as gvi
from coordinator_core.workweek_complete.release import merge_rule
from coordinator_core.workweek_complete.release.contract import GATE_IDS, GateRecord

NOTES = "archive/release-notes/2026-10-12-v1.2.3.md"


def _g(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


def _commit(repo: Path, rel: str, text: str, msg: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _g(repo, "add", rel)
    _g(repo, "commit", "-m", msg)
    return _g(repo, "rev-parse", "HEAD")


class Env:
    def __init__(self, tmp: Path) -> None:
        origin = tmp / "origin.git"
        _g(tmp, "init", "--bare", "-b", "main", str(origin))
        self.repo = tmp / "work"
        _g(tmp, "clone", str(origin), str(self.repo))
        _g(self.repo, "checkout", "-b", "main")
        _commit(self.repo, "shared.txt", "base\n", "base")
        _g(self.repo, "push", "origin", "main")
        _g(self.repo, "checkout", "-b", "feature")
        self.reviewed = _commit(self.repo, "code.py", "x = 1\n", "code")
        self.candidate = _commit(self.repo, NOTES, "notes\n", "stamp")
        self.calls: list[tuple[str, list[str]]] = []

    def gates(self, **over: GateRecord) -> tuple[GateRecord, ...]:
        recs = {g: GateRecord(g, "ran", 0, self.candidate) for g in GATE_IDS}
        recs.update(over)
        return tuple(recs.values())

    def run(self, **over):
        kw = dict(
            candidate=self.candidate,
            version="1.2.3",
            review_result={"reviewed_sha": self.reviewed, "verdict": "PASS"},
            gate_records=self.gates(),
            ledger_rows=[{"id": "changelog", "status": "done"}],
            project_subtypes=(),
            stamped_paths=(NOTES,),
        )
        kw.update(over)
        return merge_rule.evaluate(self.repo, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)

    def fake(changed, base, root):
        e.calls.append((base, list(changed)))
        return gvi.DimensionResult("review", e.verdict, "fake detail")

    e.verdict = gvi.Verdict.PASS
    monkeypatch.setitem(gvi._DIMENSION_REGISTRY, "review", fake)
    return e


def test_pass(env):
    r = env.run()
    assert r.passed, r.evidence
    assert r.evidence["main"] == "merges-clean"


def test_project_checks_not_composed(env):
    assert env.run(project_subtypes=("unreal",)).reason == "project-checks-not-composed"


def test_review_missing_absent(env):
    assert env.run(review_result=None).reason == "review-missing"


def test_review_missing_not_ancestor(env):
    _g(env.repo, "checkout", "-q", "main")
    other = _commit(env.repo, "o.txt", "o\n", "other")
    _g(env.repo, "checkout", "-q", "feature")
    r = env.run(review_result={"reviewed_sha": other, "verdict": "PASS"})
    assert r.reason == "review-missing"


def test_review_blocked(env):
    r = env.run(review_result={"reviewed_sha": env.reviewed, "verdict": "BLOCKED"})
    assert r.reason == "review-blocked"


def test_unreviewed_tail(env):
    (env.repo / "tail.py").write_text("y = 1\n", encoding="utf-8")
    _g(env.repo, "add", "tail.py")
    _g(env.repo, "commit", "--amend", "--no-edit")
    cand = _g(env.repo, "rev-parse", "HEAD")
    r = env.run(candidate=cand, gate_records=tuple(GateRecord(g, "ran", 0, cand) for g in GATE_IDS))
    assert r.reason == "unreviewed-tail"
    assert r.evidence["unreviewed_paths"] == ["tail.py"]


def test_review_uncovered(env):
    env.verdict = gvi.Verdict.FAIL
    r = env.run()
    assert r.reason == "review-uncovered"
    assert r.evidence["dimension_detail"] == "fake detail"


def test_dimension_range_ends_at_candidate_parent(env):
    env.run()
    base, changed = env.calls[0]
    assert base == f"origin/main..{env.reviewed}"
    assert "code.py" in changed and NOTES not in changed


def test_gate_missing_and_not_started(env):
    assert env.run(gate_records=env.gates()[:2]).reason == "gate-tests-missing"
    nope = GateRecord("d6", "not-started", None, env.candidate)
    assert env.run(gate_records=env.gates(d6=nope)).reason == "gate-tests-missing"


def test_gate_other_commit(env):
    rec = GateRecord("d6", "ran", 0, env.reviewed)
    assert env.run(gate_records=env.gates(d6=rec)).reason == "gate-tests-other-commit"


def test_gate_failed(env):
    rec = GateRecord("d6", "ran", 1, env.candidate)
    assert env.run(gate_records=env.gates(d6=rec)).reason == "gate-tests-failed"


def test_not_present_passes_for_d0_only(env):
    d0 = GateRecord("d0", "not-present", None, env.candidate)
    assert env.run(gate_records=env.gates(d0=d0)).passed
    d6 = GateRecord("d6", "not-present", None, env.candidate)
    assert env.run(gate_records=env.gates(d6=d6)).reason == "gate-tests-missing"


def test_unbound_gate_blocks_on_red_but_is_never_candidate_evidence(env):
    from coordinator_core.workweek_complete.release.gates import GateEvidence

    green = GateEvidence("d0", "ran", 0, env.candidate, bound_to_candidate=False)
    r = env.run(gate_records=env.gates(d0=green))
    assert r.passed
    assert r.evidence["unbound_gates"] == ["d0"]
    red = GateEvidence("d0", "ran", 1, env.candidate, bound_to_candidate=False)
    assert env.run(gate_records=env.gates(d0=red)).reason == "gate-tests-failed"


@pytest.mark.parametrize("status", ["failed", "halted-by-gate", "not-composed", "cadence-skipped"])
def test_ledger_not_clean(env, status):
    r = env.run(ledger_rows=[{"id": "step-a", "status": status}])
    assert r.reason == "ledger-not-clean"
    assert r.evidence["steps"] == ["step-a"]


def test_not_stamped(env):
    assert env.run(version="9.9.9").reason == "not-stamped"


def test_candidate_not_on_branch(env):
    _g(env.repo, "checkout", "-q", "-b", "elsewhere", "main")
    r = env.run()
    assert r.reason == "candidate-not-on-branch"


def test_main_conflict(env, tmp_path):
    _g(env.repo, "checkout", "-q", "main")
    _commit(env.repo, "shared.txt", "main side\n", "main edit")
    _g(env.repo, "push", "origin", "main")
    _g(env.repo, "checkout", "-q", "feature")
    cand = _commit(env.repo, "shared.txt", "feature side\n", "conflicting")
    # declared version still read from the candidate tree, which carries the notes file
    r = env.run(candidate=cand, gate_records=tuple(
        GateRecord(g, "ran", 0, cand) for g in GATE_IDS), stamped_paths=("shared.txt", NOTES))
    assert r.reason == "main-conflict"
    assert r.evidence["conflicted_paths"] == ["shared.txt"]


def test_head_moved_and_dirty_tree_still_pass(env):
    _commit(env.repo, "later1.txt", "1\n", "peer one")
    _commit(env.repo, "later2.txt", "2\n", "peer two")
    (env.repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    (env.repo / "staged.txt").write_text("s\n", encoding="utf-8")
    _g(env.repo, "add", "staged.txt")
    assert env.run().passed


def test_no_git_status_and_no_invocable_op(env, monkeypatch):
    argvs: list[list[str]] = []
    real = merge_rule.run_git

    def spy(args, **kw):
        argvs.append(list(args))
        return real(args, **kw)

    def boom(*a, **k):
        raise AssertionError("_gate_validate_invocable called")

    monkeypatch.setattr(merge_rule, "run_git", spy)
    monkeypatch.setattr(gvi, "_gate_validate_invocable", boom, raising=False)
    assert env.run().passed
    assert argvs and all("status" not in a for a in argvs)
    assert len(argvs) <= 6  # the eight-child bound minus declared_version's two, which use its own runner
