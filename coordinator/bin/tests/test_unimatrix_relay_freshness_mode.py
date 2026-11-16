"""Behavior pins for the unimatrix relay `freshness` mode."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_freshness", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RESULT = {"action": "none", "local_sha": "a" * 40}


def test_parse_accepts_bare_freshness_only():
    relay = load()
    assert relay.parse_mode("freshness") == ("freshness", None)
    assert "freshness" in relay.CAPABILITIES
    for bad in ("freshness x", "freshness --fetch", "freshnessx", "fresh"):
        assert relay.parse_mode(bad) is None


def test_freshness_wraps_the_engine_result_with_the_far_box_id():
    seen = []
    reply = load().freshness(run=lambda do_fetch: seen.append(do_fetch) or RESULT, box_id="box-far")
    assert reply == {"ok": True, "box_id": "box-far", "result": RESULT} and seen == [True]


def test_engine_exception_is_a_refusal_not_a_raise():
    def boom(do_fetch):
        raise RuntimeError("x")

    assert load().freshness(run=boom, box_id="b") == {"ok": False, "reason": "freshness_failed:RuntimeError"}


def _run_main(monkeypatch, capsys):
    relay = load()
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "freshness")
    monkeypatch.setattr("sys.argv", ["unimatrix-relay.py"])
    with pytest.raises(SystemExit) as exc:
        relay.main()
    return exc.value.code, capsys.readouterr().out


def test_main_prints_ok_reply_and_exits_zero_with_a_stubbed_engine(monkeypatch, capsys):
    import types

    stub = types.ModuleType("coordinator_core.ops.fleet.klabauter_freshness")
    stub.run_freshness = lambda do_fetch=True: RESULT
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.fleet.klabauter_freshness", stub)
    monkeypatch.setattr("coordinator_core.group_em.box_registry.local_box_id", lambda home=None: "box-far")
    code, out = _run_main(monkeypatch, capsys)
    assert code == 0
    assert json.loads(out) == {"ok": True, "box_id": "box-far", "result": RESULT}


def test_main_prints_refusal_and_exits_one_when_the_engine_import_fails(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.fleet.klabauter_freshness", None)
    code, out = _run_main(monkeypatch, capsys)
    assert code == 1
    assert json.loads(out) == {"ok": False, "reason": "engine_unavailable"}
