"""resumable_plan at arm xs resumes a ledger-check halt through the run's record spine."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261022T165956-50e55e"
SPINE = f"state/warp/{RUN}/gate.spine.md"
REMAINING = {"R1": {"remaining": ["apply_guard_level must return a Stop-valid warn shape"], "evidence": "e"}}


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _fixture(repo, *, halted=None, spine=SPINE, run=RUN):
    """X1 checkpoint-landed, X2 withheld (voided), then a committed ledger-check halt on R1."""
    _put(repo, tshirt="XS", route="dispatch")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True)
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {disp}\n  writes:\n    - some/{i}.py\n"
        for i, disp in (("X1", "open"), ("X2", "voided"))
    )
    (d / "gate.spine.md").write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — X1", "-m", f"Checkpoint-Plan: {SPINE}\nCheckpoint-Base: abc1234")
    record = {"halted": "ledger-check", "sizing_rel": REL, "plan": spine, "blocking": ["R1"]}
    record.update(halted or {})
    record["blocking_items"] = [{"id": i, **v} for i, v in REMAINING.items()]
    (d / "halted.json").write_text(json.dumps(record), encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "X1"}], "owed": [], "gated": []}), encoding="utf-8")
    _git(repo, "add", "state")
    _git(repo, "commit", "-q", "-m", halted_record_message(run, {"halted": "ledger-check", "plan": SPINE}))


def _sizing(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def test_xs_halted_record_spine_resolves_to_a_ledger_resume_carrying_remaining(repo):  # noqa: F811
    _fixture(repo)
    plan = resumable_plan(_sizing(repo), REL, repo, "xs")
    assert plan is not None and plan.plan_rel == SPINE and plan.incomplete == ()
    assert plan.ledger.run_id == RUN and plan.ledger.blocking == ("R1",)
    assert plan.ledger.remaining["R1"]["remaining"] == REMAINING["R1"]["remaining"]


def test_xs_without_a_halt_record_is_none(repo):  # noqa: F811
    _put(repo, tshirt="XS", route="dispatch")
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_xs_non_ledger_halt_is_none(repo):  # noqa: F811
    _fixture(repo, halted={"halted": "refusal"})
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_xs_halt_bound_to_another_sizing_is_none(repo):  # noqa: F811
    _fixture(repo, halted={"sizing_rel": "state/sizings/2026-10-01-other.yaml"})
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None


def test_xs_halt_naming_a_spine_outside_its_record_dir_is_none(repo):  # noqa: F811
    _fixture(repo, halted={"plan": "docs/plans/2026-10-01-gate.md"})
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None
    other_run_spine = "state/warp/ask-20261022T165956-aaaaaa/gate.spine.md"
    (repo / "state" / "warp" / RUN / "halted.json").write_text(
        json.dumps({"halted": "ledger-check", "sizing_rel": REL, "plan": other_run_spine}), encoding="utf-8"
    )
    assert resumable_plan(_sizing(repo), REL, repo, "xs") is None
