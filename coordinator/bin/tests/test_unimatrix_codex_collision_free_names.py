"""Two Codex sessions in one folder register under distinct engine-sequenced names, end to end."""
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router_collision_free', BIN / 'unimatrix-peer-router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

SID_A = '01a1b63b-acfb-78d1-86f2-a74ad6ca4572'
SID_B = '01a1b63c-040a-76f2-a9b9-929d9a0d0098'
SID_C = '01a1b63d-1111-7111-8111-111111111111'


def _register(tmp_path, monkeypatch, directory, sid, cwd):
    import psutil
    from coordinator_core.session import core
    from coordinator_core.session import external_registry as registry
    host = SimpleNamespace(pid=4242, name=lambda: 'codex', cmdline=lambda: ['codex'], create_time=lambda: 1000)
    monkeypatch.setattr(core, 'stable_pid_alive', lambda *a, **k: True)
    monkeypatch.setattr(core, 'ensure_session', lambda *a, **k: None)
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(parents=lambda: [host]))
    monkeypatch.setenv('COORDINATOR_AGENT_ROLE', 'unknown')
    return registry.register_current(
        {'session_id': sid, 'cwd': str(cwd), 'hook_event_name': 'SessionStart'}, directory)


def test_two_sessions_one_folder_get_distinct_sequenced_names(tmp_path, monkeypatch, capsys):
    settings, locks = tmp_path / 'settings', tmp_path / 'locks'
    directory = settings / 'host-sessions' / 'codex'
    locks.mkdir()
    cwd = tmp_path / 'repo'
    cwd.mkdir()
    for sid in (SID_A, SID_B):
        _register(tmp_path, monkeypatch, directory, sid, cwd)
    for sid in (SID_A, SID_B, SID_C):
        (locks / f'{sid}.lock').touch()
    metadata = {sid: {'id': sid, 'cwd': str(cwd), 'source': 'cli', 'thread_source': 'user', 'agent_path': None}
                for sid in (SID_A, SID_B, SID_C)}
    monkeypatch.setattr(router._roster(), 'settings_home', lambda: settings)
    monkeypatch.setattr(router, 'lock_held', lambda path: True)
    monkeypatch.setattr(router, 'codex_thread_metadata', lambda home: metadata)
    monkeypatch.setattr(router, 'codex_rollout_addressable', lambda home, ident, index: True)

    peers = router.load_lock_peers(locks, 'codex')

    by_target = {p['target']: p for p in peers}
    assert set(by_target) == {SID_A, SID_B}  # the third lock has no registry row
    names = [by_target[SID_A]['name'], by_target[SID_B]['name']]
    assert len(set(names)) == 2
    for name in names:
        assert re.fullmatch(rf'repo-codex-[a-z0-9-]+-[1-9][0-9]*', name), name
        assert name != 'codex-repo'
    for peer in peers:
        assert f'codex-{peer["target"]}' in peer['aliases']
        assert all(alias not in names for alias in peer['aliases'])
        assert 'codex-repo' not in peer['name']
    assert f'refusing Codex peer without engine address: {SID_C}' in capsys.readouterr().err
