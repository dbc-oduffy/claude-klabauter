"""A cross-box `from=` handed to a Claude receiver is the address its roster lists for the sender."""
import asyncio
import importlib.util
import re
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
HOST = "Machine-b"
PEER = "claude-klabauter-codex-machine-b-lo-126"
FROM_RE = re.compile(r'<cross-session-message from="([^"]*)"')


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: HOST)
    return _load("unimatrix-peer-router.py", "unimatrix_peer_router_replyable_from")


def _delivered_from(router, tmp_path, far):
    """The `from=` a Claude receiver reads on a frame the Codex peer `PEER` sends it, `far` or on this box."""
    r = router.Router([], sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state", endpoint_dir=tmp_path)
    r.peers = {PEER: {"name": PEER, "vendor": "codex", "target": "019a0000-aaaa-7000-8000-000000000001"}}
    r.resolve_send_target = lambda to: {"endpoint": "/tmp/claude.sock"}
    r.last_resolved_far, r.last_resolved_row = far, None
    r.relay_route = lambda endpoint, to: (endpoint, "")
    r.endpoint_of = lambda name: "/tmp/face.sock"
    delivered = []

    async def deliver(peer, endpoint, frame):
        delivered.append(frame)

    r._deliver_as_peer = deliver
    reply = asyncio.run(r._peer_send_to_session(PEER, "machine-a-claude", "hello"))
    assert reply["ok"], reply
    return FROM_RE.search(delivered[0]["message"]["content"]).group(1)


def test_a_far_claude_is_handed_the_from_address_its_roster_lists_for_the_sender(router, tmp_path):
    roster = _load("unimatrix-roster.py", "unimatrix_roster_replyable_from")
    listed = roster.labelled(PEER, HOST)
    assert listed == f"{PEER}.{HOST}"
    handed = _delivered_from(router, tmp_path, far=True)
    assert handed == listed, f"from={handed!r} is not the roster's {listed!r}; SendMessage resolves names exactly"


def test_a_same_box_claude_keeps_the_endpoint_from_not_a_box_suffixed_name(router, tmp_path):
    handed = _delivered_from(router, tmp_path, far=False)
    assert handed.startswith("uds:/tmp/face.sock")
