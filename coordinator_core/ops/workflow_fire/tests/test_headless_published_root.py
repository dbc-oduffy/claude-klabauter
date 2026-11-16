"""A headless fire's worker runs from the published stamped engine root, never the caller checkout."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.install.engine_root_for_install import InstallEngineRoot
from coordinator_core.ops.workflow_fire import headless


def _isolate(tmp_path, monkeypatch, resolved):
    source = tmp_path / 'workflow.mjs'
    source.write_text('export const meta={}; return {};')
    registry = tmp_path / 'registry'
    monkeypatch.setattr(headless.fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(headless.fire, '_assert_script_under_repo_root', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_registry_dir', lambda *a: registry)
    monkeypatch.setattr(headless.fire, '_acquire_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, '_release_cap_lock', lambda *a: None)
    monkeypatch.setattr(headless.fire, 'count_live_fires', lambda *a, **k: 0)
    monkeypatch.setattr(headless.fire, 'build_fire_env', lambda: {})
    monkeypatch.setattr(headless, 'stamp_fire_ownership', lambda *a: None)
    monkeypatch.setattr(headless.shutil, 'which', lambda name: '/usr/bin/node')
    monkeypatch.setattr('coordinator_core.install.engine_root_for_install.resolve_engine_root_for_install',
                        lambda: resolved)
    return source, registry


def test_worker_spawns_from_the_published_root(tmp_path, monkeypatch):
    root = tmp_path / 'published'
    source, registry = _isolate(tmp_path, monkeypatch, InstallEngineRoot('published', root, None))
    spawned = {}

    def popen(command, **kwargs):
        spawned.update(command=command, env=kwargs['env'])
        return SimpleNamespace(pid=123, poll=lambda: None, kill=lambda: None, wait=lambda: 0)

    monkeypatch.setattr(headless.subprocess, 'Popen', popen)
    record = headless.start(str(source))
    assert spawned['env']['PYTHONPATH'].split(headless.os.pathsep)[0] == str(root)
    assert record['engine_root'] == str(root)
    command = spawned['command']
    assert '-P' in command and command.index('-P') < command.index('-m')
    assert record['command'] == command
    assert json.loads(next(registry.glob('*.json')).read_text())['engine_root'] == str(root)


def test_no_published_root_is_a_typed_refusal_before_any_reservation(tmp_path, monkeypatch):
    source, registry = _isolate(tmp_path, monkeypatch, InstallEngineRoot('none', None, 'machine-local set repos.claude_klabauter X'))

    def popen(*a, **k):
        raise AssertionError('worker must not spawn')

    monkeypatch.setattr(headless.subprocess, 'Popen', popen)
    with pytest.raises(headless.HeadlessEngineRootUnavailable, match=r'repos\.claude_klabauter') as caught:
        headless.start(str(source))
    assert isinstance(caught.value, RuntimeError)
    assert not registry.exists() or not list(registry.glob('*.json'))


def test_private_engine_worker_keeps_the_source_tree_root(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError('private engine must not resolve the published root')

    source, registry = _isolate(tmp_path, monkeypatch, None)
    monkeypatch.setattr('coordinator_core.install.engine_root_for_install.resolve_engine_root_for_install', forbidden)
    spec = {'door_image': 'd', 'door_provenance': 'p', 'shared_engine_root': 's'}
    monkeypatch.setattr(headless, 'validate_private_engine_config', lambda *a, **k: spec)
    monkeypatch.setattr(headless, '_private_source_sha', lambda: 'a' * 40)
    monkeypatch.setattr('coordinator_core.ops.workflow_fire.finalizer.snapshot_contract', lambda *a: {})
    spawned = {}

    def popen(command, **kwargs):
        spawned.update(command=command, env=kwargs['env'])
        return SimpleNamespace(pid=123, poll=lambda: None, kill=lambda: None, wait=lambda: 0)

    monkeypatch.setattr(headless.subprocess, 'Popen', popen)
    tree = Path(headless.__file__).resolve().parents[3]
    record = headless.start(str(source), private_engine={'x': 1})
    assert record['engine_root'] == str(tree)
    assert spawned['env']['PYTHONPATH'].split(headless.os.pathsep)[0] == str(tree)
