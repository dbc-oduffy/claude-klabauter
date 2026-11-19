"""Cost probe for the grok/codex/agy host guard against an unreachable engine.

Prints one JSON line per measurement: `shim_import` (process seconds for a first import of
coordinator_core.host_hooks.shim in a fresh interpreter), then `grok`, `codex` and `agy` (one guard() call
each, every host in its own FRESH interpreter so interpreter start and import are inside the figure) and
`agy_hook` (one agy decide() call, in-process), then `importtime_top10` per host.

Per host row: whole_process_secs (process_time at exit, interpreter start included), in_join_secs (process_time
delta around guard(): shim import + run, the span `worker.join(budget_secs)` gates; interpreter start precedes
the join and is not gated by it), spawned_children (subprocess.Popen calls), children_cpu_secs (POSIX child CPU,
0 on Windows), answer, state, nag_in_answer (the DIY nag reached `hookSpecificOutput.additionalContext` or a
top-level `additionalContext`), degrade_rows (rows appended to host-guard-degrades.jsonl: a guard degrade),
cold_run_rows (rows in the shim's warm/degrade.jsonl sink: the engine gave no verdict, telemetry not a degrade).
in_join_wall_secs is the perf_counter delta around the same span (the budget's clock; reported, never gated:
wall time measures peer load). The `grok_fresh` row is a live-shaped first call: fresh uuid session id, empty
settings home and warm base, scratch repo apart from the engine root; `phases` is cProfile cumulative seconds of
the top in-join functions (second fresh run). The `grok_live` row runs the exact hook command line
(`unimatrix-host-guard.py --host grok --engine-root <HOST_GUARD_PROBE_ENGINE_ROOT or this repo>`, payload on stdin) as
a fresh process on a search_replace payload and reports whole-process `wall_secs`, answer, state and `phases`. `importtime_top10` comes from a second fresh
interpreter under `-X importtime` (its inflated timing is not a cost figure): the ten largest cumulative-us
modules. The grok run targets a scratch repo whose lane record holds an over-bound edit (provider env set), so the
nag path is measured; the codex and agy rows have no nag. The settings home and warm runtime base are fresh scratch dirs, so no warm socket exists; both are
removed on exit. Not a test_ file; never collected.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BIN = REPO / "coordinator" / "bin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CODEX_PAYLOAD = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"},
                 "session_id": "probe-session", "cwd": str(REPO)}
_GROK_EDIT = {"file_path": "delta.py", "old_string": "", "new_string": "x = 1\n" * 20}
GROK_PAYLOAD = {"hook_event_name": "PreToolUse", "hookEventName": "pre_tool_use", "tool_name": "search_replace",
                "toolName": "search_replace", "tool_input": _GROK_EDIT, "toolInput": _GROK_EDIT,
                "session_id": "probe-session", "sessionId": "probe-session", "cwd": str(REPO),
                "workspaceRoot": str(REPO), "permissionMode": "auto"}
AGY_PAYLOAD = {"conversationId": "probe-conv", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
PAYLOADS = {"grok": GROK_PAYLOAD, "codex": CODEX_PAYLOAD, "agy": AGY_PAYLOAD}

SHIM_IMPORT_CODE = (
    "import sys, time, json\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "t0 = time.process_time()\n"
    "import coordinator_core.host_hooks.shim\n"
    "dt = time.process_time() - t0\n"
    "print(json.dumps({'secs': dt}))\n"
)

# argv: repo, host, payload json. Prints one JSON line.
GUARD_CHILD_CODE = (
    "import importlib.util, json, os, subprocess, sys, time\n"
    "repo, host, payload = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "sys.path.insert(0, repo)\n"
    "spec = importlib.util.spec_from_file_location('unimatrix_host_guard', os.path.join(repo, 'coordinator', 'bin', 'unimatrix-host-guard.py'))\n"
    "guard_mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(guard_mod)\n"
    "count = [0]\n"
    "orig = subprocess.Popen\n"
    "class Counting(orig):\n"
    "    def __init__(self, *a, **kw):\n"
    "        count[0] += 1\n"
    "        super().__init__(*a, **kw)\n"
    "subprocess.Popen = Counting\n"
    "def lines(p):\n"
    "    try:\n"
    "        with open(p, 'r', encoding='utf-8') as fh:\n"
    "            return sum(1 for line in fh if line.strip())\n"
    "    except OSError:\n"
    "        return 0\n"
    "def cold_runs():\n"
    "    from coordinator_core.warm import telemetry\n"
    "    return lines(telemetry.degrade_path())\n"
    "prof = []\n"
    "if len(sys.argv) > 4 and sys.argv[4] == 'profile':\n"
    "    import cProfile\n"
    "    real_run = guard_mod._run_shim\n"
    "    def profiled(*a):\n"
    "        pr = cProfile.Profile()\n"
    "        prof.append(pr)\n"
    "        pr.runcall(real_run, *a)\n"
    "    guard_mod._run_shim = profiled\n"
    "t_before = os.times()\n"
    "p0, w0 = time.process_time(), time.perf_counter()\n"
    "answer, state = guard_mod.guard(host, payload, repo)\n"
    "in_join, in_join_wall = time.process_time() - p0, time.perf_counter() - w0\n"
    "t_after = os.times()\n"
    "whole = time.process_time()\n"
    "subprocess.Popen = orig\n"
    "phases = None\n"
    "if prof:\n"
    "    import pstats\n"
    "    st = pstats.Stats(prof[0]).stats\n"
    "    want = ('_measure', 'nag_text', 'lane_path', 'advisory_for_dispatch', 'decide', '_execute_child', 'hub_dir', 'resolve_hub')\n"
    "    rows = [(v[3], k) for k, v in st.items() if k[2] in want or (repo in k[0] and not k[0].startswith('<'))]\n"
    "    rows.sort(reverse=True)\n"
    "    phases = {os.path.basename(k[0]) + ':' + k[2]: round(ct, 4) for ct, k in rows[:14]}\n"
    "try:\n"
    "    after, cold = lines(guard_mod.degrade_log_path()), cold_runs()\n"
    "except Exception:\n"
    "    after, cold = 0, 0\n"
    "print(json.dumps({'whole_process_secs': round(whole, 4), 'in_join_secs': round(in_join, 4),\n"
    "    'in_join_wall_secs': round(in_join_wall, 4), 'phases': phases,\n"
    "    'spawned_children': count[0],\n"
    "    'children_cpu_secs': round((t_after[2] + t_after[3]) - (t_before[2] + t_before[3]), 4),\n"
    "    'answer': answer, 'state': state, 'degrade_rows': after, 'cold_run_rows': cold}))\n"
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


def carries_nag(answer):
    """True when the answer holds a non-empty additionalContext, nested under hookSpecificOutput or top level."""
    if not isinstance(answer, dict):
        return False
    nested = answer.get("hookSpecificOutput")
    if isinstance(nested, dict) and nested.get("additionalContext"):
        return True
    return bool(answer.get("additionalContext"))


def grok_fresh_env(work):
    """Env for a live-shaped fresh Grok call: provider grok, settings home and warm base empty dirs under `work`."""
    home, warm = work / "fresh-home", work / "fresh-warm"
    home.mkdir(exist_ok=True)
    warm.mkdir(exist_ok=True)
    return {**os.environ, "COORDINATOR_AGENT_PROVIDER": "grok", "COORDINATOR_SETTINGS_HOME": str(home),
            "COORDINATOR_WARM_RUNTIME_BASE": str(warm)}


def grok_fresh_argv(work, tag, profile=False):
    """argv for GUARD_CHILD_CODE: engine root REPO, a fresh uuid session id, and a scratch repo (separate from the
    engine) whose .coordinator-local holds only the lane record prepare_nag_repo writes."""
    sid = str(uuid.uuid4())
    repo = work / f"fresh-repo-{tag}"
    prepare_nag_repo(repo, sid)
    tool_input = {**GROK_PAYLOAD["tool_input"], "file_path": str(repo / "delta.py")}
    payload = {**GROK_PAYLOAD, "cwd": str(repo), "workspaceRoot": str(repo), "session_id": sid, "sessionId": sid,
               "tool_input": tool_input, "toolInput": tool_input}
    argv = [str(REPO), "grok", json.dumps(payload)]
    return argv + ["profile"] if profile else argv


def grok_fresh_row(work):
    """The grok_fresh row: a fresh interpreter, fresh session id, empty settings home and warm base; wall and process
    time around guard(), plus a cProfile `phases` map from a second fresh run (profiling inflates its own timing)."""
    out = run_child(grok_fresh_argv(work, "cost"), env=grok_fresh_env(work))
    try:
        row = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        row = {"error": "child_failed", "returncode": out.returncode, "stderr_tail": out.stderr[-400:]}
    row["host"] = "grok_fresh"
    row["nag_in_answer"] = carries_nag(row.get("answer"))
    prof = run_child(grok_fresh_argv(work, "profile", profile=True), env=grok_fresh_env(work))
    try:
        row["phases"] = json.loads(prof.stdout.strip().splitlines()[-1]).get("phases")
    except (IndexError, ValueError):
        row["phases"] = {"error": prof.stderr[-300:]}
    return row


# argv: hook script, engine root, then the hook CLI argv. Loads the real hook script, profiles the worker-thread
# _run_shim (cProfile sees only its own thread), then runs main() on the stdin payload; prints the phases map.
LIVE_PROFILE_CODE = (
    "import cProfile, importlib.util, io, json, os, pstats, sys\n"
    "script, engine = sys.argv[1], sys.argv[2]\n"
    "sys.path.insert(0, engine)\n"
    "spec = importlib.util.spec_from_file_location('unimatrix_host_guard', script)\n"
    "gm = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(gm)\n"
    "prof = []\n"
    "real = gm._run_shim\n"
    "def profiled(*a):\n"
    "    pr = cProfile.Profile()\n"
    "    prof.append(pr)\n"
    "    pr.runcall(real, *a)\n"
    "gm._run_shim = profiled\n"
    "out = sys.stdout\n"
    "sys.stdout = io.StringIO()\n"
    "gm.main(sys.argv[3:])\n"
    "sys.stdout = out\n"
    "st = pstats.Stats(prof[0]).stats if prof else {}\n"
    "rows = sorted(((v[3], k) for k, v in st.items() if not k[0].startswith('<') and 'coordinator' in k[0].replace(chr(92), '/')), reverse=True)\n"
    "print(json.dumps({os.path.basename(k[0]) + ':' + k[2]: round(ct, 4) for ct, k in rows[:14]}))\n"
)


def live_engine_root():
    """Engine root for the live-command row: HOST_GUARD_PROBE_ENGINE_ROOT (e.g. the klabauter checkout), else REPO."""
    return os.environ.get("HOST_GUARD_PROBE_ENGINE_ROOT") or str(REPO)


def grok_live_cli(engine_root):
    """The Grok hook command line, argv form: `python unimatrix-host-guard.py --host grok --engine-root <engine>`."""
    return [str(BIN / "unimatrix-host-guard.py"), "--host", "grok", "--engine-root", str(engine_root)]


def grok_live_payload(work, tag, lane=True):
    """(stdin payload JSON, repo) for one live-shaped search_replace PreToolUse call on a fresh uuid session. With
    `lane`, the scratch repo's `.coordinator-local` holds only the lane record (the nag answers before the guard
    chain); without, the cwd is a bare directory with no lane record, so the write-guard chain runs (the shape that
    timed out live)."""
    sid = str(uuid.uuid4())
    repo = work / f"live-repo-{tag}-{'lane' if lane else 'nolane'}"
    if lane:
        prepare_nag_repo(repo, sid)
    else:
        repo.mkdir()
    tool_input = {"file_path": str(repo / "x.txt"), "old_string": "", "new_string": "x = 1\n" * 20}
    payload = {**GROK_PAYLOAD, "cwd": str(repo), "workspaceRoot": str(repo), "session_id": sid, "sessionId": sid,
               "tool_input": tool_input, "toolInput": tool_input}
    return json.dumps(payload), repo


def grok_live_row(work, lane=True):
    """The grok_live row: the exact hook command run as a fresh process on a search_replace payload, wall clock around
    the whole process (`wall_secs`: interpreter start, import, guard, write), the answer, the degrade rows it
    appended, and a `phases` map (cProfile cumulative seconds of the in-join functions, second fresh run)."""
    engine = live_engine_root()
    env = grok_fresh_env(work)
    stdin, _ = grok_live_payload(work, "cost", lane)
    t0 = time.perf_counter()
    out = subprocess.run([sys.executable, *grok_live_cli(engine)], input=stdin, capture_output=True, text=True,
                         timeout=120, creationflags=NO_WINDOW, env=env)
    wall = time.perf_counter() - t0
    try:
        answer = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        answer = None
    state = (out.stderr.strip().splitlines() or [""])[-1]
    log = Path(env["COORDINATOR_SETTINGS_HOME"]) / "host-hooks" / "host-guard-degrades.jsonl"
    row = {"host": "grok_live" if lane else "grok_live_nolane", "engine_root": engine, "wall_secs": round(wall, 4), "answer": answer,
           "state": state, "nag_in_answer": carries_nag(answer), "degrade_rows": count_lines(log)}
    pstdin, _ = grok_live_payload(work, "profile", lane)
    prof = subprocess.run([sys.executable, "-c", LIVE_PROFILE_CODE, *grok_live_cli(engine)[:1], engine,
                           *grok_live_cli(engine)[1:]], input=pstdin, capture_output=True, text=True, timeout=120,
                          creationflags=NO_WINDOW, env=grok_fresh_env(work))
    try:
        row["phases"] = json.loads(prof.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        row["phases"] = {"error": prof.stderr[-300:]}
    return row


def run_child(argv, extra_flags=(), env=None):
    return subprocess.run([sys.executable, *extra_flags, "-c", GUARD_CHILD_CODE, *argv], capture_output=True,
                          text=True, timeout=120, creationflags=NO_WINDOW, env=env)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=probe", "-c", "user.email=probe@probe", *args],
                   check=True, capture_output=True, creationflags=NO_WINDOW)


def prepare_nag_repo(root, session_id):
    """A scratch repo whose lane record attributes an over-bound uncommitted edit to `session_id`, so the DIY nag fires."""
    root.mkdir()
    _git(root, "init", "-q")
    names = [f"f{i}.txt" for i in range(3)]
    for name in names:
        (root / name).write_text("a\n" * 5)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    for name in names:
        (root / name).write_text("b\n" * 40)
    sys.path.insert(0, str(REPO))
    from coordinator_core.session import xs_lane_budget

    lane = xs_lane_budget.lane_path(root, session_id)
    lane.parent.mkdir(parents=True, exist_ok=True)
    lane.write_text(json.dumps({"kind": "xs", "state": "open", "base_sha": "x", "paths": names}), encoding="utf-8")


def top_importtime(stderr_text, limit=10):
    """The `limit` largest cumulative-us modules in `-X importtime` stderr, as [{module, cumulative_us, self_us}]."""
    rows = []
    for line in stderr_text.splitlines():
        if not line.startswith("import time:"):
            continue
        parts = [p.strip() for p in line[len("import time:"):].split("|")]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        rows.append({"module": parts[2], "cumulative_us": int(parts[1]), "self_us": int(parts[0])})
    rows.sort(key=lambda r: r["cumulative_us"], reverse=True)
    return rows[:limit]


def guard_row(host, nag_parent=None):
    payload_obj = dict(PAYLOADS[host])
    env = None
    if host == "grok" and nag_parent is not None:
        env = {**os.environ, "COORDINATOR_AGENT_PROVIDER": "grok"}

    def argv_for(tag):
        # Each grok run gets its own repo and a fresh session id: the nag fires once per session id (counter in
        # the managed tempdir, outliving the probe), and the nag window and check stamp throttle a repeat.
        if env is not None:
            sid = str(uuid.uuid4())
            repo = nag_parent / f"nag-repo-{tag}"
            prepare_nag_repo(repo, sid)
            tool_input = {**payload_obj["tool_input"], "file_path": str(repo / "delta.py")}
            payload_obj.update(cwd=str(repo), workspaceRoot=str(repo), session_id=sid, sessionId=sid,
                               tool_input=tool_input, toolInput=tool_input)
        return [str(REPO), host, json.dumps(payload_obj)]

    out = run_child(argv_for("cost"), env=env)
    try:
        row = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        row = {"error": "child_failed", "returncode": out.returncode, "stderr_tail": out.stderr[-400:]}
    row["host"] = host
    row["nag_in_answer"] = carries_nag(row.get("answer"))
    imp = run_child(argv_for("imports"), ("-X", "importtime"), env=env)
    return row, {"host": "importtime_top10", "for": host, "modules": top_importtime(imp.stderr)}


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
                             timeout=120, creationflags=NO_WINDOW)
        info = json.loads(out.stdout.strip().splitlines()[-1])
        print(json.dumps({"host": "shim_import", "shim_import_process_secs": round(info["secs"], 4)}))

        imports = []
        for host in ("grok", "codex", "agy"):
            row, imp = guard_row(host, work)
            print(json.dumps(row))
            imports.append(imp)

        print(json.dumps(grok_fresh_row(work)))
        print(json.dumps(grok_live_row(work)))
        print(json.dumps(grok_live_row(work, lane=False)))

        guard_mod = load_sibling("unimatrix-host-guard")
        agy_mod = load_sibling("unimatrix-agy-hook-pretool")
        logs = [guard_mod.degrade_log_path()]
        print(json.dumps(measure("agy_hook", lambda: agy_mod.decide(AGY_PAYLOAD, None, str(REPO), json.dumps(AGY_PAYLOAD)), logs)))
        for imp in imports:
            print(json.dumps(imp))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
