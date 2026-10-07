"""settle_warm_miss: the single warm-miss policy shared by the cold CLI and rung 3."""

from __future__ import annotations

import pytest

MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


@pytest.fixture
def client(monkeypatch):
    import coordinator_core.warm.client as wc

    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0.5")
    monkeypatch.setattr(wc, "last_cold_reason", lambda: "")
    monkeypatch.setattr("coordinator_core.invoke.warm_miss._BOOT_POLL_MIN_SECS", 0.01)
    monkeypatch.setattr("coordinator_core.ipc.is_unstamped_dispatch_allowed", lambda: False)
    return wc


def test_served_within_bound_returns_response_quietly(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    calls = []

    def fake(msg, read_deadline_secs=None):
        calls.append(msg)
        return {"jsonrpc": "2.0", "id": 1, "result": "ok"} if len(calls) >= 2 else None

    monkeypatch.setattr(client, "try_warm_dispatch", fake)
    response = settle_warm_miss(MSG)
    assert response is not None
    assert response["result"] == "ok"
    err = capsys.readouterr().err
    assert "waiting up to" in err
    assert "ENGINE UNREACHABLE" not in err


def test_permanent_reason_skips_wait_and_prints_loud_line_once(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    monkeypatch.setattr(client, "last_cold_reason", lambda: "ENGINE_SKEW")
    monkeypatch.setattr(
        client, "try_warm_dispatch", lambda *a, **k: pytest.fail("must not poll")
    )
    assert settle_warm_miss(MSG) is None
    err = capsys.readouterr().err
    assert "waiting up to" not in err
    assert err.count("ENGINE UNREACHABLE") == 1
    assert "COLD: ENGINE_SKEW" in err


def test_unstamped_by_ruling_prints_cold_by_design_not_defect(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    reason = (
        "warm engine: this clone carries no engine build stamp, so it is not a "
        "warm-server host -- by ruling, not by defect."
    )
    monkeypatch.setattr(client, "last_cold_reason", lambda: reason)
    assert settle_warm_miss(MSG) is None
    err = capsys.readouterr().err
    assert "ENGINE UNREACHABLE" not in err
    assert "This is a defect" not in err
    assert err.count("runs cold by design") == 1


def test_expiry_prints_loud_line_once(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    monkeypatch.setattr(client, "try_warm_dispatch", lambda *a, **k: None)
    assert settle_warm_miss(MSG) is None
    err = capsys.readouterr().err
    assert err.count("ENGINE UNREACHABLE") == 1
    assert "running ping COLD: no warm server answered within" in err


def test_in_process_serve_prints_terse_line_not_defect_text(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import serving_in_process, settle_warm_miss

    monkeypatch.setattr(client, "try_warm_dispatch", lambda *a, **k: None)
    with serving_in_process():
        assert settle_warm_miss(MSG) is None
    err = capsys.readouterr().err
    assert "ENGINE UNREACHABLE" not in err
    assert "defect" not in err
    assert "COLD" not in err
    assert err.count("path=in-process") == 1

    assert settle_warm_miss(MSG) is None
    assert "ENGINE UNREACHABLE" in capsys.readouterr().err


def test_wait_off_prints_loud_line_without_waiting(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    assert settle_warm_miss(MSG) is None
    err = capsys.readouterr().err
    assert "boot wait is off" in err
    assert err.count("ENGINE UNREACHABLE") == 1


def test_allow_unstamped_carve_out_goes_cold_quietly(client, monkeypatch, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    monkeypatch.setattr("coordinator_core.ipc.is_unstamped_dispatch_allowed", lambda: True)
    monkeypatch.setattr(
        client, "try_warm_dispatch", lambda *a, **k: pytest.fail("must not poll")
    )
    assert settle_warm_miss(MSG) is None
    assert capsys.readouterr().err == ""


def test_main_reexports_moved_names():
    import coordinator_core.invoke.__main__ as m
    import coordinator_core.invoke.warm_miss as w

    for name in ("_wait_for_warm_boot", "_warm_boot_wait_deadline", "_warm_miss_wait_secs",
                 "WARM_BOOT_WAIT_SECS"):
        assert getattr(m, name) is getattr(w, name)
