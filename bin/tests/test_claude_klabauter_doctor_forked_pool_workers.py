"""
bin.tests.test_claude_klabauter_doctor_forked_pool_workers — resident warm-server
enumeration must not count the server's own dispatch-pool workers.

Linux-shaped defect. On POSIX ``ProcessPoolExecutor`` forks without re-exec, so
every dispatch-pool worker inherits the parent's cmdline byte-for-byte and
matches ``_WARM_SERVER_CMDLINE_SIGNATURE``. macOS and Windows default to
``spawn``, which re-execs with a different cmdline, so neither platform ever
saw it.

Observed on a Linux box with exactly one elected server: 31 "residents", which
exceeded ``_WARM_REACHABILITY_PROBE_CAP`` (16) and left ``claude-klabauter.warm.residency``
— a HARD-severity probe — permanently ``inconclusive``, unable to reach a
verdict at all. ``warm.generation`` separately multiplied one stale breadcrumb
into 31 stale-process warnings.

Drives the real ``psutil.process_iter`` seam with a fake module rather than
calling an internal helper, so these stay valid however the filter is factored
internally.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"

#: Must end with ``_WARM_SERVER_CMDLINE_SIGNATURE`` for a process to be matched.
#: Three ``.parent`` hops off this path is what the enumerator calls the engine
#: root, so the leading directory is the engine root under test.
_SERVER_SCRIPT = "/engine/coordinator_core/warm/server.py"


def _load_probe_module() -> Optional[ModuleType]:
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_forked_pool_workers_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module so dataclass __module__ lookups succeed.
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe_mod() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable here")
    return mod


class _FakePsutil:
    """Minimal stand-in for the one ``psutil`` surface the enumerator uses."""

    def __init__(self, procs: list[dict]) -> None:
        self._procs = procs

    def process_iter(self, _attrs):
        for info in self._procs:
            yield SimpleNamespace(info=dict(info))


def _proc(pid: int, ppid: int, script: str = _SERVER_SCRIPT) -> dict:
    return {"pid": pid, "ppid": ppid, "create_time": 1.0, "cmdline": ["python3", script]}


def _pids(servers: list[dict]) -> list[int]:
    return sorted(s["pid"] for s in servers)


def test_forked_pool_workers_are_dropped(probe_mod):
    """One elected server plus its 30 forked workers enumerates as one
    resident, not 31 — the exact shape observed on Linux."""
    procs = [_proc(4825, 1)] + [_proc(4882 + i, 4825) for i in range(30)]

    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil(procs))

    assert _pids(servers) == [4825]


def test_independent_servers_both_survive(probe_mod):
    """Two servers, neither of which parents the other, are both real
    residents; the filter must not collapse them."""
    servers = probe_mod._enumerate_resident_warm_servers(
        _FakePsutil([_proc(100, 1), _proc(200, 1)])
    )

    assert _pids(servers) == [100, 200]


def test_reparented_orphan_is_still_reported(probe_mod):
    """A worker whose parent died is re-parented to init, so its ppid is no
    longer a match and it stays in the list as a genuine orphan. This is the
    case the filter must NOT swallow: orphan detection is the whole purpose of
    the residency probe, so over-filtering here would be worse than the bug
    being fixed."""
    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil([_proc(4882, 1)]))

    assert _pids(servers) == [4882]


def test_parent_outside_the_matched_set_is_kept(probe_mod):
    """Keying is on membership in the matched set, not on ``ppid == 1``: a
    server launched by some unrelated supervisor is still a resident."""
    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil([_proc(300, 999)]))

    assert _pids(servers) == [300]


def test_non_matching_cmdlines_are_ignored(probe_mod):
    """Only the warm-server signature counts. An unrelated process that happens
    to be parented by a server must neither be enumerated nor join the matched
    set that the filter keys on."""
    procs = [
        _proc(400, 1),
        _proc(401, 400, script="/engine/coordinator_core/warm/other.py"),
    ]

    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil(procs))

    assert _pids(servers) == [400]


def test_engine_root_is_derived_from_the_matched_path(probe_mod):
    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil([_proc(500, 1)]))

    assert [s["engine_root"] for s in servers] == [Path("/engine")]


def test_returned_entries_carry_the_documented_shape(probe_mod):
    """``ppid`` is an enumeration-internal detail: the filter needs it, callers
    do not, and the docstring's stated return shape does not include it.
    Leaking it would silently widen a contract two probes read."""
    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil([_proc(600, 1)]))

    assert set(servers[0]) == {"pid", "create_time", "engine_root"}


def test_no_matching_processes_enumerates_empty(probe_mod):
    servers = probe_mod._enumerate_resident_warm_servers(_FakePsutil([]))

    assert servers == []


class _PpidUnreadable(dict):
    """A ``proc.info`` whose ``ppid`` read raises, as psutil's lazy accessor can.

    The enumerator wraps that read in ``try/except`` and settles on ``None``; a
    plain dict never exercises it, so the branch needs a mapping that actually
    raises.
    """

    def get(self, key, default=None):
        if key == "ppid":
            raise RuntimeError("psutil could not read ppid")
        return super().get(key, default)


def test_a_process_with_no_readable_ppid_is_kept(probe_mod):
    """``None`` is never a real matched pid, so a server whose parent cannot be
    determined must survive the filter rather than be silently dropped.

    This is the failure direction that matters: the filter exists to remove
    workers, and residency is the probe's whole purpose, so dropping a process
    the enumerator merely failed to read costs a real resident. Covers both ways
    the read yields nothing -- psutil reporting ``ppid`` as ``None``, and the
    accessor raising -- because the enumerator collapses them to the same value.
    """
    reported_none = dict(_proc(700, 1))
    reported_none["ppid"] = None
    raised = _PpidUnreadable(_proc(800, 1))

    servers = probe_mod._enumerate_resident_warm_servers(
        _FakePsutil([reported_none, raised])
    )

    assert _pids(servers) == [700, 800]
