"""PORT-OWNER-IS-NOT-A-LISTEN-STATUS (DoE 2e33d48db2): the box-memory, box-launch and app-session helpers
carry no connection-table listener lookup, and the port_owner seam is the sanctioned owner resolver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.port_owner import OwnerUnknown, parse_owner_pid
from coordinator_core.tests.test_no_listener_pid_status_reliance import _scan_text

_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
_HELPERS = (
    "box-memory-snapshot.py",
    "box-memory-tree.py",
    "box-memory-trim-ws.py",
    "box-memory-wait-pid.py",
    "box-launch-windowless.py",
    "app-session.py",
)


@pytest.mark.parametrize("name", _HELPERS)
def test_helper_has_no_listener_lookup(name):
    text = (_BIN / name).read_text(encoding="utf-8", errors="replace")
    assert _scan_text(text) == [], f"PORT-OWNER-IS-NOT-A-LISTEN-STATUS: {name}"


def test_pid_zero_and_none_are_owner_unknown_not_free():
    for raw in (0, "NONE"):
        with pytest.raises(OwnerUnknown):
            parse_owner_pid(raw)
