"""Whole-backlog first-pass token benchmark over the coordinator-content-repo checkout.

`--baseline` prices the current `distill.scope` projection over DoE (via `distill_run_cost.measure`,
which runs the real entry point and removes its scratch run dir) and pins the number into
`baselines/backlog_first_pass_tokens.json` as `base`. Default mode invokes `backlog.triage_pass` over
DoE through `coordinator_core.invoke`, prices every TopicDigest plus a fixed per-topic synthesis
overhead against the vendored whole-backlog bar, records the result as `end` beside `base`, and
reports the op's process time and spawn count. Exit 1 when the total is not under the bar, a week
was not walked, or the op's process time exceeds the brightline; exit 2 on a refusal.

DoE is resolved from the machine-local key `repos.doe_claude`; a missing key is a typed refusal.
Token proxy is bytes/4.

Usage: python coordinator_core/benchmarks/backlog_first_pass_tokens.py [--baseline] [--repo PATH] [--baseline-file PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.contract import run_budget as _run_budget
from coordinator_core.win_portability import no_console_creationflags

DOE_KEY = "repos.doe_claude"
BASELINE_FILE = Path(__file__).resolve().parent / "baselines" / "backlog_first_pass_tokens.json"
SCHEMA = "backlog-first-pass-tokens/v1"
RUN_ID = "bench-backlog-first-pass"
SYNTHESIS_OVERHEAD_TOKENS_PER_TOPIC = 1500
PROCESS_TIME_BAR_MS = 500


class MissingRepoKey(RuntimeError):
    """The machine-local registry key naming a repo checkout is absent or points at no directory."""

    def __init__(self, key: str, detail: str = "") -> None:
        self.key = key
        super().__init__(f"machine-local key {key} is not set{detail}")


def resolve_doe_repo(override: Optional[Path] = None) -> Path:
    """The coordinator-content-repo checkout: `override` when given, else the machine-local key `repos.doe_claude`.

    Raises MissingRepoKey naming the key when it is unset or is not a directory."""
    if override is not None:
        return override.resolve()
    from coordinator_core._claude_klabauter_root import _machine_local_get

    raw = _machine_local_get(DOE_KEY)
    if not raw:
        raise MissingRepoKey(DOE_KEY)
    path = Path(raw)
    if not path.is_dir():
        raise MissingRepoKey(DOE_KEY, "-- it names no directory on this machine")
    return path


def measure_baseline(repo: Path) -> dict[str, Any]:
    """Price the current distill.scope projection over `repo`: total_tokens plus the scope shape."""
    from coordinator_core.benchmarks import distill_run_cost

    report = distill_run_cost.measure(repo)
    counts = report["scope_counts"]
    return {
        "files_outside_this_projection": counts.get("not_admitted", 0) + counts.get("deferred", 0),
        "total_tokens": report["input_tokens_est_total"],
        "input_bytes_total": report["input_bytes_total"],
        "scoped_file_count": report["scoped_file_count"],
        "batch_count": report["batch_count"],
        "dispatches_total": report["dispatches_total"],
        "input_bytes_by_phase": report["input_bytes_by_phase"],
        "scope_counts": report["scope_counts"],
        "scoped_files_sha256": report["scoped_files_sha256"],
        "unreadable_file_count": len(report["unreadable_files"]),
    }


def baseline_record(base: dict[str, Any]) -> dict[str, Any]:
    """The pinned baseline document: schema, repo key (never a path), the whole-backlog bar, the base measurement."""
    bar = _run_budget.whole_backlog_bar()
    return {
        "schema": SCHEMA,
        "repo_key": DOE_KEY,
        "mode": "baseline",
        "method": "distill.scope projection priced by project_run_cost, bytes/4",
        "bar": bar,
        "base": base,
        "base_over_bar": base["total_tokens"] > bar,
    }


@contextmanager
def _count_spawns() -> Iterator[list[tuple[str, ...]]]:
    """Every `subprocess.Popen` made inside the block, by argv (`run()` constructs a Popen, so it counts once)."""
    recorded: list[tuple[str, ...]] = []
    real = subprocess.Popen

    class _Popen(real):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            recorded.append(tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),))
            super().__init__(args, *a, **kw)

    subprocess.Popen = _Popen  # type: ignore[misc]
    try:
        yield recorded
    finally:
        subprocess.Popen = real  # type: ignore[misc]


def _children_cpu_s() -> Optional[float]:
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def invoke_triage_pass(source: Path, invoking_repo: Path) -> tuple[dict[str, Any], Optional[float]]:
    """backlog.triage_pass over `source` through the real `coordinator_core.invoke` entry point.

    Output lands under `invoking_repo` and the run dir is removed after. Returns the op result and the
    invoke child's CPU seconds (user+sys; None where the platform has no rusage)."""
    pfile = invoking_repo / "state" / "scratch" / "backlog-pass" / f"{RUN_ID}.params.json"
    run_dir = invoking_repo / "state" / "scratch" / "backlog-pass" / RUN_ID
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps({"run_id": RUN_ID, "source_repo": str(source)}), encoding="utf-8")
    cpu0 = _children_cpu_s()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.invoke", "backlog.triage_pass",
             "--params-file", str(pfile), "--repo", str(invoking_repo), "--allow-unstamped-dispatch"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, check=False, **no_console_creationflags(),
        )
        cpu1 = _children_cpu_s()
        digests = json.loads((run_dir / "digests.json").read_text(encoding="utf-8")) if proc.returncode == 0 else {}
    except (OSError, ValueError):
        digests = {}
    finally:
        pfile.unlink(missing_ok=True)
        shutil.rmtree(run_dir, ignore_errors=True)
    try:
        envelope = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"backlog.triage_pass gave non-JSON output rc={proc.returncode}: {proc.stderr[-400:]}") from exc
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise RuntimeError(f"backlog.triage_pass failed: {envelope.get('error') if isinstance(envelope, dict) else envelope}")
    result = envelope["result"]
    result["_topic_tokens"] = [t["tokens"] for t in digests.get("topics", [])]
    return result, (None if cpu0 is None or cpu1 is None else cpu1 - cpu0)


