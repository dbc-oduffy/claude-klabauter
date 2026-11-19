"""A halted landing retries an indeterminate peer-claim read once, then lands the run's own declared paths;
only a positive live hold, or a path the run never declared, stays withheld."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halt_record as lrh
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INDETERMINATE = "peer-claim-indeterminate"
BODY = "d = 1\n"


@pytest.fixture
def reads(monkeypatch):
    """Every claim lookup answers 'indeterminate'; `reads` counts the lookups made."""
    calls: list = []

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        calls.append(list(paths))
        return [f"claim state indeterminate for {p!r}" for p in paths]

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)
    return calls


def _stage(repo, declared: list) -> None:
    _write_record(repo, RUN)
    (repo / "done.py").write_bytes(BODY.encode("utf-8"))
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    rec = repo / RECORD_DIR_ROOT / RUN
    (rec / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _record_commit(repo, declared: list) -> dict:
    _stage(repo, declared)
    return _call(repo, LEDGER, **PASSING)


def _run_halt(repo, declared: list) -> dict:
    _stage(repo, declared)
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, **PASSING}
    return lrh.land_run_halt.land(repo, repo, params)


LANDERS = pytest.mark.parametrize("lander", [_record_commit, _run_halt], ids=["halted_record_commit", "land_run_halt"])


@LANDERS
def test_declared_path_is_not_withheld_on_an_indeterminate_read(repo, reads, lander):
    out = lander(repo, ["done.py"])

    assert out["withheld"].get("done.py") != INDETERMINATE


@LANDERS
def test_undeclared_unprinted_path_stays_withheld_on_an_indeterminate_read(repo, reads, lander):
    out = lander(repo, ["other.py"])

    assert out["withheld"]["done.py"] == INDETERMINATE


@LANDERS
def test_indeterminate_read_is_retried_once_before_crediting(repo, reads, lander):
    lander(repo, ["done.py"])

    assert sum("done.py" in paths for paths in reads) == 2
    assert reads[0] == reads[1]


@LANDERS
def test_positive_live_hold_withholds_a_declared_path(repo, monkeypatch, lander):
    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        holds.extend((p, "peer-sid") for p in paths if p == "done.py")
        return [f"claim state indeterminate for {p!r}" for p in paths if p != "done.py"]

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)

    out = lander(repo, ["done.py"])

    assert out["withheld"]["done.py"] == "peer-held"


@LANDERS
def test_retry_that_resolves_indeterminate_withholds_nothing_as_indeterminate(repo, monkeypatch, lander):
    seen: list = []

    def fake(root, paths, holds=None, own_sid=None, abandoned_holds=None, held_out=None):
        seen.append(1)
        return [f"claim state indeterminate for {p!r}" for p in paths] if len(seen) == 1 else []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", fake)

    out = lander(repo, ["other.py"])

    assert out["withheld"].get("done.py") != INDETERMINATE
