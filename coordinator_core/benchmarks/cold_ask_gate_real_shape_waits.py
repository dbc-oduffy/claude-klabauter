"""Which wait a headless fire's cold `dispatch.ask_gate` spends its time in, at the real invocation shape.

Reproduces the shape of a real headless engineOp, not the fixture of `cold_operation_ask_gate_split`: the real
repo root, the real settings home (resolved through `COORDINATOR_SETTINGS_HOME`; an unresolvable home is a
typed refusal naming that key), the stamped engine, and `headless._warm_reply` followed by the cold child
`headless._invoke_source` spawns (`python -m coordinator_core.ops.workflow_fire.cold_operation`, cwd = the repo,
`COORDINATOR_WARM=0`, `PYTHONPATH` = the executor root). Nothing in `headless.py` or `warm/client.py` is edited.

Waits, process ms unless noted (never wall clock as the figure for compute):
`stamp_check_ms` (`skew.compute_client_token`), `warm_probe_ms` (`headless._warm_reply`, whole call),
`child_process_ms` (the cold child tree's CPU), `child_idle_wait_ms` (child wall minus its CPU: a lock wait, a
blocked read or any other sleep) and `cold_await_attempt_ms` (wall of the ipc cold-await attempts the child
reported in its reply's `diagnosis.attempts`; 0 when the op answered inside its backstop). The last overlaps
`child_idle_wait_ms` when the child hit the ceiling. `dominant_wait` is the largest; ties go in the order listed.

Default params name a sizing that does not exist, so the op refuses without writing anything. Pass
`--sizing-path` and `--run-id` for a run-bound sizing: the gate then mutates it, and a running warm server
answers the warm probe for real. `--throwaway-run` builds that run-bound shape itself: a sizing and a run record
dir it creates under the repo and removes in a finally, never a live sizing. The child is bounded by ipc's own
cold-await ceiling, never by this script.

`--profile` re-runs the cold child under cProfile (a second child: the profiled figure is inflated, the unprofiled
`child_process_ms` stays the measurement) and reports the top 20 by cumulative time. `--enforce-bar` exits 1 when
the cold child's process ms reaches `BAR_MS`.

Run: `python -m coordinator_core.benchmarks.cold_ask_gate_real_shape_waits [--repo PATH] [--sizing-path REL]
[--run-id ID] [--throwaway-run] [--profile] [--enforce-bar]`. Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

from coordinator_core.benchmarks.cold_operation_ask_gate_split import BAR_MS

SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"
COLD_MODULE = "coordinator_core.ops.workflow_fire.cold_operation"
OP = "dispatch.ask_gate"
PROBE_SIZING = "state/sizings/2026-11-13-cold-ask-gate-real-shape-probe-absent.yaml"
WAITS = ("stamp_check_ms", "warm_probe_ms", "child_process_ms", "child_idle_wait_ms", "cold_await_attempt_ms")
PROFILE_TOP = 20
_PROFILE_OUT_ENV = "COLD_ASK_GATE_PROFILE_OUT"
THROWAWAY_STEM = "cold-ask-gate-throwaway"


class SettingsHomeKeyMissing(RuntimeError):
    """The settings home the real shape needs does not resolve; the message names the key to set."""


class RepoRootMissing(RuntimeError):
    """No git repo root at or above the starting directory."""


def resolve_settings_home() -> Path:
    """The real settings home, or `SettingsHomeKeyMissing` naming `COORDINATOR_SETTINGS_HOME`."""
    from coordinator_core import _settings_home

    try:
        home = _settings_home.settings_home()
    except Exception as exc:  # noqa: BLE001 -- any resolver refusal is the same typed refusal
        raise SettingsHomeKeyMissing(f"settings home unresolved ({type(exc).__name__}): set {SETTINGS_HOME_KEY}") from exc
    if not home.is_dir():
        raise SettingsHomeKeyMissing(f"settings home {home} is not a directory: set {SETTINGS_HOME_KEY} to the real one")
    return home


def resolve_repo_root(start: Path) -> Path:
    """The nearest ancestor of `start` (inclusive) holding a `.git` entry."""
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    raise RepoRootMissing(f"no .git at or above {start}; pass --repo")


def dominant_wait(waits: dict) -> str:
    """The key of `waits` with the largest figure, restricted to `WAITS`, ties in `WAITS` order."""
    return max(WAITS, key=lambda k: (waits[k], -WAITS.index(k)))


def attempts_wall_ms(reply: dict) -> tuple[float, list]:
    """Total wall ms and the records of the cold-await attempts a reply's error diagnosis carries."""
    err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    diag = data.get("diagnosis") if isinstance(data.get("diagnosis"), dict) else {}
    if not diag and isinstance(reply.get("data"), dict):
        diag = reply["data"].get("diagnosis") or {}
    attempts = diag.get("attempts") if isinstance(diag.get("attempts"), list) else []
    return round(sum(float(a.get("wall_ms", 0.0)) for a in attempts), 1), attempts


