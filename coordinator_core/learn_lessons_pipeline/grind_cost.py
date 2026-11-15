"""coordinator_core.learn_lessons_pipeline.grind_cost -- read-only probe of what the
default learn-lessons queue emit costs per lesson.

`measure(repo_root)` replays the selection and batching legs of
`queue_emit.emit_queue_script` (select_rows, then `_group_into_batches`) for the
`lessons` profile at the `standard` appetite and reports admitted rows, declined
rows, triage batches (one triage agent spawn each) and spawns per admitted
lesson. For a `mechanical_close.PROFILES` profile it then replays the run-budget fit
(`fit_survivors`) and reports `budget_before` / `budget` per axis (agents, tokens, minutes),
`fitted_rows` and `deferred`. It writes no ledger, claim or run file; a profile that declares a
`source` gets a throwaway tmp dir for the row files select_rows materialises.

CLI: `python -m coordinator_core.learn_lessons_pipeline.grind_cost` prints JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract import run_budget
from coordinator_core.contract.run_budget import RunBudget, UnitCost
from coordinator_core.distill.prompt_tier import estimate_tokens
from coordinator_core.learn_lessons_pipeline import mechanical_close
from coordinator_core.learn_lessons_pipeline.mechanical_close import close_candidates, repo_relative
from coordinator_core.session import record_homes
from coordinator_core.temp_layout import managed_temp_dir

DEFAULT_PROFILE = "lessons"
DEFAULT_APPETITE = "standard"
CEREMONY = "learn-lessons"

# Per-spawn tokens by role. Triage and verify: learn-lessons spend live run 20261113T124818Z,
# 881,973 tokens over 20 agents. The prevet rate is not a constant: it is read from the
# calibration file, and only a profile that declares a prevet needs it.
_MEASURED_AGENT_TOKENS = 881_973 // 20
ROLE_TOKENS = {"triage": _MEASURED_AGENT_TOKENS, "verify": _MEASURED_AGENT_TOKENS}
ROLES = ("prevet", "triage", "verify")
PREVET_CALIBRATION = Path(__file__).with_name("prevet_calibration.json")
PREVET_RATE_KEY = "tokens_per_prevet_spawn"
# Minutes per triage batch round (triage, pre-vet and verify legs of one batch run as a unit).
MINUTES_PER_BATCH = 1


class PrevetCalibrationError(RuntimeError):
    """The profile declares a prevet but its per-spawn token rate is unreadable."""


def prevet_rate(path: Optional[Path] = None) -> int:
    """Tokens per prevet spawn from `prevet_calibration.json`; typed refusal naming the file."""
    path = Path(path) if path is not None else PREVET_CALIBRATION
    try:
        rate = json.loads(path.read_text(encoding="utf-8")).get(PREVET_RATE_KEY)
    except (OSError, ValueError, AttributeError) as exc:
        raise PrevetCalibrationError(
            f"{path} is missing or unreadable ({exc}); the profile declares a prevet, so its "
            f"{PREVET_RATE_KEY} must be calibrated"
        ) from exc
    if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0:
        raise PrevetCalibrationError(f"{path} carries no positive integer {PREVET_RATE_KEY}")
    return rate


def role_rates(prevet_declared: bool, calibration: Optional[Path] = None) -> dict[str, int]:
    """Tokens per spawn for each role; the prevet rate is read only when a prevet is declared."""
    return {**ROLE_TOKENS, "prevet": prevet_rate(calibration) if prevet_declared else 0}


def batch_unit_cost(
    has_verify: bool, prevet_size: int, batch_size: int, rates: Optional[dict[str, int]] = None
) -> UnitCost:
    """Cost of one triage batch: its triage agent, its verify leg, and the pre-vet agents
    covering its rows, each at its role rate."""
    rates = rates if rates is not None else role_rates(prevet_size > 0)
    counts = {
        "prevet": -(-batch_size // prevet_size) if prevet_size > 0 else 0,
        "triage": 1,
        "verify": 1 if has_verify else 0,
    }
    return UnitCost(
        agents=sum(counts.values()),
        tokens=sum(counts[r] * rates[r] for r in ROLES),
        minutes=MINUTES_PER_BATCH,
    )


def profile_unit_cost(loaded: Any, knobs: Any, rates: Optional[dict[str, int]] = None) -> UnitCost:
    """`batch_unit_cost` read off a loaded profile and its resolved appetite knobs."""
    size = knobs.get("batch_size", 4)
    default_size = max(1, int(size.get("default", 4) if hasattr(size, "get") else size))
    prevet = getattr(loaded, "prevet", None)
    return batch_unit_cost(
        any(n.kind == "verify" for n in loaded.graph.values()),
        int(getattr(prevet, "batch_size", 0) or 0),
        default_size,
        rates,
    )


def fit_survivors(
    manifest: Any,
    survivors: list[str],
    loaded: Any,
    knobs: Any,
    repo_root: Path,
    budget: RunBudget,
    rates: Optional[dict[str, int]] = None,
) -> list[str]:
    """Longest leading run of `survivors` (queue order) whose triage batches fit `budget`.

    Narrows only: a run always keeps at least its first row, and the rows left out stay in the
    queue for the next run.
    """
    from coordinator_core.ops.dispatch_emit.grind_compose import _group_into_batches
    from coordinator_core.ops.dispatch_emit.queue_select import restrict_manifest

    def batches(k: int) -> int:
        part = restrict_manifest(manifest, survivors[:k])
        return len(_group_into_batches(part, knobs, loaded, repo_root=repo_root))

    total = batches(len(survivors))
    allowed = run_budget.fit_units(budget, UnitCost(), profile_unit_cost(loaded, knobs, rates), total)
    if allowed >= total:
        return list(survivors)
    lo, hi = 1, len(survivors)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if batches(mid) <= allowed:
            lo = mid
        else:
            hi = mid - 1
    return list(survivors[:lo])


def projected_cost(unit: UnitCost, triage_batches: int, spawns: dict[str, int], rates: dict[str, int]) -> UnitCost:
    """Agents are the per-role spawn counts summed; tokens are each role's count at its rate."""
    return UnitCost(
        agents=sum(spawns.values()),
        tokens=sum(spawns[r] * rates[r] for r in ROLES),
        minutes=triage_batches * unit.minutes,
    )


