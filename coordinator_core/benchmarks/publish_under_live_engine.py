"""Falsifier harness: a default publish round under a live warm pair.

Run: `python -m coordinator_core.benchmarks.publish_under_live_engine` (default arguments).
Builds a fixture destination engine clone under the repo's scratch/, starts its warm pair,
runs `publish.py <row>` with no flags, and polls a health op and one PreToolUse guard
each second for the whole round. Prints

    minus_32004=<n> fail_open=<n> round_wall_s=<f> round_cpu_s=<f>

followed by the publish [timing] table. `minus_32004` counts replies whose error code is
`warm.client.WARM_DISPATCH_INDETERMINATE`; `fail_open` counts guard probes that returned
no verdict (no reply, an error envelope, or the door's fail-open text).

After the swap completes the harness sends the successor its first health op, first guard and a
fixture `dispatch.emit`, then prints

    first_health_ms=<f> first_guard_ms=<f> first_emit_ms=<f> post_swap_minus_32004=<n>

(server-side process ms per request, read from the engine's `process_time` telemetry row; never
wall clock; `nan` when the row is absent) and a per-leg table `leg <name>=<ms>` for
`pool_worker_ready`, `first_import` and `token_flip` (`nan` when not observed).

The parser and counters are pure and take a recorded reply stream (JSON lines of
`{"probe": "health"|"guard", "reply": <JSON-RPC response>|null}`; the post-swap leg adds
`{"probe": "first_health"|"first_guard"|"first_emit", "reply": ..., "process_ms": <f>|null,
"source_path": "pool_worker"|"accept_thread"|null}` and one `{"probe": "boot", ...}` record),
so tests never start a pair. The live leg is an EM slot run; it never targets a real destination root.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

#: Mirrors `coordinator_core.warm.client.WARM_DISPATCH_INDETERMINATE` (pinned equal by test).
MINUS_32004 = -32004
HEALTH_OP = "session.whoami_live"
GUARD_OP = "hooks.preuse_bash_dispatch"
GUARD_COMMAND = "git status"
EMIT_OP = "dispatch.emit"
EMIT_FIXTURE_ASK = "publish-under-live-engine fixture ask"
#: Post-swap record kinds, in send order; `boot` carries the successor's server_boot row.
FIRST_PROBES = ("first_health", "first_guard", "first_emit")
LEG_NAMES = ("pool_worker_ready", "first_import", "token_flip")
#: Texts of the door's fail-open envelopes: engine failure after delivery, and an unreachable engine.
FAIL_OPEN_MARKERS = ("guard not evaluated", "guard did not run")
POLL_INTERVAL_S = 1.0
PAIR_UP_WAIT_S = 60.0
DEFAULT_ROW = "harness-row"
NEXT_ENGINE_STAMP = "sha:harness-fixture-next\n"

_TIMING_PREFIX = "  [timing]"
_CPU_RE = re.compile(r"\[timing\] round driver cpu: ([0-9.]+)s")
_WALL_RE = re.compile(r"\[timing\] round wall time: ([0-9.]+)s")
_FLIP_RE = re.compile(r"\[timing\][^\n]*token flip: ([0-9.]+)s")


def reply_error_code(reply: Optional[dict]) -> Optional[int]:
    """The JSON-RPC error code of `reply`, or None when it carries none."""
    if not isinstance(reply, dict):
        return None
    err = reply.get("error")
    if isinstance(err, dict) and isinstance(err.get("code"), int):
        return err["code"]
    return None


def is_minus_32004(reply: Optional[dict]) -> bool:
    """True iff `reply` is the warm client's indeterminate-dispatch envelope."""
    return reply_error_code(reply) == MINUS_32004


def is_fail_open(reply: Optional[dict]) -> bool:
    """True iff a guard probe's `reply` is no guard verdict: absent, an error envelope, or fail-open text."""
    if not isinstance(reply, dict) or reply.get("error"):
        return True
    return any(marker in json.dumps(reply) for marker in FAIL_OPEN_MARKERS)


