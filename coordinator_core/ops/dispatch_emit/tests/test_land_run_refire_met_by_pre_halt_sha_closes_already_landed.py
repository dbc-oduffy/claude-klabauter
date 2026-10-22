"""A ledger-resume refire whose met rows cite a commit at or before the halt closes already_landed through
land_run; a post-halt cite, an unresolvable cite and a footprint breach stay refused."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_FOOTPRINT_FILE,
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
)
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    RUN2,
    _composed,
    _git,
    _refire,
    _write,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _verdict(met_by) -> dict:
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def _terminal(root, token, resume, verdict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": "run2", "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def _sha(root, rev: str) -> str:
    return _git(root, "rev-parse", rev).strip()


def test_met_by_the_halt_commit_closes_already_landed_and_does_not_loop(halted):  # noqa: F811
    halt = _sha(halted, "HEAD")
    resume = _composed(halted)
    refire = _refire(halted, edit=False)

    out = _terminal(halted, refire["token"], resume, _verdict([halt[:10]]))

    assert out.get("refused") is None, out
    assert out["committed"] is True and out[ALREADY_LANDED_KEY] is True, out
    assert out["sha"] == halt and _sha(halted, "HEAD") == halt
    again = _terminal(halted, refire["token"], resume, _verdict([halt[:10]]))
    assert again["committed"] is True and again["sha"] == halt, again


def test_met_by_a_commit_before_the_halt_closes_already_landed(halted):  # noqa: F811
    before = _sha(halted, "HEAD~1")
    halt = _sha(halted, "HEAD")
    resume = _composed(halted)
    refire = _refire(halted, edit=False)

    out = _terminal(halted, refire["token"], resume, _verdict([before[:8]]))

    assert out.get("refused") is None, out
    assert out[ALREADY_LANDED_KEY] is True and out["sha"] == before, out
    assert _sha(halted, "HEAD") == halt


def test_met_by_a_post_halt_commit_stays_refused(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    _git(halted, "commit", "-q", "--allow-empty", "-m", "after the halt")
    after = _sha(halted, "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict([after[:10]]))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == REFUSED
    assert _sha(halted, "HEAD") == after


def test_a_post_commit_row_citing_an_ancestor_is_refused_not_already_landed(halted):  # noqa: F811
    halt = _sha(halted, "HEAD")
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    verdict = {"items": [{"id": "R1", "status": "post_commit", "wired": True, "met_by": [halt[:10]]}]}

    out = _terminal(halted, refire["token"], resume, verdict)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert REFUSED[0] in out["ledger_failures"]
    assert not out.get(ALREADY_LANDED_KEY)
    assert _sha(halted, "HEAD") == halt


def test_a_footprint_breach_is_never_already_landed(halted):  # noqa: F811
    halt = _sha(halted, "HEAD")
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    _write(halted, f"{RUN_DIR_ROOT}/{RUN2}/{LEG_FOOTPRINT_FILE}", json.dumps({"writes": ["a.py"]}))
    _write(halted, B, "breach = 1\n")

    out = _terminal(halted, refire["token"], resume, _verdict([halt[:10]]))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert not out.get(ALREADY_LANDED_KEY)
