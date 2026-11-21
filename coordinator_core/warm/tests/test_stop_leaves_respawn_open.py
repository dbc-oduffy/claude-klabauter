"""After `warm-engine-stop`, the next client call must be allowed to respawn warm.

The documented roll step is "stop warm; next call respawns it". A stop that left a
tombstone, publish marker or live debounce record would turn that into a permanent
cold run. Only a publish marker or a ladder-gone tombstone may suppress a spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, idle, pair_stop


@pytest.fixture
def engine_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "runtime"))
    monkeypatch.delenv("COORDINATOR_WARM_SERVER_AUTOSTART", raising=False)
    root = tmp_path / "engine"
    root.mkdir()
    monkeypatch.setattr(client, "_engine_clone_root", lambda: root)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_spawned_this_process", False)
    return root


def test_stop_writes_no_marker_that_blocks_a_spawn(engine_root: Path) -> None:
    pytest.importorskip("psutil")
    result = pair_stop.stop_warm_pair(engine_root)
    assert result.ok()
    assert not idle.ladder_gone_tombstoned(engine_root)
    assert not breadcrumb.publish_marker_held(breadcrumb.svc_dir(engine_root))
    assert breadcrumb.should_spawn(engine_root) is True


def test_first_client_call_after_a_stop_spawns_the_server(
    engine_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns: list = []
    monkeypatch.setattr(
        client, "spawn_detached", lambda root, script, args=None, **kw: spawns.append((root, script)) or True
    )
    client._spawn_once()
    assert spawns == [(str(engine_root), client.SERVER_ENTRY_SCRIPT)]


def test_hook_boot_wait_off_still_spawns_and_never_blocks(
    engine_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook (`COORDINATOR_WARM_BOOT_WAIT_SECS=0`) triggers the respawn on its miss; only the wait is off."""
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    spawns: list = []
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: spawns.append(a) or True)
    client._spawn_once()
    assert len(spawns) == 1
    assert client._boot_wait_disabled() is True
