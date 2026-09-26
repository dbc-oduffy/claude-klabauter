
from __future__ import annotations

import os

from coordinator_core.install import substrate


def test_writing_one_slot_leaves_its_hardlinked_siblings_alone(tmp_path):
    door = tmp_path / "door-image"
    door.write_bytes(b"\xcf\xfa\xed\xfe native door")
    slot, sibling = tmp_path / "slot-a", tmp_path / "slot-b"
    os.link(door, slot)
    os.link(door, sibling)

    substrate._write_agent_forwarder("slot-a", slot, False, target="slot-a.py")

    assert sibling.read_bytes() == b"\xcf\xfa\xed\xfe native door"
    assert door.read_bytes() == b"\xcf\xfa\xed\xfe native door"
    assert b'exec_cli("slot-a.py")' in slot.read_bytes()
    assert os.stat(slot).st_nlink == 1
    assert os.access(slot, os.X_OK)
    assert not list(tmp_path.glob(".*.tmp"))
