"""A `PUBLISHER_ONLY_TARGETS` name never gets a native door image: the door
dials a mirror that does not ship the script, so every call fails -32004. The
installer reaps a stale image and writes the claude-klabauter-resolving forwarder.

The set under test is the real one, read from the repo's own resolver.
"""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.install import substrate

_REPO = Path(__file__).resolve().parents[3]
_IMAGE = b"\xcf\xfa\xed\xfe native door image"


def _real_publisher_only() -> "frozenset[str]":
    names = substrate._publisher_only_names(_REPO)
    assert "klabauter-promote.py" in names and "percolate-push.py" in names
    return names


def test_publisher_only_names_reap_image_and_only_launchable_get_forwarder(tmp_path, monkeypatch):
    publisher_only = _real_publisher_only()
    launchable = substrate._publisher_only_names(_REPO, "PUBLISHER_ONLY_LAUNCHABLE")
    assert launchable and launchable <= publisher_only
    assert not {"publish.py", "percolate-push.py", "percolate-round.py"} & launchable
    target_map = {t[:-3]: t for t in publisher_only if t.endswith(".py")}
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    door = bin_dst / "coordinator-door"
    door.write_bytes(_IMAGE)
    for name in target_map:
        os.link(door, bin_dst / name)

    def _no_door(name, *a, **k):
        raise AssertionError(f"door cut-over attempted for publisher-only {name}")

    monkeypatch.setattr(substrate, "_cut_over_to_native_door", _no_door)
    substrate._write_agent_helper_forwarders(
        target_map, bin_dst, False,
        engine_root=tmp_path / "engine",
        resolver_module="_resolve_claude_klabauter",
        publisher_only_names=publisher_only,
        publisher_only_launchable=launchable,
    )

    assert door.read_bytes() == _IMAGE
    for name, target in target_map.items():
        dest = bin_dst / name
        if target in launchable:
            body = dest.read_bytes()
            assert not body.startswith(b"\xcf\xfa"), name
            assert b"_resolve_claude_klabauter" in body, name
        else:
            assert not dest.exists() and not dest.is_symlink(), name
