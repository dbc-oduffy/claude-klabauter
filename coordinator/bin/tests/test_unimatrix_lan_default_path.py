"""Default-entry configuration premise checks for LAN-first peer routing."""
import importlib.util
import io
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1]


def load_roster():
    spec = importlib.util.spec_from_file_location("lan_default_roster", BIN / "unimatrix-roster.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ordinary_roster_missing_settings_refuses_before_mkdir(monkeypatch):
    roster = load_roster()
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    mkdirs = []
    monkeypatch.setattr(Path, "mkdir", lambda path, *a, **kw: mkdirs.append(path))

    def unexpected_open(*args, **kwargs):
        pytest.fail("ordinary roster reached log I/O without a missing-settings diagnostic")

    monkeypatch.setattr(roster, "open", unexpected_open, raising=False)
    with pytest.raises(RuntimeError, match="COORDINATOR_SETTINGS_HOME") as caught:
        roster.main(["roster.log", "--remote", "operator@peer"])
    assert type(caught.value).__name__ == "MissingSettingsError"
    assert mkdirs == []


def _registry_reads(roster, monkeypatch, tmp_path, runs):
    """Drive ordinary roster.main for each (settings_home, config_dir); return the registry dirs read."""
    reads = []

    class RegistryObserved(BaseException):
        pass

    def observe_registry(path):
        reads.append(Path(path))
        raise RegistryObserved

    monkeypatch.setattr(roster, "open", lambda *a, **kw: io.StringIO(), raising=False)
    monkeypatch.setattr(roster, "proc_start", lambda pid: None)
    monkeypatch.setattr(roster, "write_json_atomic", lambda path, data: None)
    monkeypatch.setattr(roster, "tree_kill_many", lambda pids: None)
    monkeypatch.setattr(roster, "_signal_pid", lambda pid, sig: None)  # survivor SIGKILL must not reach a real pid
    monkeypatch.setattr(roster.signal, "signal", lambda *args: None)
    monkeypatch.setattr(roster.os, "listdir", observe_registry)
    for home, config in runs:
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        with pytest.raises(RegistryObserved):
            roster.main([str(home / "roster.log"), "--remote", "operator@far", "--key", str(home / "relay-key")])
    return reads


def test_ordinary_roster_config_dirs_do_not_share_registry(monkeypatch, tmp_path):
    reads = _registry_reads(load_roster(), monkeypatch, tmp_path,
                            [(tmp_path / "home", tmp_path / "cfg-a"), (tmp_path / "home", tmp_path / "cfg-b")])
    assert reads == [tmp_path / "cfg-a" / "sessions", tmp_path / "cfg-b" / "sessions"]


def test_ordinary_roster_settings_homes_under_one_config_dir_share_registry(monkeypatch, tmp_path):
    reads = _registry_reads(load_roster(), monkeypatch, tmp_path,
                            [(tmp_path / "first", tmp_path / "cfg"), (tmp_path / "second", tmp_path / "cfg")])
    assert reads == [tmp_path / "cfg" / "sessions"] * 2


@pytest.mark.parametrize("reachable,expected", [(True, "peer-lan"), (False, "peer-tunnel")])
def test_old_caller_string_compatibility(reachable, expected):
    roster = load_roster()
    assert roster.pick_remote("peer-lan", "peer-tunnel", reachable=lambda _: reachable) == expected


def test_old_caller_without_tunnel_remains_unprobed():
    roster = load_roster()

    def unexpected_probe(alias):
        pytest.fail("legacy caller without a tunnel must stay unprobed")

    assert roster.pick_remote("peer", reachable=unexpected_probe) == "peer"
