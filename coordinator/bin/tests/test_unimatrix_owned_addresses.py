"""Source-only tests for projection of engine-owned Codex addresses."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), BIN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pc = load('unimatrix-peer-contract')
hook = load('unimatrix-codex-hook-sessionstart')
router = load('unimatrix-peer-router')
SID = '01a1b63b-acfb-78d1-86f2-a74ad6ca4572'
OTHER = '01a1b63c-040a-76f2-a9b9-929d9a0d0098'
CLI_ROOT = {SID: {'id': SID, 'cwd': '/work/claude-klabauter', 'source': 'cli',
                  'thread_source': 'user', 'agent_path': None}}


def address(**updates):
    return {'schema_version': 1, 'provider': 'codex', 'session_id': SID,
            'address_version': 1, 'name': 'claude-klabauter-codex-machine-a-27', 'box': 'Machine-a', **updates}


def write_binding(directory, row):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{SID}.json'
    path.write_text(json.dumps(row), encoding='utf-8')
    return path


def test_projection_preserves_engine_alias_and_remote_source_box():
    row = address(box='Mac-Mini', name='rag-codex-mac-mini-28')
    assert pc.owned_codex_address(row, SID) == row
    with pytest.raises(pc.PeerConfigError):
        pc.owned_codex_address(row, SID, expected_box='Machine-a')
    assert pc.owned_codex_address(address(), SID, expected_box='machine-a')['name'] == address()['name']


@pytest.mark.parametrize('updates', [
    {'schema_version': True}, {'schema_version': 2}, {'provider': 'claude'},
    {'session_id': OTHER}, {'session_id': SID.upper()}, {'address_version': True},
    {'address_version': 2}, {'name': '../escape'}, {'name': 'claude-klabauter-codex-machine-a-0'},
    {'name': 'claude-klabauter-codex-machine-a-9223372036854775808'}, {'box': ''},
])
def test_malformed_claimed_owned_address_is_refused(updates):
    with pytest.raises(pc.PeerConfigError):
        pc.owned_codex_address(address(**updates), SID)


def test_legacy_missing_address_version_is_not_an_owned_address():
    row = address()
    del row['address_version']
    assert pc.owned_codex_address(row, SID) is None
    with pytest.raises(pc.PeerConfigError):
        pc.owned_codex_address(address(), OTHER)


def test_hook_projects_only_engine_record_and_preserves_legacy_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(hook.socket, 'gethostname', lambda: 'Machine-a')
    directory, peers = tmp_path / 'host-sessions/codex', tmp_path / 'peers.json'
    binding = write_binding(directory, address())
    before = binding.read_bytes()
    peers.write_text(json.dumps([{'name': 'old-safe-alias', 'vendor': 'codex', 'target': SID}]))
    out = hook.run({'session_id': SID, 'cwd': str(tmp_path)}, peers_file=peers, registry_dir=directory)
    rows = pc.load_peers(peers)
    assert len(rows) == 1
    assert rows[0]['name'] == address()['name'] and rows[0]['target'] == SID
    assert rows[0]['aliases'] == ['old-safe-alias']
    assert rows[0]['name'] in out['systemMessage']
    assert binding.read_bytes() == before
    assert not (directory / '.alias-sequence').exists()


def test_default_registration_cannot_replace_an_owned_alias(tmp_path):
    peers = tmp_path / 'peers.json'
    hook.register_peer(peers, address()['name'], 'codex', SID, address=address())
    before = peers.read_bytes()
    assert hook.register_peer(peers, 'default-name', 'codex', SID) == 'present'
    assert peers.read_bytes() == before


def test_engine_cannot_change_owned_alias_on_resume(tmp_path):
    peers = tmp_path / 'peers.json'
    hook.register_peer(peers, address()['name'], 'codex', SID, address=address())
    before = peers.read_bytes()
    with pytest.raises(ValueError, match='immutable'):
        hook.register_peer(peers, 'new-name', 'codex', SID, address=address(name='claude-klabauter-codex-machine-a-29'))
    assert peers.read_bytes() == before


def test_alias_conflict_is_refused_without_rebinding_other_target(tmp_path):
    peers = tmp_path / 'peers.json'
    peers.write_text(json.dumps([{'name': address()['name'], 'vendor': 'codex', 'target': OTHER}]))
    before = peers.read_bytes()
    with pytest.raises(ValueError, match='conflict'):
        hook.register_peer(peers, address()['name'], 'codex', SID, address=address())
    assert peers.read_bytes() == before


def test_malformed_owned_binding_does_not_fall_back_to_legacy_allocator(tmp_path, monkeypatch):
    monkeypatch.setattr(hook.socket, 'gethostname', lambda: 'Machine-a')
    directory, peers = tmp_path / 'bindings', tmp_path / 'peers.json'
    write_binding(directory, address(provider='other'))
    with pytest.raises(ValueError, match='engine-owned'):
        hook.run({'session_id': SID, 'cwd': str(tmp_path)}, peers_file=peers, registry_dir=directory)
    assert not peers.exists()


def test_picker_accepts_owned_name_legacy_alias_and_native_uuid(tmp_path):
    peer = {**address(), 'vendor': 'codex', 'target': SID, 'aliases': ['legacy-safe'], 'codexPid': os.getpid()}
    r = router.Router([peer], sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path)
    for name in (peer['name'], 'legacy-safe', SID, f'codex-{SID}'):
        assert r.pick_peer(name) == peer['name']
    assert r.pick_peer('missing') is None
    async def turn(name, text):
        assert name == peer['name']
        return 'delivered'
    r.turn = turn
    assert asyncio.run(r._dispatch({'op': 'send', 'to': SID, 'text': 'work'}))['ok']


def test_picker_and_config_both_refuse_alias_conflicts(tmp_path):
    rows = [{'name': 'one', 'vendor': 'codex', 'target': SID, 'aliases': ['shared']},
            {'name': 'shared', 'vendor': 'codex', 'target': OTHER}]
    path = tmp_path / 'peers.json'
    path.write_text(json.dumps(rows))
    with pytest.raises(pc.PeerConfigError, match='alias conflict'):
        pc.load_peers(path)
    r = router.Router(rows, sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path)
    with pytest.raises(ValueError, match='alias conflict'):
        r.pick_peer('shared')
    assert not asyncio.run(r._dispatch({'op': 'send', 'to': 'shared', 'text': 'work'}))['ok']


def test_discovery_projects_engine_alias_and_default_refresh_retains_it(tmp_path, monkeypatch):
    settings = tmp_path / 'settings'
    binding = write_binding(settings / 'host-sessions/codex', address())
    locks = tmp_path / 'locks'
    locks.mkdir()
    (locks / f'{SID}.lock').touch()
    monkeypatch.setattr(router.socket, 'gethostname', lambda: 'Machine-a')
    monkeypatch.setattr(router._roster(), 'settings_home', lambda: settings)
    monkeypatch.setattr(router, 'lock_held', lambda path: True)
    monkeypatch.setattr(router, 'codex_thread_metadata', lambda home: CLI_ROOT)
    monkeypatch.setattr(router, 'codex_rollout_addressable', lambda home, ident, index: True)
    peer, = router.load_lock_peers(locks, 'codex')
    assert peer['name'] == address()['name'] and peer['target'] == SID
    assert f'codex-{SID}' in peer['aliases']
    r = router.Router([peer], sessions_dir=tmp_path, state_dir=tmp_path,
                      endpoint_dir=tmp_path, codex_locks_dir=locks)
    r.discovered = {peer['name']}
    assert list(r._live_discovered()) == [peer['name']]
    binding.unlink()
    assert list(r._live_discovered()) == []


def test_discovery_refuses_codex_peer_without_engine_address(tmp_path, monkeypatch, capsys):
    settings, locks = tmp_path / 'settings', tmp_path / 'locks'
    (settings / 'host-sessions/codex').mkdir(parents=True)
    locks.mkdir()
    (locks / f'{SID}.lock').touch()
    monkeypatch.setattr(router._roster(), 'settings_home', lambda: settings)
    monkeypatch.setattr(router, 'lock_held', lambda path: True)
    monkeypatch.setattr(router, 'codex_thread_metadata', lambda home: CLI_ROOT)
    monkeypatch.setattr(router, 'codex_rollout_addressable', lambda home, ident, index: True)
    assert router.load_lock_peers(locks, 'codex') == []
    assert f'refusing Codex peer without engine address: {SID}' in capsys.readouterr().err


def test_discovery_refuses_malformed_owned_binding(tmp_path, monkeypatch, capsys):
    settings, locks = tmp_path / 'settings', tmp_path / 'locks'
    write_binding(settings / 'host-sessions/codex', address(name='bad'))
    locks.mkdir()
    (locks / f'{SID}.lock').touch()
    monkeypatch.setattr(router._roster(), 'settings_home', lambda: settings)
    monkeypatch.setattr(router, 'lock_held', lambda path: True)
    monkeypatch.setattr(router, 'codex_thread_metadata', lambda home: CLI_ROOT)
    assert router.load_lock_peers(locks, 'codex') == []
    assert 'refusing Codex address' in capsys.readouterr().err


def test_face_exports_owned_identity_for_read_only_mirror_projection(tmp_path):
    peer = {**address(), 'vendor': 'codex', 'target': SID, 'aliases': ['legacy-safe'], 'codexPid': os.getpid()}
    face = router.Registration(peer['name'], 'test-endpoint', str(tmp_path), vendor='codex', peer=peer)
    assert face.data['sessionId'].startswith('peer-')  # harness identity remains its genuine face
    assert face.data['session_id'] == SID and face.data['target'] == SID
    assert face.data['nameSource'] == 'engine'
    assert 'legacy-safe' in pc.projected_addresses(face.data)
    remote = {**face.data, 'name': peer['name'] + '.remote', 'unimatrixFarName': peer['name'],
              'box': 'Mac-Mini'}
    assert 'legacy-safe' in pc.projected_addresses(remote)
    assert SID in pc.projected_addresses(remote)
    with pytest.raises(ValueError):
        router.Registration('other-name', 'test-endpoint', str(tmp_path), vendor='codex', peer=peer)


def test_remote_picker_accepts_projected_legacy_alias_and_native_target(tmp_path, monkeypatch):
    entry = {**address(box='Mac-Mini'), 'name': address()['name'] + '.remote',
             'unimatrixFarName': address()['name'], 'vendor': 'codex', 'target': SID,
             'aliases': ['legacy-safe'], 'pid': 999, 'messagingSocketPath': 'remote-endpoint'}
    (tmp_path / '999.json').write_text(json.dumps(entry))
    monkeypatch.setattr(router._roster(), 'pid_alive', lambda pid: True)
    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path)
    assert r.resolve_sender('legacy-safe') == 'remote-endpoint'
    assert r.resolve_sender(SID) == 'remote-endpoint'
    (tmp_path / '998.json').write_text(json.dumps({
        'name': 'legacy-safe', 'vendor': 'codex', 'target': OTHER, 'pid': 998,
        'messagingSocketPath': 'conflicting-endpoint'}))
    assert r.resolve_sender('legacy-safe') == ''
    (tmp_path / '998.json').unlink()
    entry['session_id'] = OTHER
    (tmp_path / '999.json').write_text(json.dumps(entry))
    assert r.resolve_sender('legacy-safe') == ''
