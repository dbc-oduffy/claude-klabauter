from coordinator_core.ops.dispatch_emit.fan_launcher import _invoke_command


def test_windows_relay_uses_native_executable(monkeypatch, tmp_path):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path / 'settings with spaces'))
    command = _invoke_command('nt')
    assert command == '"' + (tmp_path / 'settings with spaces/bin/coordinator-invoke.exe').as_posix() + '"'
    assert '${' not in command


def test_posix_relay_keeps_late_bound_settings_home():
    assert '${COORDINATOR_SETTINGS_HOME:' in _invoke_command('posix')