def parse_reply_stream(lines: Iterable[str]) -> list[dict]:
    """Probe records from JSON lines; blank and malformed lines are skipped."""
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("probe") in ("health", "guard"):
            records.append(rec)
    return records


def count_probes(records: Iterable[dict]) -> tuple[int, int]:
    """(minus_32004, fail_open) over `records`; fail_open counts guard probes only."""
    minus = 0
    fail = 0
    for rec in records:
        reply = rec.get("reply")
        if is_minus_32004(reply):
            minus += 1
        if rec.get("probe") == "guard" and is_fail_open(reply):
            fail += 1
    return minus, fail


def extract_timing_table(publish_output: str) -> str:
    """The publish `[timing]` lines of `publish_output`, in order."""
    return "\n".join(
        line.rstrip() for line in publish_output.splitlines() if line.startswith(_TIMING_PREFIX)
    )


def parse_round_cpu_s(publish_output: str) -> float:
    """The publish driver's own `round driver cpu` seconds; nan when the line is absent."""
    found = _CPU_RE.findall(publish_output)
    return float(found[-1]) if found else math.nan


def parse_round_wall_s(publish_output: str) -> float:
    """The publish driver's own `round wall time` seconds; nan when the line is absent."""
    found = _WALL_RE.findall(publish_output)
    return float(found[-1]) if found else math.nan


def format_summary(minus: int, fail_open: int, round_wall_s: float, round_cpu_s: float) -> str:
    """The pinned one-line output contract."""
    return (
        f"minus_32004={minus} fail_open={fail_open} "
        f"round_wall_s={round_wall_s:.3f} round_cpu_s={round_cpu_s:.3f}"
    )


def parse_token_flip_ms(publish_output: str) -> float:
    """The publish `[timing] ... token flip: <s>s` line in ms; nan when the line is absent."""
    found = _FLIP_RE.findall(publish_output)
    return float(found[-1]) * 1000.0 if found else math.nan


def parse_post_swap_stream(lines: Iterable[str]) -> list[dict]:
    """Post-swap records (`first_*` and `boot`) from JSON lines; anything else is skipped."""
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and (rec.get("probe") in FIRST_PROBES or rec.get("probe") == "boot"):
            records.append(rec)
    return records


def _process_ms(rec: Optional[dict]) -> float:
    return _ms_or_nan(rec.get("process_ms") if isinstance(rec, dict) else None)


def _last(records: Iterable[dict], probe: str) -> Optional[dict]:
    found = [r for r in records if r.get("probe") == probe]
    return found[-1] if found else None


