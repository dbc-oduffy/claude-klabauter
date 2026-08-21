"""Standing gate: the process-time cost of reaching the warm engine.

WHY THIS FILE EXISTS. The cost of reaching the warm engine regressed twice
(2026-08-19, 2026-08-21), each time after a fix believed sufficient, because
nothing asserted the property -- see state/handoffs/
2026-08-21_103635_reaching-the-warm-engine.md. That handoff's own § Measured
findings retired the baton's original AC ("a warm hit is strictly cheaper
than COORDINATOR_WARM=0") as a wall-clock artifact: in process time a warm
hit (91.4ms) and a cold one (93.0ms) sat within tick noise of each other --
warmth currently buys ~nothing on `ping` and, more importantly, costs
nothing either. A relative gate built on that comparison cannot catch what
actually regressed (an eager top-level `asyncio` import in `invoke/
__main__.py` paid on every warm call, whether or not the cold branch that
needs it ever runs). This file therefore asserts an ABSOLUTE ceiling on
process time as the primary check, and treats warm-vs-cold as secondary.

UNIT: process time (job-object `TotalUserTime + TotalKernelTime`), never
wall clock -- see `coordinator_core.benchmarks.process_time` module
docstring for the three measurement traps this avoids. Batched over K=20
invocations in one job object to beat the ~15.6ms scheduler-tick
quantisation a single sample cannot resolve against a sub-100ms bar.

THE THRESHOLD, AND WHY. This gate covers the `python -m
coordinator_core.invoke ping` door -- the entrypoint measured in the
handoff's own table, and stable across whichever wrapper (`.cmd`, a future
native relay) ends up calling it; those wrapper layers are actively being
redesigned by concurrent sibling work (`coordinator_core/warm/door/`,
`warm/server.py`, `invoke/__main__.py` are out of this file's writable
scope) and are NOT this gate's subject. Measured baseline this session,
warm server up (batched, K=20): 90.6-93.0ms for both the warm hit and the
`COORDINATOR_WARM=0` control -- essentially one number, since job-object
process time does not include the peer-load noise wall clock does.
`PROCESS_TIME_CEILING_MS = 200` sits with wide headroom above that measured
baseline (>2x) but well below the magnitude of a real regression this class
of defect produces: the diagnosed root cause alone (the top-level `asyncio`
import) costs +40.4ms on its own account, and the sibling defect this
baton's own § "Confirmed NOT the cause" ruled out (the op registry
re-imported per call) costs +302ms standalone -- either fully clears 200ms
on its own, so 200ms fails loudly on a real regression while carrying
enough margin that normal box-load and tick noise never flap it. This
ceiling is deliberately NOT set at the PM's ~60ms target bar for the whole
door (handoff § "The bar") -- that bar names the DESTINATION for the
door-redesign work in progress in the sibling-owned files above, and a gate
landed here ahead of that work at 60ms would fail on every run until it
lands, which is a worklist, not a regression signal. Move this number down
deliberately, in the same commit as whatever door-shaving fix earns it, not
by feel.

PRECONDITION IS ESTABLISHED, NOT DISCOVERED (revision 2 -- see below for
why revision 1 was wrong). This file boots its OWN warm server rather than
looking for one that happens to already be running. Revision 1 discovered
an ambient breadcrumb; re-run minutes later against an unchanged tree, that
run SKIPPED -- the ambient klabauter server had been evicted in the
interim (engine skew eviction fires on every publish round, and publish
rounds run continuously on this box). A gate whose precondition depends on
ambient fleet state skips far more often than it runs, which is no better
than no gate: the property this file exists to catch already regressed
twice with nothing noticing.

WHAT "ESTABLISH" MEANS, MECHANICALLY -- `_boot_isolated_server()` below:
  1. Resolve a SOURCE engine root with a valid build stamp (the sibling
     `claude-klabauter` published-mirror clone by convention, override via
     `COORDINATOR_WARM_GATE_ENGINE_ROOT`) via `warm.engine_root.is_engine_root`.
  2. Hardlink (`os.link`, same-volume, zero data copy -- ~1.4s for this
     package's ~2850 files) ONLY its `coordinator_core/` subtree into a
     fresh temp directory created BESIDE the source root (same drive,
     required for `os.link`; never the default temp dir, which is commonly
     a different drive on this box). This is the isolation: a genuinely
     distinct on-disk path, so `warm.election.pipe_name`'s path-hash keys
     a DIFFERENT pipe than the resident fleet server's -- this gate's own
     boot/measure/teardown can never race or evict a session's real warm
     server, and a concurrent publish/eviction on the real one can never
     flap this gate either.
  3. Spawn `python -m coordinator_core.warm.server` (own `subprocess.Popen`,
     detached, `cwd=<isolated root>`) -- deliberately NOT
     `ops.ceremony.detached_spawn.spawn_detached`, the production spawn
     helper `warm.client`/`warm_start.py` use. Found empirically while
     building this fixture: `spawn_detached` resolves its `script_path` via
     `os.path.abspath(script_path)` against the CALLER's cwd, not the
     `repo_root` argument it is also passed, AND launches the server by
     direct script path (`python <path>/coordinator_core/warm/server.py`,
     never `-m`) -- Python then puts the SCRIPT'S OWN directory
     (`.../coordinator_core/warm/`) on `sys.path[0]`, not the repo root, so
     the server's own `from coordinator_core.warm import skew` cannot
     resolve `coordinator_core` from that clone at all and falls through to
     this box's ambient editable-install pin (see
     editable-install-pins-coordinator-core-to-live-tree in project memory)
     -- which right now points at the live, UNSTAMPED tree, so the spawned
     process immediately raises `UnstampedEngineRootError` and never writes
     a breadcrumb. Reproduced directly: running `spawn_detached`'s own argv
     shape by hand against an isolated clone crashes with exactly that
     traceback; running the SAME clone via `python -m
     coordinator_core.warm.server` with `cwd` set (which DOES put cwd on
     `sys.path[0]`) boots cleanly. This is a real, load-bearing fragility
     in the production spawn path (reported to the dispatching EM
     separately -- `coordinator_core/ops/ceremony/detached_spawn.py` and
     `warm_start.py`/`warm/client.py`'s calls into it are outside this
     file's writable scope) -- this fixture routes around it with a
     deterministic `-m` launch rather than depending on it or silently
     fixing it out of scope.
  4. Poll (bounded wall-clock wait, `_BOOT_WAIT_DEADLINE_SECS`) for a live,
     PID-alive breadcrumb under the isolated root -- this wait is WALL
     CLOCK BY NATURE (a boot-completion precondition, not the measurement)
     and never enters any `process_time_ms` assertion.
  5. On success: yield the isolated root to the tests, which measure
     against it exactly as revision 1 measured against the discovered
     ambient root.
  6. Teardown (always, via `finally`): terminate the spawned server
     (`psutil`, mirroring `warm-engine-stop.py`'s direct-signal fallback --
     no graceful ask needed, this server has exactly one client, this
     fixture), remove the breadcrumb's OWN directory
     (`breadcrumb.svc_dir(engine_root=isolated_root)` --
     `%LOCALAPPDATA%/coordinator/warm/<clone_hash>/`, machine-global and
     OUTSIDE the isolated clone -- found missing in revision 1 by code
     review: the temp-clone rmtree below never touched it, so every run
     left a permanent stray directory naming a dead pid in the same store
     a human or agent reads to answer "is there a warm engine, and
     whose"), then remove the isolated directory itself. Hardlinks share
     the source clone's file DATA, never its directory entries -- deleting
     the isolated tree's entries does not touch the source clone's files.

REPRESENTATIVE OF WHAT, PRECISELY -- the source clone's `coordinator_core`
bytes AS OF THE HARDLINK (a point-in-time snapshot immune to a concurrent
publish round rewriting the resident server mid-measurement), running the
real `warm.server`/`warm.election`/`ipc.dispatch_message` code, over a real
named-pipe transport, with a real one-time op-registry import paid at this
boot. The only thing NOT representative of the production path is how the
server process itself got started (`-m` here vs. the currently-fragile
direct-script spawn in production) -- irrelevant to what this gate measures
(the CLIENT's `python -m coordinator_core.invoke ping` cost against an
already-warm server), and a one-time boot cost this file's wall-clock-bounded
wait absorbs, never the timed assertion.

REMAINING SKIP CASES, NAMED (this file no longer skips on "no server
happened to be running" -- it boots one):
  - Off Windows (job-object accounting has no POSIX equivalent).
  - No candidate source root carries a valid build stamp anywhere on this
    box (`warm.engine_root.is_engine_root` false for every candidate) --
    genuinely nothing stamped to boot a server FROM.
  - The isolated server does not reach a PID-alive breadcrumb within
    `_BOOT_WAIT_DEADLINE_SECS` -- a bounded wait, not an infinite one.

Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
§ Measured findings, § The bar.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

import pytest

from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms
from coordinator_core.warm import breadcrumb
from coordinator_core.warm.engine_root import is_engine_root
from coordinator_core.session.core import stable_pid_alive

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PROCESS_TIME_CEILING_MS = 200.0
"""See module docstring's "THE THRESHOLD, AND WHY" for the full derivation."""

