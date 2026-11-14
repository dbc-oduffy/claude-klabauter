"""Relay and long-prompt-tier census over the four LLM-workflow input surfaces, at default arguments.

Per surface: `relays_by_phase` (op-relay agents, i.e. content-transcription agents, per phase; not
work agents) and `max_prompt_tokens` (largest single-agent prompt input, bytes/4), each flagged
against `prompt_tier.LONG_PROMPT_TIER_TOKENS`. Surfaces: distill (distill.scope through the real
invoke entry + project_run_cost), update-docs (ceremony.update_docs_worklist result bytes),
architecture-atlas (refresh_cost.estimate over the gate's default census buckets) and learn-lessons
(grind_cost.measure counts + the largest triage batch's row bytes). Exit 0 always: this measures.

Usage: python coordinator_core/benchmarks/relay_tier_census.py [--repo PATH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS, estimate_tokens, over_tier
from coordinator_core.temp_layout import managed_temp_dir
from coordinator_core.win_portability import no_console_creationflags

SURFACES = ("distill", "update-docs", "architecture-atlas", "learn-lessons")
ATLAS_GATE_CONFIG = "docs/research/architecture-atlas-performant/2026-11-12-spend/outputs/gate-config.json"
UPDATE_DOCS_OP = "ceremony.update_docs_worklist"


def _row(relays_by_phase: dict[str, int], max_prompt_tokens: int, **extra: Any) -> dict[str, Any]:
    """One surface's report; `over_tier` and `relay_source` are derived here so every surface carries them."""
    return {
        "relays_by_phase": relays_by_phase,
        "relays_total": sum(relays_by_phase.values()),
        "max_prompt_tokens": max_prompt_tokens,
        "tier_tokens": LONG_PROMPT_TIER_TOKENS,
        "over_tier": over_tier(max_prompt_tokens),
        **extra,
    }


def census_distill(repo: Path) -> dict[str, Any]:
    """Load relays = dispatches_by_phase.load over the inlined args (`load_shard_mode` keeps the base count); max prompt = the largest single batch's scan bytes / 4."""
    from coordinator_core.benchmarks import distill_run_cost as drc
    from coordinator_core.distill import launch_args
    from coordinator_core.distill.run_cost import project_run_cost

    result = drc.run_scope(repo)
    run_dir = repo / "state" / "scratch" / "artifact-distillation" / drc.RUN_ID
    try:
        payload = drc.load_workflow_input(repo, result["workflow_input_path"])
        shard_mode_load = project_run_cost(payload, repo)["dispatches_by_phase"]["load"]
        built = launch_args.build(payload, run_dir=run_dir)
        cost = project_run_cost(built["args"], repo)
        per_batch = [
            project_run_cost({**payload, "batches": [batch]}, repo)["input_bytes_by_phase"]["scan"]
            for batch in payload["batches"]
        ]
    finally:
        import shutil

        shutil.rmtree(run_dir, ignore_errors=True)
    biggest = max(per_batch, default=0)
    return _row(
        {"load": cost["dispatches_by_phase"]["load"]},
        estimate_tokens(biggest),
        load_shard_mode=shard_mode_load,
        scan_batches=len(per_batch),
        scan_batches_over_tier=sum(1 for b in per_batch if over_tier(estimate_tokens(b))),
        relay_source="project_run_cost over launch_args.build args: dispatches_by_phase.load (inlined path)",
    )


def census_update_docs(repo: Path) -> dict[str, Any]:
    """The single worklist op result is engine output, not a relay agent: relays 0; prompt = result bytes / 4."""
    with managed_temp_dir(prefix="relay-tier-") as tmp:
        pfile = Path(tmp) / "params.json"
        pfile.write_text("{}", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.invoke", UPDATE_DOCS_OP,
             "--params-file", str(pfile), "--repo", str(repo), "--allow-unstamped-dispatch"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, check=False, **no_console_creationflags(),
        )
    try:
        envelope = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"{UPDATE_DOCS_OP} gave non-JSON output rc={proc.returncode}: {proc.stderr[-400:]}") from exc
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise RuntimeError(f"{UPDATE_DOCS_OP} failed: {envelope.get('error') if isinstance(envelope, dict) else envelope}")
    result_bytes = len(json.dumps(envelope["result"], separators=(",", ":")).encode("utf-8"))
    return _row(
        {"worklist": 0},
        estimate_tokens(result_bytes),
        result_bytes=result_bytes,
        relay_source="none at base: the worklist op result is engine output",
    )