def _ms_or_nan(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else math.nan


def post_swap_legs(records: Iterable[dict], publish_output: str = "") -> dict[str, float]:
    """Per-leg ms, `LEG_NAMES` keys, nan when unobserved.

    `pool_worker_ready` is the first pool-worker-served request's process ms (the worker's cold
    cost lands on its first op); `first_import` is the successor's spawn-to-listener boot (imports);
    `token_flip` is the publish driver's own timing line.
    """
    records = list(records)
    pool = math.nan
    for name in FIRST_PROBES:
        rec = _last(records, name)
        if rec and rec.get("source_path") == "pool_worker":
            pool = _process_ms(rec)
            break
    boot = _last(records, "boot")
    listener = boot.get("listener_secs") if boot else None
    first_import = _ms_or_nan(listener) * 1000.0 if listener is not None else math.nan
    return {
        "pool_worker_ready": pool,
        "first_import": first_import,
        "token_flip": parse_token_flip_ms(publish_output),
    }


def post_swap_summary(records: Iterable[dict]) -> dict[str, float]:
    """`first_health_ms`, `first_guard_ms`, `first_emit_ms` (server process ms, nan when absent) and
    `post_swap_minus_32004` over the `first_*` records."""
    records = list(records)
    out: dict[str, float] = {
        f"{name}_ms": _process_ms(_last(records, name)) for name in FIRST_PROBES
    }
    out["post_swap_minus_32004"] = sum(
        1 for r in records if r.get("probe") in FIRST_PROBES and is_minus_32004(r.get("reply"))
    )
    return out


def format_post_swap(records: Iterable[dict], publish_output: str = "") -> str:
    """The pinned post-swap contract line followed by one `leg <name>=<ms>` line per leg."""
    records = list(records)
    s = post_swap_summary(records)
    line = (
        f"first_health_ms={s['first_health_ms']:.1f} first_guard_ms={s['first_guard_ms']:.1f} "
        f"first_emit_ms={s['first_emit_ms']:.1f} post_swap_minus_32004={int(s['post_swap_minus_32004'])}"
    )
    legs = post_swap_legs(records, publish_output)
    return "\n".join([line] + [f"leg {name}={legs[name]:.1f}" for name in LEG_NAMES])


def render_report(
    records: Iterable[dict],
    publish_output: str,
    round_wall_s: Optional[float] = None,
    post_swap: Optional[Iterable[dict]] = None,
) -> str:
    """Contract line, the post-swap block when `post_swap` is given, then the publish timing table.
    `round_wall_s` defaults to the driver's own figure."""
    minus, fail = count_probes(records)
    wall = round_wall_s if round_wall_s is not None else parse_round_wall_s(publish_output)
    out = format_summary(minus, fail, wall, parse_round_cpu_s(publish_output))
    if post_swap is not None:
        out += "\n" + format_post_swap(post_swap, publish_output)
    table = extract_timing_table(publish_output)
    return out + ("\n" + table if table else "")


# ---------------------------------------------------------------------------
# Live leg (EM slot run): fixture, pair, publish child, poller.
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = r"""
import json, sys
from coordinator_core.warm import client
msg = json.loads(sys.argv[1])
try:
    reply = client.try_warm_dispatch(msg, read_deadline_secs=8.0, hook_path=True)
except Exception as exc:
    reply = {"error": {"code": 0, "message": repr(exc)}}
print(json.dumps({"probe": sys.argv[2], "reply": reply}))
"""

#: One request, then the server-side `process_time` row for it (op match, started after t0), read from
#: the destination engine's own op-latency sink; the row lands just after the reply, so it is awaited.
_FIRST_SCRIPT = r"""
import json, sys, time
from pathlib import Path
from coordinator_core.warm import client
msg = json.loads(sys.argv[1])
t0 = time.time()
try:
    reply = client.try_warm_dispatch(msg, read_deadline_secs=30.0, hook_path=True)
except Exception as exc:
    reply = {"error": {"code": 0, "message": repr(exc)}}
process_ms = source_path = None
try:
    from coordinator_core.lifecycle import git_common_dir
    from coordinator_core.telemetry import op_latency
    sink = op_latency._sink_path(git_common_dir(Path.cwd()))
    for _ in range(20):
        rows, _t = op_latency.tail_entries(sink, tail_bytes=1 << 20, max_rows=400)
        hit = [r for r in rows if r.get("kind") == "process_time" and r.get("op") == msg["method"]
               and isinstance(r.get("t_start"), (int, float)) and r["t_start"] >= t0 - 1.0]
        if hit:
            process_ms, source_path = hit[-1].get("process_ms"), hit[-1].get("source_path")
            break
        time.sleep(0.1)
except Exception:
    pass
print(json.dumps({"probe": sys.argv[2], "reply": reply, "process_ms": process_ms, "source_path": source_path}))
"""

_BOOT_SCRIPT = r"""
import json, sys
from pathlib import Path
from coordinator_core.warm import telemetry
rows = telemetry.server_boot_samples(Path(sys.argv[1]))
row = rows[-1] if rows else {}
print(json.dumps({"probe": "boot", "listener_secs": row.get("listener_secs"), "ready_secs": row.get("ready_secs")}))
"""

_PUBLISH_SCRIPT = r"""
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("publish_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
row = sys.argv[2]
fixture_store = Path(sys.argv[3])
mod.load_targets = lambda setup_dir, target_filter=None, **_: [row]
mod.locate_percolate_store = lambda setup_dir: fixture_store
raise SystemExit(mod.main([row.split("|")[0]]))
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _no_console() -> dict:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=h", "-c", "user.email=h@h",
         "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, **_no_console(),
    )


def write_fixture_store(work: Path) -> Path:
    """A percolate-store.yaml under `work` declaring only the fixture row (empty section).

    The round reads this instead of the real `setup/percolate-hooks/percolate-store.yaml`, which
    has no row for a fixture target and is never read or modified by the harness.
    """
    store = work / "percolate-store.yaml"
    store.write_text(
        f'schema_version: "1.0.0"\nbase: {{}}\ntargets:\n  {DEFAULT_ROW}: {{}}\n',
        encoding="utf-8", newline="\n",
    )
    return store


def build_fixture(work: Path, repo: Path) -> tuple[Path, Path]:
    """(source_dir, dest_engine_root) under `work`: dest is a stamped copy of `repo`'s engine."""
    dest = work / "dest"
    src_repo = work / "src"
    src = src_repo / "payload"  # publish treats a source root equal to its repo toplevel as absent
    origin = work / "origin.git"
    for stale in (dest, src_repo, origin):
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copytree(
        repo / "coordinator_core", dest / "coordinator_core",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "benchmarks"),
    )
    (dest / "coordinator_core" / "_engine_stamp").write_text("sha:harness-fixture\n", encoding="utf-8")
    # A mirror row deletes any destination top-level directory the source lacks, so the source
    # carries the engine tree too, plus one engine-code delta so the round really changes it.
    shutil.copytree(dest / "coordinator_core", src / "coordinator_core", ignore=shutil.ignore_patterns("__pycache__", "_engine_stamp"))
    with (src / "coordinator_core" / "__init__.py").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n# harness delta\n")
    # The handover spawns a successor only when the client token changes, and the token is the stamp's
    # bytes; a fixture row is not the engine row, so no round writes a stamp and the source ships one.
    (src / "coordinator_core" / "_engine_stamp").write_text(NEXT_ENGINE_STAMP, encoding="utf-8", newline="\n")
    (src / "payload.txt").write_text("published\n", encoding="utf-8", newline="\n")
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")
    # publish reads the source from its own committed tree, so the source is a repo of its own.
    _git(src_repo, "init", "-q", "-b", "main")
    _git(src_repo, "add", "-A")
    _git(src_repo, "commit", "-q", "-m", "fixture source")
    # publish refuses a destination whose branch has no upstream tip to measure against.
    _git(work, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(dest, "remote", "add", "origin", str(origin))
    _git(dest, "push", "-q", "-u", "origin", "main")
    return src, dest


def _env_for(work: Path, dest: Path) -> dict:
    """The env every fixture client and its client-spawned server run under.

    `COORDINATOR_CLONE_ROOT` pins the client's configured engine root to the fixture: without it a
    settings-home `.claude-klabauter-root` (or door sidecar) naming the real engine makes the client
    judge the fixture foreign, go cold and never spawn the pair.
    """
    env = dict(os.environ)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(work / "rt")
    env["PYTHONPATH"] = str(dest)
    env["COORDINATOR_CLONE_ROOT"] = str(dest)
    return env


def _publish_env(work: Path) -> dict:
    """Env for the publish child: the fixture's runtime base, so the swap's breadcrumb read, successor
    spawn and answer wait address the fixture pair; PYTHONPATH stays unset (publish runs the repo's own engine)."""
    env = dict(os.environ)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(work / "rt")
    return env


_DIAG_SCRIPT = r"""
import json
from coordinator_core.warm import client
print(json.dumps({
    "configured_root": client._configured_root(),
    "clone_root": str(client._engine_clone_root()),
    "foreign": client._configured_root_is_foreign(),
    "live_tree_cold": client._live_tree_cold,
}))
"""

#: The client's detached spawn sends the child's stdio to DEVNULL, so its stderr is unrecoverable;
#: these are the places its cause does land.
_STDERR_TAIL_CHARS = 1500


def spawn_diagnosis(dest: Path, env: dict) -> str:
    """Why the fixture client would not spawn a pair, from a client process under `env`."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _DIAG_SCRIPT],
            cwd=str(dest), env=env, capture_output=True, text=True, timeout=30, **_no_console(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"diagnosis probe failed: {exc!r}"
    out = proc.stdout.strip() or "(no stdout)"
    err = proc.stderr.strip()
    return f"client view: {out}" + (f"\ndiagnosis stderr: {err[-_STDERR_TAIL_CHARS:]}" if err else "")


def spawn_failure_report(work: Path, dest: Path, env: dict, last_probe: dict) -> str:
    """Cause lines for the 'pair never answered' error: last probe stderr, the client's own view of
    its configured root, the client cold-log path, and the spawn-failure log path (with its tail)."""
    lines = []
    stderr = (last_probe.get("stderr") or "").strip()
    lines.append(f"last probe stderr: {stderr[-_STDERR_TAIL_CHARS:] if stderr else '(empty)'}")
    lines.append(spawn_diagnosis(dest, env))
    lines.append(f"client cold log: {work / 'rt' / 'coordinator' / 'warm'}/*/client-cold.jsonl")
    spawn_log = dest / "state" / "housekeeping-failures.log"
    try:
        tail = spawn_log.read_text(encoding="utf-8", errors="replace")[-_STDERR_TAIL_CHARS:].strip()
    except OSError:
        tail = ""
    lines.append(f"spawn failure log: {spawn_log} " + (f"tail: {tail}" if tail else "(absent or empty: Popen did not fail)"))
    return "\n".join(lines)


def _probe_once(dest: Path, env: dict, kind: str) -> dict:
    if kind == "health":
        msg = {"jsonrpc": "2.0", "id": 1, "method": HEALTH_OP, "params": {}}
    else:
        msg = {"jsonrpc": "2.0", "id": 1, "method": GUARD_OP, "params": {
            "hook_event_name": "PreToolUse", "session_id": "publish-under-live-engine",
            "cwd": str(dest), "tool_name": "Bash", "tool_input": {"command": GUARD_COMMAND}}}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT, json.dumps(msg), kind],
            cwd=str(dest), env=env, capture_output=True, text=True, timeout=30, **_no_console(),
        )
        recs = parse_reply_stream(proc.stdout.splitlines())
        if recs:
            return recs[-1]
        return {"probe": kind, "reply": None, "stderr": proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"probe": kind, "reply": None, "stderr": repr(exc)}


def first_request_message(kind: str, dest: Path) -> dict:
    """The JSON-RPC message for post-swap probe `kind` (a `FIRST_PROBES` name)."""
    if kind == "first_health":
        return {"jsonrpc": "2.0", "id": 1, "method": HEALTH_OP, "params": {}}
    if kind == "first_guard":
        return {"jsonrpc": "2.0", "id": 1, "method": GUARD_OP, "params": {
            "hook_event_name": "PreToolUse", "session_id": "publish-under-live-engine",
            "cwd": str(dest), "tool_name": "Bash", "tool_input": {"command": GUARD_COMMAND}}}
    return {"jsonrpc": "2.0", "id": 1, "method": EMIT_OP, "params": {
        "ask": EMIT_FIXTURE_ASK, "repo_root": str(dest), "session_id": "publish-under-live-engine"}}


def _run_script(dest: Path, env: dict, script: str, *argv: str) -> list[dict]:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, *argv],
            cwd=str(dest), env=env, capture_output=True, text=True, timeout=90, **_no_console(),
        )
        return parse_post_swap_stream(proc.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return []


def post_swap_records(dest: Path, env: dict) -> list[dict]:
    """The successor's first health, guard and emit (in that order) plus its boot row."""
    records: list[dict] = []
    for kind in FIRST_PROBES:
        recs = _run_script(dest, env, _FIRST_SCRIPT, json.dumps(first_request_message(kind, dest)), kind)
        records.append(recs[-1] if recs else {"probe": kind, "reply": None, "process_ms": None})
    records.extend(_run_script(dest, env, _BOOT_SCRIPT, str(dest)))
    return records


_STOP_SCRIPT = (
    "import sys\nfrom pathlib import Path\n"
    "from coordinator_core.warm.pair_stop import stop_warm_pair\n"
    "stop_warm_pair(Path(sys.argv[1]))\n"
)


def _stop_pair(dest: Path, env: dict) -> None:
    """Stop the fixture's warm pair; best effort, a pair that will not stop is the OS's to reap."""
    try:
        subprocess.run(
            [sys.executable, "-c", _STOP_SCRIPT, str(dest)],
            cwd=str(dest), env=env, capture_output=True, timeout=120, **_no_console(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _poll(dest: Path, env: dict, stop: threading.Event, sink: list) -> None:
    while not stop.is_set():
        for kind in ("health", "guard"):
            sink.append(_probe_once(dest, env, kind))
        stop.wait(POLL_INTERVAL_S)


def run_round(work: Path, repo: Path) -> str:
    """One default publish round under a live pair; the rendered report."""
    src, dest = build_fixture(work, repo)
    env = _env_for(work, dest)
    # First dial spawns the fixture's pair (the client's own spawn path); wait for an answer.
    deadline = time.monotonic() + PAIR_UP_WAIT_S
    while time.monotonic() < deadline and is_fail_open(_probe_once(dest, env, "guard")["reply"]):
        time.sleep(1.0)
    last = _probe_once(dest, env, "guard")
    if is_fail_open(last["reply"]):
        _stop_pair(dest, env)
        raise RuntimeError(
            f"fixture warm pair never answered a guard probe within {PAIR_UP_WAIT_S:.0f}s; no round run\n"
            + spawn_failure_report(work, dest, env, last)
        )
    stop = threading.Event()
    records: list[dict] = []
    poller = threading.Thread(target=_poll, args=(dest, env, stop, records), daemon=True)
    poller.start()
    row = f"{DEFAULT_ROW}|mirror|{src}|{dest}"
    fixture_store = write_fixture_store(work)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PUBLISH_SCRIPT, str(_repo_root() / "coordinator" / "bin" / "publish.py"),
             row, str(fixture_store)],
            cwd=str(_repo_root()), env=_publish_env(work), capture_output=True, text=True, **_no_console(),
        )
    finally:
        wall = time.perf_counter() - started
        stop.set()
        poller.join(timeout=60)
    try:
        post_swap = post_swap_records(dest, env)
    finally:
        _stop_pair(dest, env)
    output = proc.stdout + "\n" + proc.stderr
    report = render_report(records, output, round_wall_s=wall, post_swap=post_swap) + f"\npublish_exit={proc.returncode}"
    if proc.returncode != 0:
        report += "\npublish_output_tail:\n" + output.strip()[-1500:]
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", help="fixture directory (default: scratch/publish-under-live-engine)")
    args = ap.parse_args(argv)
    repo = _repo_root()
    work = Path(args.work) if args.work else repo / "scratch" / "publish-under-live-engine"
    work.mkdir(parents=True, exist_ok=True)
    print(run_round(work, repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
