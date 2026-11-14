"""Bench for `distill.bookkeeping`: process time, spawn count and modeled token cost over a seeded fixture repo.

Builds a tmp git repo holding a stamped disposal manifest with one eligible handoff, one terminal and
one live plan, and an aged scratch directory. After one warm-up on a separate fixture it runs, each on
a fresh fixture at default arguments: the composite handler and each composed step in process
(`distill.apply_disposal`, `fleet.archive_completed_plans`, `fleet.scratch_hygiene`), and reports CPU
time (own process plus reaped children) and spawns, never wall clock. The composite's effects are
audited by checks that share no code with the op. Also reports the distill token cost before (one agent
dispatch per bookkeeping phase, reading the artifacts the phase acts on) and after (zero dispatches;
the op response is the only text read); both MODELED, bytes/4, not telemetry.

Usage: python coordinator_core/benchmarks/distill_bookkeeping_bench.py [--json]
Exit 0 when every bar holds, 1 when one fails, 2 when the op is not registered.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.run import run_git
from coordinator_core.telemetry import spawn_counter
from coordinator_core.temp_layout import managed_temp_dir

OP = "distill.bookkeeping"
BASE_SHA = "cd95f8ea49"
BASE_PATH = "coordinator_core/ops/distill_bookkeeping.py"
PROCESS_MS_BAR = 500.0
BYTES_PER_TOKEN = 4
LEGS = ("composite", "disposal", "spec_archive", "scratch")
RECORDED_RUN_REFERENCE = {
    "source": "docs/wiki/distill-ceremony.md, distill_run_cost.py before (bfb7eb4167), load plus scan only",
    "dispatches_total": 337,
    "input_tokens_est_total": 20807244,
    "note": "the bookkeeping phases are not metered separately in any recorded run",
}
_SESSION_ID = "00000000-0000-4000-8000-000000000000"
_RUN = "2026-11-14-01h00"
_HANDOFF = "archive/handoffs/eligible.md"
_DONE_PLAN = "docs/plans/2026-11-01-done-plan.md"
_LIVE_PLAN = "docs/plans/2026-11-02-live-plan.md"
_SCRATCH_FILE = "scratch/old-residue/blob.txt"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    result = run_git(
        ["-c", "user.name=bench", "-c", "user.email=bench@example.com", *args], cwd=str(root)
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plan(status: str) -> str:
    return f"---\ntitle: plan\nstatus: {status}\ncreated: 2026-11-01\n---\n\n# plan\n"


def build_fixture(root: Path) -> None:
    """Seed `root` as a committed repo with a stamped disposal manifest, plans and aged scratch residue."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _write(root, "README.md", "root\n")
    _git(root, "add", "--", "README.md")
    _git(root, "commit", "-q", "-m", "root")
    _write(root, "state/distillation-log.md", "## Run seed\n- seed -> EPHEMERAL, seed (run: seed)\n")
    _git(root, "add", "--", "state/distillation-log.md")
    _git(root, "commit", "-q", "-m", "harvest")
    (root / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    _write(
        root, _HANDOFF,
        "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\nrealized_by: inline\n---\nbody\n",
    )
    _write(root, _DONE_PLAN, _plan("implemented"))
    _write(root, _LIVE_PLAN, _plan("executing"))
    _git(root, "add", "--", _HANDOFF, _DONE_PLAN, _LIVE_PLAN)
    _git(root, "commit", "-q", "-m", "candidates")
    _write(root, _SCRATCH_FILE, "residue\n")
    old = 1_700_000_000
    os.utime(root / _SCRATCH_FILE, (old, old))
    os.utime((root / _SCRATCH_FILE).parent, (old, old))
    from coordinator_core.ops.distill_disposal_manifest import _handler as assemble
    from coordinator_core.ops.distill_stamp_disposal import _handler as stamp

    common_dir = root / ".git"
    assemble({"run_id": _RUN, "candidates": [_HANDOFF]}, repo_root=common_dir)
    stamp(
        {"run_id": _RUN, "by": "pm", "at": "2026-11-14T10:00:00Z", "note": "approved, mass-throttle-ack"},
        repo_root=common_dir,
    )


def _cpu_ms() -> float:
    t = os.times()
    return (time.process_time() + t.children_user + t.children_system) * 1000.0


def _legs(root: Path) -> dict[str, Callable[[], Any]]:
    """Each leg is a zero-arg callable over the fixture at `root`, at default arguments."""
    from coordinator_core.ops import distill_bookkeeping as bk

    common_dir = root / ".git"

    def composite() -> dict:
        return asyncio.run(bk._handler({"run_id": _RUN}, repo_root=common_dir))

    def disposal() -> dict:
        return asyncio.run(bk._disposal_step(common_dir, root, _RUN, None))

    def spec_archive() -> dict:
        return asyncio.run(asyncio.to_thread(bk._spec_archive_step, common_dir, bk.DEFAULT_PLAN_CAP))

    def scratch() -> dict:
        return asyncio.run(asyncio.to_thread(bk._scratch_step, root, None))

    return {"composite": composite, "disposal": disposal, "spec_archive": spec_archive, "scratch": scratch}


def _file_bytes(root: Path, rels: list[str]) -> int:
    return sum((root / rel).stat().st_size for rel in rels if (root / rel).is_file())


def phase_input_bytes(root: Path) -> dict[str, int]:
    """Bytes an agent would read to do each bookkeeping phase by hand: the artifacts the phase acts on."""
    from coordinator_core.ops.distill_stamp_disposal import manifest_path_for_run

    manifest = manifest_path_for_run(root, _RUN)
    return {
        "disposal": _file_bytes(root, [_HANDOFF]) + (manifest.stat().st_size if manifest.is_file() else 0),
        "spec_archive": _file_bytes(root, [_DONE_PLAN, _LIVE_PLAN]),
        "scratch": _file_bytes(root, [_SCRATCH_FILE]),
    }


def audit_composite(root: Path) -> list[str]:
    """Effects the composite must leave, checked by plain filesystem tests."""
    bad = []
    if (root / _HANDOFF).exists():
        bad.append("disposed handoff still present")
    if (root / _DONE_PLAN).exists() or not (root / "archive/specs/2026-11/2026-11-01-done-plan.md").is_file():
        bad.append("terminal plan not archived")
    if not (root / _LIVE_PLAN).is_file():
        bad.append("live plan was moved")
    if (root / "scratch/old-residue").exists():
        bad.append("aged scratch residue still present")
    return bad


def measure_leg(name: str, work: Path) -> dict:
    """Build a fresh fixture under `work`, run one leg inside the measured window, return its row."""
    build_fixture(work)
    leg = _legs(work)[name]
    in_bytes = phase_input_bytes(work)
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = _SESSION_ID
    try:
        spawns0 = spawn_counter.spawn_count()
        cpu0, wall0 = _cpu_ms(), time.perf_counter()
        out = leg()
        cpu1, wall1 = _cpu_ms(), time.perf_counter()
        spawns = spawn_counter.spawn_count() - spawns0
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    row: dict[str, Any] = {
        "leg": name,
        "ran": out.get("ran", True) if name != "composite" else all(
            s.get("ran") for s in out.get("steps", {}).values()
        ),
        "errored": bool(out.get("error") or out.get("errored")),
        "process_ms": round(cpu1 - cpu0, 1),
        "wall_ms": round((wall1 - wall0) * 1000.0, 1),
        "spawns": spawns,
        "response_bytes": len(json.dumps(out, default=str).encode("utf-8")),
        "phase_input_bytes": in_bytes,
    }
    if name == "composite":
        row["audit_failures"] = audit_composite(work)
    return row


def token_model(composite: dict) -> dict:
    """Modeled token cost: before = one agent dispatch per bookkeeping phase reading its artifacts."""
    phases = composite["phase_input_bytes"]
    before_bytes = sum(phases.values())
    return {
        "unit": f"bytes/{BYTES_PER_TOKEN}, modeled",
        "before": {
            "agent_dispatches": len(phases),
            "input_tokens_est": before_bytes // BYTES_PER_TOKEN,
            "phases": {k: v // BYTES_PER_TOKEN for k, v in phases.items()},
        },
        "after": {
            "agent_dispatches": 0,
            "op_calls": 1,
            "input_tokens_est": composite["response_bytes"] // BYTES_PER_TOKEN,
        },
        "recorded_run_reference": RECORDED_RUN_REFERENCE,
    }


def classify_base(path_present_at_base: Optional[bool]) -> str:
    """Verdict for the base sha: the op file absent there means the op was not registered."""
    if path_present_at_base is None:
        return "base-unavailable"
    return "registered" if path_present_at_base else "op-not-registered"


def base_verdict(base_sha: str = BASE_SHA, root: Optional[Path] = None) -> str:
    cwd = str(root or repo_root())
    if run_git(["cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=cwd).returncode != 0:
        return classify_base(None)
    return classify_base(run_git(["cat-file", "-e", f"{base_sha}:{BASE_PATH}"], cwd=cwd).returncode == 0)


def verdict(row: dict) -> list[str]:
    """Failed bars for one leg; empty means it holds."""
    failed = []
    if row["errored"] or not row["ran"]:
        failed.append("leg did not run clean")
    if row["process_ms"] >= PROCESS_MS_BAR:
        failed.append(f"process_ms={row['process_ms']} >= {PROCESS_MS_BAR:g}")
    for f in row.get("audit_failures", []):
        failed.append(f)
    return failed


def run_bench() -> dict:
    """Warm up once, measure every leg on its own fixture, and return the full report."""
    from coordinator_core import ipc

    report: dict = {"op": OP, "base": {"sha": BASE_SHA, "verdict": base_verdict()}}
    if ipc.get_op_handler(OP) is None:
        report["verdict"] = "op-not-registered"
        return report
    if shutil.which("rg") is None:
        report.update({"verdict": "fail", "failed": ["ripgrep (rg) not installed; spec archive cannot run"]})
        return report
    rows, failed = [], []
    with managed_temp_dir(prefix="distill-bookkeeping-bench-") as tmp:
        base = Path(tmp)
        measure_leg("composite", base / "warmup")
        for name in LEGS:
            row = measure_leg(name, base / name)
            row["failed"] = verdict(row)
            failed.extend(f"{name}: {f}" for f in row["failed"])
            rows.append(row)
    composite = next(r for r in rows if r["leg"] == "composite")
    report.update(
        {"process_ms_bar": PROCESS_MS_BAR, "rows": rows, "tokens": token_model(composite),
         "verdict": "pass" if not failed else "fail", "failed": failed}
    )
    return report


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON only")
    args = ap.parse_args(argv)
    report = run_bench()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"base {report['base']['sha']}: {report['base']['verdict']}")
        for row in report.get("rows", []):
            print(f"{row['leg']:13} process_ms={row['process_ms']} spawns={row['spawns']} ran={row['ran']}")
        if "tokens" in report:
            t = report["tokens"]
            print(
                f"tokens (modeled): before {t['before']['input_tokens_est']} over "
                f"{t['before']['agent_dispatches']} dispatches; after {t['after']['input_tokens_est']} "
                f"over {t['after']['op_calls']} op call, 0 dispatches"
            )
        print(f"verdict: {report['verdict']}")
        for f in report.get("failed", []):
            print(f"  FAIL {f}")
    if report["verdict"] == "op-not-registered":
        return 2
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