K_INVOCATIONS = 20
"""Amortisation factor recovering sub-tick resolution -- see
`coordinator_core.benchmarks.process_time` module docstring, trap 2."""

_ENGINE_ROOT_OVERRIDE_ENV = "COORDINATOR_WARM_GATE_ENGINE_ROOT"

_BOOT_WAIT_DEADLINE_SECS = 20.0
"""Bounded wall-clock wait for the isolated server's own breadcrumb to go
PID-alive -- a boot precondition, never part of the timed measurement (see
module docstring point 4)."""

_BOOT_POLL_INTERVAL_SECS = 0.25

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _candidate_source_roots() -> list[Path]:
    """Stamped-engine-root candidates to hardlink FROM, in preference
    order. An explicit env override always wins (a different box's clone
    layout); otherwise the sibling `claude-klabauter` published-mirror
    directory next to this live tree's own parent -- the only warm-capable
    build this box conventionally has (this live tree itself carries no
    stamp, DR-315 §2)."""
    override = os.environ.get(_ENGINE_ROOT_OVERRIDE_ENV)
    if override:
        return [Path(override)]
    live_tree_root = Path(__file__).resolve().parents[3]
    return [live_tree_root.parent / "claude-klabauter"]


def _resolve_stamped_source_root() -> Optional[Path]:
    """First candidate that is a valid, stamped engine root, or None."""
    for candidate in _candidate_source_roots():
        if candidate.is_dir() and is_engine_root(candidate):
            return candidate
    return None


