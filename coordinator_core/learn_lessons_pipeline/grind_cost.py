"""coordinator_core.learn_lessons_pipeline.grind_cost -- read-only probe of what the
default learn-lessons queue emit costs per lesson.

`measure(repo_root)` replays the selection and batching legs of
`queue_emit.emit_queue_script` (select_rows, then `_group_into_batches`) for the
`lessons` profile at the `standard` appetite and reports admitted rows, declined
rows, triage batches (one triage agent spawn each) and spawns per admitted
lesson. It writes no ledger, claim or run file; a profile that declares a
`source` gets a throwaway tmp dir for the row files select_rows materialises.

CLI: `python -m coordinator_core.learn_lessons_pipeline.grind_cost` prints JSON.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from coordinator_core.distill.prompt_tier import estimate_tokens
from coordinator_core.learn_lessons_pipeline.mechanical_close import close_candidates
from coordinator_core.session import record_homes

DEFAULT_PROFILE = "lessons"
DEFAULT_APPETITE = "standard"


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
    `llm_routed` is admitted minus `mechanically_closed` (close_candidates);
    `routed_out` counts schema-failing declines; `projected_spawns` is triage
    batches plus pre-vet batches (profile `prevet.batch_size`, when declared)
    plus one verify leg per triage batch when the graph has a verify node (a floor:
    a row takes one verdict branch, so branches are not multiplied).
    """
    from coordinator_core.ops.dispatch_emit.grind_compose import _entry_bytes, _group_into_batches
    from coordinator_core.ops.dispatch_emit.grind_profile import load_profile, resolve_appetite, validate_graph
    from coordinator_core.ops.dispatch_emit.queue_emit import _resolved_batch_sizes
    from coordinator_core.ops.dispatch_emit.queue_select import load_row_schema_gate, select_rows

    repo_root = Path(repo_root).resolve()
    queue_dir = Path(queue) if queue is not None else Path(record_homes.home_dir(str(repo_root), "lessons"))
    pdir = Path(profile_dir) if profile_dir is not None else _default_profile_dir(profile)

    loaded = load_profile(profile, pdir)
    validate_graph(loaded)
    knobs = resolve_appetite(loaded, appetite, None)

    with tempfile.TemporaryDirectory(prefix="grind-cost-") as tmp:
        manifest = select_rows(
            [queue_dir],
            where=knobs.get("where"),
            order=(loaded.priority["field"], loaded.priority["order"]),
            limit=knobs.get("limit"),
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

        mechanically_closed = len(close_candidates(manifest, repo_root))

    admitted = len(manifest.entries)
    triage_batches = len(batches)
    llm_routed = admitted - mechanically_closed
    routed_out = sum(1 for d in manifest.declined if _is_routed_out(d.reason))
    prevet = getattr(loaded, "prevet", None)
    prevet_size = int(getattr(prevet, "batch_size", 0) or 0)
    prevet_batches = -(-llm_routed // prevet_size) if prevet_size > 0 else 0
    has_verify = any(n.kind == "verify" for n in loaded.graph.values())
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
        "projected_spawns": triage_batches + prevet_batches + (triage_batches if has_verify else 0),
    }


def main() -> int:
    print(json.dumps(measure(Path.cwd()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
