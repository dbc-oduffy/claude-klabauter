"""`dispatch.terminal_commit` at a real session's shape: the process-time bar, and the late result of a
warm-served call that outlives its budget.

The shape is `terminal_commit_real_session_shape`: a session of WF_DIRS `wf_` dirs and AGENT_METAS agent
metas, a wave-bookkeeping stem, and a landing through `ceremony.commit_v2` of LANDING_PATHS paths.
Evidence of the red at 2d96cf63f5 is C2's real-shape measurement
(docs/research/spike-verdicts/2026-10-27-warm-mutating-op-process-cost.md), not a checkout of the
baseline: `locate_run_dir` about 117ms at 370 dirs, and the landing leg's `head_classification`
4.1-5.4s on a 7.6k-path commit.

Process time only: wall clock measures peer load (CLAUDE.md, The brightline).
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from coordinator_core import dispatch_abandon, ipc
from coordinator_core.benchmarks import terminal_commit_real_session_shape as shape
from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.ops.dispatch_emit import process_artifact_policy, terminal_commit
from coordinator_core.telemetry import op_latency
from coordinator_core.warm import dispatch_ack, server
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BAR_MS = 500.0
K_INVOCATIONS = 1
SAMPLES = 2
_OP = "dispatch.terminal_commit"
_DRIVER = "coordinator_core.benchmarks.terminal_commit_real_session_shape"
_SMALL_WF_DIRS = 25
_SMALL_LANDING_PATHS = 40
# The landing must finish inside the budget: the engine refuses a ref update after the timeout
# (`dispatch_abandon`), so the op overruns in its post-landing tail, as the incident's did.
_BUDGET = 6.0

_PENDING = pytest.mark.pending_fix(
    reason=(
        "bug row 29d78c496834 (landing leg) was addressed but the 7.5k-path shape still measures 7796.9ms net "
        "process time / 35 procs (bar 500ms); residual phase (per-path work in commit._checkin_bytes, pre_commit_gates) "
        "awaits the residual bug row (id not yet filed); closing that row must remove this marker."
    )
)


def test_the_shape_is_a_real_session(tmp_path):
    session = shape.build_session_tree(tmp_path, "sid", "run", wf_dirs=3)
    workflows = session / "subagents" / "workflows"
    assert len(list(workflows.glob("wf_0*"))) == 3
    assert len(list(workflows.glob("wf_0*/agent-*.meta.json"))) == 3 * shape.AGENTS_PER_WF
    assert shape.WF_DIRS >= 300 and shape.AGENT_METAS >= 4000


def _min_sample(workdir, *extra: str) -> dict:
    samples = []
    for _ in range(SAMPLES):
        result = batched_process_time_ms(
            [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
        )
        assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
        samples.append(result)
    return min(samples, key=lambda r: r["process_time_ms"])


@_PENDING
def test_terminal_commit_at_real_session_shape_is_under_the_bar(tmp_path):
    setup = _min_sample(tmp_path, "--setup-only")
    full = _min_sample(tmp_path)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    detail = (
        f"{net_ms}ms net tree process time over {shape.WF_DIRS} wf_ dirs / {shape.AGENT_METAS} metas / "
        f"{shape.LANDING_PATHS} landed paths "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    assert net_ms > 0, f"call cost not observed (full <= setup-only): {detail}"
    assert net_ms < BAR_MS, detail


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The landing fixture at a size the op still outlives a 50ms budget, served by a real AckStore."""
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "COORDINATOR_SESSION_ID"):
        monkeypatch.setenv(key, "restored-by-monkeypatch")
    monkeypatch.setattr(process_artifact_policy, "hosts_coordinator_state", lambda root: True)
    monkeypatch.setattr(terminal_commit, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.WARM_SERVER)
    monkeypatch.setattr(ipc, "_timeout_for", lambda m, msg=None: _BUDGET)
    monkeypatch.setattr(ipc, "_report_ceremony_overrun", lambda *a: None)
    import coordinator_core.warm.client as wc

    monkeypatch.setattr(wc, "_op_may_mutate", lambda m: True)
    monkeypatch.setattr(server, "_ack_store", dispatch_ack.AckStore())

    repo, params = shape.build_fixture(
        tmp_path, wf_dirs=_SMALL_WF_DIRS, landing_paths=_SMALL_LANDING_PATHS
    )
    landing = terminal_commit._handler

    def outlives_its_budget(params, repo_root=None):
        out = landing(params, repo_root=repo_root)
        while True:
            left = dispatch_abandon.remaining()
            if left is None or left <= 0.0:
                return out
            time.sleep(min(left, 0.05) + 0.01)

    monkeypatch.setitem(ipc._REGISTRY, _OP, outlives_its_budget)
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, _OP)
    msg = {"jsonrpc": "2.0", "id": 7, "method": _OP, "params": params, "_origin_worktree": str(repo)}
    return repo, msg, key


def _landed_files(repo, sha: str) -> list:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=", sha], cwd=str(repo), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.split()


class _DoneFuture:
    def __init__(self, result):
        self._result = result

    def cancelled(self):
        return False

    def exception(self):
        return None

    def result(self):
        return self._result


def test_pool_served_late_result_reaches_the_ack_record_with_the_landed_sha(served):
    repo, msg, key = served

    response = server._pool_dispatch_worker_body(msg, None)
    server._stamp_pool_future(key, _DoneFuture(response))

    status = server._ack_store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED, status
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT, status
    assert status["result"].get("committed") is True, status["result"]
    sha = status["result"]["sha"]
    assert sha == response["result"]["sha"]
    assert "pkg/f00000.py" in _landed_files(repo, sha)


def test_inline_served_call_replies_indeterminate_and_the_ack_record_holds_the_landed_sha(served):
    repo, msg, key = served

    response = server._run_dispatch(msg, dispatch_key=key)

    assert response["error"]["code"] == -32004, response
    status = server._ack_store.status(key)
    assert status["state"] == dispatch_ack.STATE_FINISHED, status
    assert status["outcome"] == dispatch_ack.OUTCOME_RESULT, status
    assert status["result"].get("sha"), status["result"]
    assert "pkg/f00000.py" in _landed_files(repo, status["result"]["sha"])