def _hardlink_coordinator_core(source_root: Path, isolated_root: Path) -> int:
    """Hardlinks `source_root/coordinator_core` into
    `isolated_root/coordinator_core`, skipping `__pycache__` (stale
    cross-invocation bytecode is not worth carrying, and would itself be a
    fresh compile on first import either way). Returns the file count.

    `os.link` requires both paths on the SAME VOLUME -- `isolated_root`'s
    caller is responsible for placing it beside `source_root`, never in the
    platform default temp directory (routinely a different drive here).
    """
    n = 0
    src_pkg = source_root / "coordinator_core"
    for root, dirs, files in os.walk(src_pkg):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(src_pkg)
        dst_dir = isolated_root / "coordinator_core" / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, dst_dir / name)
            n += 1
    return n


def _wait_for_live_breadcrumb(isolated_root: Path, deadline_secs: float) -> Optional[dict]:
    """Polls (bounded, wall clock) for a PID-alive breadcrumb under
    `isolated_root`. Returns the breadcrumb dict on success, None on
    timeout -- never raises."""
    deadline = time.time() + deadline_secs
    while time.time() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb:
            pid = crumb.get("pid")
            epoch = crumb.get("stable_pid_start_epoch") or ""
            if pid is not None and stable_pid_alive(pid, stored_start_epoch=str(epoch)):
                return crumb
        time.sleep(_BOOT_POLL_INTERVAL_SECS)
    return None


def _terminate(pid: int) -> None:
    """Best-effort direct-signal stop, mirroring `warm-engine-stop.py`'s
    fallback mechanism -- no graceful ask needed, this server has exactly
    ONE client (this fixture)."""
    import psutil

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass


