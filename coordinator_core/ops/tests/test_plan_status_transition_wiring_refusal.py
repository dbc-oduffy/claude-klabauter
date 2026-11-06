"""stamp-implemented refuses a plan whose `wiring:` lists a non-wired part.

Drives `plan_status_transition.main(["stamp-implemented", ...])` at default
arguments; fixture plans reuse the goal-refusal suite's builders.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import (
    _head_sha,
    _init_repo,
    _land_the_shipping_chunk,
    _seed_goal_plan,
    _stamp,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REAL_EVIDENCE = "state/warp/run/receipts/real.json"
_HEAD = "@HEAD@"


def _entry(path: str = _REAL_EVIDENCE, sha: str = _HEAD, dirty: str = "false") -> str:
    return f'{{path: {path}, sha: "{sha}", dirty: {dirty}}}'


def _wired_part(pid: str = "good") -> str:
    return (
        f"  - id: {pid}\n    status: wired\n    built_path: built.py\n"
        f"    runs_evidence: [{_entry()}]\n"
    )


def _plan(tmp_path: Path, wiring_block: str) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "built.py").write_text("x = 1\n", encoding="utf-8")
    sha = _land_the_shipping_chunk(tmp_path)
    plan = _seed_goal_plan(
        tmp_path, goal_frontmatter=wiring_block, shipping_sha=sha, created="2026-07-01"
    )
    # Evidence must name the HEAD the stamp sees, which the seeding commit just made.
    text = plan.read_text(encoding="utf-8").replace(_HEAD, _head_sha(tmp_path))
    plan.write_text(text, encoding="utf-8")
    return plan


@pytest.mark.parametrize(
    "part, expect",
    [
        ("  - id: p1\n    status: not_built\n", ("p1", "not_built")),
        ("  - id: p2\n    status: blocked\n    blocked_on: dep\n", ("p2", "blocked")),
        (
            "  - id: p3\n    status: wired\n    built_path: nope.py\n"
            f"    runs_evidence: [{_entry()}]\n",
            ("p3", "unwired"),
        ),
        (
            "  - id: p4\n    status: wired\n    built_path: built.py\n"
            f"    runs_evidence: [{_entry('tests/fixtures/a.json')}]\n",
            ("p4", "unwired"),
        ),
        (
            "  - id: p5\n    status: wired\n    built_path: built.py\n"
            f"    runs_evidence: [{_entry(sha='0' * 40)}]\n",
            ("p5", "unwired"),
        ),
    ],
)
def test_non_wired_part_refuses_and_names_status(tmp_path, capsys, part, expect):
    plan = _plan(tmp_path, "wiring:\n" + _wired_part() + part)
    before = plan.read_text(encoding="utf-8")
    head = _head_sha(tmp_path)

    rc = _stamp(plan)

    assert rc != 0
    assert plan.read_text(encoding="utf-8") == before
    assert _head_sha(tmp_path) == head
    err = capsys.readouterr().err
    assert f"wiring: {expect[0]} {expect[1]}" in err
    assert "wiring: good" not in err


def test_every_non_wired_part_is_named(tmp_path, capsys):
    plan = _plan(
        tmp_path,
        "wiring:\n  - id: a1\n    status: not_built\n  - id: b2\n    status: blocked\n",
    )
    assert _stamp(plan) != 0
    err = capsys.readouterr().err
    assert "a1 not_built" in err and "b2 blocked" in err


def test_malformed_wiring_refuses(tmp_path, capsys):
    plan = _plan(tmp_path, "wiring: nonsense\n")
    assert _stamp(plan) != 0
    assert "wiring: malformed" in capsys.readouterr().err
    assert "status: implemented" not in plan.read_text(encoding="utf-8")


def test_override_reason_does_not_discharge(tmp_path):
    plan = _plan(tmp_path, "wiring:\n  - id: a1\n    status: not_built\n")
    assert _stamp(plan, "--override-reason", "ship it") != 0
    assert "status: implemented" not in plan.read_text(encoding="utf-8")


def test_all_wired_stamps(tmp_path):
    plan = _plan(tmp_path, "wiring:\n" + _wired_part())
    assert _stamp(plan) in (0, 2)
    assert "status: implemented" in plan.read_text(encoding="utf-8")


def test_absent_wiring_is_legacy_pass(tmp_path):
    plan = _plan(tmp_path, "")
    assert _stamp(plan) in (0, 2)
    assert "status: implemented" in plan.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "evidence, reason",
    [
        (_entry(sha="0" * 40), "evidence-sha-mismatch"),
        (_entry(dirty="true"), "evidence-dirty-tree"),
        (_REAL_EVIDENCE, "evidence-sha-missing"),
    ],
)
def test_evidence_not_from_clean_head_refuses(tmp_path, capsys, evidence, reason):
    plan = _plan(
        tmp_path,
        "wiring:\n  - id: e1\n    status: wired\n    built_path: built.py\n"
        f"    runs_evidence: [{evidence}]\n",
    )
    assert _stamp(plan) != 0
    assert reason in capsys.readouterr().err
    assert "status: implemented" not in plan.read_text(encoding="utf-8")
