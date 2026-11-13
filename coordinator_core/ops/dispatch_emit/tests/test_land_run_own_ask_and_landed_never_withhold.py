"""dispatch.land_run: a path held only by the landing run's own pre-fire ask token, or by an ask run whose
sizing's successor already landed, lands; a path held by a live foreign ask run still refuses."""

from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit import run_token, sibling_claims
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, stamp_run_stage
from coordinator_core.ops.dispatch_emit.tests.test_land_run_live_siblings_one_path import (
    BASE,
    BOTH_HUNKS,
    F,
    SELF,
    SENIOR_SESSION,
    SIZING,
    _author,
    _declare_run,
    _dirty,
    _git,
    _land,
    _write,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_live_siblings_one_path import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING_B = "state/sizings/20260102-b.yaml"


def _stamp(seconds_ago: int) -> str:
    """A run-id stamp inside the token TTL, in the UTC form the engine parses."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - seconds_ago))


PREFIRE = f"ask-{_stamp(60)}-aaaaaa"
FOREIGN_ASK = f"ask-{_stamp(60)}-bbbbbb"
LANDING = f"warp-20260101-a-{_stamp(20)}-cccccc"
OTHER_ASK = f"ask-{_stamp(60)}-dddddd"
OTHER_SUCCESSOR = f"warp-20260102-b-{_stamp(10)}-eeeeee"


def _bind_sizing_b(root) -> None:
    _write(root, SIZING_B, (root / SIZING).read_text(encoding="utf-8"))


def _hold(root, run_id: str, sizing: str, session: str) -> str:
    """A live ask run of `session` declaring F, bound to `sizing`; returns its token."""
    _bind_sizing_b(root)
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", f'{{"sizing_rel": "{sizing}"}}')
    token = _declare_run(root, run_id, (F,), session=session)
    assert run_token.bind_sizing(root, run_id, sizing)
    return token


def _landing_run(root) -> str:
    """The landing run, minted after every holder, firing from SELF over SIZING."""
    time.sleep(0.05)
    token = _declare_run(root, LANDING, (F,), session=SELF)
    stamp_run_stage(root, LANDING, "minted", session_id=SELF)
    _author(root, F, BOTH_HUNKS)
    return token


def _assert_holds(root, token: str) -> None:
    out = _land(root, LANDING, token)
    assert out.get("committed") is not True, out
    assert out.get("refused"), out
    assert F in _dirty(root)
    assert _git(root, "show", f"HEAD:{F}") == BASE


def test_a_path_held_only_by_the_landing_runs_own_pre_fire_ask_lands(repo):
    _hold(repo, PREFIRE, SIZING, SELF)
    token = _landing_run(repo)

    out = _land(repo, LANDING, token)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert _git(repo, "show", f"HEAD:{F}") == BOTH_HUNKS
    assert F not in _dirty(repo)
    assert PREFIRE not in str(out.get("withheld", "")), out


def test_a_path_held_only_by_an_ask_run_whose_sizings_successor_landed_lands(repo):
    _hold(repo, OTHER_ASK, SIZING_B, SENIOR_SESSION)
    _write(repo, f"{RECORD_DIR_ROOT}/{OTHER_SUCCESSOR}/run.json", f'{{"sizing_rel": "{SIZING_B}"}}')
    _write(repo, f"{OTHER_SUCCESSOR}.mjs", "// emitted\n")
    run_token.mint(repo, OTHER_SUCCESSOR, f"{OTHER_SUCCESSOR}.mjs", session_id=SENIOR_SESSION)
    run_token.consume(repo, OTHER_SUCCESSOR, landed={"sha": "a" * 40, "rows": [], "paths": []})
    token = _landing_run(repo)
    assert sibling_claims.released_ask_holder(repo, OTHER_ASK, LANDING) == sibling_claims.RELEASED_LANDED_ASK

    out = _land(repo, LANDING, token)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert _git(repo, "show", f"HEAD:{F}") == BOTH_HUNKS


def test_a_path_held_by_a_live_foreign_ask_run_of_the_same_sizing_still_refuses(repo):
    _hold(repo, FOREIGN_ASK, SIZING, SENIOR_SESSION)
    token = _landing_run(repo)

    _assert_holds(repo, token)


def test_a_live_ask_run_of_another_sizing_with_no_landed_successor_still_refuses(repo):
    _hold(repo, OTHER_ASK, SIZING_B, SENIOR_SESSION)
    token = _landing_run(repo)

    _assert_holds(repo, token)


def test_released_ask_holder_names_the_reason_and_never_frees_a_foreign_session(repo):
    _hold(repo, PREFIRE, SIZING, SELF)
    _hold(repo, FOREIGN_ASK, SIZING, SENIOR_SESSION)
    _landing_run(repo)

    assert sibling_claims.released_ask_holder(repo, PREFIRE, LANDING) == sibling_claims.RELEASED_OWN_PREFIRE
    assert sibling_claims.released_ask_holder(repo, FOREIGN_ASK, LANDING) is None
    assert sibling_claims.released_ask_holder(repo, LANDING, LANDING) is None
