"""A ready entry whose readyOpReply carries `error` and no manifest_handle halts as a stage refusal with that
text and its owed ids; error-with-handle and error-less handle-less replies still halt as not whole."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_plan_blitz_ready_restage import (  # noqa: F401
    _PLAN,
    _DISK,
    _labels,
    _run,
    _script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    reader_file,
    readers_cfg,
    stage_reply,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ERR = "design-review: route canvas, board docs/design/people-design/project/; the PM has not approved the canvas board"


def test_an_error_only_readyop_reply_halts_as_stage_refused_with_the_error(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": {"error": _ERR}}))
    halt = out["result"]["halted_by"]
    assert halt["reason"] == "stage refused: " + _ERR
    assert "blitz-stage-read" not in _labels(out)


def test_an_error_only_readyop_reply_keeps_its_owed_ids(monkeypatch):
    reply = {"error": "boom", "owed_em_ids": ["C9"], "owed_plan": _PLAN}
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": reply}))
    halt = out["result"]["halted_by"]
    assert halt["kind"] == "refusal"
    assert halt["reason"].startswith("stage refused: boom")
    assert "C9" in str(halt)


def test_an_error_with_a_handle_still_halts_as_not_whole(monkeypatch):
    reply = {**stage_reply(_DISK), "error": _ERR}
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": reply}))
    assert out["result"]["halted_by"]["reason"].startswith("stage manifest relay is not whole")


def test_a_handleless_errorless_reply_still_halts_as_not_whole(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": {"rows": []}}))
    assert out["result"]["halted_by"]["reason"] == "stage manifest relay is not whole: manifest_handle"
