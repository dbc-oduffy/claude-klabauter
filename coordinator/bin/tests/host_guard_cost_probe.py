"""Cost probe for the codex/agy host guard against an unreachable engine.

Prints one JSON line per measurement: `shim_import` (process seconds for a first import of
coordinator_core.host_hooks.shim in a fresh interpreter), then `codex` and `agy` (one guard() call each) and
`agy_hook` (one agy decide() call). Per host: spawned_children (subprocess.Popen calls during the call),
guard_process_secs (time.process_time delta; no wall clock), children_cpu_secs (POSIX child CPU, 0 on
Windows), answer, state, degrade_rows (rows appended to host-guard-degrades.jsonl plus the shim's
warm/degrade.jsonl sink). The settings home and warm runtime base are fresh scratch dirs, so no warm
socket exists; both are removed on exit. Not a test_ file; never collected.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BIN = REPO / "coordinator" / "bin"

CODEX_PAYLOAD = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                 "session_id": "probe-session", "cwd": str(REPO)}
AGY_PAYLOAD = {"conversationId": "probe-conv", "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(REPO)}}}

SHIM_IMPORT_CODE = (
    "import sys, time, json\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "t0 = time.process_time()\n"
    "import coordinator_core.host_hooks.shim\n"
    "dt = time.process_time() - t0\n"
    "from coordinator_core.warm import telemetry\n"
    "print(json.dumps({'secs': dt, 'degrade_path': str(telemetry.degrade_path())}))\n"
)


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def count_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


class PopenCounter:
    """Wraps subprocess.Popen with a counting subclass that still delegates."""

    def __init__(self):
        self.count = 0
        self._orig = subprocess.Popen

    def __enter__(self):
        counter, orig = self, self._orig

        class Counting(orig):
            def __init__(self, *a, **kw):
                counter.count += 1
                super().__init__(*a, **kw)

        subprocess.Popen = Counting
        return self

    def __exit__(self, *exc):
        subprocess.Popen = self._orig


def measure(host, call, log_paths):
    before = sum(count_lines(p) for p in log_paths)
    t_before = os.times()
    p0 = time.process_time()
    with PopenCounter() as counter:
        result = call()
    secs = time.process_time() - p0
    t_after = os.times()
    answer, state = result if isinstance(result, tuple) else (result, None)
    return {
        "host": host,
        "spawned_children": counter.count,
        "guard_process_secs": round(secs, 4),
        "children_cpu_secs": round((t_after[2] + t_after[3]) - (t_before[2] + t_before[3]), 4),
        "answer": answer,
        "state": state,
        "degrade_rows": sum(count_lines(p) for p in log_paths) - before,
    }


def main():
    scratch_parent = REPO / "scratch"
    scratch_parent.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="host-guard-cost-probe-", dir=scratch_parent))
    try:
        home, warm_base = work / "settings-home", work / "warm-base"
        home.mkdir()
        warm_base.mkdir()
        os.environ["COORDINATOR_SETTINGS_HOME"] = str(home)
        os.environ["COORDINATOR_WARM_RUNTIME_BASE"] = str(warm_base)

        out = subprocess.run([sys.executable, "-c", SHIM_IMPORT_CODE, str(REPO)], capture_output=True, text=True,
                             timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(out.stdout.strip().splitlines()[-1])
        print(json.dumps({"host": "shim_import", "shim_import_process_secs": round(info["secs"], 4)}))

        guard_mod = load_sibling("unimatrix-host-guard")
        agy_mod = load_sibling("unimatrix-agy-hook-pretool")
        logs = [guard_mod.degrade_log_path(), Path(info["degrade_path"])]
        root = str(REPO)

        rows = [
            measure("codex", lambda: guard_mod.guard("codex", json.dumps(CODEX_PAYLOAD), root), logs),
            measure("agy", lambda: guard_mod.guard("agy", json.dumps(AGY_PAYLOAD), root), logs),
            measure("agy_hook", lambda: agy_mod.decide(AGY_PAYLOAD, None, root, json.dumps(AGY_PAYLOAD)), logs),
        ]
        for row in rows:
            print(json.dumps(row))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
