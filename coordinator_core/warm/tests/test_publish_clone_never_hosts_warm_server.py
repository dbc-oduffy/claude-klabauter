"""A publish-only clone never hosts a warm-pair process; every other root and script still spawns."""

from __future__ import annotations

import pytest

from coordinator_core import machine_resolver
from coordinator_core.warm import client

SERVER = "coordinator_core/warm/server.py"


def _registry(monkeypatch, publish, engine=None):
    flat = {"publish.mirrors.claude_klabauter.path": str(publish)}
    if engine is not None:
        flat["repos.claude_klabauter"] = str(engine)
    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: flat)


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    import coordinator_core.ops.ceremony.detached_spawn as ds

    monkeypatch.setattr(ds, "spawn_detached", lambda *a, **k: calls.append(a) or True)
    return calls


def test_publish_only_clone_is_refused_for_every_pair_script(monkeypatch, tmp_path):
    pub, eng = tmp_path / "pub", tmp_path / "eng"
    _registry(monkeypatch, pub, eng)

    for script in (SERVER, "coordinator_core/warm/supervisor.py", "coordinator_core/warm/front_door.py"):
        refusal = client.publish_clone_refusal(str(pub), script)
        assert isinstance(refusal, client.PublishCloneRefusal)
        assert refusal.mirror_key == "claude_klabauter"
        assert "repos.claude_klabauter" in str(refusal)


def test_spawn_detached_returns_false_and_never_spawns_for_publish_clone(monkeypatch, tmp_path, spawned, capsys):
    pub, eng = tmp_path / "pub", tmp_path / "eng"
    _registry(monkeypatch, pub, eng)

    assert client.spawn_detached(str(pub), SERVER) is False
    assert spawned == []
    assert "warm spawn refused" in capsys.readouterr().err


def test_engine_clone_still_spawns(monkeypatch, tmp_path, spawned):
    pub, eng = tmp_path / "pub", tmp_path / "eng"
    _registry(monkeypatch, pub, eng)

    assert client.publish_clone_refusal(str(eng), SERVER) is None
    assert client.spawn_detached(str(eng), SERVER) is True
    assert len(spawned) == 1


def test_sole_clone_that_is_also_the_mirror_is_never_refused(monkeypatch, tmp_path):
    only = tmp_path / "only"
    _registry(monkeypatch, only, only)
    assert client.publish_clone_refusal(str(only), SERVER) is None

    _registry(monkeypatch, only, None)
    assert client.publish_clone_refusal(str(only), SERVER) is None


def test_non_pair_script_from_publish_clone_is_not_refused(monkeypatch, tmp_path):
    pub, eng = tmp_path / "pub", tmp_path / "eng"
    _registry(monkeypatch, pub, eng)

    assert client.publish_clone_refusal(str(pub), "coordinator/bin/housekeeping.py") is None


def test_registry_fault_does_not_block_spawn(monkeypatch, tmp_path):
    def boom():
        raise OSError("registry unreadable")

    monkeypatch.setattr(machine_resolver, "merged_flat_registry", boom)

    assert client.publish_clone_refusal(str(tmp_path), SERVER) is None
