"""A warm or in-process rung relays the op's error text exactly as the cold spawn does."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cc_invoke  # noqa: E402

_REASON = "fleet op setup error: summary exceeds 120 characters (got 131)"


def _refusing_op(*_args):
    print(_REASON, file=sys.stderr)
    return {"jsonrpc": "2.0", "id": 1, "result": {"exit_code": 1, "failed": []}}


def _raising_op(*_args):
    print(_REASON, file=sys.stderr)
    raise RuntimeError("cc_invoke: in-engine dispatch raised (op=memo.send)")


def test_in_engine_error_dict_reaches_the_refusal_message(monkeypatch):
    monkeypatch.setattr(cc_invoke, "_try_in_engine_dispatch", _refusing_op)
    sink: list = []

    result = cc_invoke.cc_invoke("memo.send", {}, ".", _claude_klabauter_root=".", _stderr_sink=sink)

    assert result["exit_code"] == 1
    message = cc_invoke.mutation_refusal_message("memo.send", result, op_stderr="\n".join(sink))
    assert _REASON in message
    assert "no reason reached the caller" not in message


def test_in_engine_error_dict_reaches_the_bare_ladder_too(monkeypatch):
    monkeypatch.setattr(cc_invoke, "_try_in_engine_dispatch", _refusing_op)
    sink: list = []

    cc_invoke.cc_invoke_bare("memo.send", {}, ".", _claude_klabauter_root=".", _stderr_sink=sink)

    assert _REASON in "\n".join(sink)


def test_in_engine_raising_op_keeps_its_stderr_and_raises(monkeypatch, capsys):
    monkeypatch.setattr(cc_invoke, "_try_in_engine_dispatch", _raising_op)

    with pytest.raises(RuntimeError, match="in-engine dispatch raised"):
        cc_invoke.cc_invoke("memo.send", {}, ".", _claude_klabauter_root=".")

    assert _REASON in capsys.readouterr().err


def test_warm_error_envelope_carries_the_served_stderr():
    envelope = {"error": {"code": -32603, "message": "handler raised"}}

    with pytest.raises(RuntimeError) as excinfo:
        cc_invoke._apply_warm_envelope("memo.send", envelope, _REASON + "\n", None)

    assert "handler raised" in str(excinfo.value)
    assert _REASON in str(excinfo.value)


def test_warm_error_dict_result_reaches_the_sink():
    sink: list = []
    envelope = {"result": {"exit_code": 1}}

    cc_invoke._apply_warm_envelope("memo.send", envelope, _REASON, sink)

    assert sink == [_REASON]
