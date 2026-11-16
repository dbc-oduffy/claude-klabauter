"""Cost of a default `machine-local get <set key>` through the door: process time and spawn count.

Never wall clock. Two legs against a fixture registry (settings-home override into a managed temp
dir, never the real registry):

  cold  `coordinator/bin/machine-local.py` run in a fresh interpreter, the path the door takes when
        `machine-local` is not on the warm allowlist. process_ms is that child's own CPU time from
        interpreter start (`time.process_time`); spawn_count is that interpreter (1) plus every
        process it spawns.
  warm  the in-process entrypoint call the warm engine serves (`invoke_from_argv._run_entrypoint`),
        measured after a warm-up call; spawn_count is every Popen made by the call. Skipped, and
        recorded as not_warm_servable, while the name is absent from the warm allowlist.

`door_path` names the leg a default call takes today; the closing `door_default:` line carries its
process_ms and spawn_count. Measurement only: exit 0, unless --check, which exits 1 when the door
default is over 500ms process time or spawns anything.

Run: `python coordinator_core/benchmarks/machine_local_get_cost.py`
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = "machine-local"
KEY = "paths.bench_key"
VALUE = "bench-value"
BAR_MS = 500.0


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _fixture(work: Path) -> dict[str, str]:
    reg = work / "registry"
    reg.mkdir()
    (reg / "registry.local.toml").write_text(f'"{KEY}" = "{VALUE}"\n', encoding="utf-8")
    env = dict(os.environ)
    env.update(
        MACHINE_LOCAL_REGISTRY_DIR=str(reg),
        COORDINATOR_SETTINGS_HOME=str(work / "settings"),
        COORDINATOR_LOCK_ROOT=str(work / "locks"),
    )
    env.pop("MACHINE_LOCAL_DEADLINE_S", None)
    return env


def _count_popen():
    """Patch subprocess.Popen with a counter; returns (counter list, restore fn)."""
    real = subprocess.Popen
    calls: list[tuple[str, ...]] = []

    class _Counting(real):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw) -> None:
            calls.append(tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),))
            super().__init__(args, *a, **kw)

    subprocess.Popen = _Counting  # type: ignore[misc]
    return calls, lambda: setattr(subprocess, "Popen", real)


def _cold_child() -> int:
    """Child entry: stdlib-only, so startup CPU is the entrypoint's own cost, not the harness's."""
    import importlib.util

    from coordinator_core.bin_lib_binding import exec_module_bin_bound

    calls, restore = _count_popen()
    bin_dir = REPO_ROOT / "coordinator" / "bin"
    spec = importlib.util.spec_from_file_location("_ml_entry", bin_dir / "machine-local.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    exec_module_bin_bound(spec.loader, mod, str(bin_dir))
    saved_out = sys.stdout
    import io

    buf = io.StringIO()
    sys.stdout = buf
    try:
        rc = mod.main(["get", KEY])
    finally:
        sys.stdout = saved_out
        restore()
    print(json.dumps({"process_ms": time.process_time() * 1000.0, "child_spawns": len(calls),
                      "rc": rc, "stdout": buf.getvalue().strip()}))
    return 0


def _cold_leg(env: dict[str, str], reps: int) -> dict:
    rows = []
    for _ in range(reps):
        p = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--cold-child"],
            env=env, capture_output=True, text=True, creationflags=_no_console(), timeout=60,
        )
        rows.append(json.loads(p.stdout.strip().splitlines()[-1]))
    ok = all(r["rc"] == 0 and r["stdout"] == VALUE for r in rows)
    return {
        "ok": ok,
        "process_ms": round(sorted(r["process_ms"] for r in rows)[len(rows) // 2], 1),
        "spawn_count": 1 + max(r["child_spawns"] for r in rows),
    }


def _warm_leg(env: dict[str, str], reps: int) -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from coordinator_core.ops import invoke_from_argv as ifa

    try:
        ifa._resolve_entrypoint_script(ENTRYPOINT)
    except Exception as exc:  # noqa: BLE001 - any refusal means the warm engine cannot serve the name
        return {"servable": False, "reason": f"{type(exc).__name__}"}

    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if k in saved})
    calls, restore = _count_popen()
    try:
        argv = ["get", KEY]
        cwd = str(REPO_ROOT)
        ifa._run_entrypoint(ENTRYPOINT, argv, cwd, "")
        times, last = [], {}
        for _ in range(reps):
            t0 = time.process_time()
            last = ifa._run_entrypoint(ENTRYPOINT, argv, cwd, "")
            times.append((time.process_time() - t0) * 1000.0)
    finally:
        restore()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    ok = last.get("exit_code") == 0 and str(last.get("stdout", "")).strip() == VALUE
    return {"servable": True, "ok": ok, "process_ms": round(sorted(times)[len(times) // 2], 1),
            "spawn_count": len(calls)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--check", action="store_true", help="exit 1 when the door default breaks the bar")
    ap.add_argument("--cold-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.cold_child:
        return _cold_child()

    sys.path.insert(0, str(REPO_ROOT))
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir(prefix="ml-get-cost-") as tmp:
        env = _fixture(Path(tmp))
        cold = _cold_leg(env, args.reps)
        warm = _warm_leg(env, args.reps)

    print(f"cold: ok={cold['ok']} process_ms={cold['process_ms']} spawn_count={cold['spawn_count']}")
    if warm["servable"]:
        print(f"warm: ok={warm['ok']} process_ms={warm['process_ms']} spawn_count={warm['spawn_count']}")
    else:
        print(f"warm: not_warm_servable ({warm['reason']})")
    default, path = (warm, "warm") if warm["servable"] else (cold, "cold")
    print(f"door_default: door_path={path} process_ms={default['process_ms']} spawn_count={default['spawn_count']}")
    if args.check and not (default["ok"] and default["process_ms"] <= BAR_MS and default["spawn_count"] == 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