_ROUTED_OUT_MARKERS = ("row fails its schema and cannot be closed", "row carries a schema hand-back mark")


def _is_routed_out(reason: str) -> bool:
    """True for a decline `select_rows` writes for a schema-failing row: the first-run schema
    decline and the later-run decline by the `needs-judgment` mark that first run wrote."""
    return reason.startswith("admission:") and any(m in reason for m in _ROUTED_OUT_MARKERS)


def _default_profile_dir(profile: str) -> Path:
    from coordinator_core.resolve_coordinator_clone import resolve_content_root

    profile_dir = Path(resolve_content_root()) / "queue-profiles"
    if not (profile_dir / f"{profile}.yaml").is_file():
        raise FileNotFoundError(f"{profile_dir / (profile + '.yaml')} does not exist; pass profile_dir")
    return profile_dir


def measure(
    repo_root: Path,
    *,
    profile: str = DEFAULT_PROFILE,
    appetite: str = DEFAULT_APPETITE,
    queue: Optional[Path] = None,
    profile_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Return the cost shape of the default emit.

    `spawns_per_lesson` is triage_batches / admitted (0.0 when nothing is admitted).
    `max_triage_batch_tokens` is the largest triage batch's summed row-file tokens (bytes/4).
    `tokens_before` sums row-file tokens over admitted rows; `tokens_after` excludes rows a
    dedupe/discard verdict closes (a retag keeps its row).
    `llm_routed` is admitted minus `mechanically_closed` (close_candidates);
    `routed_out` counts schema-failing declines; `projected_spawns` is triage
    batches plus pre-vet batches (profile `prevet.batch_size`, when declared)
    plus one verify leg per triage batch when the graph has a verify node (a floor:
    a row takes one verdict branch, so branches are not multiplied).
    """
    from coordinator_core.ops.dispatch_emit.grind_compose import _entry_bytes, _group_into_batches
    from coordinator_core.ops.dispatch_emit.grind_profile import load_profile, resolve_appetite, validate_graph
    from coordinator_core.ops.dispatch_emit.queue_emit import _resolved_batch_sizes
    from coordinator_core.ops.dispatch_emit.queue_select import (
        load_row_schema_gate,
        restrict_manifest,
        select_rows,
    )

    repo_root = Path(repo_root).resolve()
    queue_dir = Path(queue) if queue is not None else Path(record_homes.home_dir(str(repo_root), "lessons"))
    pdir = Path(profile_dir) if profile_dir is not None else _default_profile_dir(profile)

    loaded = load_profile(profile, pdir)
    validate_graph(loaded)
    knobs = resolve_appetite(loaded, appetite, None)
    fits_budget = loaded.name in mechanical_close.PROFILES

    with managed_temp_dir("grind-cost-", repo_root) as tmp:
        manifest = select_rows(
            [queue_dir],
            where=knobs.get("where"),
            order=(loaded.priority["field"], loaded.priority["order"]),
            limit=None if fits_budget else knobs.get("limit"),
            batch_key=list(loaded.batch_key),
            batch_sizes=_resolved_batch_sizes(knobs),
            row_id_key=loaded.row_id_key,
            profile=loaded.name,
            repo_root=repo_root,
            source=loaded.source,
            absent_sentinels=loaded.absent_sentinels,
            source_row_dir=Path(tmp) / "source-rows" if loaded.source else None,
            claimant_sid=None,
            row_schema_gate=load_row_schema_gate(loaded, repo_root),
        )
        batches = _group_into_batches(manifest, knobs, loaded, repo_root=repo_root)
        max_triage_batch_tokens = max(
            (sum(estimate_tokens(_entry_bytes(e, repo_root)) for e in entries) for _id, entries in batches),
            default=0,
        )

        verdicts = close_candidates(manifest, repo_root)
        closing = [v for v in verdicts if v.verdict != "retag"]  # a retag rewrites the row; it stays queued
        archived = {repo_relative(v.row_path, repo_root) for v in closing}
        mechanically_closed = len(closing)
        tokens_before = tokens_after = 0
        for e in manifest.entries:
            t = estimate_tokens(_entry_bytes(e, repo_root))
            tokens_before += t
            if repo_relative(str(e.path), repo_root) not in archived:
                tokens_after += t

        prevet = getattr(loaded, "prevet", None)
        prevet_size = int(getattr(prevet, "batch_size", 0) or 0)
        rates = role_rates(prevet_size > 0)
        unit = profile_unit_cost(loaded, knobs, rates)
        budget = run_budget.load(CEREMONY)
        has_verify = any(n.kind == "verify" for n in loaded.graph.values())

        def _spawns(triage: int, routed: int) -> dict[str, int]:
            return {
                "prevet": -(-routed // prevet_size) if prevet_size > 0 else 0,
                "triage": triage,
                "verify": triage if has_verify else 0,
            }

        fitted_rows = len(manifest.entries)
        fitted_batches = len(batches)
        fitted_routed = fitted_rows - mechanically_closed
        if fits_budget:
            closed_ids = {v.row_id for v in closing}
            survivors = [e.row_id for e in manifest.entries if e.row_id not in closed_ids]
            limit = knobs.get("limit")
            if limit is not None:
                survivors = survivors[:limit]
            kept = fit_survivors(manifest, survivors, loaded, knobs, repo_root, budget, rates)
            fitted_rows = len(kept)
            fitted_routed = len(kept)
            fitted_batches = len(
                _group_into_batches(restrict_manifest(manifest, kept), knobs, loaded, repo_root=repo_root)
            )
            deferred = len(manifest.entries) - mechanically_closed - len(kept)
        else:
            deferred = 0

    admitted = len(manifest.entries)
    triage_batches = len(batches)
    llm_routed = admitted - mechanically_closed
    routed_out = sum(1 for d in manifest.declined if _is_routed_out(d.reason))
    spawns_by_role = _spawns(triage_batches, llm_routed)
    projected_spawns = sum(spawns_by_role.values())
    before = projected_cost(unit, triage_batches, spawns_by_role, rates)
    after = projected_cost(unit, fitted_batches, _spawns(fitted_batches, fitted_routed), rates)
    return {
        "profile": profile,
        "appetite": appetite,
        "admitted": admitted,
        "declined": len(manifest.declined),
        "triage_batches": triage_batches,
        "spawns_per_lesson": (triage_batches / admitted) if admitted else 0.0,
        "max_triage_batch_tokens": max_triage_batch_tokens,
        "llm_routed": llm_routed,
        "mechanically_closed": mechanically_closed,
        "routed_out": routed_out,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "projected_spawns": projected_spawns,
        "spawns_by_role": spawns_by_role,
        "fitted_rows": fitted_rows,
        "deferred": deferred,
        "refused": False,
        "budget_before": run_budget.budget_report(budget, before),
        "budget": run_budget.budget_report(budget, after),
    }


def main() -> int:
    print(json.dumps(measure(Path.cwd()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