def measure_in_process(source: Path) -> dict[str, Any]:
    """Warm in-process `run_pass` over `source`: process time (never wall clock) and spawn count."""
    from coordinator_core.ops.backlog_triage_pass import run_pass

    run_pass(source)
    with _count_spawns() as spawns:
        t0 = time.process_time()
        run_pass(source)
        ms = (time.process_time() - t0) * 1000.0
    return {"process_ms_warm": round(ms, 1), "spawns": len(spawns)}


def price(result: dict[str, Any]) -> dict[str, Any]:
    """The end figures from a backlog.triage_pass result: digest tokens plus the fixed per-topic synthesis overhead."""
    tokens = result["_topic_tokens"]
    digest_tokens = sum(tokens) if tokens else result["total_tokens"]
    topics = result["topics"]
    total = digest_tokens + topics * SYNTHESIS_OVERHEAD_TOKENS_PER_TOPIC
    return {
        "total_tokens": total,
        "digest_tokens": digest_tokens,
        "synthesis_overhead_per_topic": SYNTHESIS_OVERHEAD_TOKENS_PER_TOPIC,
        "topics": topics,
        "weeks_walked": result["weeks_walked"],
        "weeks_total": result["weeks_total"],
        "artifacts_opened": result["artifacts_opened"],
        "artifacts_path_only": result.get("artifacts_path_only", 0),
        "artifacts_cited": result["artifacts_cited"],
        "artifacts_not_opened": result["artifacts_cited"] - result["artifacts_opened"]
        - result.get("artifacts_path_only", 0),
        "rag_gap_forms": sorted(g["form"] for g in result.get("rag_gaps", [])),
    }


def run_default(repo: Path, invoking_repo: Optional[Path] = None) -> dict[str, Any]:
    """Default mode: price the changelog-first pass over `repo` against the vendored bar; verdict in `passed`."""
    bar = _run_budget.whole_backlog_bar()
    result, child_cpu_s = invoke_triage_pass(repo, invoking_repo or Path(__file__).resolve().parents[2])
    end = price(result)
    proc = measure_in_process(repo)
    process_ms = round(child_cpu_s * 1000.0, 1) if child_cpu_s is not None else proc["process_ms_warm"]
    end.update(proc, process_ms=process_ms)
    end["process_time_source"] = "invoke child cpu" if child_cpu_s is not None else "in-process warm"
    checks = {
        "under_bar": end["total_tokens"] < bar,
        "every_week_walked": end["weeks_walked"] == end["weeks_total"],
        "process_time_within_brightline": process_ms <= PROCESS_TIME_BAR_MS,
    }
    return {
        "status": "measured",
        "repo_key": DOE_KEY,
        "bar": bar,
        "bar_source": _run_budget.load("distill").source,
        "process_time_bar_ms": PROCESS_TIME_BAR_MS,
        "end": end,
        "checks": checks,
        "passed": all(checks.values()),
    }


def record_end(baseline_file: Path, report: dict[str, Any]) -> None:
    """Write `end` into the baselines file beside the pinned `base`; refuses to invent a base when none is pinned."""
    pinned = json.loads(baseline_file.read_text(encoding="utf-8"))
    pinned["end"] = report["end"]
    pinned["end_mode"] = "backlog.triage_pass"
    pinned["end_method"] = "sum of TopicDigest token_proxy plus fixed per-topic synthesis overhead, bytes/4"
    pinned["end_under_bar"] = report["checks"]["under_bar"]
    pinned["end_passed"] = report["passed"]
    pinned["bar"] = report["bar"]
    baseline_file.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="store_true", help="price the current distill.scope projection and pin it")
    ap.add_argument("--repo", type=Path, default=None, help="DoE checkout override (default: machine-local repos.doe_claude)")
    ap.add_argument("--baseline-file", type=Path, default=BASELINE_FILE)
    ns = ap.parse_args(argv)
    try:
        repo = resolve_doe_repo(ns.repo)
    except MissingRepoKey as exc:
        print(json.dumps({"refused": True, "missing_key": exc.key, "message": str(exc)}, indent=2))
        return 2
    if ns.baseline:
        record = baseline_record(measure_baseline(repo))
        ns.baseline_file.parent.mkdir(parents=True, exist_ok=True)
        ns.baseline_file.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report: dict[str, Any] = {**record["base"], "bar": record["bar"], "baseline_file": ns.baseline_file.name}
        print(json.dumps(report, indent=2))
        return 0
    report = run_default(repo)
    if ns.baseline_file.is_file():
        record_end(ns.baseline_file, report)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
