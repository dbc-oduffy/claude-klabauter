"""An `implemented` plan closes its sizing and baton, whether the stamp flips it or finds it flipped.

Run: python -m pytest coordinator_core/ops/tests/test_plan_status_transition_closes_sizing.py -q
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
    _seed_sizing,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/2026-10-10-closes-its-sizing.md"
_DLV = "dlv-closes-its-sizing-000001"


def _commit_plan(repo, status: str) -> None:
    plan = repo / _PLAN
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: T\nstatus: {status}\ndeliverable_id: {_DLV}\n"
        "sizing_object: state/sizings/s.yaml\n---\n\nBody.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")


def _baton_state(path) -> str:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    return read_fm_field_unquoted(split.fm_text, "deployment_state")


def _sizing_status(path) -> str:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["status"]


def test_stamping_a_plan_implemented_ships_its_sizing_and_baton(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id=_DLV, plan=_PLAN)
    baton = _seed_handoff(repo, "b.md", deliverable_id=_DLV)
    _commit_plan(repo, "approved")

    assert main(["stamp-implemented", "--plan", str(repo / _PLAN)]) == 0

    assert _sizing_status(sizing) == "shipped"
    assert _baton_state(baton) == "shipped"


def test_a_plan_already_implemented_on_disk_still_closes_its_plan_fk_sizing(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id="null", plan=_PLAN)
    _commit_plan(repo, "implemented")

    assert _sizing_status(sizing) == "routed"
    assert main(["stamp-implemented", "--plan", str(repo / _PLAN)]) == 0

    assert _sizing_status(sizing) == "shipped"


def test_a_repeat_stamp_on_a_closed_sizing_changes_nothing(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "s.yaml", deliverable_id="null", plan=_PLAN)
    _commit_plan(repo, "implemented")
    assert main(["stamp-implemented", "--plan", str(repo / _PLAN)]) == 0
    closed = sizing.read_text(encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD").stdout

    assert main(["stamp-implemented", "--plan", str(repo / _PLAN)]) == 0

    assert sizing.read_text(encoding="utf-8") == closed
    assert _git(repo, "rev-parse", "HEAD").stdout == head
