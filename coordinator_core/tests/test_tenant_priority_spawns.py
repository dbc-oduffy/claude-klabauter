"""Simulated-Mini spawn test: a prime-tenant machine-local profile lowers children; every other profile leaves them alone."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core import machine_local_spawn, tenant_priority

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def _child(code, argv=None):
    kwargs = tenant_priority.popen_kwargs(argv)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs,
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


@pytest.mark.skipif(not POSIX, reason="nice is POSIX")
def test_prime_profile_child_reads_nice_15(monkeypatch):
    _profile(monkeypatch, "true")
    assert "preexec_fn" in tenant_priority.popen_kwargs([sys.executable])
    assert _child(_NICE_PROBE) == tenant_priority.PRIME_TENANT_NICE == 15


@pytest.mark.skipif(not POSIX, reason="nice is POSIX")
@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_non_prime_profile_child_nice_equals_parent(monkeypatch, value):
    import os

    _profile(monkeypatch, value)
    assert tenant_priority.popen_kwargs([sys.executable]) == {}
    assert _child(_NICE_PROBE) == os.getpriority(os.PRIO_PROCESS, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="background QoS is darwin")
def test_darwin_child_inherits_bg(monkeypatch):
    _profile(monkeypatch, "true")
    assert _child(_BG_PROBE) == _DARWIN_BG
    tenant_priority._reset_for_tests()
    _profile(monkeypatch, None)
    assert _child(_BG_PROBE) == 0
