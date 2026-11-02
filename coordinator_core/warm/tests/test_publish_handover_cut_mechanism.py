"""Spike: what a publish's warm-pair stop + file swap + re-warm does to a run's engine ops.

A fixture "run" issues ops through `warm.client.try_warm_dispatch` in a loop against a real warm
server child while the real `hold_publish_marker` + `pair_stop.stop_warm_pair` + a slow file swap
+ a re-warm run in the foreground. Every op is classified:

  (a) in-flight op killed: an op already running when the stop began did not get its reply.
  (b) cold under the swap: an op went cold while the publish marker was held AND the tree was only
      partly copied. A cold op executes in a fresh interpreter from that tree, so this is the cut.
  (c) other cold: the op went cold on an intact tree (refused while draining, or between the swap
      and the successor boot). Not a cut: the cold path is a success path on a complete tree.

The server child runs the real `_ServerContext` accept/drain/evict path with only `_pool_dispatch`
replaced, so the op body is a sleep and everything the stop and the client do around it is real.
The classification is the spike's finding; see docs/research/2026-11-02-warm-stop-cut-mechanism.md.

`test_handover_contract_*` is the contract: no op fails or runs cold over a partly copied tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, lifecycle, pair_stop

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOKEN = "cut-mechanism-token"
INFLIGHT_SECS = 1.5
SWAP_FILES = 4
SWAP_STEP_SECS = 0.4
LOOP_GAP_SECS = 0.05
_BOOT_DEADLINE_SECS = 90.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

assert SWAP_FILES * SWAP_STEP_SECS > breadcrumb.DRAIN_WAIT_SECS
assert INFLIGHT_SECS < client.READ_DEADLINE_SECS

_SERVER_CHILD = r'''
import os, sys, time
from pathlib import Path

repo, root, stop_token, token = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server


class _Version:
    server_sha = "cut-mechanism"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return client_token == stop_token


def _dispatch(self, msg, **_kw):
    secs = float((msg.get("params") or {}).get("sleep", 0))
    if secs:
        (root / "dispatch-entered.flag").write_text("1")
        time.sleep(secs)
    return {"jsonrpc": "2.0", "id": msg["id"], "result": "served"}


server._ServerContext._pool_dispatch = _dispatch
on_windows = sys.platform == "win32"
elected = (
    server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
)
ctx = server._ServerContext(
    name=elected.endpoint,
    sid=elected.identity,
    version_state=_Version(),
    engine_root=root,
    listen_socket=elected.listen_socket,
    endpoint_path=elected.endpoint_path,
)
breadcrumb.write_breadcrumb(
    pipe=elected.endpoint,
    pid=os.getpid(),
    stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
    engine_sha=_Version.server_sha,
    engine_root=root,
    transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
)
if elected.first_handle is not None:
    ctx.serve_forever(elected.first_handle)
else:
    ctx.serve_forever_unix(elected.listen_socket)
'''


@dataclass
class _Op:
    seq: int
    name: str
    outcome: str
    phase_end: str
    marker_held: bool
    tree_complete: bool
    detail: str = ""


@dataclass
class _State:
    phase: str = "pre"
    tree_complete: bool = True
    ops: list = field(default_factory=list)
    spawn_attempts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _repo_dir() -> str:
    import coordinator_core

    return str(Path(coordinator_core.__file__).resolve().parents[1])


@pytest.fixture()
def engine_root(tmp_path: Path) -> Path:
    root = tmp_path / "eng"
    (root / "coordinator_core" / "warm").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:cut-mechanism\n", encoding="utf-8")
    return root


@pytest.fixture()
def spawned(engine_root: Path):
    procs: list[subprocess.Popen] = []
    yield procs
    import psutil

    for p in pair_stop.find_pair_processes(engine_root):
        try:
            psutil.Process(p.pid).kill()
        except psutil.Error:
            pass
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture()
def state(engine_root: Path, monkeypatch: pytest.MonkeyPatch) -> _State:
    """Point the real client at the fixture server and count the spawns it would attempt."""
    st = _State()
    monkeypatch.setenv("COORDINATOR_WARM", "1")
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "2")
    monkeypatch.setattr(client, "_engine_clone_root", lambda: engine_root)
    monkeypatch.setattr(breadcrumb, "_default_engine_clone", lambda: engine_root)
    monkeypatch.setattr(client, "engine_token", lambda: TOKEN)
    monkeypatch.setattr(client, "_configured_root_is_foreign", lambda: False)
    monkeypatch.setattr(client, "_sidecar_keyed_clone", lambda: None)

    def _count_spawn() -> None:
        st.spawn_attempts += 1

    monkeypatch.setattr(client, "_spawn_once", _count_spawn)
    return st


def _boot_server(engine_root: Path, spawned: list, *, not_pid: "int | None" = None) -> dict:
    script = engine_root / "coordinator_core" / "warm" / "server.py"
    script.write_text(_SERVER_CHILD, encoding="utf-8")
    log = open(engine_root / "server.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, str(script), _repo_dir(), str(engine_root), pair_stop._STOP_REQUEST_TOKEN, TOKEN],
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=_NO_WINDOW,
    )
    spawned.append(proc)
    deadline = time.monotonic() + _BOOT_DEADLINE_SECS
    while time.monotonic() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root)
        if crumb and crumb.get("pid") == proc.pid and crumb.get("pid") != not_pid:
            return crumb
        if proc.poll() is not None:
            pytest.fail(f"server child exited {proc.returncode} before it was ready")
        time.sleep(0.1)
    pytest.fail(f"server child not ready within {_BOOT_DEADLINE_SECS}s")


class _RunLoop:
    """The fixture run: one op per tick through the real client, each stamped with the publish state."""

    def __init__(self, st: _State, engine_root: Path) -> None:
        self._st = st
        self._root = engine_root
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._seq = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(30)
        assert not self._thread.is_alive()

    def _loop(self) -> None:
        while not self._halt.is_set():
            self.one_op("tick", 0)
            time.sleep(LOOP_GAP_SECS)

    def one_op(self, name: str, sleep_secs: float) -> None:
        st = self._st
        self._seq += 1
        seq = self._seq
        msg = {"jsonrpc": "2.0", "id": seq, "method": "ping", "params": {"sleep": sleep_secs}}
        response = client.try_warm_dispatch(msg)
        if response is None:
            outcome = "cold"
        elif response.get("result") == "served":
            outcome = "served"
        else:
            outcome = "error"
        held = breadcrumb.publish_marker_held(breadcrumb.svc_dir(self._root))
        with st.lock:
            st.ops.append(_Op(seq, name, outcome, st.phase, held, st.tree_complete, repr(response)[:300]))


def _swap(engine_root: Path, st: _State) -> None:
    st.tree_complete = False
    for n in range(SWAP_FILES):
        (engine_root / "coordinator_core" / f"new_{n}.py").write_text("x = 1\n", encoding="utf-8")
        time.sleep(SWAP_STEP_SECS)
    st.tree_complete = True


def _publish_sequence(engine_root: Path, spawned: list, st: _State) -> pair_stop.PairStopResult:
    """The publish.py order: hold marker -> stop pair (drains) -> copy -> release -> re-warm."""
    first = _boot_server(engine_root, spawned)
    run = _RunLoop(st, engine_root)
    inflight = threading.Thread(target=run.one_op, args=("inflight", INFLIGHT_SECS), daemon=True)
    run.start()
    try:
        _wait(lambda: any(o.outcome == "served" for o in st.ops), "first served op", detail=lambda: repr(st.ops[:2]))
        with breadcrumb.hold_publish_marker(engine_root, holder="cut-mechanism-test"):
            st.phase = "drain"
            inflight.start()
            _wait(lambda: (engine_root / "dispatch-entered.flag").exists(), "in-flight op in dispatch")
            result = pair_stop.stop_warm_pair(engine_root)
            inflight.join(30)
            st.phase = "swap"
            _swap(engine_root, st)
        st.phase = "rewarm"
        time.sleep(0.3)
        _boot_server(engine_root, spawned, not_pid=first["pid"])
        st.phase = "post"
        _wait(lambda: any(o.outcome == "served" and o.phase_end == "post" for o in st.ops), "post-handover served op")
    finally:
        run.stop()
    return result


def _wait(predicate, what: str, *, deadline_secs: float = 60.0, detail=lambda: "") -> None:
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"{what}: not reached within {deadline_secs}s {detail()}")


def census(st: _State) -> str:
    """`phase/outcome/op: count` lines, for the failure message and a `-s` run."""
    counts: dict = {}
    for o in st.ops:
        key = f"{o.phase_end}/{o.outcome}/{o.name}"
        counts[key] = counts.get(key, 0) + 1
    return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) + f"\nspawn_attempts: {st.spawn_attempts}"


def classify(ops: list) -> dict:
    return {
        "a_inflight_cut": [o for o in ops if o.name == "inflight" and o.outcome != "served"],
        "b_cold_under_swap": [
            o for o in ops if o.outcome == "cold" and o.marker_held and not o.tree_complete
        ],
        "c_other_cold": [
            o
            for o in ops
            if o.outcome == "cold" and not (o.marker_held and not o.tree_complete) and o.name != "inflight"
        ],
        "errors": [o for o in ops if o.outcome == "error"],
    }


def test_handover_contract_no_op_fails_or_runs_cold_over_a_partly_copied_tree(
    engine_root, spawned, state
) -> None:
    _publish_sequence(engine_root, spawned, state)
    cls = classify(state.ops)

    assert cls["a_inflight_cut"] == []
    assert cls["errors"] == []
    assert cls["b_cold_under_swap"] == []


@pytest.mark.parametrize("dispatch_timeout", ["2", "30", "120"])
def test_drain_ceiling_outlasts_every_deadline_a_waiting_caller_holds(monkeypatch, dispatch_timeout) -> None:
    """Mechanism (a) cannot cut an op whose caller is still waiting: the ceiling is the dispatch
    timeout plus a margin, and the client's own read deadlines are bounded by the dispatch timeout."""
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", dispatch_timeout)
    ceiling = lifecycle._drain_ceiling_secs()

    assert ceiling > client._mutation_deadline_for("test.unclassified_mutation")
    assert ceiling > client.READ_DEADLINE_SECS
