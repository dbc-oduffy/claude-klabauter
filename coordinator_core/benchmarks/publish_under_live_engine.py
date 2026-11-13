"""Falsifier harness: a default publish round under a live warm pair.

Run: `python -m coordinator_core.benchmarks.publish_under_live_engine` (default arguments).
Builds a fixture destination engine clone under the repo's scratch/, starts its warm pair,
runs `publish.py <row>` with no flags, and polls a health op and one PreToolUse guard
each second for the whole round. Prints

    minus_32004=<n> fail_open=<n> round_wall_s=<f> round_cpu_s=<f>

followed by the publish [timing] table. `minus_32004` counts replies whose error code is
`warm.client.WARM_DISPATCH_INDETERMINATE`; `fail_open` counts guard probes that returned
no verdict (no reply, an error envelope, or the door's fail-open text).

The parser and counters are pure and take a recorded reply stream (JSON lines of
`{"probe": "health"|"guard", "reply": <JSON-RPC response>|null}`), so tests never start a pair.
The live leg is an EM slot run; it never targets a real destination root.
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
#: Texts of the door's fail-open envelopes: engine failure after delivery, and an unreachable engine.
FAIL_OPEN_MARKERS = ("guard not evaluated", "guard did not run")
POLL_INTERVAL_S = 1.0
DEFAULT_ROW = "harness-row"

_TIMING_PREFIX = "  [timing]"
_CPU_RE = re.compile(r"\[timing\] round driver cpu: ([0-9.]+)s")
_WALL_RE = re.compile(r"\[timing\] round wall time: ([0-9.]+)s")


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


def render_report(records: Iterable[dict], publish_output: str, round_wall_s: Optional[float] = None) -> str:
    """Contract line plus the publish timing table. `round_wall_s` defaults to the driver's own figure."""
    minus, fail = count_probes(records)
    wall = round_wall_s if round_wall_s is not None else parse_round_wall_s(publish_output)
    out = format_summary(minus, fail, wall, parse_round_cpu_s(publish_output))
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

_PUBLISH_SCRIPT = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("publish_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
row = sys.argv[2]
mod.load_targets = lambda setup_dir, target_filter=None, **_: [row]
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


def build_fixture(work: Path, repo: Path) -> tuple[Path, Path]:
    """(source_dir, dest_engine_root) under `work`: dest is a stamped copy of `repo`'s engine."""
    dest = work / "dest"
    src = work / "src"
    for stale in (dest, src):
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copytree(
        repo / "coordinator_core", dest / "coordinator_core",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "benchmarks"),
    )
    (dest / "coordinator_core" / "_engine_stamp").write_text("sha:harness-fixture\n", encoding="utf-8")
    src.mkdir(parents=True)
    (src / "payload.txt").write_text("published\n", encoding="utf-8", newline="\n")
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")
    return src, dest


def _env_for(work: Path, dest: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(work / "rt")
    env["PYTHONPATH"] = str(dest)
    return env


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
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"probe": kind, "reply": None}


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
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and is_fail_open(_probe_once(dest, env, "guard")["reply"]):
        time.sleep(1.0)
    if is_fail_open(_probe_once(dest, env, "guard")["reply"]):
        _stop_pair(dest, env)
        raise RuntimeError("fixture warm pair never answered a guard probe within 60s; no round run")
    stop = threading.Event()
    records: list[dict] = []
    poller = threading.Thread(target=_poll, args=(dest, env, stop, records), daemon=True)
    poller.start()
    row = f"{DEFAULT_ROW}|mirror|{src}|{dest}"
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PUBLISH_SCRIPT, str(_repo_root() / "coordinator" / "bin" / "publish.py"), row],
            cwd=str(_repo_root()), env=dict(os.environ), capture_output=True, text=True, **_no_console(),
        )
    finally:
        wall = time.perf_counter() - started
        stop.set()
        poller.join(timeout=60)
        _stop_pair(dest, env)
    output = proc.stdout + "\n" + proc.stderr
    return render_report(records, output, round_wall_s=wall) + f"\npublish_exit={proc.returncode}"


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
