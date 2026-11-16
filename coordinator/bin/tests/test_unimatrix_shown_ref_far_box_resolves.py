"""The ref a bridged row, delivery record and failure notice show is the `<bare>.<box>` address the far relay resolves."""
import importlib.util
import io
import json
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
PIPE = "\\\\.\\pipe\\LOCAL\\cc-msg-holo"
SID = "sid-holo"


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod = _load("unimatrix-relay.py", "shown_ref_relay")
    monkeypatch.setattr(mod, "local_box", lambda: "machine-a")
    monkeypatch.setattr(mod, "endpoint_kind", lambda platform=None: "pipe")
    monkeypatch.setattr(mod, "_router_resolve", lambda name: None)
    monkeypatch.setattr(mod, "entries", lambda: iter(
        [(11, {"name": "example-game-repo-19", "sessionId": SID, "messagingSocketPath": PIPE}, "11.key"),
         (12, {"name": "claude-klabauter-c6", "sessionId": "sid-other", "messagingSocketPath": PIPE + "2"}, "12.key")]))
    return mod


def test_relay_resolves_the_shown_name_box_to_its_own_bare_session(relay):
    assert relay._session_path(11, expected_name_digest=relay.name_digest("example-game-repo-19.machine-a")) == PIPE
    assert relay._REACHED["session"] == SID


def test_relay_still_resolves_the_bare_name(relay):
    assert relay._session_path(11, expected_name_digest=relay.name_digest("example-game-repo-19")) == PIPE


@pytest.mark.parametrize("ref", ["example-game-repo-19.mini", "claude-klabauter-c6.machine-a", "example-game-repo-19.machine-a.machine-a"])
def test_relay_refuses_a_ref_that_is_not_this_sessions_own_box_address(relay, ref):
    with pytest.raises(SystemExit) as refused:
        relay._session_path(11, expected_name_digest=relay.name_digest(ref))
    assert str(refused.value).startswith("target mismatch")


def test_receipt_and_delivery_rows_quote_the_name_box_ref(relay, monkeypatch, capsys):
    class Stdin:
        buffer = io.BytesIO(b'{"from":"uds:/a","message":"x"}\n')

    monkeypatch.setattr(relay.sys, "stdin", Stdin)
    monkeypatch.setattr(relay, "_write_line", lambda p, l: None)
    relay.send_batch(11, None, relay.name_digest("example-game-repo-19.machine-a"), SID)
    reply = json.loads(capsys.readouterr().out)
    assert reply["reached_ref"] == "example-game-repo-19.machine-a"
    (receipt,) = relay.receipts(SID, 0.0)["rows"]
    assert receipt["ref"] == "example-game-repo-19.machine-a" and "[" not in receipt["ref"]
    rows = [json.loads(x) for x in relay.deliveries_path().read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["ref"] == "example-game-repo-19.machine-a"


def test_failure_notice_and_delivery_row_quote_the_name_box_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge = _load("unimatrix-bridge.py", "shown_ref_bridge")
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    sent = []
    monkeypatch.setattr(bridge, "write_local", lambda sender, frame: sent.append(json.loads(frame)["message"]))
    try:
        line = b'{"from": "uds:/s.sock", "message": "hi"}'
        bridge._record_outcome(line, "example-game-repo-19 (pid 7, session sid)", "t", 5, "ssh exit 255", "machine-a", "ssh",
                               "example-game-repo-19.machine-a")
        bridge._record_outcome(line, "example-game-repo-19 (pid 7, session sid)", "t", 5, None, "machine-a", "ssh",
                               "example-game-repo-19.machine-a")
        (row_fail, row_ok) = [json.loads(x) for x in bridge.deliveries_path().read_text(encoding="utf-8").splitlines()]
        assert row_fail["ref"] == row_ok["ref"] == "example-game-repo-19.machine-a"
        assert any("FAILED" in s and "example-game-repo-19.machine-a" in s for s in bridge.delivery_status())
    finally:
        bridge.log.close()
    (notice,) = sent
    assert notice.startswith("Delivery failed: your message to example-game-repo-19.machine-a (")
    assert "[" not in notice.split("did not reach")[0]
