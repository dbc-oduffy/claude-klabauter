"""A halted run's withhold index entry stops holding a path once HEAD carries the holder's withheld blob
or the holder's record is gone: the afd408/50fd28 shape through `land_run._needed_path_gate`.

The EM hand-landed the holder's content, the landing run then edited the same file on top (so the path is
dirty and no kept.json frees it), yet the index entry still named the holder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_withheld_index, land_run
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_needed_path_omitted import (
    CALLEE, CALLER, M_BASE, M_SIGNATURE, U_NEW, _seed,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HOLDER = "ask-20261113T133425-50fd28"
HOLDER_EDIT = M_SIGNATURE
LANDING_EDIT = M_SIGNATURE.replace("base_ref=None", "base_ref=None, extra=None")


def _plant_holder(root: Path, *, prints_text: str | None, record: bool) -> None:
    """The halted holder's index entry (prints of `prints_text`), and its record dir when `record`."""
    (root / "state/warp").mkdir(parents=True, exist_ok=True)
    halted_withheld_index.rebuild(root)  # steady state: the sentinel exists, so no later read rebuilds over the entry
    if record:
        rec = root / "state/warp" / HOLDER
        rec.mkdir(parents=True)
        (rec / "halted.json").write_text(json.dumps({
            "status": "halted", "sizing_rel": "state/sizings/holder.yaml",
            "run_artifacts": {"withheld": {CALLEE: "halt-record-only"}},
        }), encoding="utf-8")
    halted_withheld_index.add_run(
        root, HOLDER, [CALLEE], prints={CALLEE: blob_sha(prints_text.encode())} if prints_text is not None else {},
    )


def _gate(root: Path) -> tuple:
    kw = dict(
        session_id=prod.SELF, rid=prod.RUN, script_path=prod.SCRIPT, plan_rel=None, run_base=None,
        minted_before=None, own_run_ids=(prod.RUN, None),
    )
    return land_run._needed_path_gate(root, [CALLER], [], {CALLEE}, reasons={}, barred=set(), **kw)


def _hand_landed_then_edited(root: Path) -> None:
    """HEAD carries the holder's content for CALLEE; the worktree edits it further and CALLER reads the change."""
    _seed(root)
    prod._write(root, CALLEE, HOLDER_EDIT)
    prod._git(["commit", "-q", "-am", "hand-land holder content"], root)
    prod._write(root, CALLEE, LANDING_EDIT)
    prod._write(root, CALLER, U_NEW)


def test_a_path_whose_head_blob_is_the_holders_print_is_carried_not_refused(product_repo):
    _hand_landed_then_edited(product_repo)
    _plant_holder(product_repo, prints_text=HOLDER_EDIT, record=True)

    assert halted_hold_release.held_paths(product_repo, HOLDER, prod.RUN, None) == frozenset()
    assert _gate(product_repo) == ([CALLEE], [])


def test_an_index_entry_whose_record_is_gone_is_carried_not_refused(product_repo):
    _hand_landed_then_edited(product_repo)
    _plant_holder(product_repo, prints_text=None, record=False)

    assert _gate(product_repo) == ([CALLEE], [])


def test_a_path_never_hand_landed_still_refuses_against_the_holder(product_repo):
    """The other side: HEAD does not carry the holder's blob, so the hold stands."""
    _seed(product_repo)
    prod._write(product_repo, CALLEE, LANDING_EDIT)
    prod._write(product_repo, CALLER, U_NEW)
    _plant_holder(product_repo, prints_text=HOLDER_EDIT, record=True)

    assert M_BASE != HOLDER_EDIT
    carry, refusal = _gate(product_repo)
    assert carry == [] and [r["holder"] for r in refusal] == [HOLDER], refusal