def census_atlas(repo: Path) -> dict[str, Any]:
    """Per-agent analysis tokens (the single-system read) from refresh_cost.estimate over the default census
    buckets; relays 0. The census figure in `per_agent_tokens` is an aggregate average, informational only."""
    from coordinator_core.architecture_survey import refresh_cost, tree_census

    config = json.loads((repo / ATLAS_GATE_CONFIG).read_text(encoding="utf-8"))
    buckets = config["census_buckets"]
    census = tree_census.build(repo, buckets)
    file_count = census["counts"]["bucketed"]
    projected = {b["bucket_id"]: b["projected_tokens"] for b in census.get("censusShapedResults", [])}
    est = refresh_cost.estimate(
        file_count, len(buckets), census_skipped=False, projected_tokens_by_system=projected or None
    )
    per_agent = {
        "census": est["phases"]["census"]["tokens"] // max(1, est["phases"]["census"]["agents"]),
        "analysis": est["phases"]["analysis"]["per_agent_tokens"],
        "synthesis": refresh_cost.SYNTHESIS_SHARD_TOKENS if buckets else 0,
    }
    return _row(
        {"census": 0, "analysis": 0, "synthesis": 0},
        per_agent["analysis"],
        per_agent_tokens=per_agent,
        file_count=file_count,
        bucket_count=len(buckets),
        label=est["label"],
        relay_source="none found: survey.workflow.js dispatches work agents only; max is a planning estimate",
    )


def _largest_triage_batch_bytes(repo: Path) -> int:
    """Replay grind_cost.measure's select and batch legs; return the biggest batch's summed row-file bytes."""
    from coordinator_core.learn_lessons_pipeline import grind_cost
    from coordinator_core.ops.dispatch_emit.grind_compose import _group_into_batches
    from coordinator_core.ops.dispatch_emit.grind_profile import load_profile, resolve_appetite, validate_graph
    from coordinator_core.ops.dispatch_emit.queue_emit import _resolved_batch_sizes
    from coordinator_core.ops.dispatch_emit.queue_select import load_row_schema_gate, select_rows
    from coordinator_core.session import record_homes

    loaded = load_profile(grind_cost.DEFAULT_PROFILE, grind_cost._default_profile_dir(grind_cost.DEFAULT_PROFILE))
    validate_graph(loaded)
    knobs = resolve_appetite(loaded, grind_cost.DEFAULT_APPETITE, None)
    queue_dir = Path(record_homes.home_dir(str(repo), "lessons"))
    with managed_temp_dir(prefix="relay-tier-grind-") as tmp:
        manifest = select_rows(
            [queue_dir], where=knobs.get("where"),
            order=(loaded.priority["field"], loaded.priority["order"]), limit=knobs.get("limit"),
            batch_key=list(loaded.batch_key), batch_sizes=_resolved_batch_sizes(knobs),
            row_id_key=loaded.row_id_key, profile=loaded.name, repo_root=repo, source=loaded.source,
            absent_sentinels=loaded.absent_sentinels,
            source_row_dir=Path(tmp) / "source-rows" if loaded.source else None,
            claimant_sid=None, row_schema_gate=load_row_schema_gate(loaded, repo),
        )
        batches = _group_into_batches(manifest, knobs, loaded)

        def size(entry: Any) -> int:
            p = Path(entry.path)
            try:
                return (p if p.is_absolute() else repo / p).stat().st_size
            except OSError:
                return 0

        return max((sum(size(e) for e in entries) for _id, entries in batches), default=0)


def census_learn_lessons(repo: Path) -> dict[str, Any]:
    """Triage agents read their rows directly: relays 0; max prompt = the largest triage batch's row bytes / 4."""
    from coordinator_core.learn_lessons_pipeline import grind_cost

    cost = grind_cost.measure(repo)
    biggest = _largest_triage_batch_bytes(repo)
    return _row(
        {"triage": 0},
        estimate_tokens(biggest),
        triage_batches=cost["triage_batches"],
        admitted=cost["admitted"],
        relay_source="none found: triage agents are work agents reading rows directly",
    )


_CENSUS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "distill": census_distill,
    "update-docs": census_update_docs,
    "architecture-atlas": census_atlas,
    "learn-lessons": census_learn_lessons,
}


def measure(repo: Path, surfaces: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
    """One report per surface; a surface that raises reports its error and never stops the others."""
    out: dict[str, Any] = {}
    for name in surfaces or SURFACES:
        try:
            out[name] = _CENSUS[name](repo)
        except Exception as exc:  # noqa: BLE001 - a census measures; one broken surface must not hide the rest
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ns = ap.parse_args(argv)
    print(json.dumps(measure(ns.repo.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
