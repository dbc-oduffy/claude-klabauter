"""Distill run cost benchmark: projected agent dispatches and agent-read input bytes per phase.

Calls `distill.scope` at default arguments through the real `coordinator_core.invoke` entry
point over the live repo, rebuilds the Workflow input from the shard files it wrote, and
prices it with `project_run_cost`. Also writes a fates fixture (one synthetic fate per scoped
file) and the disposal manifest computed from it, so before/after runs can compare the
manifest sha and the scoped-file-set sha. Exit 0 always: this measures, it does not gate.

Token proxy is bytes/4. The guide term is an upper bound (every listed guide is opened).

Usage: python coordinator_core/benchmarks/distill_run_cost.py [--repo PATH] [--out-dir PATH] [--keep]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.contract import run_budget as _run_budget
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.distill.run_cost import project_run_cost, scoped_files
from coordinator_core.ops import backlog_pass_ledger
from coordinator_core.win_portability import no_console_creationflags

RUN_ID = "bench-run-cost"
FATES: tuple[str, ...] = ("NEW_GUIDE", "ALREADY_CAPTURED", "EPHEMERAL")
BYTES_PER_TOKEN = 4


def run_scope(repo: Path) -> dict[str, Any]:
    """distill.scope at default arguments through the real invoke entry point; returns its result dict."""
    pfile = repo / "state" / "scratch" / "artifact-distillation" / f"{RUN_ID}.params.json"
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.invoke", "distill.scope",
             "--params-file", str(pfile), "--repo", str(repo), "--allow-unstamped-dispatch"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, check=False, **no_console_creationflags(),
        )
    finally:
        pfile.unlink(missing_ok=True)
    try:
        envelope = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"distill.scope gave non-JSON output rc={proc.returncode}: {proc.stderr[-400:]}") from exc
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise RuntimeError(f"distill.scope failed: {envelope.get('error') if isinstance(envelope, dict) else envelope}")
    return envelope["result"]


def load_workflow_input(repo: Path, workflow_input_path: str) -> dict[str, Any]:
    """Reassemble the consumer payload from the sharded files the Workflow reads."""
    manifest_path = repo / workflow_input_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_dir = manifest_path.parent
    batches = [
        {k: v for k, v in json.loads((run_dir / n).read_text(encoding="utf-8")).items() if k != "file_count"}
        for n in manifest["batchFiles"]
    ]
    slugs: dict[str, str] = {}
    for n in manifest["slugShardFiles"]:
        slugs.update(json.loads((run_dir / n).read_text(encoding="utf-8"))["entries"])
    return {
        "runId": manifest["runId"],
        "repoRoot": manifest["repoRoot"],
        "wikiDirs": manifest["wikiDirs"],
        "batches": batches,
        "wikiSlugs": slugs,
        "batch_count": manifest["batch_count"],
        "total_file_count": manifest["total_file_count"],
        "wikiSlugCount": manifest["wikiSlugCount"],
    }


def build_fates(files: list[str]) -> dict[str, str]:
    """One synthetic fate per scoped file, assigned by sorted index so it is stable for a fixed file set."""
    return {path: FATES[i % len(FATES)] for i, path in enumerate(sorted(files))}


def disposal_manifest_for(fates: dict[str, str]) -> dict[str, Any]:
    """Disposal manifest over the fates fixture: EPHEMERAL rows are eligible, every other fate is retained."""
    rows = [
        _schema.make_disposal_row(
            path, "synthetic", [_schema.make_guard_receipt("synthetic-fate", "pass" if fate == "EPHEMERAL" else "block", fate)],
            fate == "EPHEMERAL",
            None if fate == "EPHEMERAL" else f"fate-{fate}", f"- {path} -> {fate} (run: {RUN_ID})",
        )
        for path, fate in sorted(fates.items())
    ]
    eligible = sum(1 for r in rows if r["eligible"])
    return _schema.make_disposal_manifest(RUN_ID, rows, _schema.make_scan_stats(len(rows), eligible, len(rows) - eligible))


def bar_verdict(dispatches_total: int, tokens_total: int, minutes_total: int) -> dict[str, Any]:
    """The run budget per axis (agents, tokens, minutes): projected against cap, `met` only when
    every axis is within it. A run is narrowed to fit, never refused, so `refused` is always False."""
    budget = _run_budget.load("distill")
    projected = _run_budget.UnitCost(dispatches_total, tokens_total, minutes_total)
    report = _run_budget.budget_report(budget, projected)
    return {**report, "met": all(report["within"].values()), "refused": False, "source": budget.source}


def modeled_minutes(batch_count: int) -> int:
    """Minutes the distill.scope cost model prices for a run of `batch_count` scan batches."""
    return _schema.LOAD_RELAY_MINUTES + batch_count * _schema.SCAN_BATCH_MINUTES


def before_after(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-axis `{before, after, cap}` from two `measure` reports, either bar shape (agents, tokens, minutes)."""
    def axes(report: dict[str, Any]) -> dict[str, int]:
        return {
            "agents": report["dispatches_total"],
            "tokens": report["input_tokens_est_total"],
            "minutes": modeled_minutes(report["batch_count"]),
        }

    was, now, cap = axes(before), axes(after), after["bar"]["cap"]
    return {axis: {"before": was[axis], "after": now[axis], "cap": cap[axis]} for axis in was}


