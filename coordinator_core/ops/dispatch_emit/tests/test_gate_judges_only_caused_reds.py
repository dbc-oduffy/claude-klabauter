"""Regression for run ask-20261114T134300-de71e6: a corpus node red on a peer file outside the run's writes must not
hold a row, while a finding in a run-written file still does.

One inertness-style node id, replayed through the Python `classify_reds` and through the composed ask script's JS
legs at default arguments: the review-recover leg (cases a-c) and the ledger-recover leg (d, e), the leg that held
de71e6's C1."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.self_correction import (
    FOREIGN_FIELD,
    HALT_FIX_LABEL,
    HALT_REVERIFY_LABEL,
    SELF_CORRECTION_FIELD,
    classify_reds,
)
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_ledger_recovery_loop as loop
from coordinator_core.ops.dispatch_emit.tests import test_halt_self_correction_replays_e2e as e2e
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NODE = "coordinator/tests/test_bin_entrypoint_inertness.py::test_inert"
_PEER = "coordinator/bin/unimatrix-peer-router.py"
_OWN_FILE = "coordinator/bin/update-docs-bookkeeping.py"
_OWN_TEST = "coordinator/bin/tests/test_update_docs_bookkeeping_cli.py"
_OWN = [_OWN_FILE, _OWN_TEST]
_PEER_MSG = f"inert at import:\n  {_PEER}:1304 spawns a process"
_OWN_MSG = f"inert at import:\n  {_PEER}:1304 spawns a process\n  {_OWN_FILE}:40 opens a file"

_DISK = {**e2e._DISK, "rows": [{**e2e._ROWS[0], "writes": _OWN}], "review_declared_paths": _OWN}
_SCOPE = {"status": "SCOPE", "paths": [], "reason": "needs a path nobody declared", "needs_paths": ["x/y.py"]}


def _detail(message: str) -> list[dict]:
    return [{"node_id": _NODE, "message": message}]


# --- Python classifier at default arguments -----------------------------------------------------------------


def test_py_a_peer_file_only_finding_is_foreign():
    r = classify_reds([_NODE], _OWN, detail={_NODE: _PEER_MSG})
    assert r.owned == () and r.foreign == (_NODE,)


def test_py_b_a_run_written_file_in_the_message_is_owned():
    r = classify_reds([_NODE], _OWN, detail={_NODE: _OWN_MSG})
    assert r.owned == (_NODE,) and r.foreign == ()


def test_py_c_a_pre_existing_node_is_foreign_even_naming_a_run_written_file():
    r = classify_reds([_NODE], _OWN, pre_existing=[_NODE], detail={_NODE: _OWN_MSG})
    assert r.owned == () and r.foreign == (_NODE,)


# --- JS review-recover leg through the composed script ------------------------------------------------------


def _review(monkeypatch, message: str, *, pre_existing=(), replies=None, wave=("CLEAN", 0)) -> dict:
    record = e2e._record([_NODE], failing_detail=_detail(message), pre_existing=list(pre_existing))
    return e2e._fresh_run(monkeypatch, {
        e2e._REVIEW_LABEL: e2e._wave(wave[0], wave[1], 0, _OWN), e2e._REVERIFY_LABEL: record, **(replies or {}),
    }, _DISK)


def test_js_a_peer_file_only_finding_runs_no_fix_leg_and_does_not_halt(monkeypatch):
    out = _review(monkeypatch, _PEER_MSG)

    labels = e2e._labels(out)
    assert HALT_FIX_LABEL not in labels and HALT_REVERIFY_LABEL not in labels and "halt-commit" not in labels, labels
    assert out["result"]["halted_by"] is None
    assert "commit" in labels
    assert out["result"]["review"]["reverify"][FOREIGN_FIELD] == [_NODE]


def test_js_b_a_run_written_file_in_the_message_makes_the_red_owned_and_runs_the_fix_leg(monkeypatch):
    out = _review(monkeypatch, _OWN_MSG, replies={HALT_FIX_LABEL: _SCOPE})

    assert e2e._labels(out).count(HALT_FIX_LABEL) == 1, e2e._labels(out)
    assert not out["result"]["review"]["reverify"].get(FOREIGN_FIELD)


def test_js_b_a_run_written_file_in_the_message_holds_a_blocked_review_wave(monkeypatch):
    out = _review(monkeypatch, _OWN_MSG, replies={HALT_FIX_LABEL: _SCOPE}, wave=("BLOCKED", 1))

    assert e2e._labels(out).count(HALT_FIX_LABEL) == 1
    halted = out["result"]["halted_by"]
    assert halted["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert halted[SELF_CORRECTION_FIELD]["status"] == "scope"


def test_js_c_a_pre_existing_node_is_foreign_whatever_the_message_names(monkeypatch):
    out = _review(monkeypatch, _OWN_MSG, pre_existing=[_NODE])

    labels = e2e._labels(out)
    assert HALT_FIX_LABEL not in labels and "halt-commit" not in labels, labels
    assert out["result"]["halted_by"] is None
    assert "commit" in labels


# --- JS ledger-recover leg: the de71e6 replay ---------------------------------------------------------------

_LEDGER_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": _OWN, "wave": 0, "deps": [], "register_rows": ["R1"]},
    {"id": "C1b", "brief_path": "b/C1b.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["coordinator/bin/update-docs-bookkeeping.cmd"], "wave": 1, "deps": ["C1"], "register_rows": ["R2"]},
]
_LEDGER_DISK = {**loop._DISK, "rows": _LEDGER_ROWS, "review_declared_paths": _OWN}
_DE71E6_PROSE = (
    "C1: Notes N1–N4 are in place and the C1 tests pass, but the inertness gate is still red on peer files "
    f"(e.g. `{_PEER.rsplit('/', 1)[-1]}:1304`)."
)


def _ledger(monkeypatch, remaining: str) -> dict:
    blocked = {"status": "BLOCKED", "paths": [], "reason": "no"}
    return loop._run(
        loop._script(monkeypatch), items=[loop._unmet([remaining], rid="R1")], manifest=_LEDGER_DISK,
        replies={"ledger-fix:C1": blocked},
    )


def _assert_c1_held(out: dict) -> None:
    """The ledger item stayed blocking: its fix leg ran, blocked, and C1 is withheld from the landing."""
    assert loop._fix_labels(out) == ["ledger-fix:C1"], e2e._labels(out)
    assert loop._record(out)["chunks"]["C1"]["status"] == "blocked"
    assert "C1" in out["result"]["incomplete"]


def test_d_de71e6_peer_file_prose_is_foreign_only_and_holds_neither_c1_nor_c1b(monkeypatch):
    out = _ledger(monkeypatch, _DE71E6_PROSE)

    assert loop._fix_labels(out) == [], e2e._labels(out)
    res = out["result"]
    assert res["halted_by"] is None
    assert "C1" not in res["incomplete"] and "C1b" not in res["incomplete"]
    assert "commit" in e2e._labels(out)


@pytest.mark.parametrize("named", [f"{_OWN_FILE}:40", "update-docs-bookkeeping.py:40"])
def test_e_prose_naming_a_run_written_file_stays_blocking(monkeypatch, named):
    out = _ledger(monkeypatch, f"C1: the inertness gate is still red on `{named}`.")

    _assert_c1_held(out)


def test_e_prose_naming_no_file_stays_blocking(monkeypatch):
    _assert_c1_held(_ledger(monkeypatch, "C1: the inertness gate is still red on peer files."))
