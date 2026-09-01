"""Spawn-count gate for `cross-repo-memo`'s machine-local registry reads.

WHAT REGRESSED, MEASURED. `cross-repo-memo draft` resolved the machine-local
registry one key per process, and re-probed which python was on PATH before
each of those processes. Neither result can change inside one run, and neither
was memoized, so a single `draft` bought 45 `python3 --version` probes and 45
`_machine_local.py get <key>` interpreter starts for what `machine-local dump`
answers in one. Measured on this box with the sanctioned instrument
(`coordinator_core.benchmarks.process_time.batched_process_time_ms`, k=1 into a
fresh `git init` tmp repo so the write cannot collide with itself), against the
pre-fix CLI still published in this box's stamped fleet clone versus this tree:

    before : 92.0 procs/call, 6031-6141 ms process time, 15.6-17.6 s wall
    after  :  8.0 procs/call,  531- 734 ms process time,  2.0- 3.2 s wall

WHY THE OP ITSELF WAS NEVER THE COST, and why this gate sits on the CLI rather
than on `memo.draft`: the op the CLI wraps measures 60 ms warm / 450 ms cold
through `coordinator-invoke`, and `cc_invoke.route_mutation` — the whole door
round trip that carries the write — accounted for 0.56 s of a 19.66 s `draft`.
97% of the cost was pre-flight the door never sees, which is exactly why the
door's own dispatch budget could not catch it.

THE RESIDUAL, NOT ROUNDED AWAY. 8 procs / ~600 ms is still over CLAUDE.md §
The brightline's 500 ms bar. What is left is not registry reads: it is this
clone's unstamped-dev-checkout state (DR-315 s2 — no `coordinator_core/
_engine_stamp`, so every call from this tree goes cold unconditionally), the
same finding `test_op_cli_warm_hop_process_time.py` reports at length for the
same CLI. That is a separate defect with a separate owner; this file gates the
axis this fix moved and states the rest rather than absorbing it.

WHAT THIS FILE GATES, AND WHY IT IS HERMETIC. `test_registry_reads_cost_one_process`
pins the axis directly and exactly — ONE process for every registry read a run
issues — against a stub registry, sub-second, with no live `machine-local` and
no dependence on box load. A live `batched_process_time_ms` leg over the real
CLI was written and then dropped rather than weakened: `coordinator_core/
conftest.py` quarantines HOME/USERPROFILE for every test under that tree, so a
spawned CLI cannot reach the real settings-home registry and exits 1 (measured:
rc=1 / 7 procs under the quarantine against rc=0 / 8 procs outside it). The
live figures above were taken with the instrument outside that quarantine and
are recorded here as the record of the fix, not re-measured per run — process
time on this box tracks the ~50 concurrent peers anyway, never this CLI
(CLAUDE.md § Load norm).

Negative spec: do NOT relax the hermetic assertion to "fewer than before". The
number that matters is ONE — a single batch read — because any per-key number
is a number that grows with the registry, which is how this got to 45.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PY_CLI = _REPO_ROOT / "coordinator" / "bin" / "cross-repo-memo.py"

_STUB_KEYS = {
    # Opaque marker values, never real paths: this stub answers a spawn-count
    # question, and a drive-lettered literal here would be host-specific for
    # no gain.
    f"repos.stub_repo_{i}": f"/stub/repo{i}" for i in range(20)
}
_STUB_KEYS["publish.mirrors.stub_mirror.owner"] = "stub-em"

_STUB_IMPL = '''\
import json, sys
KEYS = {keys!r}
verb = sys.argv[1]
if verb == "dump":
    print(json.dumps(KEYS))
elif verb == "keys":
    print("\\n".join(KEYS))
elif verb == "get":
    key = sys.argv[2]
    if key in KEYS:
        print(KEYS[key])
    else:
        sys.exit(1)
else:
    sys.exit(2)
'''


def _load_cli_module(name: str):
    """Import `cross-repo-memo.py` under a private module name.

    Its hyphenated filename is not importable, and each test needs its own
    module object so one test's process-lifetime caches cannot answer another's.
    """
    bin_dir = str(_PY_CLI.parent)
    lib_dir = str(_PY_CLI.parent / "lib")
    for path in (bin_dir, lib_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, _PY_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_reads_cost_one_process(tmp_path, monkeypatch):
    """Every registry read in one run resolves through a single batch process.

    Reads 20 distinct keys twice each, plus both key-enumeration surfaces —
    the shape a `draft` actually issues. The pre-fix CLI spawned 40 `get`
    processes, 2 `keys` processes, and a `python --version` probe ahead of each
    of them; the post-fix CLI spawns one `dump`.
    """
    stub = tmp_path / "_ml_stub.py"
    stub.write_text(_STUB_IMPL.format(keys=_STUB_KEYS), encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(stub))
    monkeypatch.setenv("CROSS_REPO_MEMO_PYTHON", sys.executable)

    cli = _load_cli_module("_crm_batched_registry_read")

    spawned = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        spawned.append(list(args[0]) if args else list(kwargs.get("args") or []))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", counting_run)

    for _ in range(2):
        for key in _STUB_KEYS:
            if key.startswith("repos."):
                assert cli._machine_local_get(key) == _STUB_KEYS[key]
    assert sorted(cli._machine_local_repos_keys()) == sorted(
        k for k in _STUB_KEYS if k.startswith("repos.")
    )
    assert cli._machine_local_mirror_keys() == ["stub_mirror"]

    assert len(spawned) == 1, (
        f"expected exactly ONE registry process (the batch `dump`), got "
        f"{len(spawned)}: {spawned}"
    )
    assert spawned[0][-2:] == ["dump", "--include-unset"], spawned[0]


def test_batch_read_failure_falls_back_to_per_key_get(tmp_path, monkeypatch):
    """A batch read that is not fully answerable must not become a silent absence.

    `dump` exits non-zero on a per-key operational failure (an ambiguous
    autodiscovery match). The fallback to the real `get` spawn is what keeps
    machine-local's own remediation reaching `_machine_local_get_detail`'s
    stderr element and the diagnostics that match on it — the batch read is a
    cost fix and must never widen into a correctness change.
    """
    stub = tmp_path / "_ml_stub_nodump.py"
    stub.write_text(
        _STUB_IMPL.format(keys=_STUB_KEYS).replace(
            'if verb == "dump":', 'if verb == "dump":\n    sys.exit(2)\nif False:'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(stub))
    monkeypatch.setenv("CROSS_REPO_MEMO_PYTHON", sys.executable)

    cli = _load_cli_module("_crm_batch_read_unavailable")

    assert cli._machine_local_dump() is None
    assert cli._machine_local_get("repos.stub_repo_3") == "/stub/repo3"
    value, invocation_ok, _stderr = cli._machine_local_get_detail("repos.absent_key")
    assert value is None
    assert invocation_ok is True
    assert cli._machine_local_mirror_keys() == ["stub_mirror"]
