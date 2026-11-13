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
answers the warm probe for real. The child is bounded by ipc's own cold-await ceiling, never by this script.

Run: `python -m coordinator_core.benchmarks.cold_ask_gate_real_shape_waits [--repo PATH] [--sizing-path REL]
[--run-id ID]`. Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"
COLD_MODULE = "coordinator_core.ops.workflow_fire.cold_operation"
OP = "dispatch.ask_gate"
PROBE_SIZING = "state/sizings/2026-11-13-cold-ask-gate-real-shape-probe-absent.yaml"
WAITS = ("stamp_check_ms", "warm_probe_ms", "child_process_ms", "child_idle_wait_ms", "cold_await_attempt_ms")


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


def measure(repo: Path, sizing_path: str = PROBE_SIZING, run_id: str | None = None) -> dict:
    """Bill each wait of one headless-shaped cold `dispatch.ask_gate` and name the dominant one."""
    from coordinator_core.benchmarks.cold_operation_ask_gate_split import (
        _CARVEOUT_LAUNCH, engine_root, needs_unstamped_carveout,
    )
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
    from coordinator_core.ops.workflow_fire import headless
    from coordinator_core.temp_layout import managed_tempdir
    from coordinator_core.warm import skew

    home = resolve_settings_home()
    params = {"sizing_path": sizing_path}
    if run_id:
        params["run_id"] = run_id
    root = engine_root()
    carveout = needs_unstamped_carveout(root)
    scratch = Path(tempfile.mkdtemp(prefix="cold-ask-gate-real-shape-", dir=str(managed_tempdir())))
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
        waits = {
            "stamp_check_ms": stamp_ms,
            "warm_probe_ms": warm_ms,
            "child_process_ms": tree["process_time_ms"],
            "child_idle_wait_ms": max(0.0, tree["wall_ms"] - tree["process_time_ms"]),
            "cold_await_attempt_ms": attempts_ms,
        }
        return {
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
    args = ap.parse_args()
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    try:
        repo = Path(args.repo).resolve() if args.repo else resolve_repo_root(Path.cwd())
        report = measure(repo, args.sizing_path, args.run_id)
    except (SettingsHomeKeyMissing, RepoRootMissing) as exc:
        print(json.dumps({"refused": type(exc).__name__, "message": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
