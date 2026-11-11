"""The relay's specs mode returns this box's RAM/CPU/GPU record and degrades per probe."""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
KEYS = {"box", "ram_gb", "ram_available_gb", "cpu_cores", "gpu", "sampled_at"}


@pytest.fixture(scope="module")
def relay():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_specs", BIN / "unimatrix-relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_mode_admits_bare_specs_only(relay):
    assert relay.parse_mode("specs") == ("specs", None)
    assert relay.parse_mode("specs extra") is None
    assert relay.parse_mode("specs; id") is None


def test_probe_specs_record_is_json_with_the_pinned_keys(relay):
    record = json.loads(json.dumps(relay.probe_specs()))
    assert set(record) == KEYS
    assert isinstance(record["cpu_cores"], int)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_probe_specs_degrades_when_every_probe_raises(relay, monkeypatch, platform):
    def boom(*_a, **_k):
        raise OSError("no probe on this OS")

    for name in ("_ram_windows", "_ram_darwin", "_ram_linux", "_gpu_windows", "_gpu_darwin", "_gpu_linux"):
        monkeypatch.setattr(relay, name, boom)
    record = relay.probe_specs(platform)
    assert record["ram_gb"] is None and record["ram_available_gb"] is None and record["gpu"] is None
    assert set(record) == KEYS


def test_probe_specs_rounds_ram_to_gib(relay, monkeypatch):
    monkeypatch.setattr(relay, "_ram_linux", lambda: (16 * relay.GIB, 4 * relay.GIB))
    monkeypatch.setattr(relay, "_gpu_linux", lambda: "Test GPU")
    record = relay.probe_specs("linux")
    assert (record["ram_gb"], record["ram_available_gb"], record["gpu"]) == (16.0, 4.0, "Test GPU")


def test_main_specs_mode_prints_the_record(relay, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "specs")
    relay.main()
    assert set(json.loads(capsys.readouterr().out)) == KEYS
