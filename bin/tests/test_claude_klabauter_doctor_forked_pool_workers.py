"""
bin.tests.test_claude_klabauter_doctor_forked_pool_workers — resident warm-server
enumeration must not count the server's own dispatch-pool workers.

Linux-shaped defect. `ProcessPoolExecutor` defaults to the `fork` start
method on Linux, and a forked worker inherits the parent's `cmdline`
verbatim — so every one of `warm.server.WORKER_POOL_SIZE` workers matched
`_WARM_SERVER_CMDLINE_SIGNATURE` and was reported as a separate resident
warm server. macOS and Windows default to `spawn`, which re-execs with a
different cmdline, so neither platform ever saw it.

Observed before the fix on a Linux box with exactly one elected server:
`claude-klabauter.warm.residency` reported 31 residents and went
`inconclusive` because that exceeded `_WARM_REACHABILITY_PROBE_CAP` (16),
and `claude-klabauter.warm.generation` multiplied one stale token into 31
warnings. residency is a hard-severity probe, so on Linux it could not
reach a verdict at all.

Covers `_drop_forked_pool_workers` directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_forked_pool_workers_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe_mod() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable here")
    return mod


def _server(pid: int, ppid: int) -> dict:
    return {"pid": pid, "ppid": ppid, "create_time": 1.0, "engine_root": Path("/engine")}


def test_forked_pool_workers_are_dropped(probe_mod):
    """One elected server (parented by init) plus its 30 forked workers must
    enumerate as one resident, not 31."""
    elected = _server(4825, 1)
    workers = [_server(4882 + i, 4825) for i in range(30)]

    kept = probe_mod._drop_forked_pool_workers([elected] + workers)

    assert [s["pid"] for s in kept] == [4825]


def test_independent_servers_both_survive(probe_mod):
    """Two genuinely independent servers (neither parented by the other) are
    both real residents — the filter must not collapse them."""
    a = _server(100, 1)
    b = _server(200, 1)

    kept = probe_mod._drop_forked_pool_workers([a, b])

    assert {s["pid"] for s in kept} == {100, 200}


def test_only_direct_parentage_is_filtered(probe_mod):
    """A server whose parent is an unrelated, unmatched process stays — the
    filter keys on membership in the matched set, not on ppid != 1."""
    kept = probe_mod._drop_forked_pool_workers([_server(300, 999)])

    assert [s["pid"] for s in kept] == [300]


def test_missing_ppid_is_not_treated_as_a_match(probe_mod):
    """psutil can fail to report a ppid; such a process must be kept rather
    than silently dropped, since None is never a real matched pid."""
    kept = probe_mod._drop_forked_pool_workers(
        [{"pid": 400, "ppid": None, "create_time": 1.0, "engine_root": Path("/engine")}]
    )

    assert [s["pid"] for s in kept] == [400]


def test_empty_population_is_empty(probe_mod):
    assert probe_mod._drop_forked_pool_workers([]) == []