def _process_delta_ms(fn):
    """`(result, process ms, wall ms)` of `fn()` in this process."""
    cpu0, wall0 = time.process_time(), time.monotonic()
    try:
        out = fn()
    except BaseException as exc:  # noqa: BLE001 -- returned as the result, classified by the caller
        out = exc
    return out, (time.process_time() - cpu0) * 1000.0, (time.monotonic() - wall0) * 1000.0


def over_bar(report: dict) -> bool:
    """True when the cold child's process time reaches `BAR_MS`."""
    return report["waits"]["child_process_ms"] >= BAR_MS


def profile_launch(carveout: bool) -> str:
    """The `-c` program that runs the cold module under cProfile and dumps stats to `$COLD_ASK_GATE_PROFILE_OUT`;
    `carveout` adds the unstamped-dispatch allowance the stamp gate needs when the engine root carries no stamp."""
    allow = "from coordinator_core import ipc; ipc.allow_unstamped_dispatch()\n" if carveout else ""
    return (
        "import cProfile, os, runpy\n"
        "pr = cProfile.Profile(); pr.enable()\n"
        f"{allow}"
        "try:\n"
        f"    runpy.run_module({COLD_MODULE!r}, run_name='__main__')\n"
        "finally:\n"
        f"    pr.disable(); pr.dump_stats(os.environ[{_PROFILE_OUT_ENV!r}])\n"
    )


def top_cumulative(stats_path: Path, n: int = PROFILE_TOP, by: str = "cumulative") -> list[dict]:
    """The `n` largest entries of a cProfile dump by `by` (`cumulative` or `self` time), as
    `{func, calls, tottime_ms, cumtime_ms}`."""
    import pstats

    rows = pstats.Stats(str(stats_path)).stats
    column = 2 if by == "self" else 3
    top = sorted(rows.items(), key=lambda kv: kv[1][column], reverse=True)[:n]
    return [
        {
            "func": f"{Path(f).name}:{ln}({name})" if ln else f"{f}({name})",
            "calls": nc, "tottime_ms": round(tt * 1000.0, 1), "cumtime_ms": round(ct * 1000.0, 1),
        }
        for (f, ln, name), (_cc, nc, tt, ct, _callers) in top
    ]


def _profiled_child(
    root: Path, repo: Path, request: Path, response: Path, scratch: Path, carveout: bool, home: Path | None = None,
) -> dict:
    """One more cold child under cProfile; the unprofiled billing stays the measurement."""
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    out = scratch / "child.prof"
    env = dict(os.environ, COORDINATOR_WARM="0", PYTHONPATH=str(root), **{_PROFILE_OUT_ENV: str(out)})
    if home is not None:
        env[SETTINGS_HOME_KEY] = str(home)
    tree = single_invocation_tree_process_time(
        [sys.executable, "-c", profile_launch(carveout), str(request), str(response)], env=env, cwd=str(repo),
        stdout_path=str(scratch / "profiled.stdout"), stderr_path=str(scratch / "profiled.stderr"),
    )
    found = out.is_file()
    return {
        "profiled_process_ms": round(tree["process_time_ms"], 1), "rc": tree["rc"],
        "top": top_cumulative(out) if found else [],
        "top_self": top_cumulative(out, by="self") if found else [],
    }


