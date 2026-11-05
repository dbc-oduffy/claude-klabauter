"""`_retire_stale_forwarder` never signals this process or an ancestor."""

import os

from coordinator_core.box_memory import orphan_reap
from coordinator_core.hooks import sessionstart_ensure_http_forwarder as fwd


def test_retire_refuses_self_and_ancestor_and_kills_unrelated(monkeypatch):
    sent = []
    monkeypatch.setattr(fwd.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(orphan_reap, "protected_pids", lambda self_pid=None: frozenset({os.getpid(), 7}))
    assert fwd._retire_stale_forwarder(os.getpid()) is False
    assert fwd._retire_stale_forwarder(7) is False
    assert sent == []
    assert fwd._retire_stale_forwarder(987654) is True
    assert sent == [987654]
