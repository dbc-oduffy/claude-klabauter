"""Simulated-Mini spawn test: a prime-tenant machine-local profile lowers children; every other profile leaves them alone."""

from __future__ import annotations

import sys

import pytest

from coordinator_core import machine_local_spawn, tenant_priority

POSIX = sys.platform != "win32"
_NICE_PROBE = "import os;print(os.getpriority(os.PRIO_PROCESS,0))"
_BG_PROBE = (
    "import ctypes,sys;"
    "f=ctypes.CDLL(None).getpriority;f.argtypes=[ctypes.c_int,ctypes.c_uint];"
    "print(f(4,0))"
)
_DARWIN_BG = 0x1000


@pytest.fixture(autouse=True)
def _fresh_state():
    tenant_priority._reset_for_tests()
    yield
    tenant_priority._reset_for_tests()


def _profile(monkeypatch, value):
    calls = []

    def fake_cached_get(impl, key):
        calls.append(key)
        if key == tenant_priority.PRIME_TENANT_KEY and value is not None:
            return value, True
        return None, True

    monkeypatch.setattr(machine_local_spawn, "cached_get", fake_cached_get)
    return calls


@pytest.mark.parametrize("marker", tenant_priority.SERVE_LINEAGE_MARKERS)
def test_serve_lineage_argv_is_never_lowered(monkeypatch, marker):
    _profile(monkeypatch, "true")
    assert tenant_priority.popen_kwargs([sys.executable, "x/" + marker]) == {}
    assert tenant_priority.popen_kwargs([sys.executable, "x\\" + marker]) == {}
    if POSIX:
        assert "preexec_fn" in tenant_priority.popen_kwargs([sys.executable, "x/other.py"])


@pytest.mark.parametrize("marker", tenant_priority.SERVE_LINEAGE_MARKERS)
def test_serve_descendant_spawner_is_never_lowered(monkeypatch, marker):
    _profile(monkeypatch, "true")
    monkeypatch.setattr(sys, "argv", ["python", "/opt/x/" + marker])
    assert tenant_priority.popen_kwargs([sys.executable, "child.py"]) == {}
    monkeypatch.setattr(sys, "argv", ["python", "engine.py"])
    assert ("preexec_fn" in tenant_priority.popen_kwargs([sys.executable, "child.py"])) == POSIX


@pytest.mark.parametrize("marker", tenant_priority.SERVE_LINEAGE_MARKERS)
def test_argv_and_string_predicates_agree_on_mid_argument_marker(marker):
    argv = ["python", f"--cfg={marker}:x"]
    assert tenant_priority.is_serve_lineage(argv) is tenant_priority.is_serve_lineage(" ".join(argv)) is True


@pytest.mark.parametrize("marker", tenant_priority.SERVE_LINEAGE_MARKERS)
def test_is_serve_lineage_matches_string_and_argv(marker):
    assert tenant_priority.is_serve_lineage(f"/usr/bin/python3 /opt/x/{marker} --port 1")
    assert tenant_priority.is_serve_lineage(f"x\\y\\{marker}")
    assert tenant_priority.is_serve_lineage(["python", "/opt/x/" + marker])
    assert not tenant_priority.is_serve_lineage("/usr/bin/python3 /opt/x/unimatrix-relay.py")
    assert not tenant_priority.is_serve_lineage(["python", "other.py"])
    assert not tenant_priority.is_serve_lineage(None)


def test_settings_home_prefix_is_slash_terminated_and_prefix_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    prefix = tenant_priority.settings_home_prefix()
    assert prefix.endswith("/") and "\\" not in prefix
    assert (prefix + "unimatrix/unimatrix-relay.py").startswith(prefix)
    assert not (str(tmp_path / "sh-other").replace("\\", "/") + "/x").startswith(prefix)


def test_settings_home_prefix_refusal_names_the_key(monkeypatch):
    import coordinator_core._settings_home as sh

    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(sh, "settings_home", boom)
    with pytest.raises(tenant_priority.SettingsHomeKeyMissing, match="COORDINATOR_SETTINGS_HOME"):
        tenant_priority.settings_home_prefix()


def test_is_prime_tenant_is_memoised_and_fails_closed(monkeypatch):
    calls = _profile(monkeypatch, "true")
    assert tenant_priority.is_prime_tenant() is True
    assert tenant_priority.is_prime_tenant() is True
    assert calls == [tenant_priority.PRIME_TENANT_KEY]

    tenant_priority._reset_for_tests()

    def raising(*_):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(machine_local_spawn, "cached_get", raising)
    assert tenant_priority.is_prime_tenant() is False

    tenant_priority._reset_for_tests()
    monkeypatch.setattr(machine_local_spawn, "cached_get", lambda *_: (None, False))
    assert tenant_priority.is_prime_tenant() is False


@pytest.mark.skipif(POSIX, reason="win32 only")
def test_win32_is_unchanged(monkeypatch):
    _profile(monkeypatch, "true")
    assert tenant_priority.popen_kwargs([sys.executable]) == {}