@contextlib.contextmanager
def throwaway_run(repo: Path, run_id: str | None = None, plan_rows: int = 0):
    """Create a run-bound sizing and its run record dir under `repo`, yield `(sizing_rel, run_id)`, remove both.
    The sizing stem is unique, so a live sizing is never named. `plan_rows` > 0 adds a `plan:` edge to a throwaway
    plan with that many open rows (the resume shape: the gate then reads the plan and walks history for landed rows)."""
    import yaml

    from coordinator_core.benchmarks.ask_gate_run_bound_process_budget import _sizing_doc

    tag = secrets.token_hex(3)
    stem = f"2026-11-14-{THROWAWAY_STEM}-{tag}"
    run_id = run_id or f"ask-{time.strftime('%Y%m%dT%H%M%S')}-{tag}"
    sizings = repo / "state" / "sizings"
    sizing = sizings / f"{stem}.yaml"
    run = repo / "state" / "warp" / run_id
    plan = repo / "docs" / "plans" / f"{stem}.md"
    scripts = repo / "scratch" / "warp"
    doc = _sizing_doc(stem)
    sizings.mkdir(parents=True, exist_ok=True)
    try:
        if plan_rows > 0:
            doc["plan"] = f"docs/plans/{stem}.md"
            rows = "".join(f"- id: A{n}\n  title: T{n}\n  disposition: open\n  writes:\n    - some/a{n}.py\n" for n in range(1, plan_rows + 1))
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(
                f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n",
            )
        sizing.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
        run.mkdir(parents=True)
        (run / "run.json").write_text("{}\n", encoding="utf-8", newline="\n")
        if plan_rows > 0:
            from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGED_INLINE_SLOT

            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / f"{run_id}.workflow.mjs").write_text(f"{STAGED_INLINE_SLOT}\n", encoding="utf-8", newline="\n")
        yield f"state/sizings/{stem}.yaml", run_id
    finally:
        for stray in scripts.glob(f"{run_id}*"):
            if stray.is_dir():
                shutil.rmtree(stray, ignore_errors=True)
            else:
                stray.unlink(missing_ok=True)
        for stray in sizings.glob(f"{stem}.*"):
            stray.unlink(missing_ok=True)
        plan.unlink(missing_ok=True)
        shutil.rmtree(run, ignore_errors=True)


def stage_outcome(reply: dict) -> tuple[bool, str | None]:
    """`(staged, refusal)` of a gate reply: staged when the result carries a `manifest_handle`; the refusal is
    the stage error (or the halt reason) when it carries one."""
    result = reply["result"] if isinstance(reply.get("result"), dict) else {}
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    halt = result.get("halt") if isinstance(result.get("halt"), dict) else {}
    err = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    refusal = manifest.get("error") or halt.get("reason") or err.get("message")
    return bool(result.get("manifest_handle")), (str(refusal) if refusal else None)


