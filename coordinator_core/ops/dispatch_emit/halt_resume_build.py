"""Halt-time producer of the halt-resume record: runs once, detached, off the halting op's path.

`build(root, run_id)` runs what the inline resume path ran (the sizing gate, `sizing_fire.resumable_plan`,
`ask_compose.compose_ask_script`, `emit.resume_landed_state`) and writes `resume.json` beside
`halted.json` with the fingerprint of its inputs. It also serves as the background recompose a
fingerprint mismatch spawns. `write_halt_marker` schedules it through `ops.ceremony.detached_spawn`,
which runs a script by absolute path, so this file carries a `__main__` taking root and run_id.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SOURCE_PM, HALT_PLAN_FIELD, RUN_DIR_ROOT, record_dir

COMPOSED_SCRIPT_FILE = "resume-script.workflow.mjs"
REFUSAL_BACKOFF_S = 300


def _jsonable(value):
    """JSON-safe copy: dataclasses and named tuples become dicts, sets become sorted lists."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return _jsonable(value._asdict())
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _landed_state_sha(root: Path, plan_rel: Optional[str]) -> Optional[str]:
    """Newest checkpoint sha for the plan, else the newest ledger-check halt sha; None when neither."""
    if not plan_rel:
        return None
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    landed = resume_landed_state(root, plan_rel)
    return (landed.checkpoint_shas[0] if landed.checkpoint_shas else None) or landed.ledger_sha


def build(root, run_id: str) -> Optional[dict]:
    """Compute the halted run's resume work and write its record; the record, or None when the run
    has no `halted` marker naming a sizing. A gate refusal is recorded as the verdict (no script)."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs
    from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit, _installed_plugin_root
    from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
    from coordinator_core.ops.dispatch_emit.emit import resolve_agent_type_host
    from coordinator_core.ops.dispatch_emit.sizing_fire import (
        SizingFireRefused, SizingHandBack, load_sizing_effective, resolve_arm, resumable_plan,
    )
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN

    root = Path(root)
    rd = record_dir(root, run_id)
    marker = halted_run._read(rd)
    if not marker or marker.get("status") != STATUS_HALTED or not marker.get("sizing_rel"):
        return None
    sizing_rel = marker["sizing_rel"]
    plan_rel = marker.get(HALT_PLAN_FIELD)
    try:
        hrr.write_hint(root, sizing_rel, run_id)
    except OSError:
        pass  # the hint only serves a refire whose halted-run lookup stalls
    run_base = halted_run.halted_run_base(rd)

    verdict: dict = {}
    script_rel: Optional[str] = None
    fragments: dict = {}
    resume_plan_rel = None
    retry_not_before: Optional[float] = None
    try:
        ask_sizing = _gate_sizing_at_emit(root, sizing_rel, [])
        sizing = load_sizing_effective(root, sizing_rel)
        resume = resumable_plan(sizing, sizing_rel, root, resolve_arm(sizing))
        resume_plan_rel = resume.plan_rel if resume else None
        verdict = {"ask_sizing": _jsonable(ask_sizing), "resume": _jsonable(resume)}
        roster, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
        script = compose_ask_script(
            staged_out=fragments,
            review_roster_fragment=roster,
            review_stage_schemas=schemas,
            repo_root=root.as_posix(),
            prompt=None,
            sizing_rel=sizing_rel,
            run_id=run_id,
            session_id=None,
            script_path=f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
            plan_blitz_args=ask_sizing.get("plan_blitz_args"),
            writes=ask_sizing.get("writes", ()),
            agent_type_host=resolve_agent_type_host(
                coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
                claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT") or _installed_plugin_root(),
            ),
            baton=ask_sizing.get("baton"),
            accept_pending=bool(ask_sizing.get("accept_pending")),
            in_run_commit=True,
            ask_source=ASK_SOURCE_PM,
            run_base_sha=run_base,
        )
        target = rd / COMPOSED_SCRIPT_FILE
        atomic_write_bytes(target, script.encode("utf-8"))
        script_rel = target.relative_to(root).as_posix()
    except (SizingFireRefused, SizingHandBack) as exc:
        verdict = {
            "refused": _jsonable(getattr(exc, "fields", None) or [str(exc)]),
            "halt": {"kind": type(exc).__name__, "reason": str(exc)},
        }
        retry_not_before = time.time() + REFUSAL_BACKOFF_S

    landed = _landed_state_sha(root, resume_plan_rel or plan_rel)
    effective_plan = hrr.resolve_fingerprint_plan(root, rd, sizing_rel)
    fingerprint = hrr.fingerprint_for_run(root, rd, sizing_rel, effective_plan, landed)
    record = hrr.build_record(
        resume_plan_rel=effective_plan,
        composed_script_rel=script_rel,
        hoisted_fragments=fragments,
        gate_verdict=verdict,
        landed_state_sha=landed,
        fingerprint=fingerprint,
        retry_not_before=retry_not_before,
    )
    hrr.write(rd, record)
    return record


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: halt_resume_build.py <root> <run_id>", file=sys.stderr)
        return 2
    try:
        build(argv[0], argv[1])
        hrr.release_recompose(record_dir(Path(argv[0]), argv[1]))
    except Exception as exc:  # noqa: BLE001 -- a detached child reports through the shared failures log
        from coordinator_core.ops.ceremony.detached_spawn import record_child_failure

        record_child_failure(argv[0], __file__, exc=exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
