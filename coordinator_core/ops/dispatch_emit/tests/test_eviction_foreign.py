"""eviction_foreign: live holders, foreign hunk owners and owner notices of a halt eviction."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.eviction_foreign import live_holders

REL = "f.py"
HEAD = [f"line{i}\n" for i in range(1, 11)]
EDIT_A = {1: "A edit of line2\n"}
EDIT_B = {7: "B edit of line8\n"}


def test_live_holders_empty_when_run_unknown_or_read_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert live_holders(tmp_path, None, "f.py") == {}

    def boom(*a, **k):
        raise RuntimeError("deadline")

    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", boom)
    assert live_holders(tmp_path, "run-a", "f.py") == {}
