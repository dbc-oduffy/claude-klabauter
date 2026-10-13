"""dispatch.ask_gate with `record`: the ruling is written through its single writer, then the sizing is gated."""

from __future__ import annotations

import time

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo
from coordinator_core.ops.sizing_accept_exit_criterion import _handler as accept
from coordinator_core.ops.sizing_record_xl_exit import _handler as record_xl

RULING = "Accepted: the criterion is observable and bounded."
EXIT_REC = {"kind": "exit_criterion", "apm_ruling": RULING, "ruling_ref": "ask-run-1"}
XL_REC = {"kind": "xl_exit", "apm_ruling": "one deliverable", "ruling_ref": "ask-run-1", "xl_exit": "accept_multi_session"}


@pytest.fixture
def repo(_bare_repo):
    (_bare_repo / ".git").mkdir()
    return _bare_repo


def _doc(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def _call(repo, **extra):
    return _handler({"sizing_path": REL, **extra}, repo)


def test_exit_criterion_record_writes_what_the_writer_writes_then_gates(repo):
    _put(repo, tshirt="XL", route="plan", mode="ceo")
    assert _call(repo)["halt"]["kind"] == "touchpoint"
    reply = _call(repo, record={**EXIT_REC, "statement": "the gate records"})
    assert reply["recorded"] == {"ok": True}
    assert reply["halt"] is None or reply["halt"]["kind"] != "touchpoint"
    got = _doc(repo)

    _put(repo, tshirt="XL", route="plan", mode="ceo")
    assert accept(
        {"sizing": REL, "apm_ruling": RULING, "ruling_ref": "ask-run-1", "statement": "the gate records"}, repo
    )["exit_code"] == 0
    assert {k: v for k, v in got.items() if k != "baton"} == _doc(repo)  # the M+ gate mints a baton


def test_xl_exit_record_writes_what_the_writer_writes_then_passes_the_room(repo):
    _put(repo, tshirt="XL", route="pm-decision", mode="ceo", accepted=ACCEPTED)
    assert _call(repo)["halt"]["apm_xl_exit"] is True
    reply = _call(repo, record=XL_REC)
    assert reply["recorded"] == {"ok": True}
    assert reply["halt"] is None or reply["halt"]["kind"] != "room"
    got = _doc(repo)

    _put(repo, tshirt="XL", route="pm-decision", mode="ceo", accepted=ACCEPTED)
    assert record_xl(
        {"sizing": REL, "xl_exit": "accept_multi_session", "apm_ruling": "one deliverable", "ruling_ref": "ask-run-1"},
        repo,
    )["exit_code"] == 0
    assert got["xl_exit"] == _doc(repo)["xl_exit"] == "accept_multi_session"
    assert got["pm_resolution"]["xl_exit_apm"] == _doc(repo)["pm_resolution"]["xl_exit_apm"]


def test_a_refused_write_reports_the_error_and_still_returns_the_original_halt(repo):
    _put(repo, tshirt="XL", route="plan", mode="pm")
    before = (repo / REL).read_text(encoding="utf-8")
    reply = _call(repo, record={**EXIT_REC, "apm_ruling": "merge to main and publish"})
    assert reply["recorded"]["ok"] is False and reply["recorded"]["error"]
    assert reply["halt"]["kind"] == "touchpoint"
    assert (repo / REL).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "kind, route",
    [("exit_criterion", "plan"), ("xl_exit", "pm-decision")],
)
def test_an_irreversible_ruling_is_refused_for_each_kind_and_leaves_the_sizing_byte_identical(repo, kind, route):
    _put(repo, tshirt="XL", route=route, mode="ceo", **({"accepted": ACCEPTED} if kind == "xl_exit" else {}))
    before = (repo / REL).read_text(encoding="utf-8")
    original = _call(repo)["halt"]
    base = XL_REC if kind == "xl_exit" else EXIT_REC
    reply = _call(repo, record={**base, "apm_ruling": "merge to main and publish"})
    assert reply["recorded"]["ok"] is False and reply["recorded"]["error"]
    assert reply["halt"] == original
    assert (repo / REL).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "record",
    [
        "x",
        {**EXIT_REC, "kind": "other"},
        {"kind": "exit_criterion", "ruling_ref": "r"},
        {"kind": "exit_criterion", "apm_ruling": "r"},
        {**EXIT_REC, "statement": "  "},
        {**XL_REC, "xl_exit": "shape"},
        {k: v for k, v in XL_REC.items() if k != "xl_exit"},
    ],
)
def test_a_malformed_record_is_refused_and_writes_nothing(repo, record):
    _put(repo, tshirt="XL", route="plan", mode="ceo")
    before = (repo / REL).read_text(encoding="utf-8")
    assert "error" in _call(repo, record=record)
    assert (repo / REL).read_text(encoding="utf-8") == before


def test_a_request_without_record_has_no_recorded_member(repo):
    _put(repo, tshirt="XL", route="plan", mode="ceo")
    assert "recorded" not in _call(repo)


def test_a_gate_call_carrying_a_record_stays_under_the_process_time_bar(repo):
    _put(repo, tshirt="XL", route="plan", mode="ceo")
    t0 = time.process_time()
    reply = _call(repo, record=EXIT_REC)
    elapsed = time.process_time() - t0  # in-process CPU only; spawn count is the amplification gate's axis
    assert reply["recorded"]["ok"] is True
    assert elapsed < 0.5, elapsed
