"""Param validation and pass-through of roadmap.blitz_stage's approval and trail_dir."""

from pathlib import Path

import pytest

from coordinator_core.ops import roadmap_blitz_stage as op

GOOD = {"verdict": "approved", "approver": "pm", "utterance": "go"}


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(op, "main_worktree_root", lambda root: root)
    monkeypatch.setattr(op, "stage_roadmap", lambda root, rm, **kw: seen.append(kw) or {})
    return seen


def _run(**extra):
    return op._handler({"roadmap": "state/roadmap/x", **extra}, repo_root=Path("."))


def test_absent_approval_passes_nothing_new(calls):
    _run()
    assert set(calls[0]) == {"edges_path", "branch", "commit", "interaction_mode"}
    assert calls[0]["interaction_mode"] is None


def test_well_formed_approval_passes_through(calls):
    ap = {**GOOD, "ruling_ref": "r1"}
    ap.pop("utterance")
    ap["ruling"] = "apm-ruling"
    _run(approval=ap, trail_dir="scratch/run/trail")
    assert calls[0]["approval"] == ap
    assert calls[0]["trail_dir"] == "scratch/run/trail"


@pytest.mark.parametrize(
    "approval",
    [
        "yes",
        {**GOOD, "verdict": "maybe"},
        {**GOOD, "approver": " "},
        {"verdict": "approved", "approver": "pm"},
        {**GOOD, "ruling": "both"},
        {**GOOD, "utterance": ""},
        {**GOOD, "ruling_ref": 3},
    ],
)
def test_malformed_approval_refused(calls, approval):
    with pytest.raises(ValueError):
        _run(approval=approval, trail_dir="scratch/t")
    assert not calls


@pytest.mark.parametrize(
    "trail",
    [None, "", "/abs/scratch/t", "C:/scratch/t", "scratch/../x", "scratch\\t", "state/t", "scratch", "scratch/"],
)
def test_bad_trail_dir_refused(calls, trail):
    with pytest.raises(ValueError):
        _run(approval=GOOD, trail_dir=trail)
    assert not calls


def test_trail_dir_without_approval_refused(calls):
    with pytest.raises(ValueError):
        _run(trail_dir="scratch/t")
