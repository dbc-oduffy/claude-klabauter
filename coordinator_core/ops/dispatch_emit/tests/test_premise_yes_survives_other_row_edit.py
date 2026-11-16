"""A recorded premise yes is keyed on the premise row, never on the rest of the plan text."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts
from coordinator_core.ops.dispatch_emit.tests.test_premise_verdict_widened import (
    ADDED,
    _record,
    _repo,
    _stage,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _edit(plan, old: str, new: str) -> None:
    text = plan.read_text(encoding="utf-8")
    assert old in text
    plan.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    - docs/x/c2a.md", "    - docs/x/c2a.md\n    - docs/x/c2a_more.md"),
        ("# p\n", "# p\n\nA body paragraph the run added.\n"),
        ("title: p\n", "title: p amended\n"),
    ],
)
def test_recorded_yes_survives_an_edit_to_another_part_of_the_plan(tmp_path, old, new):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "widened", added_scope=ADDED)
    _record(repo, plan, "yes")
    _edit(plan, old, new)

    reply = _stage(repo)

    assert "error" not in reply, reply
    assert {r["id"] for r in reply["rows"]} == {"C2a", "C2b"}


def test_widened_still_refuses_after_an_edit_to_another_row(tmp_path):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "widened", added_scope=ADDED)
    _edit(plan, "    - docs/x/c2a.md", "    - docs/x/c2a.md\n    - docs/x/c2a_more.md")

    assert "PREMISE-WIDENED" in _stage(repo)["error"]


def test_recorded_no_still_closes_after_an_edit_to_another_row(tmp_path):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "no")
    _edit(plan, "    - docs/x/c2b.md", "    - docs/x/c2b.md\n    - docs/x/c2b_more.md")

    err = _stage(repo)["error"]

    assert "zero dispatchable rows" in err and "PREMISE-WIDENED" not in err


def test_evidence_sidecar_is_halt_landable_and_seam_sidecar_is_not():
    assert "evidence-sidecar" in run_artifacts.HALT_LANDABLE_CLASSES
    assert "seam-sidecar" not in run_artifacts.HALT_LANDABLE_CLASSES
    assert "slice-sizing" not in run_artifacts.HALT_LANDABLE_CLASSES
