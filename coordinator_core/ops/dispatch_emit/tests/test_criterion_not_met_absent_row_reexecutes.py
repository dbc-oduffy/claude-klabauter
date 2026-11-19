"""A criterion_not_met halt naming a kept row absent from the tree drops it from kept, so the refire dispatches it."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

pytestmark = pytest.mark.complete

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _setup(repo, *, kind, reason, present):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open"), ("C3", "open")])
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=kind, sizing_rel=REL, plan_rel=PLAN_REL, reason=reason)
    target = repo / "some" / "C2.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('c2')\n")
    kept_work.write(record, kept_work.kept_chunks(repo, {"some/C2.py": "C2"}, {"some/C2.py": "halt-record-only"}), None)
    if not present:
        target.unlink()
    return target


def _kept(repo):  # noqa: F811
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert v.halt is None and v.resume_rows == ["C2", "C3"]
    return sink


@pytest.fixture
def _still_kept(monkeypatch):
    monkeypatch.setattr(kept_work, "still_kept", lambda *a, **k: True)


def test_named_absent_row_is_not_kept(repo, _still_kept):  # noqa: F811
    _setup(repo, kind="criterion_not_met", reason="C2 absent from the working tree", present=False)
    assert _kept(repo) == []


def test_row_whose_writes_hold_stays_kept(repo, _still_kept):  # noqa: F811
    _setup(repo, kind="criterion_not_met", reason="C2 absent from the working tree", present=True)
    assert _kept(repo) == [(["C2"], RUN)]


def test_halt_naming_no_absent_row_keeps_it(repo, _still_kept):  # noqa: F811
    _setup(repo, kind="criterion_not_met", reason="C3 failed its acceptance test", present=False)
    assert _kept(repo) == [(["C2"], RUN)]


def test_other_halt_kind_keeps_it(repo, _still_kept):  # noqa: F811
    _setup(repo, kind="commit", reason="C2 absent from the working tree", present=False)
    assert _kept(repo) == [(["C2"], RUN)]


def test_row_id_is_matched_as_a_whole_token(repo, _still_kept):  # noqa: F811
    _setup(repo, kind="criterion_not_met", reason="C22 absent from the working tree", present=False)
    assert _kept(repo) == [(["C2"], RUN)]
