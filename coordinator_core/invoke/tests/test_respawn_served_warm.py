"""After the publish marker is released, a respawned warm server answers: no COLD fallback.

The server is a stub keyed on the breadcrumb marker; no real server is spawned and the
runtime dir is a tmp_path.
"""

from __future__ import annotations

import pytest

from coordinator_core.warm import breadcrumb

MSG = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import coordinator_core.warm.client as wc

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0.5")
    monkeypatch.setattr(wc, "last_cold_reason", lambda: "")
    monkeypatch.setattr("coordinator_core.invoke.warm_miss._BOOT_POLL_MIN_SECS", 0.01)
    monkeypatch.setattr("coordinator_core.ipc.is_unstamped_dispatch_allowed", lambda: False)
    dest = tmp_path / "dest"
    dest.mkdir()
    svc = breadcrumb.svc_dir(dest)

    def stub_server(msg, read_deadline_secs=None):
        if breadcrumb.publish_marker_held(svc):
            return None
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "warm"}

    monkeypatch.setattr(wc, "try_warm_dispatch", stub_server)
    return wc, dest


def test_reply_is_warm_served_once_marker_released(client, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    wc, dest = client
    with breadcrumb.hold_publish_marker(dest, holder="test"):
        assert wc.try_warm_dispatch(MSG) is None
    response = settle_warm_miss(MSG)
    assert response is not None and response["result"] == "warm"
    assert "ENGINE UNREACHABLE" not in capsys.readouterr().err


def test_reply_is_cold_while_marker_still_held(client, capsys):
    from coordinator_core.invoke.warm_miss import settle_warm_miss

    _wc, dest = client
    with breadcrumb.hold_publish_marker(dest, holder="test"):
        assert settle_warm_miss(MSG) is None
    assert "ENGINE UNREACHABLE" in capsys.readouterr().err
