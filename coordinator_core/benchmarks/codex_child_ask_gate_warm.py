"""Does a codex headless child's `dispatch.ask_gate` reach the live warm engine of its own pinned clone?

Boots an isolated warm engine (`isolated_clone.booted_warm_engine`), builds the child env through
`codex_runner.workflow._environment`, pins `COORDINATOR_ENGINE_ROOT` / `COORDINATOR_DOOR_ENGINE_ROOT` / `COORDINATOR_CLONE_ROOT` to that clone
(the real child's `current_engine_clone()` is the clone it runs from; here that clone is the isolated one), then runs
the real `coordinator-invoke.py dispatch.ask_gate` under that env as one measured process tree.

Reported, never wall clock as the figure: `process_time_ms` and `child_tree_spawns` (root excluded) of the child tree;
`reply_source` (`warm` when the child's warm client got an answer, else `cold`), `warm_enabled`, `pinned_root_ok`
(the child's warm client resolved the clone as its engine root), and `autostart_spawns` (warm-server spawns the child
issued through `warm.client.spawn_detached`, plus processes rooted in the clone outside the engine's own tree).

Base (COORDINATOR_WARM=0 in the child env; reproducible only from a pre-change checkout): expect `reply_source: cold`. After the env builders stop forcing warmth
off: expect `warm`, `process_time_ms` under `BAR_MS`, `autostart_spawns` 0. `--enforce` exits 1 unless all three hold.

Run: `python coordinator_core/benchmarks/codex_child_ask_gate_warm.py [--repo PATH] [--enforce]`. The engine boot is a
long-running leg: run it once, foreground, as the EM's measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BAR_MS = 500.0
OP = "dispatch.ask_gate"
ENTRYPOINT = "coordinator-invoke"
PROBE_SIZING = "state/sizings/2026-11-13-cold-ask-gate-real-shape-probe-absent.yaml"
_REPORT_ENV = "CODEX_CHILD_ASK_GATE_REPORT"

# Runs the real CLI under the child env and records, in-process, what its warm client did. Everything it imports
# beyond the CLI's own path is `warm.client` / `warm.settings`, which the CLI imports itself when warmth is on.
_CHILD_PROGRAM = r"""
import json, os, runpy, sys
rec = {"warm_enabled": None, "warm_answers": [], "autostart_spawns": 0, "pinned_root": None, "exit": None,
       "endpoint": None, "error": None}