def measure(repo: Path, out_dir: Optional[Path] = None, keep: bool = False) -> dict[str, Any]:
    result = run_scope(repo)
    run_dir = repo / "state" / "scratch" / "artifact-distillation" / RUN_ID
    try:
        payload = load_workflow_input(repo, result["workflow_input_path"])
        cost = project_run_cost(payload, repo)
        files = scoped_files(payload)
        fates = build_fates(files)
        disposal = disposal_manifest_for(fates)
        fates_sha = _schema.compute_manifest_sha(fates)
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "fates-fixture.json").write_text(json.dumps(fates, indent=2, sort_keys=True), encoding="utf-8")
        (out_dir / "disposal-manifest.json").write_bytes(_schema.canonical_manifest_bytes(disposal))
    dispatches = cost["dispatches_by_phase"]
    input_bytes = cost["input_bytes_by_phase"]
    counts = result["counts"]
    tokens_total = sum(input_bytes.values()) // BYTES_PER_TOKEN
    return {
        "scope_counts": counts,
        "not_admitted_count": counts.get("not_admitted", 0),
        "deferred_count": counts.get("deferred", 0),
        "log_cited_plan_count": counts.get("log_cited_plan_count", 0),
        "bar": bar_verdict(sum(dispatches.values()), tokens_total, modeled_minutes(len(payload["batches"]))),
        "backlog_pass": backlog_pass_ledger.summary(str(repo), "distill"),
        "scoped_file_count": len(files),
        "scoped_files_sha256": cost["scoped_files_sha256"],
        "dispatches_by_phase": dispatches,
        "dispatches_total": sum(dispatches.values()),
        "input_bytes_by_phase": input_bytes,
        "scan_bytes_split": cost["scan_bytes_split"],
        "input_bytes_total": sum(input_bytes.values()),
        "input_tokens_est_total": sum(input_bytes.values()) // BYTES_PER_TOKEN,
        "unreadable_files": cost["unreadable_files"],
        "batch_count": len(payload["batches"]),
        "wiki_guide_count": len(set(payload["wikiSlugs"].values())),
        "fates_sha256": fates_sha,
        "disposal_manifest_sha256": _schema.compute_manifest_sha(disposal),
        "assumption": "guide bytes are an upper bound: every guide listed to a scan agent is opened",
    }


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--out-dir", type=Path, default=None, help="write fates-fixture.json and disposal-manifest.json here")
    ap.add_argument("--keep", action="store_true", help="keep the run's scratch directory")
    ap.add_argument("--before", type=Path, default=None, help="an earlier report JSON; adds per-axis before/after")
    ns = ap.parse_args(argv)
    report = measure(ns.repo.resolve(), ns.out_dir, ns.keep)
    if ns.before is not None:
        report["before_after"] = before_after(json.loads(ns.before.read_text(encoding="utf-8")), report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
