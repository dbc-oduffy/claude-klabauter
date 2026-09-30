"""
coordinator_core.ops.tests.test_plan_status_transition_records_exit_criterion --
a landed plan with no `## Tasks` spine has no close-out row to carry
`exit_criterion_met`, so `stamp-implemented` records it from the caller's
already-judged observation (`--falsifier-verdict/--falsifier-output/--prose`).
"""
from __future__ import annotations

import pytest

from coordinator_core.execute_plan_assemble.tests.test_close_out_goal_refusal import (
    _prime_exit_criterion_block,
)
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import (
    _head_sha,
    _init_repo,
    _land_the_shipping_chunk,
    _run_git,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SPINELESS_PLAN = """---
title: "Fixture plan -- spineless"
created: 2026-08-27
author: test-fixture
status: approved
branch: "work/test-fixture/2026-08-27"
plan_id: "pln-fixture-spineless-000001"
deliverable_id: "dlv-fixture-spineless-000001"
{goal_frontmatter}---

# Fixture plan -- spineless

No spine, no dispatch ledger.
"""

_FLAGS = [
    "--falsifier-verdict", "pass",
    "--falsifier-output", "right",
    "--prose", "matches the stated pass condition at HEAD abc1234",
]


def _seed(root, *, exit_criterion_met=None, with_prime=True):
    sha = _land_the_shipping_chunk(root)
    goal_fm = (
        _prime_exit_criterion_block(baseline_ref=sha, exit_criterion_met=exit_criterion_met)
        if with_prime
        else ""
    )
    plan = root / "plan.md"
    plan.write_text(_SPINELESS_PLAN.format(goal_frontmatter=goal_fm), encoding="utf-8")
    _run_git(["add", "plan.md"], root)
    sizing = root / "state/sizings/2026-08-27-fixture-sizing.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text("estimate:\n  tshirt: M\n", encoding="utf-8")
    _run_git(["add", "state"], root)
    _run_git(["commit", "-q", "-m", "seed"], root)
    return plan


def _stamp(plan, *extra):
    return pst.main(["stamp-implemented", "--plan", str(plan), *extra])


def test_spineless_plan_without_record_is_refused(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "exit_criterion_met_absent" in capsys.readouterr().err


def test_flags_record_the_observation_and_stamp(tmp_path):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    assert _stamp(plan, *_FLAGS) in (0, 2)

    fm = parse_frontmatter(plan.read_text(encoding="utf-8"))["frontmatter"]
    assert fm["status"] == "implemented"
    met = fm["exit_criterion_met"]
    assert met["asserted"] is True
    assert met["falsifier_verdict"] == "pass"
    assert met["falsifier_output"] == "right"
    assert "abc1234" in met["prose"]
    assert met["asserted_by"] and met["asserted_at"]


def test_output_with_yaml_structure_round_trips(tmp_path):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    output = "line one: 'x'\n- not a list # nor a comment\nlast"

    assert _stamp(
        plan, "--falsifier-verdict", "pass", "--falsifier-output", output, "--prose", "p"
    ) in (0, 2)

    fm = parse_frontmatter(plan.read_text(encoding="utf-8"))["frontmatter"]
    assert fm["exit_criterion_met"]["falsifier_output"] == output


def test_refused_stamp_leaves_no_record_behind(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    (tmp_path / "state/sizings/2026-08-27-fixture-sizing.yaml").unlink()
    plan.write_text(
        plan.read_text(encoding="utf-8").replace("baseline_ref:", "baseline_ref: 0000000 #", 1),
        encoding="utf-8",
    )
    before = plan.read_text(encoding="utf-8")
    head = _head_sha(tmp_path)

    assert _stamp(plan, *_FLAGS) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert _head_sha(tmp_path) == head
    assert "exit_criterion_met:" not in plan.read_text(encoding="utf-8")


def test_existing_record_stands(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(
        tmp_path,
        exit_criterion_met=(
            "  asserted: true\n  falsifier_output: 'right'\n"
            "  falsifier_verdict: pass\n  prose: earlier\n"
        ),
    )
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, *_FLAGS) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "already carries exit_criterion_met" in capsys.readouterr().err


def test_no_prime_exit_criterion_refuses_recording(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path, with_prime=False)
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, *_FLAGS) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert "no prime_exit_criterion" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags, needle",
    [
        (["--falsifier-verdict", "pass"], "needs all of"),
        (["--falsifier-verdict", "fail", "--falsifier-output", "x", "--prose", "p"], "must be 'pass'"),
        (["--falsifier-verdict", "pass", "--falsifier-output", "x" * 4097, "--prose", "p"], "exceeds 4096"),
    ],
)
def test_malformed_recording_flags_refuse_before_any_write(tmp_path, capsys, flags, needle):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    before = plan.read_text(encoding="utf-8")

    assert _stamp(plan, *flags) == 1
    assert plan.read_text(encoding="utf-8") == before
    assert needle in capsys.readouterr().err


def test_flags_are_rejected_on_other_verbs(tmp_path, capsys):
    _init_repo(tmp_path)
    plan = _seed(tmp_path)

    rc = pst.main(["stamp-reopened", "--plan", str(plan), "--reason", "r", *_FLAGS])

    assert rc == 1
    assert "does not accept --falsifier-verdict" in capsys.readouterr().err
