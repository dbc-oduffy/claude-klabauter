"""review.reachability's output for a wf_97265411-edf-shaped diff, fed through the judge verdict rules under node."""

from __future__ import annotations

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_commit_pending_e2e import (  # noqa: F401
    _item,
    _judge_result,
    _run,
    _xs_arm,
)
from coordinator_core.ops.review_reachability import _handler
from coordinator_core.ops.tests._reachability_fixture import PLAN_REL, _git, _plan, _write, build

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LIFECYCLE = "lib/lifecycle.py"


def _scan(tmp_path):
    """Scanner reply on a fixture with a sibling-only-called new function and an undeclared diff file."""
    root = build("unwired", tmp_path / "r")
    _write(root, _LIFECYCLE, "def owner_lease_watchdog():\n    return 1\n")
    _write(root, "lib/runner.py", "from lib.lifecycle import owner_lease_watchdog\n\nowner_lease_watchdog()\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pre-existing module")
    base = run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()
    _write(
        root, _LIFECYCLE,
        "def owner_lease_tick():\n    return 2\n\n\ndef owner_lease_watchdog():\n    return owner_lease_tick()\n",
    )
    _write(root, "lib/provider.py", "def pin_checkouts_root():\n    return 3\n")
    _write(root, PLAN_REL, _plan([_LIFECYCLE]))
    _git(root, "add", "-A")
    return _handler({"repo_root": str(root), "base_sha": base, "worktree": True, "plan": PLAN_REL})


def _reachability(res) -> dict:
    return {
        "unreachable": [e["ref"] for e in res["entries"] if e["verdict"] == "unreachable"]
        + [c["broken_at"] for c in res["click_paths"] if c["verdict"] == "broken"],
        "undecidable": [e["ref"] for e in res["entries"] if e["verdict"] == "undecidable"],
    }


def _met_judge(reachability: dict) -> dict:
    return _judge_result(
        [_item("R1", "met")], status="met", observed=["x"], reachability=reachability,
        falsifier={"verdict": "pass"}, clauses=[{"id": "c1", "status": "met"}],
    )


def test_fixed_scanner_output_leaves_a_met_judge_met(tmp_path):
    res = _scan(tmp_path)
    reach = _reachability(res)
    assert reach["unreachable"] == [], reach
    assert f"{_LIFECYCLE}:owner_lease_tick" in reach["undecidable"]
    assert not any("pin_checkouts_root" in r for r in reach["undecidable"])
    assert res["cost"]["process_ms"] < 500
    (out,) = _run([_met_judge(reach)], judged=True)
    assert out["revise_and_refire"] is None
    assert "commit" in out["phases"]


def test_baseline_shape_unreachable_list_demotes_the_same_judge():
    (out,) = _run([_met_judge({"unreachable": [f"{_LIFECYCLE}:owner_lease_tick"], "undecidable": []})], judged=True)
    assert out["revise_and_refire"] is not None
    assert out["revise_and_refire"]["reason"].startswith("not wired up: ")
    assert out["commit"] is None  # a halt commits its record only (a93b0ee881)