@pytest.fixture(scope="module")
def warm_engine_root() -> Iterator[Path]:
    if not IS_WINDOWS:
        pytest.skip("process-time job-object accounting is a Windows-only primitive")

    source_root = _resolve_stamped_source_root()
    if source_root is None:
        pytest.skip(
            "no candidate engine root carries a valid build stamp among "
            f"{_candidate_source_roots()!r} -- nothing stamped to boot an isolated "
            f"warm server from; point {_ENGINE_ROOT_OVERRIDE_ENV} at one to run this gate"
        )

    tmp_parent = Path(tempfile.mkdtemp(prefix="warm-door-gate-", dir=str(source_root.parent)))
    isolated_root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _hardlink_coordinator_core(source_root, isolated_root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(isolated_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        crumb = _wait_for_live_breadcrumb(isolated_root, _BOOT_WAIT_DEADLINE_SECS)
        if crumb is None:
            if proc.poll() is None:
                proc.terminate()
            pytest.skip(
                f"isolated warm server (source={source_root}) did not reach a "
                f"PID-alive breadcrumb within {_BOOT_WAIT_DEADLINE_SECS}s"
            )
        yield isolated_root
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb and crumb.get("pid") is not None:
            _terminate(int(crumb["pid"]))
        elif proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        # The breadcrumb does NOT live inside `isolated_root` -- `svc_dir`
        # resolves it under the machine-global, per-clone-hash runtime base
        # (`%LOCALAPPDATA%/coordinator/warm/<clone_hash>/`, see
        # `breadcrumb.svc_dir`'s own docstring), so rmtree-ing the temp
        # clone alone leaves that directory behind forever -- a permanent,
        # ever-growing stray naming a server that will never exist again.
        # Safe to remove UNCONDITIONALLY here (never `unlink_breadcrumb`'s
        # ownership-checked partial case): `isolated_root` is a freshly
        # `mkdtemp`'d path unique to THIS run, so its derived clone hash can
        # by construction never collide with a real clone's, and this run
        # is the only possible writer of anything under it.
        shutil.rmtree(breadcrumb.svc_dir(engine_root=isolated_root), ignore_errors=True)
        shutil.rmtree(tmp_parent, ignore_errors=True)


def _invoke_ping_cmd() -> list:
    return [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"]


def test_warm_door_process_time_is_under_the_ceiling(warm_engine_root):
    """Primary assertion (see module docstring's "THE THRESHOLD, AND WHY"):
    the process-time cost of `python -m coordinator_core.invoke ping`,
    batched over K_INVOCATIONS runs against a freshly-booted, isolated warm
    engine, must not exceed PROCESS_TIME_CEILING_MS.
    """
    result = batched_process_time_ms(
        _invoke_ping_cmd(), k=K_INVOCATIONS, cwd=str(warm_engine_root)
    )
    assert result["rc"] == 0, (
        f"warm-door ping invocation exited rc={result['rc']} -- a failing "
        "invocation cannot stand in for a valid process-time sample"
    )
    assert result["process_time_ms"] <= PROCESS_TIME_CEILING_MS, (
        f"warm-door process time regressed: {result['process_time_ms']}ms "
        f"exceeds the {PROCESS_TIME_CEILING_MS}ms ceiling "
        f"(k={result['k']}, wall_ms={result['wall_ms']} for context only). "
        "See this file's module docstring for the ceiling's derivation."
    )


def test_warm_door_is_not_dramatically_costlier_than_cold(warm_engine_root):
    """Secondary/informational assertion only (per the handoff's own
    finding that a strict warm-cheaper-than-cold comparison is too weak a
    gate on its own -- it passed comfortably the same session the door cost
    121ms). This does not require warm to beat cold; it only bounds warm
    from being pathologically WORSE than cold, which would indicate the
    warm path picked up extra per-call cost the cold path does not pay.
    """
    warm = batched_process_time_ms(_invoke_ping_cmd(), k=K_INVOCATIONS, cwd=str(warm_engine_root))
    cold_env = dict(os.environ)
    cold_env["COORDINATOR_WARM"] = "0"
    cold = batched_process_time_ms(
        _invoke_ping_cmd(), k=K_INVOCATIONS, env=cold_env, cwd=str(warm_engine_root)
    )
    assert warm["rc"] == 0 and cold["rc"] == 0, (
        f"warm rc={warm['rc']}, cold rc={cold['rc']} -- both must succeed for "
        "this comparison to mean anything"
    )
    slack_ms = 60.0
    assert warm["process_time_ms"] <= cold["process_time_ms"] + slack_ms, (
        f"warm door ({warm['process_time_ms']}ms) is more than {slack_ms}ms "
        f"costlier than cold ({cold['process_time_ms']}ms) -- informational "
        "secondary check, see this test's own docstring"
    )
