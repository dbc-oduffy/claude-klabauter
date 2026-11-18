"""Two sibling runs in one session at one base: each run's review prep selects only its own prep diff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
from coordinator_core.ops.tests.test_review_prep import _base, _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
RUN_A = "ask-20261118T114303-0fd3b6"
RUN_B = "ask-20261118T114303-f04599"


def _run_prep(repo: Path, run: str) -> dict:
    return rp._handler(
        {"base": _base(repo), "plan_path": "docs/plans/p.md", "worktree": True, "session_id": SESSION, "run_id": run},
        repo,
    )


@pytest.fixture
def two_runs(repo):
    _write(repo, ["a/one.py", "b/two.py"], 3)
    _stage(repo, ["a/one.py"], run_id=RUN_A)
    _stage(repo, ["b/two.py"], run_id=RUN_B)
    replies = {run: _run_prep(repo, run) for run in (RUN_A, RUN_B)}
    assert all(r["error"] is None for r in replies.values())
    return replies


def _diff_paths(reply: dict) -> list:
    out = [reply["whole_diff_path"], reply["sidecar_path"]]
    return out + [p for s in reply["slices"] for p in (s["diff_path"], s["sidecar_path"])]


def test_each_run_selects_only_its_own_prep_diff(repo, two_runs):
    base = _base(repo)
    for run, other in ((RUN_A, RUN_B), (RUN_B, RUN_A)):
        stored = rp.stored_reply_for_run(repo, SESSION, base, run_key=run)
        assert stored is not None
        own, foreign = run_prep_slice_prefix(run, base), run_prep_slice_prefix(other, base)
        assert own in stored["sidecar_path"] and foreign not in stored["sidecar_path"]
        paths = _diff_paths(stored)
        assert all(own in p for p in paths) and not any(foreign in p for p in paths)


def test_a_run_less_selection_never_takes_a_run_owned_reply(repo, two_runs):
    assert rp.stored_reply_for_run(repo, SESSION, _base(repo)) is None


def test_a_sole_run_owned_reply_is_not_taken_by_a_run_less_selection(repo):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"], run_id=RUN_A)
    assert _run_prep(repo, RUN_A)["error"] is None
    base = _base(repo)
    assert rp.stored_reply_for_run(repo, SESSION, base) is None
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_A) is not None


def test_sibling_runs_do_not_make_a_plan_route_reply_ambiguous(repo, two_runs):
    base = _base(repo)
    plan = rp.review_prep(
        repo, base=base, paths=["a/one.py"], slice_prefix=f"p-plan1-{base[:12]}-prep",
        plan_path="docs/plans/p.md", session_id=SESSION,
    )
    assert plan["error"] is None
    got = rp.stored_reply_for_run(repo, SESSION, base)
    assert got is not None and "p-plan1-" in got["sidecar_path"]


def _point(repo: Path, name: str, sidecar: str) -> None:
    share = Path(rp.share_dir(str(repo), SESSION))
    (share / name).write_text(json.dumps({"sidecar": sidecar, "ambiguous": False}), encoding="utf-8")


def test_a_pointer_naming_a_sibling_sidecar_is_not_taken(repo, two_runs):
    base = _base(repo)
    sibling = f"{run_prep_slice_prefix(RUN_B, base)}.review-prep.md"
    own = f"{run_prep_slice_prefix(RUN_A, base)}.review-prep.md"
    _point(repo, rp._pointer_name(RUN_A, base), sibling)
    got = rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_A)
    assert got is not None and got["sidecar_path"].endswith(own)
    Path(rp.share_dir(str(repo), SESSION), own).unlink()
    assert rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_A) is None


def test_a_pointer_naming_its_own_sidecar_is_taken(repo, two_runs):
    base = _base(repo)
    own = f"{run_prep_slice_prefix(RUN_A, base)}.review-prep.md"
    _point(repo, rp._pointer_name(RUN_A, base), own)
    got = rp.stored_reply_for_run(repo, SESSION, base, run_key=RUN_A)
    assert got is not None and got["sidecar_path"].endswith(own)
