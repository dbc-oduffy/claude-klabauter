"""Pins coordinator/lib/spawn_detect.py as a byte-for-byte copy of spawn_policy/detect.py."""

from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_spawn_detect_mirror_is_byte_identical():
    source = _ROOT / "coordinator_core" / "spawn_policy" / "detect.py"
    mirror = _ROOT / "coordinator" / "lib" / "spawn_detect.py"
    assert mirror.read_bytes() == source.read_bytes()
