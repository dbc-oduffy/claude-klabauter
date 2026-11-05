"""How long each hook-shaped warm dial takes to answer while a publish stages and while it swaps.

Two windows, each held in a throwaway runtime base (`COORDINATOR_WARM_RUNTIME_BASE`), never the box's:

  staging  the handover-pending breadcrumb only (`hold_handover_pending`); the pair is not stopped.
  swap     the breadcrumb plus the publish marker (`hold_publish_marker`); no warm pair answers.

Each hook runs once per window through its real entry, with the spawn side effect stubbed to a counter:

  guard_dial   `entry_seam.try_warm_guard_dispatch` (every guard-shaped hook; `hook_path=True`)
  host_shim    `host_hooks.shim.decide` (codex PreToolUse; 150ms dial wall)
  emit_frame   `client.try_warm_dispatch` of a `dispatch.emit` ask frame with `hook_path=True` (the
               submit hook's mint, the one frame `client._await_stage_admission` queues)

Both units are reported. Process time (`time.process_time`) is the DR-344 bar (500ms) and is blind to a
sleep; wall is what a blocked hook costs the operator (1s floor). A call still running at `CAP_SECS` is
reported as `cap` and fails the wall floor.

Run: `python -m coordinator_core.benchmarks.hook_latency_under_publish_quiesce [--check]`.
`--check` exits 1 when any hook misses either bar in a held window (staging, swap).
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional
from unittest import mock

PROCESS_BAR_MS = 500.0
WALL_FLOOR_MS = 1000.0
CAP_SECS = 3.0
WINDOWS = ("none", "staging", "swap")


@dataclass
class Row:
    hook: str
    window: str
    process_ms: float
    wall_ms: float
    capped: bool
    spawn_attempts: int
    outcome: str

    @property
    def ok(self) -> bool:
        return not self.capped and self.process_ms < PROCESS_BAR_MS and self.wall_ms < WALL_FLOOR_MS


def _guard_dial() -> str:
    from coordinator_core.warm import entry_seam

    outcome = entry_seam.try_warm_guard_dispatch("warm_guard.evaluate", {"payload": {"hook_event_name": "PreToolUse"}})
    return f"hit={outcome.hit}"


def _host_shim(cwd: str) -> Callable[[], str]:
    def call() -> str:
        from coordinator_core.host_hooks import shim

        answer = shim.decide(
            "codex",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "bench",
                "tool_name": "Bash",
                "tool_input": {"command": "echo ok"},
                "cwd": cwd,
            },
        )
        return f"answer_keys={sorted(answer)}"

    return call


def _emit_frame() -> str:
    from coordinator_core.warm import client

    msg = {"jsonrpc": "2.0", "id": 1, "method": "dispatch.emit", "params": {"ask": "bench ask"}}
    response = client.try_warm_dispatch(msg, hook_path=True)
    return f"warm_response={response is not None}"


@contextlib.contextmanager
def _window(name: str, clone_root: Path) -> Iterator[None]:
    from coordinator_core.warm import breadcrumb

    if name == "none":
        yield
        return
    with contextlib.ExitStack() as stack:
        stack.enter_context(breadcrumb.hold_handover_pending(None))
        if name == "swap":
            stack.enter_context(breadcrumb.hold_publish_marker(clone_root, holder=f"bench:{os.getpid()}"))
        yield


def _measure(hook: str, window: str, call: Callable[[], str], spawns: List[int]) -> Row:
    box: dict = {}

    def run() -> None:
        try:
            box["outcome"] = call()
        except Exception as exc:  # noqa: BLE001 -- a raising hook is a reported row, not a crash
            box["outcome"] = f"raised {type(exc).__name__}: {exc}"

    spawns_before = spawns[0]
    cpu0, wall0 = time.process_time(), time.perf_counter()
    worker = threading.Thread(target=run, name=f"hook-{hook}", daemon=True)
    worker.start()
    worker.join(CAP_SECS)
    cpu_ms = (time.process_time() - cpu0) * 1000.0
    wall_ms = (time.perf_counter() - wall0) * 1000.0
    capped = worker.is_alive()
    return Row(hook, window, cpu_ms, wall_ms, capped, spawns[0] - spawns_before, "cap" if capped else box.get("outcome", "?"))


def run_benchmark() -> List[Row]:
    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home

    declare_benchmark_origin()
    rows: List[Row] = []
    with tempfile.TemporaryDirectory(prefix="hook-latency-") as raw:
        base = Path(raw)
        spawns = [0]

        def count_spawn(*_a, **_k) -> None:
            spawns[0] += 1

        with contextlib.ExitStack() as env:
            env.enter_context(mock.patch.dict(os.environ, {"COORDINATOR_WARM_RUNTIME_BASE": str(base / "rt"), "COORDINATOR_WARM": "1"}))
            env.enter_context(fixture_settings_home(base))
            from coordinator_core.warm import client, entry_seam
            from coordinator_core.warm.engine_root import current_engine_clone

            env.enter_context(mock.patch.object(client, "_live_tree_cold", False))
            env.enter_context(mock.patch.object(client, "engine_token", lambda: "benchtoken"))
            env.enter_context(mock.patch.object(client, "_configured_root_is_foreign", lambda: False))
            env.enter_context(mock.patch.object(client, "_spawn_once", count_spawn))
            env.enter_context(mock.patch.object(entry_seam, "_trigger_listener_boot", lambda: None))
            clone_root = Path(current_engine_clone())
            hooks = (
                ("guard_dial", _guard_dial),
                ("host_shim", _host_shim(str(base))),
                ("emit_frame", _emit_frame),
            )
            for window in WINDOWS:
                for hook, call in hooks:
                    with _window(window, clone_root):
                        rows.append(_measure(hook, window, call, spawns))
    return rows


def render(rows: List[Row]) -> str:
    lines = [f"{'hook':<11} {'window':<8} {'process_ms':>10} {'wall_ms':>9} {'spawns':>6}  verdict  outcome"]
    for r in rows:
        lines.append(
            f"{r.hook:<11} {r.window:<8} {r.process_ms:>10.1f} {r.wall_ms:>9.1f} {r.spawn_attempts:>6}  "
            f"{'ok' if r.ok else 'OVER':<7}  {r.outcome}"
        )
    lines.append(f"bars: process < {PROCESS_BAR_MS:.0f}ms, wall < {WALL_FLOOR_MS:.0f}ms, cap {CAP_SECS:.0f}s")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    rows = run_benchmark()
    print(render(rows))
    held = [r for r in rows if r.window != "none"]
    return 1 if "--check" in args and not all(r.ok for r in held) else 0


if __name__ == "__main__":
    sys.exit(main())
