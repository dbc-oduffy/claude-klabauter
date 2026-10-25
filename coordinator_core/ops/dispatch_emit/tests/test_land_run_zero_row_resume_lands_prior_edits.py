"""A zero-row ledger-resume landing commits the product paths the run's earlier legs and review recorded
(leg-footprint ``writes``, review ``tests.edited``/``created``) that are still dirty, and nothing else
dirty in the tree."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    _second_run,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    _git,
    _put,
    _write,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import _composed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
EDITED, CREATED, CLEAN, REVERTED, FOREIGN = "pkg/edited.py", "pkg/created.py", "pkg/clean.py", "pkg/reverted.py", "pkg/foreign.py"


def _prior_fire(root) -> None:
    """Tracked product files at HEAD, the earlier fire's edits left dirty, and the run's leg footprint."""
    for rel in (EDITED, CLEAN, REVERTED, FOREIGN):
        _write(root, rel, "v = 1\n")
    _git(root, "add", "pkg")
    _git(root, "commit", "-q", "-m", "prior product")
    _put(root, EDITED, "v = 2\n")
    _put(root, REVERTED, "v = 2\n")
    _put(root, FOREIGN, "v = 2\n")
    _put(root, CREATED, "v = 1\n")


def _terminal(root, resume, *, tests=None, footprint=(EDITED, CLEAN, REVERTED)) -> dict:
    if resume is not None:
        resume = {**resume, "unlanded": []}  # a resume with nothing unlanded owes no since-halt diff
    token = _second_run(root)
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/{LEG_FOOTPRINT_FILE}", json.dumps({"writes": list(footprint)}))
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": {"integration_stem": "rev-stem", "slices": 1, "fixes": 0, "tests": tests or {}},
            "session_id": _SID, "ledger_verdict": _MET, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def _show(root, sha: str, rel: str) -> str:
    return _git(root, "show", f"{sha}:{rel}")


def test_recorded_dirty_paths_land_and_foreign_clean_and_reverted_stay_out(halted):  # noqa: F811
    resume = _composed(halted)
    _prior_fire(halted)

    out = _terminal(
        halted, resume, tests={"created": [CREATED], "reverted": [REVERTED]},
    )

    assert out["committed"] is True and out.get("refused") is None, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert {EDITED, CREATED} <= landed
    assert not {FOREIGN, CLEAN, REVERTED} & landed
    assert _show(halted, out["sha"], EDITED) == "v = 2\n"
    assert out["resume_recorded"]["landed"] == sorted([CREATED, EDITED])
    assert out["resume_recorded"]["withheld"] == {REVERTED: "reverted"}
    status = _git(halted, "status", "--porcelain", "--", "pkg")
    assert FOREIGN in status and REVERTED in status and EDITED not in status and CREATED not in status


def test_a_fresh_fire_does_not_widen_its_landing_set(halted):  # noqa: F811
    _prior_fire(halted)

    out = _terminal(halted, None)

    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split()) if out.get("committed") else set()
    assert not {EDITED, CREATED, FOREIGN} & landed, out
    assert "resume_recorded" not in out
    assert EDITED in _git(halted, "status", "--porcelain", "--", "pkg")


def test_a_recorded_path_deleted_from_the_tree_never_refuses_the_landing(halted):  # noqa: F811
    resume = _composed(halted)
    _prior_fire(halted)
    (halted / EDITED).unlink()

    out = _terminal(halted, resume, tests={"created": [CREATED]})

    assert out["committed"] is True and out.get("refused") is None, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert CREATED in landed and EDITED not in landed
