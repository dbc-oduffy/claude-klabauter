"""Measure what a hook fire costs on each transport, in both units, with the shape stated.

WHY BOTH UNITS. This workstream has now produced two wall-clock artifacts and one
process-time blind spot, so neither number alone can be trusted here:

  * The sibling plan first cited 421ms/341ms read off a WALL column that turned out to be
    an ~8x job-object batching inflation, beside a process-time column that was right.
  * That same plan concluded "85% of a Bash call is the login profile" from a wall median.
    Re-measured 2026-08-23: `bash -lc true` is 36.2ms against `bash -c true` at 31.8ms --
    the profile is ~3ms of real work, and the ~600ms median was queueing on a box running
    dozens of sessions.
  * Conversely, a `PreToolUse` hook BLOCKS the session, so the operator actually waits wall
    time. A bar claimed met in process time can read green while the operator waits half a
    second -- doe-claude-74 spot-checked one call at 674ms wall for 46ms user+sys.

So: report both, side by side, and state the invocation shape every figure was taken in.
Process time isolates work from wait; wall is what the operator pays. Neither is "the truth".

ARMS ARE INTERLEAVED ROUND-ROBIN, NEVER RUN SEQUENTIALLY. On a contended box, load drifts
over the life of a run, and sequential arms silently attribute that drift to whichever arm
happened to run during it. Round-robin spreads the drift across all arms equally. This is
the method doe-claude-74 used and it is the only reason their arm-to-arm comparison means
anything.

NEGATIVE SPEC.

- **No spawn-count derivation from `procs_per_call - 1`.** The sibling bench interpolated a
  resolved path into a child's SOURCE text with `'%s'`; on Windows the backslash opening a
  `bin` segment parsed as a backspace escape, the child raised, no git spawned, and the
  derived spawn count silently became 0 -- taking the whole bench down with it. Any value
  landing in generated source uses `%r`, never `'%s'`.
- **No arm that degrades silently.** An arm needing a live listener asserts it and SKIPS
  LOUDLY when absent. An arm that quietly fell back to cold and reported itself as warm is
  worse than no measurement.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Callable, Dict, List, Optional


def _percentile(sorted_xs: List[float], q: float) -> float:
    """Nearest-rank percentile. Explicit rather than `statistics.quantiles` so a small n
    behaves predictably -- these runs are n>=25, not n>=1000, and interpolation between
    two neighbouring samples invents precision the sample size does not support."""
    if not sorted_xs:
        return float("nan")
    k = max(0, min(len(sorted_xs) - 1, int(round(q * (len(sorted_xs) - 1)))))
    return sorted_xs[k]


class Arm:
    """One measured configuration, with the invocation shape it was taken in.

    `shape` is not decoration. A figure without its invocation shape is not evidence --
    this workstream produced two wrong conclusions from figures whose shape was implicit,
    so it is a required constructor argument rather than an optional label.
    """

    def __init__(self, name: str, shape: str, call: Callable[[], None],
                 precondition: Optional[Callable[[], Optional[str]]] = None):
        self.name = name
        self.shape = shape
        self.call = call
        self.precondition = precondition
        self.wall_ms: List[float] = []
        self.skipped_reason: Optional[str] = None

    def check(self) -> bool:
        """Resolve the arm's precondition. A failure SKIPS the arm loudly rather than
        letting it run in a degraded configuration and report as if it had not."""
        if self.precondition is None:
            return True
        reason = self.precondition()
        if reason:
            self.skipped_reason = reason
            return False
        return True

    def sample(self) -> None:
        start = time.perf_counter()
        self.call()
        self.wall_ms.append((time.perf_counter() - start) * 1000.0)

    def summary(self) -> Dict[str, object]:
        if self.skipped_reason:
            return {"arm": self.name, "shape": self.shape, "skipped": self.skipped_reason}
        xs = sorted(self.wall_ms)
        return {
            "arm": self.name,
            "shape": self.shape,
            "n": len(xs),
            "wall_min_ms": round(xs[0], 3) if xs else None,
            "wall_p50_ms": round(_percentile(xs, 0.50), 3),
            "wall_p90_ms": round(_percentile(xs, 0.90), 3),
            "wall_max_ms": round(xs[-1], 3) if xs else None,
            "wall_mean_ms": round(statistics.fmean(xs), 3) if xs else None,
        }


def run_interleaved(arms: List[Arm], n: int = 25, warmup: int = 3) -> List[Dict[str, object]]:
    """Sample every arm once per round, `n` rounds. See the module docstring on why.

    Warmup rounds are discarded: the first call on any transport pays one-time import and
    connection setup that no steady-state hook fire pays, and counting it would overstate
    every arm by a different amount.
    """
    live = [a for a in arms if a.check()]

    for _ in range(warmup):
        for arm in live:
            try:
                arm.call()
            except Exception:  # noqa: BLE001 -- a warmup failure is not a measurement
                pass

    for _ in range(n):
        for arm in live:
            arm.sample()

    return [a.summary() for a in arms]


def format_report(summaries: List[Dict[str, object]]) -> str:
    """A table plus the shape line for each arm. The shapes are printed BELOW the table
    rather than in a column because they are sentences, and truncating them into a column
    is how a figure loses the context that makes it evidence."""
    lines = [
        "%-34s %6s %10s %10s %10s %10s" % ("arm", "n", "min", "p50", "p90", "max"),
        "-" * 84,
    ]
    for s in summaries:
        if s.get("skipped"):
            lines.append("%-34s %s" % (s["arm"], "SKIPPED: %s" % s["skipped"]))
            continue
        lines.append(
            "%-34s %6s %10.3f %10.3f %10.3f %10.3f"
            % (s["arm"], s["n"], s["wall_min_ms"], s["wall_p50_ms"],
               s["wall_p90_ms"], s["wall_max_ms"])
        )
    lines.append("")
    lines.append("wall ms. Invocation shapes:")
    for s in summaries:
        lines.append("  %-32s %s" % (s["arm"], s["shape"]))
    return "\n".join(lines)


def spawn_arm_process_time(name: str, shape: str, cmd, k: int = 20) -> Dict[str, object]:
    """Process time and spawn count for an arm that launches a child process.

    Reuses `benchmarks/process_time.py :: batched_process_time_ms` rather than re-deriving
    timing: it already handles Windows' ~15.6ms scheduler-tick quantisation (which a single
    sample cannot see past) and Darwin's kevent attach path, and it raises rather than
    silently degrading to a wrong unit.

    THIS IS THE HALF THE WALL TABLE CANNOT SEE. The HTTP arm launches nothing, so it has no
    child process time and no spawns at all -- that is not a zero to be averaged in, it is
    the entire point of the transport, and it is why `procs_per_call` is reported next to
    the timing rather than folded into it.
    """
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    r = batched_process_time_ms(cmd, k=k)
    procs = r["procs_per_call"]
    return {
        "arm": name,
        "shape": shape,
        "k": r["k"],
        "process_time_ms": round(r["process_time_ms"], 3),
        # `procs_per_call` COUNTS the measured command itself. Reporting
        # `procs_per_call - 1` as "spawns" reads as zero for a command that
        # spawns exactly one process, which is the opposite of true -- the
        # `- 1` derivation only makes sense when subtracting a harness's own
        # wrapper, which this bench does not use. Report the count as measured.
        "procs_per_call": round(procs, 3),
        "grandchildren_per_call": round(max(0.0, procs - 1), 3),
        "rc": r["rc"],
    }


def in_process_arm(name: str, shape: str) -> Dict[str, object]:
    """The process-time row for an arm that spawns no child at all.

    Stated explicitly rather than omitted: an arm missing from the process-time table reads
    as unmeasured, while an arm reporting zero spawns reads as measured and free. Those are
    very different claims and this is the one that is true of an HTTP round trip.
    """
    return {
        "arm": name,
        "shape": shape,
        "process_time_ms": 0.0,
        "procs_per_call": 0.0,
        "grandchildren_per_call": 0.0,
        "note": "no child process; cost is in-process client time plus resident server work",
    }


def to_json(summaries: List[Dict[str, object]],
            process_rows: Optional[List[Dict[str, object]]] = None) -> str:
    """Both units in one record, each labelled. Never one number called 'the' cost."""
    return json.dumps(
        {
            "wall": {"unit": "wall_ms", "arms": summaries},
            "process": {"unit": "process_time_ms", "arms": process_rows or []},
            "method": (
                "arms interleaved round-robin; warmup rounds discarded; every arm carries "
                "the invocation shape it was measured in"
            ),
        },
        indent=2,
    )


# --------------------------------------------------------------------------
# THE DRIVER. Everything above this line is the library; nothing above it
# runs the three arms and produces a figure. Without this section the repo
# owns a harness but not a measurement -- the manifest's hand-taken rows are
# not reproducible by anything committed, which is the gap this driver
# closes. Three arms, per the 2026-08-25 reconciliation of AC1 (the
# dispatch brief for this chunk): today's cold chain, HTTP warm hit,
# control -- not the four-arm list AC1 originally named, which predates the
# HTTP transport C2/C3 landed and the H4 wiring-gap fix.
# --------------------------------------------------------------------------


def _bind_test_http_listener(engine_root, *, dispatch=None):
    """Binds `supervisor._make_handler` to a real loopback socket, exactly
    the pattern `warm/tests/test_supervisor_hook_serves_real_guard.py ::
    _bind_handler` uses -- this is the harness that produced the manifest's
    `c4_supervised_listener` rows by hand; reused here verbatim so the
    driver reproduces the same shape rather than a different one.

    `dispatch` defaults to an allow stub (the real guard op, `GUARD_OP_NAME`,
    is not registered yet per `supervisor.py`'s own module comment) -- this
    arm measures the TRANSPORT, not guard evaluation cost, matching the
    manifest's own `not_yet_measured` note.
    """
    from http.server import ThreadingHTTPServer
    import threading

    from coordinator_core.warm import skew, supervisor

    skew.write_engine_stamp(engine_root, "sha-hook-seam-bench")
    version_state = skew.ServerVersionState(engine_root)

    def _allow_dispatch(msg, *, session_id=None):
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"permissionDecision": "allow"},
        }

    ctx = supervisor._ServerContext(
        httpd=None,
        engine_root=engine_root,
        version_state=version_state,
        dispatch=dispatch or _allow_dispatch,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(ctx))
    ctx.httpd = httpd
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def _post_hook(port: int, event: dict, timeout: float = 5.0) -> None:
    import urllib.request

    from coordinator_core.warm import supervisor

    body = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, supervisor.HOOK_PATH),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def measure_p_listener_up(n: int = 10) -> Dict[str, object]:
    """`p(listener up)` against THIS repo's own engine root, with its `n`.

    Per the 2026-08-25 reconciliation: a listener that is usually down makes
    a fast round trip irrelevant, so this is reported alongside the per-fire
    cost, never omitted. On this clone the answer is structurally 0/n --
    `current_engine_clone()` resolves to this unstamped dev tree, and
    `ensure_listener`'s `is_engine_root` gate refuses it by design (never
    autostarts a listener against a dev checkout). That is correct
    behaviour here, not a bug this function works around; the sampled
    number this describes exists only in a stamped publish clone, another
    repo's surface -- see `budget-manifest.json`'s
    `_hook_seam_http_transport.c4_supervised_listener.p_listener_up`.
    """
    from coordinator_core.warm.supervisor import ensure_listener

    hits = 0
    for _ in range(n):
        try:
            url = ensure_listener()
        except Exception:  # noqa: BLE001 -- a failed probe counts as "not up", never a crash
            url = None
        if url:
            hits += 1
    return {
        "n": n,
        "p_listener_up": (hits / n) if n else None,
        "shape": "ensure_listener() against this repo's own (unstamped dev) engine root",
        "note": (
            "structurally 0/n on this clone by design (is_engine_root gate) -- "
            "not a sample of production listener uptime; see docstring"
        ),
    }


def run_hook_fire_breakdown(n: int = 25, k_process: int = 20, warmup: int = 3,
                             tmp_engine_root=None) -> Dict[str, object]:
    """Runs the three reconciled arms -- today's cold chain, HTTP warm hit,
    control -- interleaved round-robin, and returns both process time and
    wall, side by side, with every arm's invocation shape.

    `tmp_engine_root` is a caller-supplied directory to stamp as the test
    listener's engine root (a pytest `tmp_path`, typically); a fresh
    `tempfile.TemporaryDirectory` is used if omitted, e.g. for ad hoc
    `python -m coordinator_core.benchmarks.hook_seam_bench` runs.
    """
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    def _run(root):
        httpd, port = _bind_test_http_listener(Path(root))
        try:
            def _cold_chain_call() -> None:
                subprocess.run(
                    [sys.executable, "-c", "import coordinator_core.ipc"],
                    check=False, capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

            def _http_warm_call() -> None:
                _post_hook(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})

            def _control_call() -> None:
                pass

            arms = [
                Arm(
                    "cold_chain_today",
                    "python3 -c \"import coordinator_core.ipc\" -- the child process every "
                    "PreToolUse hook fire spawns on this box today, no warm transport",
                    _cold_chain_call,
                ),
                Arm(
                    "http_warm_hit",
                    "in-process POST http://127.0.0.1:%d/hook to a resident "
                    "ThreadingHTTPServer bound around supervisor._make_handler "
                    "(real dispatch path, allow-stub op)" % port,
                    _http_warm_call,
                ),
                Arm(
                    "control_floor",
                    "no-op call -- measures this interleave harness's own loop "
                    "overhead, not a real transport",
                    _control_call,
                ),
            ]

            wall_summaries = run_interleaved(arms, n=n, warmup=warmup)

            process_rows = [
                spawn_arm_process_time(
                    "cold_chain_today",
                    arms[0].shape,
                    [sys.executable, "-c", "import coordinator_core.ipc"],
                    k=k_process,
                ),
                in_process_arm("http_warm_hit", arms[1].shape),
                in_process_arm("control_floor", arms[2].shape),
            ]

            return wall_summaries, process_rows
        finally:
            httpd.shutdown()

    if tmp_engine_root is not None:
        wall_summaries, process_rows = _run(tmp_engine_root)
    else:
        with tempfile.TemporaryDirectory(prefix="hook-seam-bench-") as td:
            wall_summaries, process_rows = _run(td)

    return {
        "wall_summaries": wall_summaries,
        "process_rows": process_rows,
        "p_listener_up": measure_p_listener_up(),
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: `python -m coordinator_core.benchmarks.hook_seam_bench`.
    Prints the wall table, the process-time rows, and `p(listener up)` --
    this is what reproduces the four-arm-turned-three-arm breakdown AC1
    asks for, rather than the manifest's hand-taken rows."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=25)
    parser.add_argument("--k-process", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_hook_fire_breakdown(n=args.n, k_process=args.k_process)

    if args.json:
        print(to_json(result["wall_summaries"], result["process_rows"]))
        print(json.dumps({"p_listener_up": result["p_listener_up"]}, indent=2))
        return 0

    print(format_report(result["wall_summaries"]))
    print()
    print("process time (process_time_ms, procs_per_call):")
    for row in result["process_rows"]:
        print("  %-20s %10.3f ms  procs=%s" % (
            row["arm"], row["process_time_ms"], row["procs_per_call"]))
    print()
    p = result["p_listener_up"]
    print("p(listener up) = %s (n=%d) -- %s" % (p["p_listener_up"], p["n"], p["note"]))
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