def flush():
    with open(os.environ[%r], "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


try:
    sys.path.insert(0, os.environ["COORDINATOR_ENGINE_ROOT"])
    from coordinator_core.warm import client, settings
    rec["warm_enabled"] = bool(settings.is_warm_enabled())
    _try, _spawn = client.try_warm_dispatch, client.spawn_detached

    def try_warm_dispatch(*a, **k):
        out = _try(*a, **k)
        rec["warm_answers"].append(out is not None)
        rec["cold_reason"] = client.last_cold_reason()
        flush()
        return out

    def spawn_detached(*a, **k):
        rec["autostart_spawns"] += 1
        flush()
        return _spawn(*a, **k)

    client.try_warm_dispatch, client.spawn_detached = try_warm_dispatch, spawn_detached
    rec["pinned_root"] = str(client._engine_clone_root())
    rec["endpoint"] = client._endpoint_name(client.engine_token())
    flush()
    sys.argv = sys.argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        rec["exit"] = exc.code
except BaseException as exc:
    rec["error"] = repr(exc)
finally:
    flush()
""" % _REPORT_ENV


def reply_source(record: dict) -> str:
    """`warm` when any of the child's warm-client calls returned an answer, else `cold`."""
    return "warm" if any(record.get("warm_answers") or ()) else "cold"


def child_env(clone: Path, report: Path) -> dict:
    """The codex native-worker env as `_environment` builds it, with the engine root pinned to `clone`."""
    from coordinator_core.codex_runner.workflow import _environment

    env = _environment()
    env.pop("VIRTUAL_ENV", None)
    env["COORDINATOR_ENGINE_ROOT"] = str(clone)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(clone)
    env["COORDINATOR_CLONE_ROOT"] = str(clone)
    env[_REPORT_ENV] = str(report)
    return env


def stray_clone_pids(clone: Path, engine_pid: int) -> list[int]:
    """Pids rooted in `clone` that are neither the engine nor its descendants: autostarted services."""
    import psutil

    from coordinator_core.benchmarks.isolated_clone import _normalized, _process_is_rooted_under

    try:
        own = {engine_pid, *(p.pid for p in psutil.Process(engine_pid).children(recursive=True))}
    except psutil.Error:
        own = {engine_pid}
    root_norm = _normalized(clone)
    strays = []
    for proc in psutil.process_iter():
        try:
            if proc.pid not in own and proc.pid != os.getpid() and _process_is_rooted_under(proc, root_norm):
                strays.append(proc.pid)
        except psutil.Error:
            continue
    return strays


def _crumb(clone: Path) -> dict | None:
    from coordinator_core.warm import breadcrumb

    return breadcrumb.read_breadcrumb(engine_root=clone)


def measure(repo: Path) -> dict:
    """Boot an isolated engine, run one child `dispatch.ask_gate` under the codex child env, return the report."""
    from coordinator_core.benchmarks.isolated_clone import _link_tree, booted_warm_engine, rmtree_or_raise
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="codex-child-ask-gate-")
    try:
        with booted_warm_engine(
            _REPO, prefix="cxchild", entrypoint=ENTRYPOINT, label="codex-child-ask-gate-warm"
        ) as (clone, engine_pid, _clone_env):
            _link_tree(_REPO / "coordinator" / "lib" / "resolve-claude-klabauter",
                       clone / "coordinator" / "lib" / "resolve-claude-klabauter", skip_dirs=("__pycache__", "tests"))
            report_path = scratch / "child-record.json"
            env = child_env(clone, report_path)
            script = clone / "coordinator" / "bin" / f"{ENTRYPOINT}.py"
            tree = single_invocation_tree_process_time(
                [sys.executable, "-c", _CHILD_PROGRAM, str(script), OP,
                 json.dumps({"sizing_path": PROBE_SIZING}), "--repo", str(repo)],
                env=env, cwd=str(repo),
                stdout_path=str(scratch / "child.stdout"), stderr_path=str(scratch / "child.stderr"),
            )
            try:
                record = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            strays = stray_clone_pids(clone, engine_pid)
            stderr = (scratch / "child.stderr").read_text(encoding="utf-8", errors="replace")
            return {
                "bar_ms": BAR_MS,
                "reply_source": reply_source(record),
                "process_time_ms": round(tree["process_time_ms"], 1),
                "child_tree_spawns": max(0, tree["procs"] - 1),
                "autostart_spawns": int(record.get("autostart_spawns", 0)) + len(strays),
                "stray_clone_pids": strays,
                "warm_enabled": record.get("warm_enabled"),
                "warm_answers": record.get("warm_answers"),
                "pinned_root": record.get("pinned_root"),
                "child_endpoint": record.get("endpoint"),
                "child_error": record.get("error"),
                "child_cold_reason": record.get("cold_reason"),
                "server_breadcrumb_pipe": (_crumb(clone) or {}).get("pipe"),
                "pinned_root_ok": record.get("pinned_root") is not None
                and os.path.normcase(record["pinned_root"]) == os.path.normcase(str(clone)),
                "serving_engine_root": str(clone),
                "child_env_warm": env.get("COORDINATOR_WARM"),
                "child_env_listener_autostart": env.get("COORDINATOR_WARM_LISTENER_AUTOSTART"),
                "child_env_server_autostart": env.get("COORDINATOR_WARM_SERVER_AUTOSTART"),
                "child_rc": tree["rc"],
                "child_exit": record.get("exit"),
                "warm_settings_banner": "[warm-settings] warmth is disabled" in stderr,
                "stderr_tail": stderr[-400:],
                "repo": str(repo),
            }
    finally:
        rmtree_or_raise(scratch, label="codex-child-ask-gate-warm")


def passes(report: dict) -> bool:
    """True when the reply was warm, from the pinned clone, under the bar, with no autostart."""
    return (
        report["reply_source"] == "warm"
        and report["pinned_root_ok"]
        and report["process_time_ms"] < BAR_MS
        and report["autostart_spawns"] == 0
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="repo the ask_gate targets (default: this engine's repo root)")
    ap.add_argument("--enforce", action="store_true", help="exit 1 unless warm, pinned, under the bar, no autostart")
    args = ap.parse_args()
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = measure(Path(args.repo).resolve() if args.repo else _REPO)
    print(json.dumps(report, indent=2, default=str))
    return 1 if args.enforce and not passes(report) else 0


if __name__ == "__main__":
    sys.exit(main())