def measure(
    repo: Path, sizing_path: str = PROBE_SIZING, run_id: str | None = None, profile: bool = False,
    isolated_home: bool = False,
) -> dict:
    """Bill each wait of one headless-shaped cold `dispatch.ask_gate` and name the dominant one. `isolated_home`
    points the child at an empty settings home, so no box-live index entry can refuse the stage; the report's
    `staged` says whether the stage succeeded (a refused stage's figure is invalid)."""
    from coordinator_core.benchmarks.cold_operation_ask_gate_split import (
        _CARVEOUT_LAUNCH, engine_root, needs_unstamped_carveout,
    )
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
    from coordinator_core.ops.workflow_fire import headless
    from coordinator_core.temp_layout import make_managed_temp_dir
    from coordinator_core.warm import skew

    home = resolve_settings_home()
    params = {"sizing_path": sizing_path}
    if run_id:
        params["run_id"] = run_id
    root = engine_root()
    carveout = needs_unstamped_carveout(root)
    scratch = make_managed_temp_dir(prefix="cold-ask-gate-real-shape-")
    try:
        artifacts = scratch / "operations" / "1"
        artifacts.mkdir(parents=True)
        request, response = artifacts / "request.json", artifacts / "response.json"
        request.write_text(json.dumps({"op": OP, "params": params, "repo_root": str(repo)}), encoding="utf-8")

        _, stamp_ms, stamp_wall = _process_delta_ms(lambda: skew.compute_client_token(root))
        warm, warm_ms, warm_wall = _process_delta_ms(lambda: headless._warm_reply(OP, params, str(repo), artifacts, 20.0))
        warm_route = None
        try:
            warm_route = json.loads((artifacts / "warm_route.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

        env = dict(os.environ, COORDINATOR_WARM="0", PYTHONPATH=str(root))
        iso = scratch / "settings-home" if isolated_home else None
        if iso is not None:
            (iso / "machine-local").mkdir(parents=True)
            env[SETTINGS_HOME_KEY] = str(iso)
        launch = ["-c", _CARVEOUT_LAUNCH] if carveout else ["-m", COLD_MODULE]
        tree = single_invocation_tree_process_time(
            [sys.executable, *launch, str(request), str(response)], env=env, cwd=str(repo),
            stdout_path=str(scratch / "child.stdout"), stderr_path=str(scratch / "child.stderr"),
        )
        try:
            reply = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reply = {}
        attempts_ms, attempts = attempts_wall_ms(reply)
        err = reply.get("error") if isinstance(reply.get("error"), dict) else None
        result = reply["result"] if isinstance(reply.get("result"), dict) else {}
        halt = result.get("halt")
        waits = {
            "stamp_check_ms": stamp_ms,
            "warm_probe_ms": warm_ms,
            "child_process_ms": tree["process_time_ms"],
            "child_idle_wait_ms": max(0.0, tree["wall_ms"] - tree["process_time_ms"]),
            "cold_await_attempt_ms": attempts_ms,
        }
        staged, refusal = stage_outcome(reply)
        profiled = _profiled_child(root, repo, request, response, scratch, carveout, iso) if profile else None
        return {
            "staged": staged,
            "stage_refusal": refusal,
            "isolated_home": isolated_home,
            "bar_ms": BAR_MS,
            "profile": profiled,
            "waits": {k: round(v, 1) for k, v in waits.items()},
            "dominant_wait": dominant_wait(waits),
            "wall_ms": {
                "stamp_check": round(stamp_wall, 1), "warm_probe": round(warm_wall, 1),
                "child": round(tree["wall_ms"], 1),
            },
            "cold_await_attempts": attempts,
            "warm_answered": warm is not None and not isinstance(warm, BaseException),
            "warm_probe_error": f"{type(warm).__name__}: {warm}" if isinstance(warm, BaseException) else None,
            "warm_route": warm_route,
            "child_procs": tree["procs"],
            "child_rc": tree["rc"],
            "arm": result.get("arm"),
            "halt_kind": halt.get("kind") if isinstance(halt, dict) else None,
            "error_code": err.get("code") if err else None,
            "error_message": err.get("message") if err else None,
            "engine_root": str(root),
            "engine_stamped": not carveout,
            "repo": str(repo),
            "settings_home": str(home),
            "params": params,
        }
    finally:
        rmtree_or_raise(scratch, label="cold-ask-gate-real-shape-waits")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="repo root (default: nearest .git at or above the working directory)")
    ap.add_argument("--sizing-path", default=PROBE_SIZING, help="repo-relative sizing path (default: an absent one, so the op refuses read-only)")
    ap.add_argument("--run-id", help="run id to bind; mutates the sizing")
    ap.add_argument("--throwaway-run", action="store_true", help="bind a throwaway sizing and run dir this script creates and removes")
    ap.add_argument("--plan-rows", type=int, default=0, help="with --throwaway-run: the sizing carries a plan edge to a throwaway plan with this many open rows (0: no plan)")
    ap.add_argument("--profile", action="store_true", help=f"also run the cold child under cProfile and report the top {PROFILE_TOP} by cumulative time")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 1 when the cold child's process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    try:
        repo = Path(args.repo).resolve() if args.repo else resolve_repo_root(Path.cwd())
        if args.throwaway_run:
            with throwaway_run(repo, args.run_id, args.plan_rows) as (sizing_rel, run_id):
                report = measure(repo, sizing_rel, run_id, args.profile, isolated_home=True)
            if args.plan_rows > 0 and not report["staged"]:
                print(json.dumps({"invalid": "stage refused; the figure is not a stage measurement", **report}, indent=2, default=str))
                return 3
        else:
            report = measure(repo, args.sizing_path, args.run_id, args.profile)
    except (SettingsHomeKeyMissing, RepoRootMissing) as exc:
        print(json.dumps({"refused": type(exc).__name__, "message": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 1 if args.enforce_bar and over_bar(report) else 0


if __name__ == "__main__":
    sys.exit(main())
